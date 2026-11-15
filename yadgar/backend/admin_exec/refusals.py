"""Refusal types raised by the admin ledger ops.

Split out of ``ledger.py`` when that file reached its I13 HARD file cap.
Same rationale as ``_shared/storage/sql/errors.py``: a refusal TYPE is
data, not an op body, and keeping it beside the ops buys nothing.
``ledger`` re-exports the name, so existing imports are unaffected.
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar._shared.refusal import AdminRefusal
from yadgar.backend.admin_exec.runtime_config import task_ledger_disabled

__all__ = [
    "TaskEdgePartialStateError",
    "TaskLedgerDisabledError",
    "refuse_if_task_ledger_disabled",
]


class TaskEdgePartialStateError(AdminRefusal, RuntimeError):
    """The row was created/updated, but one of its edge directions did not write.

    Car C10 (task #319): pre-C10 the ledger ops caught ``Exception`` around
    the entire body and returned ``{"ok": False, "error": "..."}`` at HTTP
    200 — operationally identical to a fully-failed create/update. The
    /admin route catches ``AdminRefusal`` → 409, so the row+missing-edge
    partial state had no way to reach that seam.

    Subclasses BOTH ``AdminRefusal`` and ``RuntimeError`` so:
      - the /admin route's ``except AdminRefusal`` arm renders it as 409 +
        a structured envelope (``refused``, ``reason``, ``task_id``,
        ``edge_kind``, ``edge_error``);
      - any pre-existing ``except RuntimeError`` catchers keep working
        (e.g. forwarder-side handlers that key off RuntimeError for retry).

    Carries ``task_id`` and ``edge_kind`` so the caller can decide whether
    to roll back the row, retry the edge sync, or accept the partial state
    — D39 partial state is a deliberate outcome, not a fault.
    """

    reason = "task_edge_partial_state"

    def __init__(self, *, task_id: int, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.task_id = int(task_id)
        self.edge_kind = str(kind)
        self.edge_error = str(reason)

    def refusal_report(self) -> dict:
        return {
            "task_id": self.task_id,
            "edge_kind": self.edge_kind,
            "edge_error": self.edge_error,
        }


class TaskLedgerDisabledError(AdminRefusal, PermissionError):
    """``tasks.ledger_disabled`` is true: the yadgar task ledger takes no writes.

    yaadgaar #699. The core ``task_write`` gate refuses first with the
    ``external_task_ledger`` envelope; this is the backend half, for callers
    that reach ``create_task_row`` / ``update_task_row`` over a raw
    ``POST /admin`` (the backfill CLI, scripts). Same ``reason`` as the core
    refusal, so a caller keys on one string.
    """

    reason = "external_task_ledger"

    def __init__(self, *, project_id: str | None = None, task_id: int | None = None) -> None:
        super().__init__(
            "the yadgar task ledger is disabled (tasks.ledger_disabled); tasks live in "
            "yaadgaar: use mcp__yaadgaar__create_task, edit_task or transition_task"
        )
        self.project_id = project_id
        self.task_id = task_id

    def refusal_report(self) -> dict:
        return {
            "ledger": "yaadgaar",
            "project_id": self.project_id,
            "task_id": self.task_id,
            "message": str(self),
        }


@observe(tier="stage", metric="backend.admin.refusals.refuse_if_task_ledger_disabled")
def refuse_if_task_ledger_disabled(
    *, project_id: str | None = None, task_id: int | None = None
) -> None:
    """Raise ``TaskLedgerDisabledError`` when ``tasks.ledger_disabled`` is true."""
    if task_ledger_disabled():
        raise TaskLedgerDisabledError(project_id=project_id, task_id=task_id)
