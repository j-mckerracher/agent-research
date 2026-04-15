"""Tests for trigger_api/app.py — HTTP API routes."""

from __future__ import annotations

import base64

import pytest
import pytest_asyncio

from tests.conftest import make_ado_comment_payload, make_trigger_event
from trigger_api.models import RunRecord


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHealth:
    async def test_ok(self, client):
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "active_runs" in body
        assert "known_actions" in body

    async def test_known_actions_includes_run(self, client):
        resp = await client.get("/api/v1/health")
        assert "run" in resp.json()["known_actions"]

    async def test_active_runs_reflects_store(self, client, store):
        store.add(RunRecord(change_id="WI-1", status="running", source="http"))
        resp = await client.get("/api/v1/health")
        assert resp.json()["active_runs"] == 1


# ---------------------------------------------------------------------------
# POST /api/v1/trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTriggerEndpoint:
    async def test_valid_trigger_returns_202(self, client):
        resp = await client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-1234"},
        )
        assert resp.status_code == 202

    async def test_response_body_is_run_record(self, client):
        resp = await client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-5678"},
        )
        body = resp.json()
        assert body["change_id"] == "WI-5678"
        assert body["status"] == "running"
        assert body["source"] == "http"

    async def test_unknown_action_returns_404(self, client):
        resp = await client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "deploy", "change_id": "WI-1"},
        )
        assert resp.status_code == 404
        assert "deploy" in resp.json()["detail"]

    async def test_duplicate_active_run_returns_409(self, client, store):
        store.add(RunRecord(change_id="WI-9999", status="running", source="http"))
        resp = await client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "WI-9999"},
        )
        assert resp.status_code == 409

    async def test_change_id_normalised_no_prefix(self, client):
        resp = await client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run", "change_id": "4461550"},
        )
        assert resp.status_code == 202
        assert resp.json()["change_id"] == "WI-4461550"

    async def test_optional_fields_accepted(self, client):
        resp = await client.post(
            "/api/v1/trigger",
            json={
                "source": "azure_devops",
                "action": "run",
                "change_id": "WI-111",
                "repo_path": "/some/path",
                "backend": "claude-code",
                "requester": "alice",
                "metadata": {"project": "abc"},
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["source"] == "azure_devops"

    async def test_invalid_source_returns_422(self, client):
        resp = await client.post(
            "/api/v1/trigger",
            json={"source": "slack", "action": "run", "change_id": "WI-1"},
        )
        assert resp.status_code == 422

    async def test_missing_change_id_returns_422(self, client):
        resp = await client.post(
            "/api/v1/trigger",
            json={"source": "http", "action": "run"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestListRuns:
    async def test_empty_list(self, client):
        resp = await client.get("/api/v1/runs")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_all_runs(self, client, store):
        store.add(RunRecord(change_id="WI-1", status="running", source="http"))
        store.add(RunRecord(change_id="WI-2", status="complete", source="http"))
        resp = await client.get("/api/v1/runs")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_status_filter_running(self, client, store):
        store.add(RunRecord(change_id="WI-1", status="running", source="http"))
        store.add(RunRecord(change_id="WI-2", status="complete", source="http"))
        resp = await client.get("/api/v1/runs?status=running")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 1
        assert results[0]["change_id"] == "WI-1"

    async def test_status_filter_no_match(self, client, store):
        store.add(RunRecord(change_id="WI-1", status="running", source="http"))
        resp = await client.get("/api/v1/runs?status=cancelled")
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{change_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetRun:
    async def test_returns_record(self, client, store):
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))
        resp = await client.get("/api/v1/runs/WI-1234")
        assert resp.status_code == 200
        assert resp.json()["change_id"] == "WI-1234"

    async def test_unknown_returns_404(self, client):
        resp = await client.get("/api/v1/runs/WI-9999")
        assert resp.status_code == 404

    async def test_change_id_lookup_case_insensitive(self, client, store):
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))
        resp = await client.get("/api/v1/runs/wi-1234")
        assert resp.status_code == 200

    async def test_change_id_lookup_without_prefix(self, client, store):
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))
        resp = await client.get("/api/v1/runs/1234")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# DELETE /api/v1/runs/{change_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCancelRun:
    async def test_cancel_active_run_returns_true(self, client, store):
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))
        resp = await client.delete("/api/v1/runs/WI-1234")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cancelled"] is True
        assert body["change_id"] == "WI-1234"

    async def test_cancel_unknown_returns_false(self, client):
        resp = await client.delete("/api/v1/runs/WI-9999")
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is False

    async def test_cancel_sets_store_status(self, client, store):
        store.add(RunRecord(change_id="WI-1234", status="running", source="http"))
        await client.delete("/api/v1/runs/WI-1234")
        assert store.get("WI-1234").status == "cancelled"


# ---------------------------------------------------------------------------
# POST /api/v1/webhooks/azure-devops
# ---------------------------------------------------------------------------


def _basic_auth_header(username: str = "", password: str = "test-secret") -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


@pytest.mark.asyncio
class TestAzureDevOpsWebhook:
    async def test_valid_run_comment_returns_202(self, client):
        payload = make_ado_comment_payload("RUN: WI-4461550")
        resp = await client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
            headers={"Authorization": _basic_auth_header()},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["accepted"] is True
        assert body["change_id"] == "WI-4461550"

    async def test_invalid_auth_returns_401(self, client):
        payload = make_ado_comment_payload("RUN: WI-1")
        resp = await client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
            headers={"Authorization": _basic_auth_header(password="wrong")},
        )
        assert resp.status_code == 401

    async def test_missing_auth_returns_401(self, client):
        payload = make_ado_comment_payload("RUN: WI-1")
        resp = await client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
        )
        assert resp.status_code == 401

    async def test_non_run_comment_returns_200_not_accepted(self, client):
        payload = make_ado_comment_payload("LGTM!")
        resp = await client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
            headers={"Authorization": _basic_auth_header()},
        )
        assert resp.status_code == 200
        assert resp.json()["accepted"] is False

    async def test_unsupported_event_type_returns_200_not_accepted(self, client):
        payload = make_ado_comment_payload("RUN: WI-1")
        payload["eventType"] = "build.complete"
        resp = await client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
            headers={"Authorization": _basic_auth_header()},
        )
        assert resp.status_code == 200
        assert resp.json()["accepted"] is False

    async def test_duplicate_active_run_returns_409(self, client, store):
        store.add(RunRecord(change_id="WI-4461550", status="running", source="http"))
        payload = make_ado_comment_payload("RUN: WI-4461550")
        resp = await client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
            headers={"Authorization": _basic_auth_header()},
        )
        assert resp.status_code == 409

    async def test_run_recorded_in_store(self, client, store):
        payload = make_ado_comment_payload("RUN: WI-7777")
        await client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
            headers={"Authorization": _basic_auth_header()},
        )
        record = store.get("WI-7777")
        assert record is not None
        assert record.source == "azure_devops"

    async def test_change_id_auto_prefixed(self, client, store):
        payload = make_ado_comment_payload("RUN: 8888888")
        resp = await client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
            headers={"Authorization": _basic_auth_header()},
        )
        assert resp.status_code == 202
        assert resp.json()["change_id"] == "WI-8888888"


# ---------------------------------------------------------------------------
# create_app without auth secret — auth check bypassed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWebhookNoSecret:
    @pytest_asyncio.fixture
    async def no_secret_client(self, store, mock_action_handler):
        from httpx import ASGITransport, AsyncClient
        from trigger_api.app import create_app

        app = create_app(
            run_store=store,
            action_registry={"run": mock_action_handler},
            discord_token="",
            ado_webhook_secret="",  # no secret = skip auth
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac

    async def test_no_auth_header_accepted_when_no_secret(self, no_secret_client):
        payload = make_ado_comment_payload("RUN: WI-1")
        resp = await no_secret_client.post(
            "/api/v1/webhooks/azure-devops",
            json=payload,
        )
        assert resp.status_code == 202
