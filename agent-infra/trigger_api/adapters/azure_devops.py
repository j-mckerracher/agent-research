"""Azure DevOps service-hook webhook adapter.

Configure an ADO service hook to POST to ``/api/v1/webhooks/azure-devops``
whenever a work item comment is added.  The adapter parses ``RUN: WI-XXXX``
commands from comment text and emits a ``TriggerEvent``.

Supported event types
---------------------
``ms.vss-work.work-item-commented-on``
    Triggered when a user adds a comment to a work item.  The comment text is
    parsed for ``RUN:`` commands.

Authentication
--------------
ADO service hooks support HTTP Basic auth on the target URL.  Set the
``ADO_WEBHOOK_SECRET`` environment variable to a shared password; ADO will be
configured with any username and that password.  If the env var is empty the
check is skipped (useful for local development).
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from ..models import TriggerEvent

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADO_WORK_ITEM_COMMENTED = "ms.vss-work.work-item-commented-on"

_SUPPORTED_EVENT_TYPES: frozenset[str] = frozenset(
    {ADO_WORK_ITEM_COMMENTED}
)

RUN_PREFIX = "RUN:"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def verify_basic_auth(authorization_header: str | None, expected_secret: str) -> bool:
    """Verify the HTTP Basic auth header sent by ADO service hooks.

    ADO sends ``Authorization: Basic base64(username:password)``.  We only
    check the *password* portion against *expected_secret*.
    """
    if not authorization_header:
        return False
    if not authorization_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization_header[6:]).decode("utf-8")
        _, password = decoded.split(":", 1)
        return password == expected_secret
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def parse_ado_webhook(payload: dict) -> TriggerEvent | None:
    """Parse an ADO service hook payload into a ``TriggerEvent``.

    Returns ``None`` if the payload is not actionable (unsupported event type,
    comment does not contain a ``RUN:`` command, etc.).
    """
    event_type = payload.get("eventType", "")

    if event_type == ADO_WORK_ITEM_COMMENTED:
        return _parse_work_item_comment(payload)

    return None


def _parse_work_item_comment(payload: dict) -> TriggerEvent | None:
    resource = payload.get("resource", {})

    # ADO puts the comment text in resource.comment (plain string)
    comment_text: str = resource.get("comment", "") or ""

    # Fall back to detailedMessage.text (sometimes richer)
    if not comment_text:
        detail = payload.get("detailedMessage", {})
        comment_text = detail.get("text", "") or ""

    trigger = _parse_run_command(comment_text)
    if trigger is None:
        return None

    change_id, repo_path = trigger

    # Requester: service hooks include a "createdBy" at the payload root
    sender = payload.get("createdBy", {})
    requester: str | None = (
        sender.get("displayName") or sender.get("uniqueName") or None
    )

    work_item_id = resource.get("workItemId")
    project_id: str | None = (
        payload.get("resourceContainers", {})
        .get("project", {})
        .get("id")
    )

    return TriggerEvent(
        source="azure_devops",
        action="run",
        change_id=change_id,
        repo_path=repo_path,
        requester=requester,
        metadata={
            "event_type": ADO_WORK_ITEM_COMMENTED,
            "work_item_id": work_item_id,
            "project_id": project_id,
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _parse_run_command(text: str) -> tuple[str, str | None] | None:
    """Extract ``(change_id, repo_path_or_None)`` from a ``RUN:`` line.

    Scans the first line of *text* that starts with ``RUN:`` (case-insensitive).
    Returns ``None`` if no such line is found.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith(RUN_PREFIX.upper()):
            body = stripped[len(RUN_PREFIX) :].strip()
            if not body:
                continue
            parts = body.split(None, 1)
            change_id_raw = parts[0].strip()
            repo_path = parts[1].strip() if len(parts) > 1 else None
            upper = change_id_raw.upper()
            change_id = upper if upper.startswith("WI-") else f"WI-{upper}"
            return change_id, repo_path
    return None
