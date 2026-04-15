"""Tests for trigger_api/actions/general_run.py."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

from trigger_api.actions.general_run import GeneralRunHandler
from trigger_api.models import RunRecord, TriggerEvent
from trigger_api.run_store import RunStore


def _general_event(
    change_id: str = "WI-GEN-ABCD1234",
    backend: str = "claude-code",
    prompt: str = "Fix the tests",
    repo_path: str | None = None,
    model: str | None = None,
    agent_file: str | None = None,
) -> TriggerEvent:
    return TriggerEvent(
        source="http",
        action="general_run",
        change_id=change_id,
        backend=backend,
        prompt=prompt,
        repo_path=repo_path,
        model=model,
        agent_file=agent_file,
        requester="test",
    )


@pytest.fixture
def fake_general_runner(tmp_path: Path) -> Path:
    """A minimal run_general.py that exits 0 and writes a pass result."""
    script = tmp_path / "run_general.py"
    script.write_text(
        "import sys, json, argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--backend', required=True)\n"
        "p.add_argument('--model', default=None)\n"
        "p.add_argument('--prompt', required=True)\n"
        "p.add_argument('--repo', required=True)\n"
        "p.add_argument('--agent', default=None)\n"
        "p.add_argument('--output-json', default=None)\n"
        "args = p.parse_args()\n"
        "print(f'running general: {args.backend} {args.prompt}')\n"
        "if args.output_json:\n"
        "    result = {'status': 'pass', 'exit_code': 0, 'backend': args.backend}\n"
        "    open(args.output_json, 'w').write(json.dumps(result))\n"
        "sys.exit(0)\n"
    )
    return script


@pytest.fixture
def failing_general_runner(tmp_path: Path) -> Path:
    """A run_general.py that exits 1."""
    script = tmp_path / "run_general_fail.py"
    script.write_text(
        "import sys, argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--backend', required=True)\n"
        "p.add_argument('--model', default=None)\n"
        "p.add_argument('--prompt', required=True)\n"
        "p.add_argument('--repo', required=True)\n"
        "p.add_argument('--agent', default=None)\n"
        "p.add_argument('--output-json', default=None)\n"
        "args = p.parse_args()\n"
        "print('general run failed')\n"
        "sys.exit(1)\n"
    )
    return script


def _make_handler(
    store: RunStore,
    runner: Path,
    tmp_path: Path,
) -> GeneralRunHandler:
    return GeneralRunHandler(
        run_store=store,
        default_repo=str(tmp_path),
        runner_script=runner,
        backend=None,
    )


# ---------------------------------------------------------------------------
# run_sync
# ---------------------------------------------------------------------------


class TestGeneralRunSync:
    def test_successful_run_sets_complete(
        self, store: RunStore, fake_general_runner: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_general_runner, tmp_path)
        event = _general_event()
        store.add(RunRecord(change_id="WI-GEN-ABCD1234", status="running", source="http"))

        handler.run_sync(event)

        record = store.get("WI-GEN-ABCD1234")
        assert record is not None
        assert record.status == "complete"
        assert record.exit_code == 0

    def test_failing_run_sets_failed(
        self, store: RunStore, failing_general_runner: Path, tmp_path: Path
    ):
        handler = _make_handler(store, failing_general_runner, tmp_path)
        event = _general_event()
        store.add(RunRecord(change_id="WI-GEN-ABCD1234", status="running", source="http"))

        handler.run_sync(event)

        record = store.get("WI-GEN-ABCD1234")
        assert record.status == "failed"
        assert record.exit_code == 1

    def test_output_callback_called(
        self, store: RunStore, fake_general_runner: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_general_runner, tmp_path)
        event = _general_event()
        store.add(RunRecord(change_id="WI-GEN-ABCD1234", status="running", source="http"))

        lines: list[str] = []
        handler.run_sync(event, output_callback=lines.append)

        assert any("running general" in ln for ln in lines)

    def test_no_backend_fails_immediately(
        self, store: RunStore, fake_general_runner: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_general_runner, tmp_path)
        event = TriggerEvent(
            source="http",
            action="general_run",
            change_id="WI-GEN-NO-BE",
            prompt="hello",
        )
        store.add(RunRecord(change_id="WI-GEN-NO-BE", status="running", source="http"))

        handler.run_sync(event)

        assert store.get("WI-GEN-NO-BE").status == "failed"

    def test_no_prompt_fails_immediately(
        self, store: RunStore, fake_general_runner: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_general_runner, tmp_path)
        event = TriggerEvent(
            source="http",
            action="general_run",
            change_id="WI-GEN-NO-PROMPT",
            backend="claude-code",
        )
        store.add(RunRecord(change_id="WI-GEN-NO-PROMPT", status="running", source="http"))

        handler.run_sync(event)

        assert store.get("WI-GEN-NO-PROMPT").status == "failed"

    def test_model_forwarded(
        self, store: RunStore, tmp_path: Path
    ):
        script = tmp_path / "check_model.py"
        script.write_text(
            "import sys, argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--backend', required=True)\n"
            "p.add_argument('--model', default=None)\n"
            "p.add_argument('--prompt', required=True)\n"
            "p.add_argument('--repo', required=True)\n"
            "p.add_argument('--agent', default=None)\n"
            "p.add_argument('--output-json', default=None)\n"
            "args = p.parse_args()\n"
            "assert args.model == 'sonnet', f'Expected sonnet, got {args.model}'\n"
            "if args.output_json:\n"
            "    open(args.output_json, 'w').write('{}')\n"
            "sys.exit(0)\n"
        )
        handler = _make_handler(store, script, tmp_path)
        event = _general_event(model="sonnet")
        store.add(RunRecord(change_id="WI-GEN-ABCD1234", status="running", source="http"))

        handler.run_sync(event)

        assert store.get("WI-GEN-ABCD1234").status == "complete"

    def test_agent_forwarded(
        self, store: RunStore, tmp_path: Path
    ):
        script = tmp_path / "check_agent.py"
        script.write_text(
            "import sys, argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--backend', required=True)\n"
            "p.add_argument('--model', default=None)\n"
            "p.add_argument('--prompt', required=True)\n"
            "p.add_argument('--repo', required=True)\n"
            "p.add_argument('--agent', default=None)\n"
            "p.add_argument('--output-json', default=None)\n"
            "args = p.parse_args()\n"
            "assert args.agent == 'spike.agent.md', f'Expected spike.agent.md, got {args.agent}'\n"
            "if args.output_json:\n"
            "    open(args.output_json, 'w').write('{}')\n"
            "sys.exit(0)\n"
        )
        handler = _make_handler(store, script, tmp_path)
        event = _general_event(agent_file="spike.agent.md")
        store.add(RunRecord(change_id="WI-GEN-ABCD1234", status="running", source="http"))

        handler.run_sync(event)

        assert store.get("WI-GEN-ABCD1234").status == "complete"

    def test_result_json_parsed(
        self, store: RunStore, fake_general_runner: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_general_runner, tmp_path)
        event = _general_event()
        store.add(RunRecord(change_id="WI-GEN-ABCD1234", status="running", source="http"))

        handler.run_sync(event)

        record = store.get("WI-GEN-ABCD1234")
        assert record.result is not None
        assert record.result["status"] == "pass"

    def test_cancelled_run_not_overwritten(
        self, store: RunStore, fake_general_runner: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_general_runner, tmp_path)
        event = _general_event()
        store.add(RunRecord(change_id="WI-GEN-ABCD1234", status="cancelled", source="http"))

        handler.run_sync(event)

        assert store.get("WI-GEN-ABCD1234").status == "cancelled"


# ---------------------------------------------------------------------------
# execute() — async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGeneralRunExecute:
    async def test_returns_running_record(
        self, store: RunStore, fake_general_runner: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_general_runner, tmp_path)
        event = _general_event()
        record = await handler.execute(event)
        assert record.status == "running"
        assert store.get("WI-GEN-ABCD1234") is not None

    async def test_background_task_completes(
        self, store: RunStore, fake_general_runner: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_general_runner, tmp_path)
        await handler.execute(_general_event())
        for _ in range(50):
            await asyncio.sleep(0.1)
            rec = store.get("WI-GEN-ABCD1234")
            if rec and rec.status == "complete":
                break
        assert store.get("WI-GEN-ABCD1234").status == "complete"


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGeneralRunCancel:
    async def test_cancel_unknown_returns_false(
        self, store: RunStore, fake_general_runner: Path, tmp_path: Path
    ):
        handler = _make_handler(store, fake_general_runner, tmp_path)
        result = await handler.cancel("WI-GEN-NOPE")
        assert result is False
