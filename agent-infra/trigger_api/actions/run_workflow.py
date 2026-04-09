"""RunWorkflowHandler — launches run_headless.py as a subprocess.

Extracted from the original monolithic discord_trigger_listener.py so the
subprocess lifecycle is owned here regardless of which adapter triggered the
run (Discord, HTTP, Azure DevOps, etc.).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..models import RunRecord, TriggerEvent
from ..run_store import RunStore

_OutputCallback = Callable[[str], None]


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[run_workflow] {ts} {msg}", flush=True)


class RunWorkflowHandler:
    """Dispatches ``action="run"`` events by invoking run_headless.py.

    Two call paths exist:

    * ``execute()``     — used by HTTP / Azure DevOps triggers.  Registers the
                          RunRecord, then runs the subprocess in an asyncio
                          background task via ``asyncio.to_thread``.

    * ``run_sync()``    — called directly by the Discord adapter from its own
                          background thread so that it can manage the line-queue
                          and streaming lifecycle itself.  The record must
                          already be in *run_store* before calling this.
    """

    action_name = "run"

    def __init__(
        self,
        run_store: RunStore,
        default_repo: str,
        runner_script: Path,
        backend: str | None = None,
    ) -> None:
        self._store = run_store
        self._default_repo = default_repo
        self._runner_script = runner_script
        self._default_backend = backend
        # change_id → Popen — lets cancel() terminate a live subprocess
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._procs_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        event: TriggerEvent,
        output_callback: _OutputCallback | None = None,
    ) -> RunRecord:
        """For HTTP / ADO triggers: registers record and fires a background task."""
        change_id = event.change_id

        record = RunRecord(
            change_id=change_id,
            status="running",
            source=event.source,
            requester=event.requester,
            started_at=datetime.now(timezone.utc),
            discord_thread_id=event.metadata.get("discord_thread_id"),
        )
        self._store.add(record)

        asyncio.create_task(
            asyncio.to_thread(self.run_sync, event, output_callback)
        )
        return record

    async def cancel(self, change_id: str) -> bool:
        with self._procs_lock:
            proc = self._procs.get(change_id)
        if proc is None:
            return False
        proc.terminate()
        self._store.update(
            change_id,
            status="cancelled",
            finished_at=datetime.now(timezone.utc),
        )
        return True

    # ------------------------------------------------------------------
    # Sync core — called by execute() via to_thread OR by Discord adapter
    # directly from its own thread.
    # ------------------------------------------------------------------

    def run_sync(
        self,
        event: TriggerEvent,
        output_callback: _OutputCallback | None = None,
    ) -> None:
        """Blocking execution of run_headless.py.

        Reads ``event.change_id`` from *run_store* to get ``started_at``
        (so elapsed time is accurate regardless of who added the record).
        Updates the record with final status when done.

        *output_callback* is called once per stdout line.  When None, lines
        are written to stdout via ``_log``.
        """
        change_id = event.change_id
        repo = event.repo_path or self._default_repo
        backend = event.backend or self._default_backend

        with tempfile.NamedTemporaryFile(
            suffix=".json", prefix=f"run-{change_id}-", delete=False
        ) as tmp:
            output_json = Path(tmp.name)

        cmd = [sys.executable, str(self._runner_script), "--change-id", change_id]
        cmd += ["--repo", repo]
        if backend:
            cmd += ["--backend", backend]
        cmd += ["--output-json", str(output_json)]

        _log(f"Spawning: {' '.join(cmd)}")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        with self._procs_lock:
            self._procs[change_id] = proc

        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if output_callback is not None:
                output_callback(line)
            else:
                _log(f"[{change_id}] {line}")

        proc.wait()
        exit_code = proc.returncode

        with self._procs_lock:
            self._procs.pop(change_id, None)

        # Read structured result produced by run_headless.py
        result: dict = {}
        if output_json.exists():
            try:
                result = json.loads(output_json.read_text(encoding="utf-8"))
            except Exception:
                pass
            try:
                output_json.unlink(missing_ok=True)
            except OSError:
                pass

        # Respect an external cancel() call
        current = self._store.get(change_id)
        if current is not None and current.status == "cancelled":
            return

        started_at = current.started_at if current else datetime.now(timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()

        self._store.update(
            change_id,
            status="complete" if exit_code == 0 else "failed",
            finished_at=datetime.now(timezone.utc),
            elapsed_seconds=elapsed,
            exit_code=exit_code,
            result=result or None,
        )
        _log(
            f"Run {change_id} finished: exit_code={exit_code}  "
            f"elapsed={elapsed:.1f}s"
        )
