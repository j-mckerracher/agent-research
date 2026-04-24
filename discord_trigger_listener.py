#!/usr/bin/env python3
"""Discord Trigger Listener — polls #trigger-agents-ado-work and #trigger-agents-general for commands.

Posts live progress and a structured completion summary to the Discord thread it creates
for each run. Runs indefinitely until killed (Ctrl-C or SIGTERM).

Usage:
    DISCORD_BOT_TOKEN=<token> python3 discord_trigger_listener.py \\
        [--repo /abs/path/to/repo] \\
        [--backend github-copilot|claude-code] \\
        [--runner-script /abs/path/to/run_headless.py]

ADO trigger format (post in #trigger-agents-ado-work):
    RUN: WI-4461550 claude-code
    RUN: WI-4461550 github-copilot
    RUN: WI-4461550 claude-code /absolute/path/to/repo

    NOTE: backend (claude-code|github-copilot) is required in every trigger message.

General trigger format (post in #trigger-agents-general):
    RUN: --backend github-copilot --prompt "your prompt" --repo mcs-products-mono-ui [--model <id>] [--agent <file>]
    HELP:          — show help message
    CLONE: <repo-name> git@github.com:org/repo.git  — provide SSH URL when repo not found

Environment variables:
    DISCORD_BOT_TOKEN        Required
    DISCORD_GUILD_NAME       Discord server name      (default: arigato-mr-roboto)
    DISCORD_TRIGGER_CHANNEL  ADO channel to watch     (default: trigger-agents-ado-work)
    DISCORD_GENERAL_CHANNEL  General channel to watch (default: trigger-agents-general)
    DISCORD_POLL_SECONDS     Poll interval in seconds (default: 10)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import random
import urllib.error
import urllib.request
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISCORD_API_BASE = "https://discord.com/api/v10"
DEFAULT_GUILD_NAME = "arigato-mr-roboto"
DEFAULT_TRIGGER_CHANNEL = "trigger-agents-ado-work"
DEFAULT_POLL_SECONDS = 10

# How many output lines to buffer before flushing to Discord
OUTPUT_FLUSH_LINES = 20
# How many seconds to wait before flushing even if buffer isn't full
OUTPUT_FLUSH_SECONDS = 30
# Maximum characters per Discord message (hard limit is 2000)
DISCORD_MAX_CHARS = 1900
# Prefix that run.py uses to emit structured events on stdout
_EVENT_PREFIX = "##EVENT##"
# How often (seconds) to post a heartbeat summary to the Discord thread
HEARTBEAT_INTERVAL_SECONDS = 180

RUN_PREFIX = "RUN:"
REPO_PREFIX = "REPO:"
CLONE_PREFIX = "CLONE:"
HELP_PREFIX = "HELP:"
VALID_BACKENDS = ("claude-code", "github-copilot")

# Map Discord-side backend names to run_headless.py / run_general.py argparse values
_BACKEND_MAP: dict[str, str] = {
    "claude-code": "claude",
    "github-copilot": "copilot",
}

DEFAULT_GENERAL_CHANNEL = "trigger-agents-general"

GENERAL_HELP_MESSAGE = """\
**🤖 General Agent Trigger — command reference**

**Start a run:**
```
RUN: --backend <backend> --prompt "your prompt" --repo <repo-name> [--model <id>] [--agent <file.agent.md>]
```

**Required args:**
  `--backend`   AI backend: `claude-code` or `github-copilot`
  `--prompt`    The prompt text (wrap in quotes if it contains spaces)
  `--repo`      Repository name (e.g. `mcs-products-mono-ui`) — searched under `~/Code`

**Optional args:**
  `--model`     Model identifier (see below)
  `--agent`     Path to an `.agent.md` file

**Available models:**
  `github-copilot` → gpt-5.4 | gpt-5.3-codex | gpt-5.2 | gpt-5.1 | gpt-5.4-mini | gpt-5-mini | gpt-4.1 | claude-sonnet-4.6 | claude-opus-4.6 | claude-haiku-4.5
  `claude-code`    → claude-opus-4-5 | claude-sonnet-4-5 | claude-haiku-4-5

**Examples:**
```
RUN: --backend github-copilot --model claude-sonnet-4.6 --prompt "Fix the failing tests" --repo mcs-products-mono-ui
RUN: --backend claude-code --model claude-sonnet-4-5 --prompt "Add unit tests" --repo my-repo --agent spike.agent.md
```

If the repo is not found in `~/Code`, the bot will ask for an SSH clone URL.
Reply with: `CLONE: <repo-name> git@github.com:org/repo.git`

Type `HELP:` at any time to see this message again.\
"""

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_FILE_LOGGER: logging.Logger | None = None


def _init_file_logger() -> None:
    """Initialize the file logger. Called once from main()."""
    global _FILE_LOGGER
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    _FILE_LOGGER = logging.getLogger("trigger_listener")
    _FILE_LOGGER.setLevel(logging.INFO)

    handler = TimedRotatingFileHandler(
        filename=str(log_dir / "trigger_listener.log"),
        when="midnight",
        backupCount=30,
        encoding="utf-8",
        utc=True,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [trigger] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _FILE_LOGGER.addHandler(handler)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _log(msg: str) -> None:
    print(f"[trigger] {_ts()} {msg}", flush=True)
    if _FILE_LOGGER is not None:
        _FILE_LOGGER.info(msg)


# ---------------------------------------------------------------------------
# Discord REST (stdlib urllib — zero extra deps)
# ---------------------------------------------------------------------------


class DiscordAPIError(RuntimeError):
    pass


_RETRY_DELAYS = [1, 2, 4, 8]  # seconds between attempts (exponential backoff)


def _discord_request(
    method: str,
    endpoint: str,
    token: str,
    payload: dict | None = None,
) -> object:
    url = f"{DISCORD_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordTriggerListener/1.0",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None

    last_exc: Exception | None = None
    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            time.sleep(delay + random.uniform(-0.5, 0.5))
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                try:
                    retry_after = float(json.loads(body).get("retry_after", delay or 1))
                except (ValueError, KeyError):
                    retry_after = delay or 1
                time.sleep(retry_after + 0.1)
                last_exc = DiscordAPIError(
                    f"Discord API {method} {endpoint} → HTTP 429 (rate limited, retry_after={retry_after:.1f}s)"
                )
                continue
            if exc.code >= 500:
                last_exc = DiscordAPIError(
                    f"Discord API {method} {endpoint} → HTTP {exc.code}: {body[:400]}"
                )
                continue
            # 4xx (non-429) — not retryable
            raise DiscordAPIError(
                f"Discord API {method} {endpoint} → HTTP {exc.code}: {body[:400]}"
            ) from exc
        except urllib.error.URLError as exc:
            last_exc = DiscordAPIError(
                f"Discord API {method} {endpoint} → network error: {exc.reason}"
            )
            continue

    raise last_exc  # type: ignore[misc]


def get_guild_id(token: str, guild_name: str) -> str:
    guilds = _discord_request("GET", "/users/@me/guilds", token)
    assert isinstance(guilds, list)
    for g in guilds:
        if g.get("name") == guild_name:
            return str(g["id"])
    names = [g.get("name") for g in guilds]
    raise DiscordAPIError(f"Guild {guild_name!r} not found. Bot is in: {names}")


def get_channel_id(token: str, guild_id: str, channel_name: str) -> str:
    channels = _discord_request("GET", f"/guilds/{guild_id}/channels", token)
    assert isinstance(channels, list)
    for c in channels:
        if c.get("name") == channel_name and c.get("type") in (0, 5):
            return str(c["id"])
    available = [c.get("name") for c in channels if c.get("type") in (0, 5)]
    raise DiscordAPIError(
        f"Channel #{channel_name!r} not found. Available: {available}"
    )


def post_message(token: str, channel_id: str, content: str) -> dict:
    result = _discord_request(
        "POST", f"/channels/{channel_id}/messages", token, {"content": content}
    )
    assert isinstance(result, dict)
    return result


def create_thread(token: str, channel_id: str, message_id: str, name: str) -> dict:
    result = _discord_request(
        "POST",
        f"/channels/{channel_id}/messages/{message_id}/threads",
        token,
        {"name": name[:100], "auto_archive_duration": 10080},
    )
    assert isinstance(result, dict)
    return result


def post_to_thread(token: str, thread_id: str, content: str) -> None:
    # Truncate if needed — Discord hard limit is 2000 chars
    if len(content) > DISCORD_MAX_CHARS:
        content = content[: DISCORD_MAX_CHARS - 20] + "\n… (truncated)"
    try:
        _discord_request(
            "POST", f"/channels/{thread_id}/messages", token, {"content": content}
        )
    except DiscordAPIError as exc:
        _log(f"Warning: could not post to thread: {exc}")


def get_channel_messages(
    token: str, channel_id: str, after_id: str | None
) -> list[dict]:
    endpoint = f"/channels/{channel_id}/messages?limit=50"
    if after_id:
        endpoint += f"&after={after_id}"
    result = _discord_request("GET", endpoint, token)
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# Trigger parser
# ---------------------------------------------------------------------------


def parse_trigger(content: str) -> tuple[str, str, str | None] | None:
    """Parse a RUN: message.

    Expected format:
        RUN: <change_id> <backend> [repo_path]

    Returns (change_id, backend, repo_path_or_None) or None if not a trigger.
    Returns a tuple where backend is the empty string '' if it was missing/invalid
    so the caller can detect and report the error.
    """
    stripped = content.strip()
    if not stripped.upper().startswith(RUN_PREFIX.upper()):
        return None
    body = stripped[len(RUN_PREFIX):].strip()
    if not body:
        return None
    parts = body.split(None, 2)  # split into at most 3 tokens
    change_id = parts[0].strip()
    backend = parts[1].strip().lower() if len(parts) > 1 else ""
    repo_path = parts[2].strip() if len(parts) > 2 else None

    # Handle swapped order: RUN: WI-XXXX /repo/path backend
    if backend not in VALID_BACKENDS and repo_path and repo_path.lower() in VALID_BACKENDS:
        backend, repo_path = repo_path.lower(), backend

    return change_id, backend, repo_path


def parse_repo_reply(content: str) -> tuple[str, str] | None:
    """Parse a REPO: reply.

    Expected format:
        REPO: WI-XXXX /absolute/path/to/repo

    Returns (normalised_change_id, repo_path) or None if malformed.
    """
    stripped = content.strip()
    if not stripped.upper().startswith(REPO_PREFIX.upper()):
        return None
    body = stripped[len(REPO_PREFIX):].strip()
    parts = body.split(None, 1)
    if len(parts) != 2:
        return None
    raw_change_id, repo_path = parts
    change_id = raw_change_id.upper()
    if not change_id.startswith("WI-"):
        change_id = f"WI-{change_id}"
    return change_id, repo_path.strip()


def _tokenise_args(s: str) -> list[str]:
    """Split ``--key value`` string into tokens, respecting double-quoted values."""
    tokens: list[str] = []
    i = 0
    while i < len(s):
        if s[i].isspace():
            i += 1
            continue
        if s[i] == '"':
            end = s.find('"', i + 1)
            if end == -1:
                tokens.append(s[i + 1:])
                break
            tokens.append(s[i + 1:end])
            i = end + 1
        else:
            end = i
            while end < len(s) and not s[end].isspace():
                end += 1
            tokens.append(s[i:end])
            i = end
    return tokens


def parse_clone_reply(content: str) -> tuple[str, str] | None:
    """Parse a ``CLONE: <repo-name> <ssh-url>`` reply.

    Returns ``(repo_name, ssh_url)`` or None if malformed.
    """
    stripped = content.strip()
    if not stripped.upper().startswith(CLONE_PREFIX.upper()):
        return None
    body = stripped[len(CLONE_PREFIX):].strip()
    parts = body.split(None, 1)
    if len(parts) != 2:
        return None
    return parts[0].strip(), parts[1].strip()


def parse_general_trigger(content: str) -> dict | None:
    """Parse a general RUN: message with ``--flag value`` style args.

    Required: ``--backend``, ``--prompt``, ``--repo``
    Optional: ``--model``, ``--agent``

    Returns a dict or None if required fields are missing / not a trigger.
    """
    stripped = content.strip()
    if not stripped.upper().startswith(RUN_PREFIX.upper()):
        return None
    body = stripped[len(RUN_PREFIX):].strip()
    if not body or not body.startswith("-"):
        return None

    tokens = _tokenise_args(body)
    result: dict = {"backend": None, "prompt": None, "repo": None, "model": None, "agent": None}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and i + 1 < len(tokens):
            key = tok[2:]
            result[key] = tokens[i + 1]
            i += 2
        else:
            i += 1

    if not result.get("backend") or not result.get("prompt") or not result.get("repo"):
        return None
    return result


# ---------------------------------------------------------------------------
# Workflow runner (subprocess)
# ---------------------------------------------------------------------------


_RUNNER_SCRIPT_OVERRIDE: Path | None = None
_AGENTS_DIR_OVERRIDE: Path | None = None


def _runner_script_path() -> Path:
    """Resolve run_headless.py. Uses --runner-script override if set."""
    if _RUNNER_SCRIPT_OVERRIDE is not None:
        return _RUNNER_SCRIPT_OVERRIDE
    return Path(__file__).resolve().parent.parent / "agent-runner" / "run_headless.py"


def _general_runner_script_path() -> Path:
    """Resolve run_general.py relative to this script."""
    return Path(__file__).resolve().parent.parent / "agent-runner" / "run_general.py"


def run_workflow_subprocess(
    change_id: str,
    repo_path: str,
    backend: str,
    output_json: Path,
    line_queue: "queue.Queue[str | None]",
) -> int:
    """Run run_headless.py in a subprocess, forwarding lines to line_queue.

    Puts None into the queue when finished. Returns exit code.
    """
    script = _runner_script_path()
    translated_backend = _BACKEND_MAP.get(backend, backend)
    cmd = [sys.executable, str(script), "--change-id", change_id]
    cmd += ["--repo", repo_path]
    cmd += ["--backend", translated_backend]
    cmd += ["--output-json", str(output_json)]
    if _AGENTS_DIR_OVERRIDE is not None:
        cmd += ["--agents-dir", str(_AGENTS_DIR_OVERRIDE)]

    _log(f"Spawning: {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert proc.stdout is not None
    for line in proc.stdout:
        line_queue.put(line.rstrip())
    proc.wait()
    line_queue.put(None)  # sentinel
    return proc.returncode


# ---------------------------------------------------------------------------
# Discord output streamer
# ---------------------------------------------------------------------------


def _format_log_chunk(lines: list[str]) -> str:
    joined = "\n".join(lines)
    return f"```\n{joined}\n```"


def stream_output_to_discord(
    token: str,
    thread_id: str,
    line_queue: "queue.Queue[str | None]",
    event_state: "EventState | None" = None,
) -> None:
    """Read from line_queue and flush to Discord in batches.

    Lines prefixed with _EVENT_PREFIX are parsed as structured events and posted
    as formatted Discord messages; they are never included in raw code-block output.
    """
    buffer: list[str] = []
    last_flush = time.monotonic()

    def _flush() -> None:
        nonlocal buffer, last_flush
        if buffer:
            post_to_thread(token, thread_id, _format_log_chunk(buffer))
            buffer = []
            last_flush = time.monotonic()

    while True:
        try:
            line = line_queue.get(timeout=OUTPUT_FLUSH_SECONDS)
        except queue.Empty:
            _flush()
            continue

        if line is None:  # sentinel — subprocess finished
            _flush()
            return

        # Structured event lines — parse, post formatted message, skip raw buffer
        if line.startswith(_EVENT_PREFIX):
            try:
                payload = json.loads(line[len(_EVENT_PREFIX):].strip())
                if event_state is not None:
                    _update_event_state(event_state, payload)
                msg = format_event_message(payload, event_state)
                if msg:
                    post_to_thread(token, thread_id, msg)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                _log(f"Failed to parse event line: {exc}")
            continue

        buffer.append(line)

        # Flush if buffer is full or content is getting long
        chars = sum(len(l) for l in buffer)
        if len(buffer) >= OUTPUT_FLUSH_LINES or chars >= DISCORD_MAX_CHARS - 200:
            _flush()
        elif time.monotonic() - last_flush >= OUTPUT_FLUSH_SECONDS:
            _flush()


# ---------------------------------------------------------------------------
# Completion summary
# ---------------------------------------------------------------------------

_STAGE_EMOJI = {True: "✅", False: "❌"}
_STATUS_EMOJI = {"pass": "✅", "fail": "❌"}


def build_completion_summary(
    change_id: str,
    elapsed_seconds: float,
    output_json: Path,
    exit_code: int,
) -> str:
    elapsed_str = _fmt_elapsed(elapsed_seconds)

    if output_json.exists():
        try:
            summary = json.loads(output_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            summary = {}
    else:
        summary = {}

    status = summary.get("status", "fail" if exit_code != 0 else "pass")
    status_emoji = _STATUS_EMOJI.get(status, "❓")
    error = summary.get("error", "")

    lines = [
        f"## {status_emoji} Workflow {'Complete' if status == 'pass' else 'Failed'} — `{change_id}`",
        f"**Elapsed:** {elapsed_str}  |  **Status:** `{status.upper()}`",
        "",
    ]

    stages: list[dict] = summary.get("stages", [])
    if stages:
        lines.append("**Stage Results:**")
        lines.append("")
        lines.append("| Stage | Result | Attempts |")
        lines.append("|---|---|---|")
        for stage in stages:
            emoji = _STAGE_EMOJI.get(stage.get("passed", False), "❓")
            name = stage.get("stage_name", "?")
            attempts = stage.get("attempts", "?")
            lines.append(f"| `{name}` | {emoji} | {attempts} |")

    if error:
        lines += ["", f"**Error:** `{error}`"]

    return "\n".join(lines)


def _fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ---------------------------------------------------------------------------
# Active run tracker
# ---------------------------------------------------------------------------


class ActiveRun:
    def __init__(self, change_id: str, thread_id: str) -> None:
        self.change_id = change_id
        self.thread_id = thread_id
        self.started_at = time.monotonic()


class PendingRun:
    """A trigger that was received but is waiting for a repo path."""
    def __init__(self, change_id: str, backend: str, triggered_by: str) -> None:
        self.change_id = change_id
        self.backend = backend
        self.triggered_by = triggered_by


class PendingClone:
    """A general trigger waiting for an SSH clone URL from the user."""
    def __init__(
        self,
        repo_name: str,
        backend: str,
        prompt: str,
        model: str | None,
        agent: str | None,
        triggered_by: str,
    ) -> None:
        self.repo_name = repo_name
        self.backend = backend
        self.prompt = prompt
        self.model = model
        self.agent = agent
        self.triggered_by = triggered_by


def _resolve_repo(name_or_path: str) -> Path | None:
    """Return a resolved Path for a repo name or absolute path.

    Accepts a bare name (searched under ~/Code) or an absolute path.
    Returns None if the directory cannot be found.
    """
    p = Path(name_or_path)
    if p.is_absolute():
        return p.resolve() if p.is_dir() else None
    candidate = Path.home() / "Code" / name_or_path
    return candidate.resolve() if candidate.is_dir() else None


# ---------------------------------------------------------------------------
# Structured event state (populated by ##EVENT## lines from run.py)
# ---------------------------------------------------------------------------


class EventState:
    """Mutable state updated as ##EVENT## lines arrive from the subprocess."""

    def __init__(self) -> None:
        self.workflow_start_time: float = 0.0
        self.current_stage: str = ""
        self.current_stage_number: int = 0
        self.total_stages: int = 6
        self.completed_stages: list[tuple[str, bool, int]] = []  # (name, passed, attempts)
        self.current_attempt: int = 0
        self.max_attempts: int = 0
        self.current_uow: str = ""
        self.current_uow_index: int = 0
        self.total_uows: int = 0


_STAGE_LABELS: dict[str, str] = {
    "intake": "intake",
    "task_generator": "task-generator",
    "task_assigner": "task-assigner",
    "software_engineer": "software-engineer",
    "qa": "QA",
    "lessons_optimizer": "lessons-optimizer",
}


def _update_event_state(state: EventState, payload: dict) -> None:
    """Update mutable EventState from a parsed ##EVENT## payload."""
    t = payload.get("type", "")
    if t == "workflow_start":
        state.total_stages = payload.get("total_stages", 6)
    elif t == "stage_start":
        state.current_stage = payload.get("stage", "")
        state.current_stage_number = payload.get("stage_number", 0)
        state.total_stages = payload.get("total_stages", state.total_stages)
        state.current_attempt = 0
        state.max_attempts = 0
        state.current_uow = ""
        state.current_uow_index = 0
        state.total_uows = 0
    elif t == "stage_complete":
        name = payload.get("stage", "")
        passed = bool(payload.get("passed", False))
        attempts = int(payload.get("attempts", 0))
        state.completed_stages.append((name, passed, attempts))
    elif t == "eval_attempt":
        state.current_attempt = int(payload.get("attempt", 0))
        state.max_attempts = int(payload.get("max_attempts", 0))
    elif t == "uow_start":
        state.current_uow = payload.get("uow_id", "")
        state.current_uow_index = int(payload.get("uow_index", 0))
        state.total_uows = int(payload.get("total_uows", 0))


def _stage_label(name: str) -> str:
    return _STAGE_LABELS.get(name, name)


def format_event_message(payload: dict, state: EventState | None) -> str | None:
    """Convert a structured ##EVENT## payload into a Discord message, or None to skip."""
    t = payload.get("type", "")

    if t == "stage_start":
        n = payload.get("stage_number", "?")
        total = payload.get("total_stages", "?")
        stage = _stage_label(payload.get("stage", ""))
        return f"**Stage {n}/{total}** — Starting _{stage}_"

    if t == "stage_complete":
        n = payload.get("stage_number", "?")
        total = state.total_stages if state else "?"
        stage = _stage_label(payload.get("stage", ""))
        passed = bool(payload.get("passed", False))
        attempts = payload.get("attempts", "?")
        elapsed = payload.get("elapsed_s", "?")
        status = "passed" if passed else "**failed**"
        elapsed_str = f"{elapsed}s" if isinstance(elapsed, (int, float)) else str(elapsed)
        return f"**Stage {n}/{total}** — _{stage}_ {status} (attempt {attempts}) — {elapsed_str}"

    if t == "eval_attempt":
        stage = _stage_label(payload.get("stage", ""))
        attempt = payload.get("attempt", "?")
        max_att = payload.get("max_attempts", "?")
        passed = bool(payload.get("passed", False))
        score = payload.get("score", "?")
        if passed:
            return f"_{stage}_ evaluator **passed** on attempt {attempt}/{max_att} (score: {score})"
        return f"_{stage}_ evaluator **failed** on attempt {attempt}/{max_att} (score: {score})"

    if t == "uow_start":
        idx = payload.get("uow_index", "?")
        total = payload.get("total_uows", "?")
        uow_id = payload.get("uow_id", "?")
        return f"Implementation: starting UoW {idx}/{total} (`{uow_id}`)"

    if t == "uow_complete":
        uow_id = payload.get("uow_id", "?")
        passed = bool(payload.get("passed", False))
        attempts = payload.get("attempts", "?")
        score = payload.get("score", "?")
        status = "**passed**" if passed else "**failed**"
        return f"Implementation: UoW `{uow_id}` {status} (attempt {attempts}, score: {score})"

    if t == "escalation_start":
        stage = _stage_label(payload.get("stage", ""))
        uow_id = payload.get("uow_id")
        suffix = f" (UoW `{uow_id}`)" if uow_id else ""
        return f":warning: **Escalation** — _{stage}_{suffix} requires human intervention"

    if t == "workflow_error":
        error = payload.get("error", "unknown error")
        return f":red_circle: **Workflow error**: {error}"

    # workflow_start / workflow_complete / unknown — skip (completion summary handles the rest)
    return None


def _build_heartbeat_summary(state: EventState, elapsed: float) -> str:
    """Format a periodic heartbeat summary for the Discord thread."""
    elapsed_str = _fmt_elapsed(int(elapsed))
    passed_count = sum(1 for _, ok, _ in state.completed_stages if ok)
    failed_count = sum(1 for _, ok, _ in state.completed_stages if not ok)
    completed_count = len(state.completed_stages)

    stage_label = _stage_label(state.current_stage) if state.current_stage else "—"
    stage_info = (
        f"{stage_label} ({state.current_stage_number}/{state.total_stages})"
        if state.current_stage_number
        else stage_label
    )

    lines = [
        "---",
        f":blue_circle: **Status Update** — {elapsed_str} elapsed",
        f"**Current stage:** {stage_info}",
        f"**Completed:** {completed_count} stages ({passed_count} passed, {failed_count} failed)",
    ]
    if state.current_attempt and state.max_attempts:
        lines.append(f"**Current attempt:** {state.current_attempt}/{state.max_attempts}")
    if state.total_uows:
        done_uows = state.current_uow_index - 1 if state.current_uow_index else 0
        uow_info = f"{done_uows}/{state.total_uows}"
        if state.current_uow:
            uow_info += f" (`{state.current_uow}` in progress)"
        lines.append(f"**UoW progress:** {uow_info}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main listener loop
# ---------------------------------------------------------------------------


def run_listener(
    token: str,
    guild_name: str,
    trigger_channel_name: str,
    poll_seconds: int,
    default_repo: str | None,
    guild_id: str | None = None,
) -> None:
    _log("Resolving Discord guild and channel…")
    if guild_id is None:
        guild_id = get_guild_id(token, guild_name)
    _log(f"Guild ID: {guild_id}")
    trigger_channel_id = get_channel_id(token, guild_id, trigger_channel_name)
    _log(f"#{trigger_channel_name} channel ID: {trigger_channel_id}")
    _log(
        f"Polling every {poll_seconds}s. "
        f"Post 'RUN: WI-XXXX claude-code|github-copilot [repo-name]' in #{trigger_channel_name} to start a workflow."
    )

    last_seen_id: str | None = None
    active_runs: dict[str, ActiveRun] = {}    # change_id → ActiveRun
    pending_runs: dict[str, PendingRun] = {}  # change_id → PendingRun (awaiting repo path)
    consecutive_failures = 0

    # Seed last_seen_id with the most recent message so we don't re-process history
    seed_msgs = get_channel_messages(token, trigger_channel_id, after_id=None)
    if seed_msgs:
        last_seen_id = str(max(seed_msgs, key=lambda m: m.get("id", "0"))["id"])
        _log(f"Seeded last_seen_id={last_seen_id} (skipping existing history)")

    while True:
        time.sleep(poll_seconds)

        try:
            messages = get_channel_messages(token, trigger_channel_id, after_id=last_seen_id)
        except DiscordAPIError as exc:
            consecutive_failures += 1
            extra_sleep = min(60 * consecutive_failures, 300)
            _log(
                f"Warning: failed to fetch messages (failure #{consecutive_failures},"
                f" backing off {extra_sleep}s): {exc}"
            )
            if extra_sleep > poll_seconds:
                time.sleep(extra_sleep - poll_seconds)
            continue

        # Process oldest-first
        consecutive_failures = 0
        for msg in sorted(messages, key=lambda m: str(m.get("id", ""))):
            msg_id = str(msg.get("id", ""))
            author = msg.get("author", {})
            is_bot = bool(author.get("bot", False))
            content = str(msg.get("content", ""))
            username = str(author.get("global_name") or author.get("username", "unknown"))

            last_seen_id = msg_id

            if is_bot:
                continue

            # ---------------------------------------------------------------
            # REPO: reply — fulfil a pending run that was waiting for a path
            # ---------------------------------------------------------------
            if content.strip().upper().startswith(REPO_PREFIX.upper()):
                parsed_repo = parse_repo_reply(content)
                if not parsed_repo:
                    try:
                        post_message(
                            token, trigger_channel_id,
                            f"⚠️ **Invalid REPO: format** from {username}.\n"
                            f"Use: `REPO: WI-XXXX /absolute/path/to/repo`",
                        )
                    except DiscordAPIError:
                        pass
                    continue

                repo_change_id, repo_path = parsed_repo

                if repo_change_id not in pending_runs:
                    try:
                        post_message(
                            token, trigger_channel_id,
                            f"⚠️ No pending run found for `{repo_change_id}`. "
                            f"Send a `RUN:` trigger first.",
                        )
                    except DiscordAPIError:
                        pass
                    continue

                if not Path(repo_path).is_dir():
                    try:
                        pending = pending_runs[repo_change_id]
                        post_message(
                            token, trigger_channel_id,
                            f"⚠️ **Path does not exist:** `{repo_path}`\n"
                            f"Please re-send with a valid path:\n"
                            f"`REPO: {repo_change_id} /absolute/path/to/repo`",
                        )
                    except DiscordAPIError:
                        pass
                    continue

                pending = pending_runs.pop(repo_change_id)
                _log(
                    f"REPO: reply from {username} for {repo_change_id}: path={repo_path}"
                )

                # Now start the workflow with the provided path
                try:
                    ack_content = (
                        f"🤖 **Workflow triggered by {pending.triggered_by}**\n"
                        f"**Change:** `{repo_change_id}`\n"
                        f"**Backend:** `{pending.backend}`\n"
                        f"**Repo:** `{repo_path}`\n"
                        f"Starting workflow… updates will appear below."
                    )
                    ack_msg = post_message(token, trigger_channel_id, ack_content)
                    thread = create_thread(
                        token,
                        trigger_channel_id,
                        str(ack_msg["id"]),
                        f"run-{repo_change_id}",
                    )
                    thread_id = str(thread["id"])
                except DiscordAPIError as exc:
                    _log(f"Could not create thread for {repo_change_id}: {exc}")
                    pending_runs[repo_change_id] = pending  # put back so user can retry
                    continue

                active_runs[repo_change_id] = ActiveRun(repo_change_id, thread_id)
                _start_run_thread(
                    token=token,
                    change_id=repo_change_id,
                    repo_path=repo_path,
                    backend=pending.backend,
                    thread_id=thread_id,
                    active_runs=active_runs,
                )
                continue

            # ---------------------------------------------------------------
            # RUN: trigger
            # ---------------------------------------------------------------
            parsed = parse_trigger(content)
            if not parsed:
                continue

            change_id_raw, backend, repo_override = parsed
            change_id = change_id_raw.upper() if change_id_raw.upper().startswith("WI-") else f"WI-{change_id_raw}"

            # Validate backend — required in the trigger message
            if backend not in VALID_BACKENDS:
                error_msg = (
                    f"⚠️ **Invalid or missing backend** in trigger from {username}.\n"
                    f"**Usage:** `RUN: {change_id} claude-code` or `RUN: {change_id} github-copilot`\n"
                    f"Backend must be one of: `{'`, `'.join(VALID_BACKENDS)}`"
                )
                _log(f"Invalid trigger from {username}: missing/invalid backend '{backend}' for {change_id}")
                try:
                    post_message(token, trigger_channel_id, error_msg)
                except DiscordAPIError:
                    pass
                continue

            # Prevent duplicate runs for the same change ID
            if change_id in active_runs:
                _log(f"Ignoring RUN: {change_id} — already running")
                try:
                    active_run = active_runs[change_id]
                    post_to_thread(
                        token,
                        active_run.thread_id,
                        f"⚠️ `{change_id}` is already running. Wait for it to finish.",
                    )
                except DiscordAPIError:
                    pass
                continue

            if change_id in pending_runs:
                _log(f"Ignoring RUN: {change_id} — already waiting for repo path")
                try:
                    post_message(
                        token, trigger_channel_id,
                        f"⚠️ `{change_id}` is already waiting for a repo path.\n"
                        f"Reply with: `REPO: {change_id} /absolute/path/to/repo`",
                    )
                except DiscordAPIError:
                    pass
                continue

            # Resolve effective repo path
            effective_repo = repo_override or default_repo

            if not effective_repo:
                # No repo available — prompt user for it
                _log(
                    f"Trigger from {username}: RUN: {change_id} backend={backend} — repo required, prompting"
                )
                try:
                    post_message(
                        token, trigger_channel_id,
                        f"⏳ **Repo path required for `{change_id}`** (triggered by {username})\n"
                        f"Please reply with:\n"
                        f"`REPO: {change_id} /absolute/path/to/repo`",
                    )
                except DiscordAPIError as exc:
                    _log(f"Could not post repo prompt for {change_id}: {exc}")
                    continue
                pending_runs[change_id] = PendingRun(change_id, backend, username)
                continue

            _log(
                f"Trigger received from {username}: "
                f"RUN: {change_id}  backend={backend}  repo={effective_repo}"
            )

            # Post an acknowledgement message and create a thread for this run
            try:
                ack_content = (
                    f"🤖 **Workflow triggered by {username}**\n"
                    f"**Change:** `{change_id}`\n"
                    f"**Backend:** `{backend}`\n"
                    f"**Repo:** `{effective_repo}`\n"
                    f"Starting workflow… updates will appear below."
                )
                ack_msg = post_message(token, trigger_channel_id, ack_content)
                thread = create_thread(
                    token,
                    trigger_channel_id,
                    str(ack_msg["id"]),
                    f"run-{change_id}",
                )
                thread_id = str(thread["id"])
            except DiscordAPIError as exc:
                _log(f"Could not create thread for {change_id}: {exc}")
                try:
                    post_message(
                        token, trigger_channel_id,
                        f"⚠️ Failed to start run for `{change_id}`: {exc}",
                    )
                except DiscordAPIError:
                    pass
                continue

            active_runs[change_id] = ActiveRun(change_id, thread_id)

            # Launch the workflow in a background thread
            _start_run_thread(
                token=token,
                change_id=change_id,
                repo_path=effective_repo,
                backend=backend,
                thread_id=thread_id,
                active_runs=active_runs,
            )

        # Prune completed runs from tracker (the thread cleans up itself, but be safe)
        # (cleanup happens inside _run_thread via active_runs.pop)


def _start_run_thread(
    *,
    token: str,
    change_id: str,
    repo_path: str,
    backend: str,
    thread_id: str,
    active_runs: dict[str, ActiveRun],
) -> None:
    """Spawn a daemon thread that runs the workflow and streams output."""

    def _run() -> None:
        line_q: queue.Queue[str | None] = queue.Queue()

        with tempfile.NamedTemporaryFile(
            suffix=".json", prefix=f"run-{change_id}-", delete=False
        ) as tmp:
            output_json = Path(tmp.name)

        start_time = time.monotonic()
        event_state = EventState()
        event_state.workflow_start_time = start_time
        done_event = threading.Event()

        def _heartbeat_loop() -> None:
            while not done_event.wait(timeout=HEARTBEAT_INTERVAL_SECONDS):
                if event_state.current_stage:
                    try:
                        summary = _build_heartbeat_summary(
                            event_state, time.monotonic() - start_time
                        )
                        post_to_thread(token, thread_id, summary)
                    except Exception as exc:  # noqa: BLE001
                        _log(f"Heartbeat post failed: {exc}")

        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop, daemon=True, name=f"heartbeat-{change_id}"
        )
        heartbeat_thread.start()

        # Start the subprocess in another thread so we can stream its output
        exit_code_holder: list[int] = [0]

        def _subprocess_thread() -> None:
            exit_code_holder[0] = run_workflow_subprocess(
                change_id=change_id,
                repo_path=repo_path,
                backend=backend,
                output_json=output_json,
                line_queue=line_q,
            )

        sub_thread = threading.Thread(target=_subprocess_thread, daemon=True)
        sub_thread.start()

        # Stream output to Discord (blocks until subprocess exits)
        stream_output_to_discord(token, thread_id, line_q, event_state)
        sub_thread.join()

        # Stop heartbeat before posting completion summary
        done_event.set()
        heartbeat_thread.join(timeout=5)

        elapsed = time.monotonic() - start_time
        exit_code = exit_code_holder[0]

        _log(f"Run {change_id} finished: exit_code={exit_code}  elapsed={elapsed:.1f}s")

        # Post completion summary
        summary_content = build_completion_summary(
            change_id, elapsed, output_json, exit_code
        )
        post_to_thread(token, thread_id, summary_content)

        # Clean up temp file
        try:
            output_json.unlink(missing_ok=True)
        except OSError:
            pass

        # Remove from active runs
        active_runs.pop(change_id, None)

    t = threading.Thread(target=_run, daemon=True, name=f"run-{change_id}")
    t.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_general_listener(
    token: str,
    guild_name: str,
    channel_name: str,
    poll_seconds: int,
    guild_id: str | None = None,
) -> None:
    """Poll #trigger-agents-general and handle HELP: and RUN: --flag-style messages."""
    try:
        _run_general_listener(token, guild_name, channel_name, poll_seconds, guild_id=guild_id)
    except Exception as exc:
        _log(f"[general] Fatal error in general poller: {exc}")


def _run_general_listener(
    token: str,
    guild_name: str,
    channel_name: str,
    poll_seconds: int,
    guild_id: str | None = None,
) -> None:
    _log(f"[general] Resolving guild and #{channel_name}…")
    if guild_id is None:
        guild_id = get_guild_id(token, guild_name)
    channel_id = get_channel_id(token, guild_id, channel_name)
    _log(f"[general] #{channel_name} channel ID: {channel_id}  (polling every {poll_seconds}s)")

    seed = get_channel_messages(token, channel_id, after_id=None)
    last_seen_id: str | None = None
    if seed:
        last_seen_id = str(max(seed, key=lambda m: m.get("id", "0"))["id"])
        _log(f"[general] Seeded last_seen_id={last_seen_id}")

    runner = _general_runner_script_path()
    pending_clones: dict[str, PendingClone] = {}  # repo_name → PendingClone
    consecutive_failures = 0

    while True:
        time.sleep(poll_seconds)
        try:
            messages = get_channel_messages(token, channel_id, after_id=last_seen_id)
        except DiscordAPIError as exc:
            consecutive_failures += 1
            extra_sleep = min(60 * consecutive_failures, 300)
            _log(
                f"[general] Warning: failed to fetch messages (failure #{consecutive_failures},"
                f" backing off {extra_sleep}s): {exc}"
            )
            if extra_sleep > poll_seconds:
                time.sleep(extra_sleep - poll_seconds)
            continue

        consecutive_failures = 0
        for msg in sorted(messages, key=lambda m: str(m.get("id", ""))):
            msg_id = str(msg.get("id", ""))
            author = msg.get("author", {})
            if author.get("bot"):
                last_seen_id = msg_id
                continue
            content = str(msg.get("content", ""))
            username = str(author.get("global_name") or author.get("username", "unknown"))
            last_seen_id = msg_id

            upper = content.strip().upper()

            # HELP: (or bare "help") — reply with the help message
            if upper == "HELP" or upper.startswith(HELP_PREFIX.upper()):
                try:
                    post_message(token, channel_id, GENERAL_HELP_MESSAGE)
                except DiscordAPIError as exc:
                    _log(f"[general] Could not post help: {exc}")
                continue

            # CLONE: reply — user provided SSH URL for a repo we couldn't find
            if upper.startswith(CLONE_PREFIX.upper()):
                parsed_clone = parse_clone_reply(content)
                if not parsed_clone:
                    try:
                        post_message(
                            token, channel_id,
                            "⚠️ **Invalid CLONE: format.**\n"
                            "Use: `CLONE: <repo-name> git@github.com:org/repo.git`",
                        )
                    except DiscordAPIError:
                        pass
                    continue

                repo_name, ssh_url = parsed_clone
                if repo_name not in pending_clones:
                    try:
                        post_message(
                            token, channel_id,
                            f"⚠️ No pending run found for repo `{repo_name}`. "
                            "Send a `RUN:` trigger first.",
                        )
                    except DiscordAPIError:
                        pass
                    continue

                pending = pending_clones.pop(repo_name)
                clone_dest = Path.home() / "Code" / repo_name
                _log(f"[general] Cloning {ssh_url} → {clone_dest}")
                try:
                    post_message(token, channel_id, f"📦 Cloning `{repo_name}`…")
                except DiscordAPIError:
                    pass

                clone_result = subprocess.run(
                    ["git", "clone", ssh_url, str(clone_dest)],
                    capture_output=True, text=True
                )
                if clone_result.returncode != 0:
                    _log(f"[general] Clone failed: {clone_result.stderr}")
                    try:
                        post_message(
                            token, channel_id,
                            f"❌ Clone failed:\n```\n{clone_result.stderr[:800]}\n```",
                        )
                    except DiscordAPIError:
                        pass
                    pending_clones[repo_name] = pending  # put back for retry
                    continue

                _log(f"[general] Clone succeeded: {clone_dest}")
                repo_path = str(clone_dest.resolve())

                def _run_cloned(
                    _backend=pending.backend, _prompt=pending.prompt,
                    _repo=repo_path, _model=pending.model, _agent=pending.agent,
                    _user=pending.triggered_by
                ) -> None:
                    _launch_general_run(
                        token, channel_id, runner,
                        _backend, _prompt, _repo, _model, _agent, _user
                    )

                threading.Thread(target=_run_cloned, daemon=True).start()
                continue

            parsed = parse_general_trigger(content)
            if not parsed:
                continue

            backend = parsed["backend"]
            prompt = parsed["prompt"]
            repo = parsed["repo"]
            model = parsed.get("model")
            agent = parsed.get("agent")

            if backend not in VALID_BACKENDS:
                try:
                    post_message(
                        token, channel_id,
                        f"⚠️ Unknown backend `{backend}`. "
                        "Use `claude-code` or `github-copilot`.",
                    )
                except DiscordAPIError:
                    pass
                continue

            # Resolve repo name → path
            repo_path_obj = _resolve_repo(repo)
            if repo_path_obj is None:
                _log(f"[general] Repo '{repo}' not found in ~/Code — asking {username} for clone URL")
                pending_clones[repo] = PendingClone(
                    repo_name=repo, backend=backend, prompt=prompt,
                    model=model, agent=agent, triggered_by=username,
                )
                try:
                    post_message(
                        token, channel_id,
                        f"❓ Repo `{repo}` not found in `~/Code`.\n"
                        f"Reply with the SSH clone URL:\n"
                        f"`CLONE: {repo} git@github.com:org/{repo}.git`",
                    )
                except DiscordAPIError:
                    pass
                continue

            repo_path = str(repo_path_obj)
            _log(f"[general] Trigger from {username}: backend={backend} repo={repo_path}")

            def _run_general(
                _backend=backend, _prompt=prompt, _repo=repo_path,
                _model=model, _agent=agent, _user=username
            ) -> None:
                _launch_general_run(
                    token, channel_id, runner,
                    _backend, _prompt, _repo, _model, _agent, _user
                )

            threading.Thread(target=_run_general, daemon=True).start()


def _launch_general_run(
    token: str,
    channel_id: str,
    runner: Path,
    backend: str,
    prompt: str,
    repo_path: str,
    model: str | None,
    agent: str | None,
    username: str,
) -> None:
    """Build and execute a general run subprocess, streaming output to Discord."""
    cmd = [sys.executable, str(runner), "--backend", _BACKEND_MAP.get(backend, backend), "--prompt", prompt, "--repo", repo_path]
    if model:
        cmd += ["--model", model]
    if agent:
        cmd += ["--agent", agent]
    if _AGENTS_DIR_OVERRIDE is not None:
        cmd += ["--agents-dir", str(_AGENTS_DIR_OVERRIDE)]

    try:
        ack = post_message(
            token, channel_id,
            f"🤖 **General run triggered by {username}**\n"
            f"**Backend:** `{backend}`" + (f"  **Model:** `{model}`" if model else "") + "\n"
            f"**Repo:** `{repo_path}`\n"
            f"**Prompt:** {prompt[:200]}\n"
            "Starting… output will stream below.",
        )
        thread = create_thread(token, channel_id, str(ack["id"]), "general-run")
        thread_id = str(thread["id"])
    except DiscordAPIError as exc:
        _log(f"[general] Could not create thread: {exc}")
        return

    import queue as _queue
    line_q: _queue.Queue[str | None] = _queue.Queue()

    def _sub() -> None:
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
            )
            assert proc.stdout
            for line in proc.stdout:
                line_q.put(line.rstrip())
            proc.wait()
        finally:
            line_q.put(None)

    sub = threading.Thread(target=_sub, daemon=True)
    sub.start()
    stream_output_to_discord(token, thread_id, line_q)
    sub.join()



def main(argv: list[str] | None = None) -> int:
    global _RUNNER_SCRIPT_OVERRIDE, _AGENTS_DIR_OVERRIDE

    _init_file_logger()

    # Write PID file so the watchdog can check if we're alive
    pid_file = Path(__file__).resolve().parent / "logs" / "trigger_listener.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    parser = argparse.ArgumentParser(description="Discord Trigger Listener for agent workflows")
    parser.add_argument(
        "--repo",
        metavar="PATH",
        help="Default repository root (absolute path). Used when the RUN: command omits a path.",
    )
    parser.add_argument(
        "--backend",
        choices=["github-copilot", "claude-code"],
        help="Default AI backend (overridden per-message by the trigger format).",
    )
    parser.add_argument(
        "--runner-script",
        metavar="PATH",
        help="Absolute path to run_headless.py. Overrides the default relative resolution.",
    )
    parser.add_argument(
        "--agents-dir",
        metavar="PATH",
        help="Path to the agents directory passed to the runner scripts (e.g. /path/to/repo/.claude/agents).",
    )
    args = parser.parse_args(argv)

    if args.runner_script:
        _RUNNER_SCRIPT_OVERRIDE = Path(args.runner_script).resolve()

    if args.agents_dir:
        _AGENTS_DIR_OVERRIDE = Path(args.agents_dir).resolve()

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN is not set.", file=sys.stderr)
        print("  export DISCORD_BOT_TOKEN=<your-bot-token>", file=sys.stderr)
        pid_file.unlink(missing_ok=True)
        return 1

    guild_name = os.environ.get("DISCORD_GUILD_NAME", DEFAULT_GUILD_NAME)
    trigger_channel = os.environ.get("DISCORD_TRIGGER_CHANNEL", DEFAULT_TRIGGER_CHANNEL)
    poll_seconds = int(os.environ.get("DISCORD_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)))

    default_repo: str | None = str(Path(args.repo).resolve()) if args.repo else None

    _log(f"Default repo:  {default_repo or '(none — required per trigger)'}")
    _log(f"Guild:         {guild_name}")
    _log(f"Channel:       #{trigger_channel}")
    _log(f"Poll interval: {poll_seconds}s")

    # Resolve guild ID once — avoids a race / rate-limit when both pollers start simultaneously
    _log("Resolving guild ID…")
    try:
        guild_id = get_guild_id(token, guild_name)
    except DiscordAPIError as exc:
        _log(f"Fatal Discord error resolving guild: {exc}")
        pid_file.unlink(missing_ok=True)
        return 1
    _log(f"Guild ID: {guild_id}")

    # Start general-purpose channel poller in a background thread
    general_channel = os.environ.get("DISCORD_GENERAL_CHANNEL", DEFAULT_GENERAL_CHANNEL)
    _log(f"General channel: #{general_channel}")
    general_thread = threading.Thread(
        target=run_general_listener,
        kwargs=dict(
            token=token,
            guild_name=guild_name,
            channel_name=general_channel,
            poll_seconds=poll_seconds,
            guild_id=guild_id,
        ),
        daemon=True,
        name="general-poller",
    )
    general_thread.start()

    try:
        run_listener(
            token=token,
            guild_name=guild_name,
            trigger_channel_name=trigger_channel,
            poll_seconds=poll_seconds,
            default_repo=default_repo,
            guild_id=guild_id,
        )
    except KeyboardInterrupt:
        _log("Shutting down.")
        return 0
    except DiscordAPIError as exc:
        _log(f"Fatal Discord error: {exc}")
        return 1
    finally:
        pid_file.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
