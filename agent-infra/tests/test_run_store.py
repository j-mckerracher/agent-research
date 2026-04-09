"""Tests for trigger_api/run_store.py."""

from __future__ import annotations

import threading
import time

import pytest

from trigger_api.models import RunRecord
from trigger_api.run_store import RunStore


def _record(change_id: str = "WI-1", status: str = "running") -> RunRecord:
    return RunRecord(change_id=change_id, source="http", status=status)  # type: ignore[arg-type]


class TestRunStoreAdd:
    def test_add_and_get(self):
        store = RunStore()
        r = _record("WI-100")
        store.add(r)
        assert store.get("WI-100") == r

    def test_get_unknown_returns_none(self):
        store = RunStore()
        assert store.get("WI-999") is None

    def test_add_duplicate_raises(self):
        store = RunStore()
        store.add(_record("WI-1"))
        with pytest.raises(ValueError, match="WI-1"):
            store.add(_record("WI-1"))


class TestRunStoreUpdate:
    def test_update_status(self):
        store = RunStore()
        store.add(_record("WI-1", "running"))
        updated = store.update("WI-1", status="complete")
        assert updated is not None
        assert updated.status == "complete"
        assert store.get("WI-1").status == "complete"

    def test_update_multiple_fields(self):
        store = RunStore()
        store.add(_record("WI-1", "running"))
        store.update("WI-1", status="complete", exit_code=0, elapsed_seconds=42.0)
        r = store.get("WI-1")
        assert r.status == "complete"
        assert r.exit_code == 0
        assert r.elapsed_seconds == 42.0

    def test_update_unknown_returns_none(self):
        store = RunStore()
        result = store.update("WI-999", status="complete")
        assert result is None

    def test_update_does_not_mutate_original(self):
        store = RunStore()
        original = _record("WI-1", "running")
        store.add(original)
        store.update("WI-1", status="complete")
        # Original Python object is unchanged (model_copy returns new instance)
        assert original.status == "running"


class TestRunStorePop:
    def test_pop_existing(self):
        store = RunStore()
        store.add(_record("WI-1"))
        popped = store.pop("WI-1")
        assert popped is not None
        assert store.get("WI-1") is None

    def test_pop_unknown_returns_none(self):
        store = RunStore()
        assert store.pop("WI-999") is None


class TestRunStoreList:
    def test_empty(self):
        store = RunStore()
        assert store.list() == []

    def test_returns_all(self):
        store = RunStore()
        store.add(_record("WI-1", "running"))
        store.add(_record("WI-2", "complete"))
        results = store.list()
        assert len(results) == 2

    def test_newest_first_ordering(self):
        store = RunStore()
        # Add records; started_at defaults to now so insertion order ≈ time order
        store.add(_record("WI-1", "running"))
        time.sleep(0.01)
        store.add(_record("WI-2", "complete"))
        results = store.list()
        # WI-2 is newer, should come first
        assert results[0].change_id == "WI-2"
        assert results[1].change_id == "WI-1"

    def test_status_filter_running(self):
        store = RunStore()
        store.add(_record("WI-1", "running"))
        store.add(_record("WI-2", "complete"))
        store.add(_record("WI-3", "failed"))
        results = store.list(status_filter="running")
        assert len(results) == 1
        assert results[0].change_id == "WI-1"

    def test_status_filter_no_match(self):
        store = RunStore()
        store.add(_record("WI-1", "running"))
        assert store.list(status_filter="cancelled") == []

    def test_status_filter_none_returns_all(self):
        store = RunStore()
        store.add(_record("WI-1", "running"))
        store.add(_record("WI-2", "complete"))
        assert len(store.list(status_filter=None)) == 2


class TestRunStoreCountActive:
    def test_zero_when_empty(self):
        assert RunStore().count_active() == 0

    def test_counts_pending_and_running(self):
        store = RunStore()
        store.add(_record("WI-1", "pending"))
        store.add(_record("WI-2", "running"))
        store.add(_record("WI-3", "complete"))
        store.add(_record("WI-4", "failed"))
        store.add(_record("WI-5", "cancelled"))
        assert store.count_active() == 2

    def test_decrements_on_update(self):
        store = RunStore()
        store.add(_record("WI-1", "running"))
        assert store.count_active() == 1
        store.update("WI-1", status="complete")
        assert store.count_active() == 0


class TestRunStoreHasActive:
    def test_running_is_active(self):
        store = RunStore()
        store.add(_record("WI-1", "running"))
        assert store.has_active("WI-1") is True

    def test_pending_is_active(self):
        store = RunStore()
        store.add(_record("WI-1", "pending"))
        assert store.has_active("WI-1") is True

    def test_complete_is_not_active(self):
        store = RunStore()
        store.add(_record("WI-1", "complete"))
        assert store.has_active("WI-1") is False

    def test_unknown_is_not_active(self):
        store = RunStore()
        assert store.has_active("WI-999") is False


class TestRunStoreThreadSafety:
    """Concurrent reads and writes must not corrupt state."""

    def test_concurrent_adds_all_succeed(self):
        store = RunStore()
        errors: list[Exception] = []

        def _add(i: int) -> None:
            try:
                store.add(_record(f"WI-{i}"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_add, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(store.list()) == 50

    def test_concurrent_updates_are_safe(self):
        store = RunStore()
        store.add(_record("WI-1", "running"))
        errors: list[Exception] = []

        def _update(status: str) -> None:
            try:
                store.update("WI-1", status=status)  # type: ignore[arg-type]
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_update, args=(s,))
            for s in ["running", "complete", "failed"] * 10
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # Final status is one of the valid values
        assert store.get("WI-1").status in ("running", "complete", "failed")

    def test_count_active_under_concurrent_updates(self):
        store = RunStore()
        for i in range(20):
            store.add(_record(f"WI-{i}", "running"))

        def _complete(i: int) -> None:
            store.update(f"WI-{i}", status="complete")

        threads = [threading.Thread(target=_complete, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert store.count_active() == 0
