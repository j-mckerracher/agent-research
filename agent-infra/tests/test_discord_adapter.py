"""Tests for trigger_api/adapters/discord.py."""

from __future__ import annotations

import json
import queue
import time
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from trigger_api.adapters.discord import (
    DiscordAPIError,
    _fmt_elapsed,
    build_completion_summary,
    discord_request,
    get_channel_id,
    get_channel_messages,
    get_guild_id,
    parse_trigger,
    post_message,
    post_to_thread,
    stream_output_to_discord,
)
from trigger_api.models import RunRecord


# ---------------------------------------------------------------------------
# parse_trigger
# ---------------------------------------------------------------------------


class TestParseTrigger:
    def test_basic_run(self):
        result = parse_trigger("RUN: WI-4461550")
        assert result == ("WI-4461550", None)

    def test_run_with_repo(self):
        result = parse_trigger("RUN: WI-100 /home/user/myrepo")
        assert result == ("WI-100", "/home/user/myrepo")

    def test_case_insensitive_prefix(self):
        assert parse_trigger("run: WI-1") == ("WI-1", None)
        assert parse_trigger("Run: WI-1") == ("WI-1", None)
        assert parse_trigger("RUN: WI-1") == ("WI-1", None)

    def test_adds_wi_prefix_if_missing(self):
        result = parse_trigger("RUN: 4461550")
        assert result == ("WI-4461550", None)

    def test_change_id_uppercased(self):
        result = parse_trigger("RUN: wi-100")
        assert result == ("WI-100", None)

    def test_leading_trailing_whitespace(self):
        result = parse_trigger("  RUN: WI-1  ")
        assert result == ("WI-1", None)

    def test_not_a_trigger_returns_none(self):
        assert parse_trigger("Hello world") is None
        assert parse_trigger("STATUS: WI-1") is None
        assert parse_trigger("") is None

    def test_run_prefix_with_no_body_returns_none(self):
        assert parse_trigger("RUN:") is None
        assert parse_trigger("RUN:   ") is None

    def test_repo_with_spaces_not_split(self):
        # Only first whitespace splits change_id from repo
        result = parse_trigger("RUN: WI-1 /path/to/my repo")
        assert result == ("WI-1", "/path/to/my repo")

    def test_multiword_body_uses_first_token(self):
        result = parse_trigger("RUN: WI-9 extra-word /some/path")
        # Second token onward becomes the repo_path
        assert result is not None
        assert result[0] == "WI-9"
        assert result[1] == "extra-word /some/path"


# ---------------------------------------------------------------------------
# _fmt_elapsed
# ---------------------------------------------------------------------------


class TestFmtElapsed:
    def test_seconds_only(self):
        assert _fmt_elapsed(45) == "45s"

    def test_minutes_and_seconds(self):
        assert _fmt_elapsed(90) == "1m 30s"

    def test_hours_minutes_seconds(self):
        assert _fmt_elapsed(3661) == "1h 1m 1s"

    def test_zero(self):
        assert _fmt_elapsed(0) == "0s"

    def test_exact_minute(self):
        assert _fmt_elapsed(60) == "1m 0s"


# ---------------------------------------------------------------------------
# build_completion_summary
# ---------------------------------------------------------------------------


class TestBuildCompletionSummary:
    def _record(self, **kwargs) -> RunRecord:
        defaults = dict(change_id="WI-1", source="http", status="complete")
        defaults.update(kwargs)
        return RunRecord(**defaults)  # type: ignore[arg-type]

    def test_complete_status_emoji(self):
        summary = build_completion_summary(self._record(status="complete", elapsed_seconds=10))
        assert "✅" in summary
        assert "COMPLETE" in summary

    def test_failed_status_emoji(self):
        summary = build_completion_summary(self._record(status="failed", elapsed_seconds=5))
        assert "❌" in summary
        assert "FAILED" in summary

    def test_cancelled_status_emoji(self):
        summary = build_completion_summary(self._record(status="cancelled", elapsed_seconds=3))
        assert "⚠️" in summary

    def test_change_id_in_summary(self):
        summary = build_completion_summary(self._record(change_id="WI-9999", elapsed_seconds=1))
        assert "WI-9999" in summary

    def test_elapsed_in_summary(self):
        summary = build_completion_summary(self._record(elapsed_seconds=125))
        assert "2m 5s" in summary

    def test_stage_table_rendered(self):
        record = self._record(
            status="complete",
            elapsed_seconds=30,
            result={
                "status": "pass",
                "stages": [
                    {"stage_name": "intake", "passed": True, "attempts": 1},
                    {"stage_name": "qa", "passed": False, "attempts": 2},
                ],
            },
        )
        summary = build_completion_summary(record)
        assert "intake" in summary
        assert "qa" in summary
        assert "✅" in summary
        assert "❌" in summary

    def test_error_shown(self):
        record = self._record(
            status="failed",
            elapsed_seconds=5,
            result={"error": "subprocess timed out"},
        )
        summary = build_completion_summary(record)
        assert "subprocess timed out" in summary

    def test_no_stages_no_table(self):
        record = self._record(status="complete", elapsed_seconds=1, result={})
        summary = build_completion_summary(record)
        assert "Stage Results" not in summary

    def test_none_result_handled(self):
        record = self._record(status="complete", elapsed_seconds=1, result=None)
        summary = build_completion_summary(record)
        assert "WI-1" in summary  # should not raise


# ---------------------------------------------------------------------------
# discord_request
# ---------------------------------------------------------------------------


def _make_urlopen_response(body: dict | list, status_code: int = 200):
    """Return a mock context manager mimicking urllib.request.urlopen."""
    encoded = json.dumps(body).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = encoded
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


class TestDiscordRequest:
    def test_get_request_parsed(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = _make_urlopen_response({"id": "123"})
            result = discord_request("GET", "/test", "token")
        assert result == {"id": "123"}

    def test_http_error_raises_discord_api_error(self):
        import urllib.error

        with patch("urllib.request.urlopen") as mock_open:
            err = urllib.error.HTTPError(
                url="", code=403, msg="Forbidden", hdrs=None, fp=BytesIO(b"forbidden")
            )
            mock_open.side_effect = err
            with pytest.raises(DiscordAPIError, match="403"):
                discord_request("GET", "/test", "token")

    def test_network_error_raises_discord_api_error(self):
        import urllib.error

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.URLError(reason="connection refused")
            with pytest.raises(DiscordAPIError, match="network error"):
                discord_request("GET", "/test", "token")

    def test_empty_response_body_returns_empty_dict(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"   "
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = discord_request("DELETE", "/test", "token")
        assert result == {}


class TestGetGuildId:
    def test_finds_guild_by_name(self):
        guilds = [{"id": "111", "name": "wrong-guild"}, {"id": "222", "name": "my-guild"}]
        with patch("trigger_api.adapters.discord.discord_request", return_value=guilds):
            assert get_guild_id("token", "my-guild") == "222"

    def test_guild_not_found_raises(self):
        with patch("trigger_api.adapters.discord.discord_request", return_value=[]):
            with pytest.raises(DiscordAPIError, match="not found"):
                get_guild_id("token", "missing-guild")


class TestGetChannelId:
    def test_finds_text_channel(self):
        channels = [
            {"id": "10", "name": "general", "type": 0},
            {"id": "20", "name": "trigger-agents", "type": 0},
        ]
        with patch("trigger_api.adapters.discord.discord_request", return_value=channels):
            assert get_channel_id("token", "guild-1", "trigger-agents") == "20"

    def test_ignores_non_text_channels(self):
        channels = [
            {"id": "10", "name": "trigger-agents", "type": 2},  # voice
            {"id": "20", "name": "trigger-agents", "type": 0},  # text
        ]
        with patch("trigger_api.adapters.discord.discord_request", return_value=channels):
            assert get_channel_id("token", "guild-1", "trigger-agents") == "20"

    def test_channel_not_found_raises(self):
        with patch("trigger_api.adapters.discord.discord_request", return_value=[]):
            with pytest.raises(DiscordAPIError, match="not found"):
                get_channel_id("token", "guild-1", "missing")


class TestPostToThread:
    def test_long_content_truncated(self):
        """Content over DISCORD_MAX_CHARS should be truncated, not raise."""
        long_content = "x" * 2000
        with patch("trigger_api.adapters.discord.discord_request") as mock_req:
            mock_req.return_value = {}
            post_to_thread("token", "thread-1", long_content)
        # Verify call was made with truncated content
        call_payload = mock_req.call_args[0][3]
        assert len(call_payload["content"]) <= 1900

    def test_api_error_does_not_raise(self):
        """post_to_thread swallows DiscordAPIError."""
        with patch(
            "trigger_api.adapters.discord.discord_request",
            side_effect=DiscordAPIError("oops"),
        ):
            post_to_thread("token", "thread-1", "hello")  # must not raise


# ---------------------------------------------------------------------------
# stream_output_to_discord
# ---------------------------------------------------------------------------


class TestStreamOutputToDiscord:
    def test_flushes_on_sentinel(self):
        posted: list[str] = []

        with patch(
            "trigger_api.adapters.discord.post_to_thread",
            side_effect=lambda token, tid, content: posted.append(content),
        ):
            q: queue.Queue[str | None] = queue.Queue()
            for i in range(3):
                q.put(f"line {i}")
            q.put(None)  # sentinel

            stream_output_to_discord("tok", "tid", q)

        assert len(posted) >= 1
        combined = "\n".join(posted)
        assert "line 0" in combined
        assert "line 2" in combined

    def test_large_buffer_triggers_flush(self):
        posted: list[str] = []

        with patch(
            "trigger_api.adapters.discord.post_to_thread",
            side_effect=lambda token, tid, content: posted.append(content),
        ):
            q: queue.Queue[str | None] = queue.Queue()
            # OUTPUT_FLUSH_LINES = 20, so 21 lines should trigger a mid-stream flush
            for i in range(21):
                q.put(f"line {i:03d}")
            q.put(None)

            stream_output_to_discord("tok", "tid", q)

        # At least two batches: one flush at 20 lines + one at sentinel
        assert len(posted) >= 2

    def test_content_wrapped_in_code_block(self):
        posted: list[str] = []

        with patch(
            "trigger_api.adapters.discord.post_to_thread",
            side_effect=lambda token, tid, content: posted.append(content),
        ):
            q: queue.Queue[str | None] = queue.Queue()
            q.put("hello")
            q.put(None)
            stream_output_to_discord("tok", "tid", q)

        assert posted[0].startswith("```")
        assert posted[0].endswith("```")
