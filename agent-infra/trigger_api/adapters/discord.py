"""Discord adapter — REST helpers + polling background task.

All Discord REST calls use stdlib urllib (zero extra deps).  The polling loop
is an async coroutine run via asyncio.create_task() from the lifespan context.

The Discord adapter integrates with RunWorkflowHandler in a special way: when
a RUN: message is detected it manages its own background *thread* (not asyncio
task) so it can run a threading.Queue-based line streamer alongside the
subprocess — preserving the original streaming behaviour.
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

RUN_PREFIX = "RUN:"

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


def parse_trigger(content: str) -> tuple[str, str | None] | None:
    """Parse a ``RUN:`` message.

    Returns ``(change_id, repo_path_or_None)`` or ``None`` if not a trigger.
    """
    stripped = content.strip()
    if not stripped.upper().startswith(RUN_PREFIX.upper()):
        return None
    body = stripped[len(RUN_PREFIX) :].strip()
    if not body:
        return None
    parts = body.split(None, 1)
    change_id_raw = parts[0].strip()
    repo_path = parts[1].strip() if len(parts) > 1 else None
    upper = change_id_raw.upper()
    change_id = upper if upper.startswith("WI-") else f"WI-{upper}"
    return change_id, repo_path


# ---------------------------------------------------------------------------
# Output streamer (runs in its own thread, drains the line queue to Discord)
# ---------------------------------------------------------------------------


def _format_log_chunk(lines: list[str]) -> str:
    return f"```\n{chr(10).join(lines)}\n```"


def stream_output_to_discord(
    token: str,
    thread_id: str,
    line_queue: "queue.Queue[str | None]",
) -> None:
    """Drain *line_queue* and flush batches to a Discord thread.

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
    ) -> None:
        self._token = token
        self._guild_name = guild_name
        self._trigger_channel_name = trigger_channel_name
        self._poll_seconds = poll_seconds
        self._handler = action_handler
        self._store = run_store

    async def run(self) -> None:
        """Entry point — call via ``asyncio.create_task(adapter.run())``."""
        _log("Resolving Discord guild and channel…")
        guild_id = await asyncio.to_thread(
            get_guild_id, self._token, self._guild_name
        )
        _log(f"Guild ID: {guild_id}")
        channel_id = await asyncio.to_thread(
            get_channel_id, self._token, guild_id, self._trigger_channel_name
        )
        _log(
            f"#{self._trigger_channel_name} channel ID: {channel_id}  "
            f"(polling every {self._poll_seconds}s)"
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

                parsed = parse_trigger(content)
                if not parsed:
                    continue

                change_id, repo_override = parsed

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
                    continue

                _log(
                    f"Trigger from {username}: RUN: {change_id}  "
                    f"repo={repo_override or '(default)'}"
                )

                event = TriggerEvent(
                    source="discord",
                    action="run",
                    change_id=change_id,
                    repo_path=repo_override,
                    requester=username,
                )

                await self._start_discord_run(event, channel_id)

    async def _start_discord_run(
        self, event: TriggerEvent, channel_id: str
    ) -> None:
        """Post ACK, create thread, register record, launch background thread."""
        change_id = event.change_id
        try:
            ack_content = (
                f"🤖 **Workflow triggered by {event.requester}**\n"
                f"**Change:** `{change_id}`\n"
                f"**Repo:** `{event.repo_path or self._handler._default_repo}`\n"
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

        streamer = threading.Thread(
            target=stream_output_to_discord,
            args=(self._token, thread_id, line_q),
            daemon=True,
        )
        streamer.start()

        self._handler.run_sync(event, output_callback=line_q.put)

        line_q.put(None)  # sentinel → streamer flushes and exits
        streamer.join()

        record = self._store.get(event.change_id)
        if record:
            summary = build_completion_summary(record)
            post_to_thread(self._token, thread_id, summary)
