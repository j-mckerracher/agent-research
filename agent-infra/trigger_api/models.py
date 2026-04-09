"""Shared data models for the trigger API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

TriggerSource = Literal["discord", "http", "azure_devops"]
RunStatusValue = Literal["pending", "running", "complete", "failed", "cancelled"]
BackendValue = Literal["copilot", "claude"]


class TriggerEvent(BaseModel):
    """Normalised trigger event from any source."""

    source: TriggerSource
    action: str  # "run", "cancel" — extensible via ActionHandler registry
    change_id: str
    repo_path: str | None = None
    backend: BackendValue | None = None
    requester: str | None = None
    metadata: dict = Field(default_factory=dict)

    @field_validator("change_id")
    @classmethod
    def normalise_change_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("change_id must not be empty")
        upper = v.upper()
        return upper if upper.startswith("WI-") else f"WI-{upper}"

    @field_validator("action")
    @classmethod
    def action_not_empty(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("action must not be empty")
        return v


class RunRecord(BaseModel):
    """Live + historical state of one workflow run."""

    change_id: str
    status: RunStatusValue = "pending"
    source: str
    requester: str | None = None
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    finished_at: datetime | None = None
    elapsed_seconds: float | None = None
    discord_thread_id: str | None = None
    exit_code: int | None = None
    result: dict | None = None


class CancelResponse(BaseModel):
    change_id: str
    cancelled: bool


class HealthResponse(BaseModel):
    status: str
    active_runs: int
    known_actions: list[str]
