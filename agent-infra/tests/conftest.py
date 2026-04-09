"""Shared pytest fixtures for the trigger_api test suite."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from trigger_api.actions.run_workflow import RunWorkflowHandler
from trigger_api.app import create_app
from trigger_api.models import RunRecord, TriggerEvent
from trigger_api.run_store import RunStore


# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> RunStore:
    """Fresh RunStore for each test."""
    return RunStore()


@pytest.fixture
def fake_runner_script(tmp_path: Path) -> Path:
    """A minimal run_headless.py that exits 0 and writes a pass result."""
    script = tmp_path / "run_headless.py"
    script.write_text(
        "import sys, json, argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--change-id')\n"
        "p.add_argument('--repo')\n"
        "p.add_argument('--backend', default=None)\n"
        "p.add_argument('--output-json')\n"
        "args = p.parse_args()\n"
        "print(f'running {args.change_id}')\n"
        "result = {'status': 'pass', 'stages': [{'stage_name': 'intake', 'passed': True, 'attempts': 1}]}\n"
        "open(args.output_json, 'w').write(__import__('json').dumps(result))\n"
        "sys.exit(0)\n"
    )
    return script


@pytest.fixture
def failing_runner_script(tmp_path: Path) -> Path:
    """A run_headless.py that exits 1."""
    script = tmp_path / "run_headless_fail.py"
    script.write_text(
        "import sys, argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--change-id')\n"
        "p.add_argument('--repo')\n"
        "p.add_argument('--backend', default=None)\n"
        "p.add_argument('--output-json')\n"
        "args = p.parse_args()\n"
        "print('something went wrong')\n"
        "sys.exit(1)\n"
    )
    return script


@pytest.fixture
def run_handler(store: RunStore, fake_runner_script: Path, tmp_path: Path) -> RunWorkflowHandler:
    return RunWorkflowHandler(
        run_store=store,
        default_repo=str(tmp_path),
        runner_script=fake_runner_script,
        backend=None,
    )


@pytest.fixture
def mock_action_handler(store: RunStore):
    """A mock ActionHandler that immediately marks runs as 'complete'."""
    handler = MagicMock()
    handler.action_name = "run"

    async def _execute(event: TriggerEvent, output_callback=None) -> RunRecord:
        record = RunRecord(
            change_id=event.change_id,
            status="running",
            source=event.source,
            requester=event.requester,
        )
        store.add(record)
        return record

    async def _cancel(change_id: str) -> bool:
        record = store.get(change_id)
        if record and record.status in ("pending", "running"):
            store.update(change_id, status="cancelled")
            return True
        return False

    handler.execute = AsyncMock(side_effect=_execute)
    handler.cancel = AsyncMock(side_effect=_cancel)
    return handler


@pytest.fixture
def app_with_mock_handler(store: RunStore, mock_action_handler):
    """FastAPI app wired to mock handler — no real subprocess, no Discord."""
    return create_app(
        run_store=store,
        action_registry={"run": mock_action_handler},
        discord_token="",  # disable Discord polling
        ado_webhook_secret="test-secret",
    )


@pytest_asyncio.fixture
async def client(app_with_mock_handler) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_mock_handler),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Convenience factories
# ---------------------------------------------------------------------------


def make_trigger_event(
    source: str = "http",
    action: str = "run",
    change_id: str = "WI-1234",
    requester: str | None = "test-user",
    **kwargs,
) -> TriggerEvent:
    return TriggerEvent(
        source=source,  # type: ignore[arg-type]
        action=action,
        change_id=change_id,
        requester=requester,
        **kwargs,
    )


def make_run_record(
    change_id: str = "WI-1234",
    status: str = "running",
    source: str = "http",
) -> RunRecord:
    return RunRecord(
        change_id=change_id,
        status=status,  # type: ignore[arg-type]
        source=source,
    )


def make_ado_comment_payload(
    comment: str = "RUN: WI-4461550",
    work_item_id: int = 4461550,
    requester_name: str = "Jane Developer",
    project_id: str = "proj-abc-123",
) -> dict:
    return {
        "subscriptionId": "sub-001",
        "notificationId": 1,
        "id": "evt-001",
        "eventType": "ms.vss-work.work-item-commented-on",
        "publisherId": "tfs",
        "message": {
            "text": f"Jane Developer commented on work item {work_item_id}",
            "html": "",
            "markdown": "",
        },
        "detailedMessage": {"text": comment, "html": "", "markdown": ""},
        "resource": {
            "comment": comment,
            "workItemId": work_item_id,
            "links": {},
        },
        "resourceVersion": "1.0",
        "resourceContainers": {
            "collection": {"id": "col-001"},
            "project": {"id": project_id},
        },
        "createdBy": {
            "displayName": requester_name,
            "uniqueName": "jane@example.com",
        },
        "createdDate": "2026-04-08T12:00:00Z",
    }
