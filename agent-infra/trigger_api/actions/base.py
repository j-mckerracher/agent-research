"""ActionHandler protocol — every action type implements this interface."""

from __future__ import annotations

from typing import Callable, Protocol

from ..models import RunRecord, TriggerEvent

OutputCallback = Callable[[str], None]


class ActionHandler(Protocol):
    """Protocol that each action type must satisfy.

    ``action_name`` is matched against ``TriggerEvent.action`` to dispatch
    incoming events.  Add new action types by implementing this protocol and
    passing an instance to ``register_handler()``.
    """

    action_name: str

    async def execute(
        self,
        event: TriggerEvent,
        output_callback: OutputCallback | None = None,
    ) -> RunRecord:
        """Start handling the event.

        Returns the initial RunRecord immediately; actual work runs in the
        background.  If *output_callback* is provided, it is called with each
        line of subprocess output (used by the Discord adapter to stream to a
        thread).
        """
        ...

    async def cancel(self, change_id: str) -> bool:
        """Attempt to cancel a running job.  Returns True if a job was found
        and signalled, False if no active job exists for *change_id*."""
        ...
