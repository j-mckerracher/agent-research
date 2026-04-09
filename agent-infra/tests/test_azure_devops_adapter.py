"""Tests for trigger_api/adapters/azure_devops.py."""

from __future__ import annotations

import base64

import pytest

from trigger_api.adapters.azure_devops import (
    ADO_WORK_ITEM_COMMENTED,
    _parse_run_command,
    parse_ado_webhook,
    verify_basic_auth,
)
from trigger_api.models import TriggerEvent


# ---------------------------------------------------------------------------
# verify_basic_auth
# ---------------------------------------------------------------------------


class TestVerifyBasicAuth:
    def _encode(self, username: str, password: str) -> str:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return f"Basic {token}"

    def test_valid_credentials(self):
        header = self._encode("ado", "my-secret")
        assert verify_basic_auth(header, "my-secret") is True

    def test_wrong_password(self):
        header = self._encode("ado", "wrong-secret")
        assert verify_basic_auth(header, "my-secret") is False

    def test_missing_header_returns_false(self):
        assert verify_basic_auth(None, "my-secret") is False

    def test_empty_header_returns_false(self):
        assert verify_basic_auth("", "my-secret") is False

    def test_non_basic_scheme_returns_false(self):
        assert verify_basic_auth("Bearer sometoken", "my-secret") is False

    def test_malformed_base64_returns_false(self):
        assert verify_basic_auth("Basic not-valid-base64!!!", "my-secret") is False

    def test_no_colon_in_decoded_returns_false(self):
        token = base64.b64encode(b"nocolon").decode()
        assert verify_basic_auth(f"Basic {token}", "nocolon") is False

    def test_empty_username_accepted(self):
        """ADO typically sends username='' when only a password is configured."""
        header = self._encode("", "supersecret")
        assert verify_basic_auth(header, "supersecret") is True

    def test_password_with_colon_parsed_correctly(self):
        """Only the first colon splits username:password."""
        header = self._encode("user", "pass:with:colons")
        assert verify_basic_auth(header, "pass:with:colons") is True


# ---------------------------------------------------------------------------
# _parse_run_command (internal helper)
# ---------------------------------------------------------------------------


class TestParseRunCommand:
    def test_basic(self):
        result = _parse_run_command("RUN: WI-1234")
        assert result == ("WI-1234", None)

    def test_with_repo(self):
        result = _parse_run_command("RUN: WI-100 /home/user/repo")
        assert result == ("WI-100", "/home/user/repo")

    def test_case_insensitive(self):
        assert _parse_run_command("run: WI-1") == ("WI-1", None)

    def test_adds_wi_prefix(self):
        assert _parse_run_command("RUN: 4461550") == ("WI-4461550", None)

    def test_uppercase_change_id(self):
        assert _parse_run_command("RUN: wi-100") == ("WI-100", None)

    def test_multiline_finds_first_run(self):
        text = "Some context.\nRUN: WI-9999\nMore text."
        assert _parse_run_command(text) == ("WI-9999", None)

    def test_no_run_prefix_returns_none(self):
        assert _parse_run_command("Please review this work item") is None

    def test_empty_body_after_prefix_skips(self):
        assert _parse_run_command("RUN:\n") is None

    def test_whitespace_only_body_skips(self):
        assert _parse_run_command("RUN:   \n") is None


# ---------------------------------------------------------------------------
# parse_ado_webhook
# ---------------------------------------------------------------------------


def _ado_payload(
    event_type: str = ADO_WORK_ITEM_COMMENTED,
    comment: str = "RUN: WI-4461550",
    work_item_id: int = 4461550,
    requester_display: str = "Jane Developer",
    requester_unique: str = "jane@example.com",
    project_id: str = "proj-abc-123",
) -> dict:
    return {
        "eventType": event_type,
        "resource": {
            "comment": comment,
            "workItemId": work_item_id,
        },
        "detailedMessage": {"text": comment},
        "resourceContainers": {
            "project": {"id": project_id},
        },
        "createdBy": {
            "displayName": requester_display,
            "uniqueName": requester_unique,
        },
    }


class TestParseAdoWebhook:
    # -- Happy paths --------------------------------------------------------

    def test_basic_run_trigger(self):
        payload = _ado_payload()
        event = parse_ado_webhook(payload)
        assert event is not None
        assert isinstance(event, TriggerEvent)
        assert event.source == "azure_devops"
        assert event.action == "run"
        assert event.change_id == "WI-4461550"
        assert event.repo_path is None

    def test_run_with_repo_path(self):
        payload = _ado_payload(comment="RUN: WI-1 /home/user/myrepo")
        event = parse_ado_webhook(payload)
        assert event is not None
        assert event.repo_path == "/home/user/myrepo"

    def test_requester_extracted(self):
        payload = _ado_payload(requester_display="Alice Smith")
        event = parse_ado_webhook(payload)
        assert event is not None
        assert event.requester == "Alice Smith"

    def test_requester_falls_back_to_unique_name(self):
        payload = _ado_payload()
        payload["createdBy"] = {"uniqueName": "bob@corp.com"}
        event = parse_ado_webhook(payload)
        assert event is not None
        assert event.requester == "bob@corp.com"

    def test_metadata_contains_work_item_id(self):
        payload = _ado_payload(work_item_id=9999)
        event = parse_ado_webhook(payload)
        assert event is not None
        assert event.metadata["work_item_id"] == 9999

    def test_metadata_contains_event_type(self):
        event = parse_ado_webhook(_ado_payload())
        assert event.metadata["event_type"] == ADO_WORK_ITEM_COMMENTED

    def test_metadata_contains_project_id(self):
        event = parse_ado_webhook(_ado_payload(project_id="proj-xyz"))
        assert event.metadata["project_id"] == "proj-xyz"

    def test_metadata_contains_received_at(self):
        event = parse_ado_webhook(_ado_payload())
        assert "received_at" in event.metadata

    def test_change_id_auto_prefixed(self):
        payload = _ado_payload(comment="RUN: 1234567")
        event = parse_ado_webhook(payload)
        assert event is not None
        assert event.change_id == "WI-1234567"

    def test_change_id_uppercased(self):
        payload = _ado_payload(comment="RUN: wi-100")
        event = parse_ado_webhook(payload)
        assert event.change_id == "WI-100"

    def test_comment_from_detailed_message_when_resource_empty(self):
        """Fall back to detailedMessage.text when resource.comment is blank."""
        payload = _ado_payload(comment="RUN: WI-5555")
        payload["resource"]["comment"] = ""
        payload["detailedMessage"]["text"] = "RUN: WI-5555"
        event = parse_ado_webhook(payload)
        assert event is not None
        assert event.change_id == "WI-5555"

    def test_multiline_comment_parsed(self):
        payload = _ado_payload(comment="Looks good.\nRUN: WI-7777\nThanks!")
        event = parse_ado_webhook(payload)
        assert event is not None
        assert event.change_id == "WI-7777"

    # -- Non-RUN comments return None --------------------------------------

    def test_non_run_comment_returns_none(self):
        payload = _ado_payload(comment="LGTM — no action needed")
        assert parse_ado_webhook(payload) is None

    def test_empty_comment_returns_none(self):
        payload = _ado_payload(comment="")
        payload["detailedMessage"]["text"] = ""
        assert parse_ado_webhook(payload) is None

    def test_run_prefix_no_body_returns_none(self):
        payload = _ado_payload(comment="RUN:")
        assert parse_ado_webhook(payload) is None

    # -- Unsupported event types -------------------------------------------

    def test_build_complete_event_returns_none(self):
        payload = _ado_payload(event_type="build.complete")
        assert parse_ado_webhook(payload) is None

    def test_work_item_updated_event_returns_none(self):
        payload = _ado_payload(event_type="ms.vss-work.work-item-updated")
        assert parse_ado_webhook(payload) is None

    def test_unknown_event_type_returns_none(self):
        payload = _ado_payload(event_type="some.random.event")
        assert parse_ado_webhook(payload) is None

    # -- Robustness ---------------------------------------------------------

    def test_missing_resource_returns_none(self):
        payload = {"eventType": ADO_WORK_ITEM_COMMENTED}
        assert parse_ado_webhook(payload) is None

    def test_missing_created_by_requester_is_none(self):
        payload = _ado_payload()
        del payload["createdBy"]
        event = parse_ado_webhook(payload)
        assert event is not None
        assert event.requester is None

    def test_missing_project_id_handled(self):
        payload = _ado_payload()
        del payload["resourceContainers"]
        event = parse_ado_webhook(payload)
        assert event is not None
        assert event.metadata.get("project_id") is None
