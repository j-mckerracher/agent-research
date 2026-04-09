"""Tests for trigger_api/actions/run_workflow.py."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trigger_api.actions.run_workflow import RunWorkflowHandler
from trigger_api.models import RunRecord, TriggerEvent
from trigger_api.run_store import RunStore


def _event(change_id: str = "WI-1234", repo_path: str | None = None) -> TriggerEvent:
    return TriggerEvent(
        source="http",
        action="run",
        change_id=change_id,
        repo_path=repo_path,
        requester="test",
    )


def _make_handler(
    store: RunStore,
    runner: Path,
    tmp_path: Path,
) -> RunWorkflowHandler:
    return RunWorkflowHandler(
        run_store=store,
        default_repo=str(tmp_path),
        runner_script=runner,
        backend=None,
    )


# ---------------------------------------------------------------------------
# run_sync — blocking, synchronous tests (no asyncio needed)
# ---------------------------------------------------------------------------


class TestRunSync:
    def test_successful_run_sets_complete(
        self, store: RunStore, fake_runner_script: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_runner_script, tmp_path)
        event = _event()
        # Pre-add record (mimicking execute() or Discord adapter)
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))

        handler.run_sync(event)

        record = store.get("WI-1234")
        assert record is not None
        assert record.status == "complete"
        assert record.exit_code == 0
        assert record.elapsed_seconds is not None
        assert record.elapsed_seconds >= 0
        assert record.finished_at is not None

    def test_failing_run_sets_failed(
        self, store: RunStore, failing_runner_script: Path, tmp_path: Path
    ):
        handler = _make_handler(store, failing_runner_script, tmp_path)
        event = _event()
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))

        handler.run_sync(event)

        record = store.get("WI-1234")
        assert record.status == "failed"
        assert record.exit_code == 1

    def test_output_callback_called_per_line(
        self, store: RunStore, fake_runner_script: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_runner_script, tmp_path)
        event = _event()
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))

        lines: list[str] = []
        handler.run_sync(event, output_callback=lines.append)

        # The fake script prints "running WI-1234"
        assert any("WI-1234" in ln for ln in lines)

    def test_no_output_callback_does_not_raise(
        self, store: RunStore, fake_runner_script: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_runner_script, tmp_path)
        event = _event()
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))
        # Should log to stdout without raising
        handler.run_sync(event, output_callback=None)
        assert store.get("WI-1234").status == "complete"

    def test_result_json_parsed(
        self, store: RunStore, fake_runner_script: Path, tmp_path: Path
    ):
        """fake_runner_script writes a structured result JSON."""
        handler = _make_handler(store, fake_runner_script, tmp_path)
        event = _event()
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))

        handler.run_sync(event)

        record = store.get("WI-1234")
        assert record.result is not None
        assert record.result["status"] == "pass"
        assert len(record.result["stages"]) == 1

    def test_cancelled_run_not_overwritten(
        self, store: RunStore, fake_runner_script: Path, tmp_path: Path
    ):
        """If cancel() is called while subprocess runs, run_sync must not
        overwrite the 'cancelled' status with 'complete'."""
        handler = _make_handler(store, fake_runner_script, tmp_path)
        event = _event()
        store.add(RunRecord(change_id="WI-1234", status="cancelled", source="http"))

        handler.run_sync(event)

        # Status stays cancelled
        assert store.get("WI-1234").status == "cancelled"

    def test_backend_forwarded_to_subprocess(
        self, store: RunStore, tmp_path: Path
    ):
        """Verify --backend is passed through to the subprocess command."""
        script = tmp_path / "check_backend.py"
        script.write_text(
            "import sys, argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--change-id')\n"
            "p.add_argument('--repo')\n"
            "p.add_argument('--backend', default=None)\n"
            "p.add_argument('--output-json')\n"
            "args = p.parse_args()\n"
            "assert args.backend == 'claude', f'Expected claude, got {args.backend}'\n"
            "open(args.output_json, 'w').write('{}')\n"
            "sys.exit(0)\n"
        )
        handler = RunWorkflowHandler(
            run_store=store,
            default_repo=str(tmp_path),
            runner_script=script,
            backend="claude",
        )
        event = _event()
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))
        handler.run_sync(event)
        assert store.get("WI-1234").status == "complete"

    def test_event_backend_overrides_default(self, store: RunStore, tmp_path: Path):
        script = tmp_path / "check_be.py"
        script.write_text(
            "import sys, argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--change-id')\n"
            "p.add_argument('--repo')\n"
            "p.add_argument('--backend', default=None)\n"
            "p.add_argument('--output-json')\n"
            "args = p.parse_args()\n"
            "assert args.backend == 'copilot'\n"
            "open(args.output_json, 'w').write('{}')\n"
            "sys.exit(0)\n"
        )
        handler = RunWorkflowHandler(
            run_store=store,
            default_repo=str(tmp_path),
            runner_script=script,
            backend="claude",  # default
        )
        event = TriggerEvent(
            source="http",
            action="run",
            change_id="WI-1234",
            backend="copilot",  # override
        )
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))
        handler.run_sync(event)
        assert store.get("WI-1234").status == "complete"


# ---------------------------------------------------------------------------
# execute() — async, fires background task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestExecute:
    async def test_returns_running_record_immediately(
        self, store: RunStore, fake_runner_script: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_runner_script, tmp_path)
        event = _event()
        record = await handler.execute(event)
        assert record.status == "running"
        assert record.change_id == "WI-1234"
        assert store.get("WI-1234") is not None

    async def test_background_task_completes(
        self, store: RunStore, fake_runner_script: Path, tmp_path: Path
    ):
        import asyncio

        handler = _make_handler(store, fake_runner_script, tmp_path)
        await handler.execute(_event())
        # Give the background task time to finish
        for _ in range(50):
            await asyncio.sleep(0.1)
            if store.get("WI-1234") and store.get("WI-1234").status == "complete":
                break
        assert store.get("WI-1234").status == "complete"

    async def test_duplicate_raises_via_store(
        self, store: RunStore, fake_runner_script: Path, tmp_path: Path
    ):
        """execute() calls store.add() which raises on duplicate change_id."""
        import asyncio

        handler = _make_handler(store, fake_runner_script, tmp_path)
        await handler.execute(_event())
        with pytest.raises(ValueError):
            await handler.execute(_event())

    async def test_source_recorded(
        self, store: RunStore, fake_runner_script: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_runner_script, tmp_path)
        event = TriggerEvent(source="azure_devops", action="run", change_id="WI-5678")
        record = await handler.execute(event)
        assert record.source == "azure_devops"


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCancel:
    async def test_cancel_unknown_returns_false(
        self, store: RunStore, fake_runner_script: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_runner_script, tmp_path)
        result = await handler.cancel("WI-9999")
        assert result is False

    async def test_cancel_live_process(self, store: RunStore, tmp_path: Path):
        """Cancel should terminate the subprocess and set status='cancelled'."""
        # Use a long-running script
        script = tmp_path / "long.py"
        script.write_text(
            "import time, argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--change-id')\n"
            "p.add_argument('--repo')\n"
            "p.add_argument('--backend', default=None)\n"
            "p.add_argument('--output-json')\n"
            "p.parse_args()\n"
            "time.sleep(30)\n"
        )
        import asyncio

        handler = RunWorkflowHandler(
            run_store=store,
            default_repo=str(tmp_path),
            runner_script=script,
        )
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))

        # Start the subprocess in a background thread
        import threading

        def _run():
            handler.run_sync(
                TriggerEvent(source="http", action="run", change_id="WI-1234")
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        # Wait until the proc is registered
        for _ in range(20):
            await asyncio.sleep(0.05)
            with handler._procs_lock:
                if "WI-1234" in handler._procs:
                    break

        cancelled = await handler.cancel("WI-1234")
        assert cancelled is True
        assert store.get("WI-1234").status == "cancelled"
