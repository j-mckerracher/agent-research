#!/usr/bin/env python3
"""Discord Trigger Listener — polls #trigger-agents for RUN: commands and launches workflows.

Posts live progress and a structured completion summary to the Discord thread it creates
for each run. Runs indefinitely until killed (Ctrl-C or SIGTERM).

Usage:
    DISCORD_BOT_TOKEN=<token> python3 discord_trigger_listener.py \\
        [--repo /abs/path/to/repo] \\
        [--backend copilot|claude] \\
        [--runner-script /abs/path/to/run_headless.py]

Trigger format (post in #trigger-agents):
    RUN: WI-4461550 claude
    RUN: WI-4461550 copilot
    RUN: WI-4461550 claude /absolute/path/to/repo

    NOTE: backend (claude|copilot) is required in every trigger message.

Environment variables:
    DISCORD_BOT_TOKEN        Required
    DISCORD_GUILD_NAME       Discord server name      (default: Agent-Escalations)
    DISCORD_TRIGGER_CHANNEL  Channel to watch         (default: trigger-agents)
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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISCORD_API_BASE = "https://discord.com/api/v10"
DEFAULT_GUILD_NAME = "Agent-Escalations"
DEFAULT_TRIGGER_CHANNEL = "trigger-agents"
DEFAULT_POLL_SECONDS = 10

# How many output lines to buffer before flushing to Discord
OUTPUT_FLUSH_LINES = 20
# How many seconds to wait before flushing even if buffer isn't full
OUTPUT_FLUSH_SECONDS = 30
# Maximum characters per Discord message (hard limit is 2000)
DISCORD_MAX_CHARS = 1900

RUN_PREFIX = "RUN:"
VALID_BACKENDS = ("claude", "copilot")

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
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise DiscordAPIError(
            f"Discord API {method} {endpoint} → HTTP {exc.code}: {body[:400]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DiscordAPIError(
            f"Discord API {method} {endpoint} → network error: {exc.reason}"
        ) from exc


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
    return change_id, backend, repo_path


# ---------------------------------------------------------------------------
# Workflow runner (subprocess)
# ---------------------------------------------------------------------------


_RUNNER_SCRIPT_OVERRIDE: Path | None = None


def _runner_script_path() -> Path:
    """Resolve run_headless.py. Uses --runner-script override if set."""
    if _RUNNER_SCRIPT_OVERRIDE is not None:
        return _RUNNER_SCRIPT_OVERRIDE
    return Path(__file__).resolve().parent.parent / "agent-runner" / "run_headless.py"


def run_workflow_subprocess(
    change_id: str,
    repo_path: str | None,
    default_repo: str,
    backend: str,
    output_json: Path,
    line_queue: "queue.Queue[str | None]",
) -> int:
    """Run run_headless.py in a subprocess, forwarding lines to line_queue.

    Puts None into the queue when finished. Returns exit code.
    """
    script = _runner_script_path()
    cmd = [sys.executable, str(script), "--change-id", change_id]
    cmd += ["--repo", repo_path or default_repo]
    cmd += ["--backend", backend]
    cmd += ["--output-json", str(output_json)]

    _log(f"Spawning: {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
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
) -> None:
    """Read from line_queue and flush to Discord in batches."""
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


# ---------------------------------------------------------------------------
# Main listener loop
# ---------------------------------------------------------------------------


def run_listener(
    token: str,
    guild_name: str,
    trigger_channel_name: str,
    poll_seconds: int,
    default_repo: str,
) -> None:
    _log("Resolving Discord guild and channel…")
    guild_id = get_guild_id(token, guild_name)
    _log(f"Guild ID: {guild_id}")
    trigger_channel_id = get_channel_id(token, guild_id, trigger_channel_name)
    _log(f"#{trigger_channel_name} channel ID: {trigger_channel_id}")
    _log(
        f"Polling every {poll_seconds}s. "
        f"Post 'RUN: WI-XXXX claude|copilot' in #{trigger_channel_name} to start a workflow."
    )

    last_seen_id: str | None = None
    active_runs: dict[str, ActiveRun] = {}  # change_id → ActiveRun

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
            _log(f"Warning: failed to fetch messages: {exc}")
            continue

        # Process oldest-first
        for msg in sorted(messages, key=lambda m: str(m.get("id", ""))):
            msg_id = str(msg.get("id", ""))
            author = msg.get("author", {})
            is_bot = bool(author.get("bot", False))
            content = str(msg.get("content", ""))
            username = str(author.get("global_name") or author.get("username", "unknown"))

            last_seen_id = msg_id

            if is_bot:
                continue

            parsed = parse_trigger(content)
            if not parsed:
                continue

            change_id_raw, backend, repo_override = parsed
            change_id = change_id_raw.upper() if change_id_raw.upper().startswith("WI-") else f"WI-{change_id_raw}"

            # Validate backend — required in the trigger message
            if backend not in VALID_BACKENDS:
                error_msg = (
                    f"⚠️ **Invalid or missing backend** in trigger from {username}.\n"
                    f"**Usage:** `RUN: {change_id} claude` or `RUN: {change_id} copilot`\n"
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

            _log(
                f"Trigger received from {username}: "
                f"RUN: {change_id}  backend={backend}  repo={repo_override or '(default)'}"
            )

            # Post an acknowledgement message and create a thread for this run
            try:
                ack_content = (
                    f"🤖 **Workflow triggered by {username}**\n"
                    f"**Change:** `{change_id}`\n"
                    f"**Backend:** `{backend}`\n"
                    f"**Repo:** `{repo_override or default_repo}`\n"
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
                continue

            active_runs[change_id] = ActiveRun(change_id, thread_id)

            # Launch the workflow in a background thread
            _start_run_thread(
                token=token,
                change_id=change_id,
                repo_override=repo_override,
                default_repo=default_repo,
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
    repo_override: str | None,
    default_repo: str,
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

        # Start the subprocess in another thread so we can stream its output
        exit_code_holder: list[int] = [0]

        def _subprocess_thread() -> None:
            exit_code_holder[0] = run_workflow_subprocess(
                change_id=change_id,
                repo_path=repo_override,
                default_repo=default_repo,
                backend=backend,
                output_json=output_json,
                line_queue=line_q,
            )

        sub_thread = threading.Thread(target=_subprocess_thread, daemon=True)
        sub_thread.start()

        # Stream output to Discord (blocks until subprocess exits)
        stream_output_to_discord(token, thread_id, line_q)
        sub_thread.join()

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


def main(argv: list[str] | None = None) -> int:
    global _RUNNER_SCRIPT_OVERRIDE

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
        choices=["copilot", "claude"],
        help="Default AI backend (overridden per-message by the trigger format).",
    )
    parser.add_argument(
        "--runner-script",
        metavar="PATH",
        help="Absolute path to run_headless.py. Overrides the default relative resolution.",
    )
    args = parser.parse_args(argv)

    if args.runner_script:
        _RUNNER_SCRIPT_OVERRIDE = Path(args.runner_script).resolve()

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN is not set.", file=sys.stderr)
        print("  export DISCORD_BOT_TOKEN=<your-bot-token>", file=sys.stderr)
        pid_file.unlink(missing_ok=True)
        return 1

    guild_name = os.environ.get("DISCORD_GUILD_NAME", DEFAULT_GUILD_NAME)
    trigger_channel = os.environ.get("DISCORD_TRIGGER_CHANNEL", DEFAULT_TRIGGER_CHANNEL)
    poll_seconds = int(os.environ.get("DISCORD_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)))

    # Resolve default repo (arg → git root of cwd → script parent)
    if args.repo:
        default_repo = str(Path(args.repo).resolve())
    else:
        import subprocess as _sp
        result = _sp.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        default_repo = result.stdout.strip() if result.returncode == 0 else str(
            Path(__file__).resolve().parent.parent
        )

    _log(f"Default repo:  {default_repo}")
    _log(f"Guild:         {guild_name}")
    _log(f"Channel:       #{trigger_channel}")
    _log(f"Poll interval: {poll_seconds}s")

    try:
        run_listener(
            token=token,
            guild_name=guild_name,
            trigger_channel_name=trigger_channel,
            poll_seconds=poll_seconds,
            default_repo=default_repo,
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
