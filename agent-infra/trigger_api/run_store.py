"""Thread-safe in-memory store for RunRecords."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from .models import RunRecord, RunStatusValue

_ACTIVE_STATUSES: frozenset[RunStatusValue] = frozenset({"pending", "running"})


class RunStore:
    """Simple thread-safe dict-backed store.

    All mutating operations acquire ``_lock`` so the store is safe to use from
    both asyncio tasks and background threads simultaneously.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, RunRecord] = {}

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add(self, record: RunRecord) -> None:
        """Insert a new record.  Raises ValueError if change_id already exists."""
        with self._lock:
            if record.change_id in self._runs:
                raise ValueError(
                    f"RunRecord for {record.change_id!r} already exists; "
                    "use update() to mutate it"
                )
            self._runs[record.change_id] = record

    def update(self, change_id: str, **kwargs: object) -> Optional[RunRecord]:
        """Apply keyword-arg patches to an existing record.  Returns updated record
        or None if the change_id is unknown."""
        with self._lock:
            record = self._runs.get(change_id)
            if record is None:
                return None
            updated = record.model_copy(update=kwargs)
            self._runs[change_id] = updated
            return updated

    def pop(self, change_id: str) -> Optional[RunRecord]:
        """Remove and return the record, or None."""
        with self._lock:
            return self._runs.pop(change_id, None)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, change_id: str) -> Optional[RunRecord]:
        with self._lock:
            return self._runs.get(change_id)

    def list(self, status_filter: Optional[str] = None) -> list[RunRecord]:
        """Return all records, newest first. Optionally filter by status."""
        with self._lock:
            runs = list(self._runs.values())
        if status_filter:
            runs = [r for r in runs if r.status == status_filter]
        return sorted(runs, key=lambda r: r.started_at, reverse=True)

    def count_active(self) -> int:
        with self._lock:
            return sum(
                1 for r in self._runs.values() if r.status in _ACTIVE_STATUSES
            )

    def has_active(self, change_id: str) -> bool:
        with self._lock:
            r = self._runs.get(change_id)
            return r is not None and r.status in _ACTIVE_STATUSES
