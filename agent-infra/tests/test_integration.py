"""Integration tests — full trigger-to-completion flow with real subprocess."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from trigger_api.app import create_app
from trigger_api.run_store import RunStore
from tests.conftest import make_ado_comment_payload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def int_store() -> RunStore:
    return RunStore()


@pytest_asyncio.fixture
async def int_client(
    int_store: RunStore,
    fake_runner_script: Path,
    tmp_path: Path,
):
    """Test client wired to a *real* RunWorkflowHandler (real subprocess)."""
    app = create_app(
        run_store=int_store,
        discord_token="",          # no Discord
        ado_webhook_secret="secret",
        default_repo=str(tmp_path),
        runner_script=fake_runner_script,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def int_fail_client(
    int_store: RunStore,
    failing_runner_script: Path,
    tmp_path: Path,
):
    """Test client wired to a failing runner."""
    app = create_app(
        run_store=int_store,
        discord_token="",
        ado_webhook_secret="",
        default_repo=str(tmp_path),
        runner_script=failing_runner_script,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for_status(store: RunStore, change_id: str, target: str, timeout: float = 10.0):
    """Poll store until record reaches *target* status or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        record = store.get(change_id)
        if record and record.status == target:
            return record
        await asyncio.sleep(0.1)
    record = store.get(change_id)
    actual = record.status if record else "missing"
    raise TimeoutError(
        f"Timed out waiting for {change_id} to reach {target!r}; current={actual!r}"
    )


import base64


def _auth(secret: str = "secret") -> dict:
    token = base64.b64encode(f":{secret}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# ---------------------------------------------------------------------------
# HTTP trigger → real subprocess → store update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHttpTriggerFlow:
    async def test_trigger_runs_and_completes(
        self, int_client: AsyncClient, int_store: RunStore
    ):
        resp = await int_client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-1001"},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "running"

        record = await _wait_for_status(int_store, "WI-1001", "complete")
        assert record.exit_code == 0
        assert record.result is not None
        assert record.result["status"] == "pass"
        assert record.elapsed_seconds is not None

    async def test_trigger_failing_runner_sets_failed(
        self, int_fail_client: AsyncClient, int_store: RunStore
    ):
        resp = await int_fail_client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-1002"},
        )
        assert resp.status_code == 202

        record = await _wait_for_status(int_store, "WI-1002", "failed")
        assert record.exit_code == 1
        assert record.status == "failed"

    async def test_get_run_reflects_completion(
        self, int_client: AsyncClient, int_store: RunStore
    ):
        await int_client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-1003"},
        )
        await _wait_for_status(int_store, "WI-1003", "complete")

        resp = await int_client.get("/api/v1/runs/WI-1003")
        assert resp.status_code == 200
        assert resp.json()["status"] == "complete"

    async def test_duplicate_trigger_rejected(
        self, int_client: AsyncClient, int_store: RunStore
    ):
        resp1 = await int_client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-1004"},
        )
        assert resp1.status_code == 202

        # Second trigger while first is still running
        resp2 = await int_client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-1004"},
        )
        assert resp2.status_code == 409

    async def test_health_shows_active_run_then_zero(
        self, int_client: AsyncClient, int_store: RunStore
    ):
        await int_client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-1005"},
        )
        # May be 1 active briefly
        health = await int_client.get("/api/v1/health")
        assert health.status_code == 200

        await _wait_for_status(int_store, "WI-1005", "complete")

        health = await int_client.get("/api/v1/health")
        assert health.json()["active_runs"] == 0

    async def test_list_runs_shows_completed_run(
        self, int_client: AsyncClient, int_store: RunStore
    ):
        await int_client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-1006"},
        )
        await _wait_for_status(int_store, "WI-1006", "complete")

        resp = await int_client.get("/api/v1/runs")
        assert any(r["change_id"] == "WI-1006" for r in resp.json())

    async def test_list_runs_status_filter(
        self, int_client: AsyncClient, int_store: RunStore
    ):
        await int_client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-1007"},
        )
        await _wait_for_status(int_store, "WI-1007", "complete")

        completed = await int_client.get("/api/v1/runs?status=complete")
        assert any(r["change_id"] == "WI-1007" for r in completed.json())

        running = await int_client.get("/api/v1/runs?status=running")
        assert not any(r["change_id"] == "WI-1007" for r in running.json())


# ---------------------------------------------------------------------------
# Azure DevOps webhook → real subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAdoWebhookFlow:
    async def test_ado_comment_triggers_run(
        self, int_client: AsyncClient, int_store: RunStore
    ):
        payload = make_ado_comment_payload(
            comment="RUN: WI-2001",
            work_item_id=2001,
            requester_name="Bob Engineer",
        )
        resp = await int_client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
            headers=_auth(),
        )
        assert resp.status_code == 202
        assert resp.json()["accepted"] is True

        record = await _wait_for_status(int_store, "WI-2001", "complete")
        assert record.source == "azure_devops"
        assert record.requester == "Bob Engineer"

    async def test_ado_run_result_populated(
        self, int_client: AsyncClient, int_store: RunStore
    ):
        payload = make_ado_comment_payload(comment="RUN: WI-2002", work_item_id=2002)
        await int_client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
            headers=_auth(),
        )
        record = await _wait_for_status(int_store, "WI-2002", "complete")
        # fake_runner_script writes a structured result
        assert record.result is not None
        assert "stages" in record.result

    async def test_ado_comment_no_prefix_ignored(
        self, int_client: AsyncClient, int_store: RunStore
    ):
        payload = make_ado_comment_payload(comment="Looks good!")
        resp = await int_client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert resp.json()["accepted"] is False
        # Nothing added to store
        assert int_store.list() == []

    async def test_ado_and_http_runs_coexist(
        self, int_client: AsyncClient, int_store: RunStore
    ):
        """Two different change IDs from different sources both complete."""
        ado_payload = make_ado_comment_payload(comment="RUN: WI-3001", work_item_id=3001)
        await int_client.post(
            "/api/v1/webhooks/azure-devops",
            json=ado_payload,
            headers=_auth(),
        )
        await int_client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-3002"},
        )

        ado_record = await _wait_for_status(int_store, "WI-3001", "complete")
        http_record = await _wait_for_status(int_store, "WI-3002", "complete")

        assert ado_record.source == "azure_devops"
        assert http_record.source == "http"
