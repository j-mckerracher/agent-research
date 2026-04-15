"""Tests for trigger_api/models.py."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trigger_api.models import (
    CancelResponse,
    HealthResponse,
    RunRecord,
    TriggerEvent,
)


# ---------------------------------------------------------------------------
# TriggerEvent
# ---------------------------------------------------------------------------


class TestTriggerEvent:
    def test_valid_minimal(self):
        e = TriggerEvent(source="http", action="run", change_id="WI-1234")
        assert e.change_id == "WI-1234"
        assert e.action == "run"
        assert e.source == "http"
        assert e.repo_path is None
        assert e.backend is None
        assert e.requester is None
        assert e.metadata == {}

    def test_valid_full(self):
        e = TriggerEvent(
            source="discord",
            action="run",
            change_id="WI-9999",
            repo_path="/some/repo",
            backend="claude-code",
            requester="alice",
            metadata={"thread_id": "abc"},
        )
        assert e.backend == "claude-code"
        assert e.metadata["thread_id"] == "abc"

    def test_change_id_normalised_no_prefix(self):
        e = TriggerEvent(source="http", action="run", change_id="4461550")
        assert e.change_id == "WI-4461550"

    def test_change_id_normalised_lowercase_wi(self):
        e = TriggerEvent(source="http", action="run", change_id="wi-4461550")
        assert e.change_id == "WI-4461550"

    def test_change_id_already_prefixed(self):
        e = TriggerEvent(source="http", action="run", change_id="WI-100")
        assert e.change_id == "WI-100"

    def test_change_id_strips_whitespace(self):
        e = TriggerEvent(source="http", action="run", change_id="  WI-200  ")
        assert e.change_id == "WI-200"

    def test_change_id_empty_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            TriggerEvent(source="http", action="run", change_id="")
        assert "change_id" in str(exc_info.value).lower() or "empty" in str(exc_info.value).lower()

    def test_action_lowercased(self):
        e = TriggerEvent(source="http", action="RUN", change_id="WI-1")
        assert e.action == "run"

    def test_action_empty_raises(self):
        with pytest.raises(ValidationError):
            TriggerEvent(source="http", action="   ", change_id="WI-1")

    def test_invalid_source_raises(self):
        with pytest.raises(ValidationError):
            TriggerEvent(source="slack", action="run", change_id="WI-1")

    def test_invalid_backend_raises(self):
        with pytest.raises(ValidationError):
            TriggerEvent(source="http", action="run", change_id="WI-1", backend="gpt4")

    def test_valid_sources(self):
        for src in ("discord", "http", "azure_devops"):
            e = TriggerEvent(source=src, action="run", change_id="WI-1")  # type: ignore[arg-type]
            assert e.source == src

    def test_valid_backends(self):
        for be in ("github-copilot", "claude-code"):
            e = TriggerEvent(source="http", action="run", change_id="WI-1", backend=be)  # type: ignore[arg-type]
            assert e.backend == be

    def test_metadata_defaults_to_empty_dict(self):
        e = TriggerEvent(source="http", action="run", change_id="WI-1")
        assert e.metadata == {}

    def test_arbitrary_action_names_allowed(self):
        """Actions beyond 'run' are valid — the registry decides if they're handled."""
        e = TriggerEvent(source="http", action="deploy", change_id="WI-1")
        assert e.action == "deploy"

    # --- General-run fields ---

    def test_general_run_fields_default_none(self):
        e = TriggerEvent(source="http", action="run", change_id="WI-1")
        assert e.prompt is None
        assert e.model is None
        assert e.agent_file is None

    def test_general_run_fields_set(self):
        e = TriggerEvent(
            source="http",
            action="general_run",
            change_id="GEN-1234",
            backend="claude-code",
            prompt="Fix the bug",
            model="sonnet",
            agent_file="spike.agent.md",
        )
        assert e.prompt == "Fix the bug"
        assert e.model == "sonnet"
        assert e.agent_file == "spike.agent.md"

    def test_general_run_action_normalised(self):
        e = TriggerEvent(
            source="http",
            action="GENERAL_RUN",
            change_id="GEN-1234",
        )
        assert e.action == "general_run"


# ---------------------------------------------------------------------------
# RunRecord
# ---------------------------------------------------------------------------


class TestRunRecord:
    def test_defaults(self):
        r = RunRecord(change_id="WI-1", source="http")
        assert r.status == "pending"
        assert r.finished_at is None
        assert r.elapsed_seconds is None
        assert r.discord_thread_id is None
        assert r.exit_code is None
        assert r.result is None
        assert r.started_at is not None

    def test_all_status_values(self):
        for s in ("pending", "running", "complete", "failed", "cancelled"):
            r = RunRecord(change_id="WI-1", source="http", status=s)  # type: ignore[arg-type]
            assert r.status == s

    def test_invalid_status_raises(self):
        with pytest.raises(ValidationError):
            RunRecord(change_id="WI-1", source="http", status="unknown")

    def test_result_stored(self):
        r = RunRecord(
            change_id="WI-1",
            source="http",
            result={"status": "pass", "stages": []},
        )
        assert r.result["status"] == "pass"


# ---------------------------------------------------------------------------
# CancelResponse
# ---------------------------------------------------------------------------


class TestCancelResponse:
    def test_true(self):
        r = CancelResponse(change_id="WI-1", cancelled=True)
        assert r.cancelled is True

    def test_false(self):
        r = CancelResponse(change_id="WI-1", cancelled=False)
        assert r.cancelled is False


# ---------------------------------------------------------------------------
# HealthResponse
# ---------------------------------------------------------------------------


class TestHealthResponse:
    def test_basic(self):
        h = HealthResponse(status="ok", active_runs=3, known_actions=["run"])
        assert h.status == "ok"
        assert h.active_runs == 3
        assert "run" in h.known_actions
