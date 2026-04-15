"""Discord adapter — REST helpers + polling background task.

All Discord REST calls use stdlib urllib (zero extra deps).  The polling loop
is an async coroutine run via asyncio.create_task() from the lifespan context.

Two channels are polled by separate DiscordPollerAdapter instances:

* **trigger-agents-ado-work** — ADO work-item triggers.  Messages use the
  format ``RUN: <change-id> [backend]``.  Parsed by :func:`parse_trigger`.

* **trigger-agents-general** — General-purpose agent triggers.  Messages use
  the format ``RUN: --backend <backend> --prompt "<text>" --repo <path>
  [--model <id>] [--agent <file>]``.  Parsed by :func:`parse_general_trigger`.

Both pollers share the same streaming-to-thread + Discord-thread output
pattern via ``stream_output_to_discord``.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from ..models import RunRecord, TriggerEvent

if TYPE_CHECKING:
    from ..actions.run_workflow import RunWorkflowHandler
    from ..run_store import RunStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_MAX_CHARS = 1900
OUTPUT_FLUSH_LINES = 20
OUTPUT_FLUSH_SECONDS = 30
_EVENT_PREFIX = "##EVENT##"
HEARTBEAT_INTERVAL_SECONDS = 180

RUN_PREFIX = "RUN:"
HELP_PREFIX = "HELP:"

_BACKEND_KEYWORDS: frozenset[str] = frozenset({"github-copilot", "claude-code"})

HELP_MESSAGE_ADO = """\
**🤖 ADO Workflow Trigger — command reference**

**Start an ADO work-item run:**
```
RUN: <change-id> [repo-path] [backend]
```
**Arguments:**
• `change-id` — Work item ID *(required)*. Accepts `WI-5002532` or bare `5002532`.
• `repo-path` — Absolute path to the repository root *(optional)*. Uses the server default when omitted.
• `backend` — AI backend to use: `claude-code` or `github-copilot` *(optional)*. Auto-detected when omitted.

**Examples:**
```
RUN: WI-5002532
RUN: WI-5002532 claude-code
RUN: WI-5002532 /Users/you/Code/my-repo claude-code
```
Type `HELP:` at any time to see this message again.\
"""

# Keep backward-compat alias
HELP_MESSAGE = HELP_MESSAGE_ADO

HELP_MESSAGE_GENERAL = """\
**🤖 General Agent Trigger — command reference**

**Start a general run:**
```
RUN: --backend <backend> --prompt "<prompt>" --repo <repo-name> [--model <model>] [--agent <file>]
```
**Required arguments:**
• `--backend` — AI backend: `claude-code` or `github-copilot`.
• `--prompt` — The prompt to send (wrap in quotes).
• `--repo` — Repository name (e.g. `mcs-products-mono-ui`) — searched under `~/Code`.

**Optional arguments:**
• `--model` — Model identifier (see below).
• `--agent` — Path to an `.agent.md` file.
• Any extra `--key value` pairs are passed through as metadata.

**Available models:**
  `github-copilot` → gpt-5.4 | gpt-5.3-codex | gpt-5.2 | gpt-5.1 | gpt-5.4-mini | gpt-5-mini | gpt-4.1 | claude-sonnet-4.6 | claude-opus-4.6 | claude-haiku-4.5
  `claude-code`    → claude-opus-4-5 | claude-sonnet-4-5 | claude-haiku-4-5

**Examples:**
```
RUN: --backend github-copilot --model claude-sonnet-4.6 --prompt "Fix failing tests" --repo mcs-products-mono-ui
RUN: --backend claude-code --model claude-sonnet-4-5 --prompt "Add unit tests" --repo my-repo --agent spike.agent.md
```
If the repo is not found in `~/Code`, the server will prompt for an SSH clone URL.
Type `HELP:` at any time to see this message again.\
"""

# ---------------------------------------------------------------------------
# Low-level REST helpers
# ---------------------------------------------------------------------------


class DiscordAPIError(RuntimeError):
    pass


def discord_request(
    method: str,
    endpoint: str,
    token: str,
    payload: dict | None = None,
) -> object:
    url = f"{DISCORD_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "AgentTriggerAPI/2.0",
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
    guilds = discord_request("GET", "/users/@me/guilds", token)
    assert isinstance(guilds, list)
    for g in guilds:
        if g.get("name") == guild_name:
            return str(g["id"])
    names = [g.get("name") for g in guilds]
    raise DiscordAPIError(f"Guild {guild_name!r} not found. Bot is in: {names}")


def get_channel_id(token: str, guild_id: str, channel_name: str) -> str:
    channels = discord_request("GET", f"/guilds/{guild_id}/channels", token)
    assert isinstance(channels, list)
    for c in channels:
        if c.get("name") == channel_name and c.get("type") in (0, 5):
            return str(c["id"])
    available = [c.get("name") for c in channels if c.get("type") in (0, 5)]
    raise DiscordAPIError(
        f"Channel #{channel_name!r} not found. Available: {available}"
    )


def post_message(token: str, channel_id: str, content: str) -> dict:
    result = discord_request(
        "POST", f"/channels/{channel_id}/messages", token, {"content": content}
    )
    assert isinstance(result, dict)
    return result


def create_thread(
    token: str, channel_id: str, message_id: str, name: str
) -> dict:
    result = discord_request(
        "POST",
        f"/channels/{channel_id}/messages/{message_id}/threads",
        token,
        {"name": name[:100], "auto_archive_duration": 10080},
    )
    assert isinstance(result, dict)
    return result


def post_to_thread(token: str, thread_id: str, content: str) -> None:
    if len(content) > DISCORD_MAX_CHARS:
        content = content[: DISCORD_MAX_CHARS - 20] + "\n… (truncated)"
    try:
        discord_request(
            "POST",
            f"/channels/{thread_id}/messages",
            token,
            {"content": content},
        )
    except DiscordAPIError as exc:
        _log(f"Warning: could not post to thread: {exc}")


def get_channel_messages(
    token: str, channel_id: str, after_id: str | None
) -> list[dict]:
    endpoint = f"/channels/{channel_id}/messages?limit=50"
    if after_id:
        endpoint += f"&after={after_id}"
    result = discord_request("GET", endpoint, token)
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# Trigger parser
# ---------------------------------------------------------------------------


def parse_trigger(content: str) -> tuple[str, str | None, str | None] | None:
    """Parse a ``RUN:`` message.

    Accepted forms::

        RUN: <change-id>
        RUN: <change-id> <backend>
        RUN: <change-id> <repo-path>
        RUN: <change-id> <repo-path> <backend>

    Where ``<backend>`` is ``claude-code`` or ``github-copilot`` (case-insensitive) and
    ``<repo-path>`` is any other token (or multi-word string).  A backend
    keyword appearing as the *last* whitespace-delimited token is always
    recognised as the backend, regardless of what precedes it.

    Returns ``(change_id, repo_path, backend)`` or ``None`` if not a trigger.
    """
    stripped = content.strip()
    if not stripped.upper().startswith(RUN_PREFIX.upper()):
        return None
    body = stripped[len(RUN_PREFIX):].strip()
    if not body:
        return None

    # First token is always the change-id.
    parts = body.split(None, 1)
    change_id_raw = parts[0].strip()
    upper = change_id_raw.upper()
    change_id = upper if upper.startswith("WI-") else f"WI-{upper}"

    remainder = parts[1].strip() if len(parts) > 1 else ""

    repo_path: str | None = None
    backend: str | None = None

    if remainder:
        # Check whether the last whitespace-delimited token is a backend keyword.
        last_space = remainder.rfind(" ")
        if last_space >= 0:
            last_token = remainder[last_space + 1:].strip()
            if last_token.lower() in _BACKEND_KEYWORDS:
                backend = last_token.lower()
                repo_path = remainder[:last_space].strip() or None
            else:
                repo_path = remainder
        else:
            # Single token: either a backend keyword or a repo path.
            if remainder.lower() in _BACKEND_KEYWORDS:
                backend = remainder.lower()
            else:
                repo_path = remainder

    return change_id, repo_path, backend


# ---------------------------------------------------------------------------
# General trigger parser
# ---------------------------------------------------------------------------

# Named fields the general parser knows about.  Anything else is metadata.
_GENERAL_KNOWN_ARGS = frozenset({"backend", "model", "prompt", "repo", "agent"})
_GENERAL_REQUIRED_ARGS = frozenset({"backend", "prompt", "repo"})


def parse_general_trigger(
    content: str,
) -> dict[str, str | None] | None:
    """Parse a ``RUN:`` message in the general channel (argparse-style).

    Accepted form::

        RUN: --backend claude-code --prompt "Fix the bug" --repo my-repo [--model claude-sonnet-4-5] [--agent spike.agent.md]

    Returns a dict with keys ``backend``, ``model``, ``prompt``, ``repo``,
    ``agent`` plus any extra ``--key value`` pairs, or *None* if the message
    is not a valid general trigger.  Missing optional keys have ``None`` values.
    """
    stripped = content.strip()
    if not stripped.upper().startswith(RUN_PREFIX.upper()):
        return None
    body = stripped[len(RUN_PREFIX):].strip()
    if not body:
        return None

    # Tokenise respecting double-quoted strings
    tokens = _tokenise_args(body)
    if not tokens:
        return None

    # Must look like flag-style args (first token starts with --)
    if not tokens[0].startswith("--"):
        return None

    result: dict[str, str | None] = {k: None for k in _GENERAL_KNOWN_ARGS}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:]
            # Peek at next token for value
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                value = tokens[i + 1]
                i += 2
            else:
                value = None
                i += 1
            if key in _GENERAL_KNOWN_ARGS:
                result[key] = value
            else:
                result[key] = value
        else:
            i += 1

    # Validate required fields
    for req in _GENERAL_REQUIRED_ARGS:
        if not result.get(req):
            return None

    return result


def _tokenise_args(text: str) -> list[str]:
    """Split *text* into tokens, respecting double-quoted strings."""
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        # Skip whitespace
        while i < n and text[i] in (" ", "\t"):
            i += 1
        if i >= n:
            break
        if text[i] == '"':
            # Quoted string — collect until closing quote
            i += 1
            start = i
            while i < n and text[i] != '"':
                i += 1
            tokens.append(text[start:i])
            if i < n:
                i += 1  # skip closing quote
        else:
            start = i
            while i < n and text[i] not in (" ", "\t"):
                i += 1
            tokens.append(text[start:i])
    return tokens


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


def _stage_label(name: str) -> str:
    return _STAGE_LABELS.get(name, name)


def _update_event_state(state: EventState, payload: dict) -> None:
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


def _format_event_message(payload: dict, state: EventState | None) -> str | None:
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

    return None


def _build_heartbeat_summary(state: EventState, elapsed: float) -> str:
    def _fmt(s: float) -> str:
        m, sec = divmod(int(s), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}h {m}m {sec}s"
        if m:
            return f"{m}m {sec}s"
        return f"{sec}s"

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
        f":blue_circle: **Status Update** — {_fmt(elapsed)} elapsed",
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
# Output streamer (runs in its own thread, drains the line queue to Discord)
# ---------------------------------------------------------------------------


def _format_log_chunk(lines: list[str]) -> str:
    return f"```\n{chr(10).join(lines)}\n```"


def stream_output_to_discord(
    token: str,
    thread_id: str,
    line_queue: "queue.Queue[str | None]",
    event_state: "EventState | None" = None,
) -> None:
    """Drain *line_queue* and flush batches to a Discord thread.

    Lines prefixed with _EVENT_PREFIX are parsed as structured events and posted
    as formatted Discord messages; they are never included in raw code-block output.
    Blocks until a ``None`` sentinel is received (subprocess finished).
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
                msg = _format_event_message(payload, event_state)
                if msg:
                    post_to_thread(token, thread_id, msg)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                _log(f"Failed to parse event line: {exc}")
            continue

        buffer.append(line)
        chars = sum(len(ln) for ln in buffer)
        if len(buffer) >= OUTPUT_FLUSH_LINES or chars >= DISCORD_MAX_CHARS - 200:
            _flush()
        elif time.monotonic() - last_flush >= OUTPUT_FLUSH_SECONDS:
            _flush()


# ---------------------------------------------------------------------------
# Completion summary builder
# ---------------------------------------------------------------------------

_STAGE_EMOJI = {True: "✅", False: "❌"}
_STATUS_EMOJI = {"complete": "✅", "failed": "❌", "cancelled": "⚠️"}


def build_completion_summary(record: RunRecord) -> str:
    elapsed_str = _fmt_elapsed(record.elapsed_seconds or 0)
    status_emoji = _STATUS_EMOJI.get(record.status, "❓")
    status_label = record.status.upper()

    lines = [
        f"## {status_emoji} Workflow {status_label} — `{record.change_id}`",
        f"**Elapsed:** {elapsed_str}  |  **Status:** `{status_label}`",
        "",
    ]

    result = record.result or {}
    stages: list[dict] = result.get("stages", [])
    if stages:
        lines += [
            "**Stage Results:**",
            "",
            "| Stage | Result | Attempts |",
            "|---|---|---|",
        ]
        for stage in stages:
            emoji = _STAGE_EMOJI.get(stage.get("passed", False), "❓")
            name = stage.get("stage_name", "?")
            attempts = stage.get("attempts", "?")
            lines.append(f"| `{name}` | {emoji} | {attempts} |")

    error = result.get("error", "")
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
# Discord poller adapter
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[discord] {ts} {msg}", flush=True)


class DiscordPollerAdapter:
    """Async polling loop that watches a Discord channel for ``RUN:`` commands.

    Supports two channel types:

    * ``"ado"`` — ADO work-item triggers (``RUN: <change-id> ...``).
    * ``"general"`` — General-purpose triggers
      (``RUN: --backend X --prompt "Y" --repo /path ...``).

    Designed to run as a long-lived asyncio background task.  All blocking
    Discord REST calls are wrapped in ``asyncio.to_thread`` so the event loop
    is never blocked.
    """

    def __init__(
        self,
        token: str,
        guild_name: str,
        trigger_channel_name: str,
        poll_seconds: int,
        action_handler: "RunWorkflowHandler",
        run_store: "RunStore",
        *,
        channel_type: str = "ado",
        help_message: str | None = None,
    ) -> None:
        self._token = token
        self._guild_name = guild_name
        self._trigger_channel_name = trigger_channel_name
        self._poll_seconds = poll_seconds
        self._handler = action_handler
        self._store = run_store
        self._channel_type = channel_type
        self._help_message = (
            help_message
            if help_message is not None
            else (HELP_MESSAGE_GENERAL if channel_type == "general" else HELP_MESSAGE_ADO)
        )

    async def run(self) -> None:
        """Entry point — call via ``asyncio.create_task(adapter.run())``."""
        _log(f"Resolving Discord guild and channel ({self._channel_type})…")
        guild_id = await asyncio.to_thread(
            get_guild_id, self._token, self._guild_name
        )
        _log(f"Guild ID: {guild_id}")
        channel_id = await asyncio.to_thread(
            get_channel_id, self._token, guild_id, self._trigger_channel_name
        )
        _log(
            f"#{self._trigger_channel_name} channel ID: {channel_id}  "
            f"(polling every {self._poll_seconds}s, type={self._channel_type})"
        )

        # Seed last_seen_id so we don't replay history on startup
        seed = await asyncio.to_thread(
            get_channel_messages, self._token, channel_id, None
        )
        last_seen_id: str | None = None
        if seed:
            last_seen_id = str(max(seed, key=lambda m: m.get("id", "0"))["id"])
            _log(f"Seeded last_seen_id={last_seen_id}")

        while True:
            await asyncio.sleep(self._poll_seconds)
            try:
                messages = await asyncio.to_thread(
                    get_channel_messages,
                    self._token,
                    channel_id,
                    last_seen_id,
                )
            except DiscordAPIError as exc:
                _log(f"Warning: failed to fetch messages: {exc}")
                continue

            for msg in sorted(messages, key=lambda m: str(m.get("id", ""))):
                msg_id = str(msg.get("id", ""))
                author = msg.get("author", {})
                if author.get("bot"):
                    last_seen_id = msg_id
                    continue
                content = str(msg.get("content", ""))
                username = str(
                    author.get("global_name") or author.get("username", "unknown")
                )
                last_seen_id = msg_id

                # HELP: command — reply inline, no thread needed
                if content.strip().upper().startswith(HELP_PREFIX.upper()):
                    await asyncio.to_thread(
                        post_message, self._token, channel_id, self._help_message
                    )
                    continue

                if self._channel_type == "general":
                    await self._handle_general_message(
                        content, username, channel_id
                    )
                else:
                    await self._handle_ado_message(
                        content, username, channel_id
                    )

    # ------------------------------------------------------------------
    # ADO channel message handler
    # ------------------------------------------------------------------

    async def _handle_ado_message(
        self, content: str, username: str, channel_id: str
    ) -> None:
        parsed = parse_trigger(content)
        if not parsed:
            return

        change_id, repo_override, backend_override = parsed

        if self._store.has_active(change_id):
            _log(f"Ignoring RUN: {change_id} — already running")
            run = self._store.get(change_id)
            if run and run.discord_thread_id:
                await asyncio.to_thread(
                    post_to_thread,
                    self._token,
                    run.discord_thread_id,
                    f"⚠️ `{change_id}` is already running.",
                )
            return

        _log(
            f"Trigger from {username}: RUN: {change_id}  "
            f"repo={repo_override or '(default)'}  "
            f"backend={backend_override or '(auto)'}"
        )

        event = TriggerEvent(
            source="discord",
            action="run",
            change_id=change_id,
            repo_path=repo_override,
            backend=backend_override,
            requester=username,
        )

        await self._start_discord_run(event, channel_id)

    # ------------------------------------------------------------------
    # General channel message handler
    # ------------------------------------------------------------------

    async def _handle_general_message(
        self, content: str, username: str, channel_id: str
    ) -> None:
        parsed = parse_general_trigger(content)
        if not parsed:
            return

        backend = parsed.get("backend")
        prompt = parsed.get("prompt")
        repo = parsed.get("repo")
        model = parsed.get("model")
        agent = parsed.get("agent")

        # Generate a synthetic change_id for tracking
        import secrets as _secrets

        change_id = f"GEN-{_secrets.token_hex(4).upper()}"

        if self._store.has_active(change_id):
            _log(f"Ignoring duplicate general run {change_id}")
            return

        # Collect extra metadata (any unknown --key value pairs)
        extra = {
            k: v for k, v in parsed.items()
            if k not in _GENERAL_KNOWN_ARGS and v is not None
        }

        _log(
            f"General trigger from {username}: backend={backend}  "
            f"model={model or '(default)'}  repo={repo}  "
            f"agent={agent or '(none)'}  prompt={prompt[:60]}…"
        )

        event = TriggerEvent(
            source="discord",
            action="general_run",
            change_id=change_id,
            repo_path=repo,
            backend=backend,
            requester=username,
            prompt=prompt,
            model=model,
            agent_file=agent,
            metadata=extra,
        )

        await self._start_discord_run(event, channel_id)

    async def _start_discord_run(
        self, event: TriggerEvent, channel_id: str
    ) -> None:
        """Post ACK, create thread, register record, launch background thread."""
        change_id = event.change_id
        try:
            backend_label = event.backend or "auto-detect"
            if self._channel_type == "general":
                model_label = event.model or "(default)"
                prompt_preview = (event.prompt or "")[:80]
                ack_content = (
                    f"🤖 **General run triggered by {event.requester}**\n"
                    f"**ID:** `{change_id}`\n"
                    f"**Backend:** `{backend_label}`  |  **Model:** `{model_label}`\n"
                    f"**Repo:** `{event.repo_path or self._handler._default_repo}`\n"
                )
                if event.agent_file:
                    ack_content += f"**Agent:** `{event.agent_file}`\n"
                ack_content += (
                    f"**Prompt:** {prompt_preview}{'…' if len(event.prompt or '') > 80 else ''}\n"
                    "Starting run… updates will appear below."
                )
            else:
                ack_content = (
                    f"🤖 **Workflow triggered by {event.requester}**\n"
                    f"**Change:** `{change_id}`\n"
                    f"**Repo:** `{event.repo_path or self._handler._default_repo}`\n"
                    f"**Backend:** `{backend_label}`\n"
                    "Starting workflow… updates will appear below."
                )
            ack_msg = await asyncio.to_thread(
                post_message, self._token, channel_id, ack_content
            )
            thread = await asyncio.to_thread(
                create_thread,
                self._token,
                channel_id,
                str(ack_msg["id"]),
                f"run-{change_id}",
            )
            thread_id = str(thread["id"])
        except DiscordAPIError as exc:
            _log(f"Could not create thread for {change_id}: {exc}")
            return

        record = RunRecord(
            change_id=change_id,
            status="running",
            source="discord",
            requester=event.requester,
            discord_thread_id=thread_id,
        )
        self._store.add(record)

        t = threading.Thread(
            target=self._discord_run_thread,
            args=(event, thread_id),
            daemon=True,
            name=f"discord-run-{change_id}",
        )
        t.start()

    def _discord_run_thread(self, event: TriggerEvent, thread_id: str) -> None:
        """Blocking thread: subprocess → line queue → Discord streamer."""
        line_q: "queue.Queue[str | None]" = queue.Queue()
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
                        post_to_thread(self._token, thread_id, summary)
                    except Exception as exc:  # noqa: BLE001
                        _log(f"Heartbeat post failed: {exc}")

        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            daemon=True,
            name=f"heartbeat-{event.change_id}",
        )
        heartbeat_thread.start()

        streamer = threading.Thread(
            target=stream_output_to_discord,
            args=(self._token, thread_id, line_q, event_state),
            daemon=True,
        )
        streamer.start()

        self._handler.run_sync(event, output_callback=line_q.put)

        line_q.put(None)  # sentinel → streamer flushes and exits
        streamer.join()

        # Stop heartbeat before posting completion summary
        done_event.set()
        heartbeat_thread.join(timeout=5)

        record = self._store.get(event.change_id)
        if record:
            summary = build_completion_summary(record)
            post_to_thread(self._token, thread_id, summary)
