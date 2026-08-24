"""C10 — task #319: one class of admin_exec failure must reach the 409 seam.

Pre-C10: ``create_task_row`` and ``update_task_row`` in
``backend/admin_exec/ledger.py`` swallowed every storage exception in
``except Exception`` and returned ``{"ok": False, "error": "..."}`` at
HTTP 200. The /admin route catches ``AdminRefusal`` → 409, so an
``AdminRefusal`` raised inside an op was the ONLY way a deliberate refusal
ever reached the 409 seam. Partial-state edge sync — a row IS created but
its ``blocked_by`` / ``blocks`` join edges did not write — was the most
common: the 2026-08-15 backfill silently lost six edges this way.

C10 wires ONE class through to 409: ``TaskEdgePartialStateError`` (a new
``AdminRefusal`` subclass). The partial state envelope keeps its existing
``ok: False`` + ``id`` payload so callers can still see what was written;
the route now ALSO returns 409 + the refusal envelope so a downstream
forwarder can discriminate "row created, edges failed" from "row created,
all edges written".

Scope discipline (per C10 plan): other silent-swallow classes stay as
findings for a follow-up car. The chosen class is the one with the
highest call-site frequency and the strongest precedent for a deliberate
refusal (D39 partial state, the comment already names it as such).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from yadgar._shared.refusal import REFUSAL_STATUS, AdminRefusal

# ── Type-level: the new exception must be an AdminRefusal, period ──────────────


class TestTaskEdgePartialStateRefusal:
    """The refusal type must be wired so the /admin route maps it to 409."""

    def test_partial_state_refusal_is_admin_refusal(self):
        """The new exception class must subclass ``AdminRefusal`` — the only
        way the /admin route can map it to 409 without a blanket except
        (which would re-file genuine faults as deliberate rejections).
        """
        from yadgar.backend.admin_exec.ledger import TaskEdgePartialStateError

        exc = TaskEdgePartialStateError(task_id=42, kind="blocked_by", reason="boom")
        assert isinstance(exc, AdminRefusal), (
            "TaskEdgePartialStateError must subclass AdminRefusal — the /admin "
            "route catches AdminRefusal to map to 409, and anything else reads "
            "as a server fault"
        )
        # Reason is machine-readable so callers can discriminate.
        assert exc.reason == "task_edge_partial_state", (
            f"expected reason 'task_edge_partial_state', got {exc.reason!r}"
        )

    def test_partial_state_refusal_carries_id_and_kind(self):
        """The envelope must surface the partial-state payload so a caller
        can decide whether to roll back the row, retry the edge sync, or
        accept the partial state.
        """
        from yadgar.backend.admin_exec.ledger import TaskEdgePartialStateError

        exc = TaskEdgePartialStateError(
            task_id=42,
            kind="blocks",
            reason="its blocks edges were not written: constraint violation",
        )
        report = exc.refusal_report()
        assert report["task_id"] == 42
        assert report["edge_kind"] == "blocks"
        assert "constraint violation" in report["edge_error"]


# ── Wire-level: the /admin route must render it as 409, not 200 or 500 ─────────


@pytest.fixture
def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from yadgar.backend.embed_service.embed_service import app

    return TestClient(app, raise_server_exceptions=False)


def _raise_partial_state(op: str, payload: dict):
    """Mock dispatcher that mimics ``create_task_row`` raising partial state."""

    async def _impl(op: str, payload: dict) -> dict:
        from yadgar.backend.admin_exec.ledger import TaskEdgePartialStateError

        raise TaskEdgePartialStateError(
            task_id=int(payload.get("id", 0)),
            kind="blocked_by",
            reason="its blocked_by edges were not written: simulated FK violation",
        )

    return _impl


def test_partial_state_renders_as_409_not_200(
    _client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /admin route must return 409 (REFUSAL_STATUS), not 200 + {ok: False}.

    Pre-C10: this case returned 200 + ``{"ok": False, ...}`` — operationally
    identical to a row-with-no-edges (which is the same envelope as the
    ok:True path minus the ok flag). C10 surfaces the refusal type to 409.
    """
    monkeypatch.setattr(
        "yadgar.backend.admin_exec.run_admin_op_async",
        _raise_partial_state("create_task_row", {"id": 99}),
    )

    resp = _client.post("/admin", json={"op": "create_task_row", "payload": {"id": 99}})

    assert resp.status_code == REFUSAL_STATUS, (
        f"partial-state edge failure must reach the 409 seam — got "
        f"{resp.status_code}. Pre-C10 this returned 200 + ok:False, which is "
        f"the exact defect the route's AdminRefusal catch was added to fix."
    )
    body = resp.json()["detail"]
    assert body["refused"] is True
    assert body["ok"] is False
    assert body["reason"] == "task_edge_partial_state"
    assert body["op"] == "create_task_row"
    # The partial-state payload survives into the wire envelope.
    assert body["task_id"] == 99
    assert body["edge_kind"] == "blocked_by"


def test_unexpected_exception_still_500_not_409(
    _client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control arm: a genuine fault (NOT an AdminRefusal) must stay a 500.

    Without this arm the 409 test proves only "returns 4xx", not
    "distinguishable from a crash". Blanket except clauses would silently
    re-file real faults as deliberate refusals.
    """

    async def _impl(op: str, payload: dict) -> dict:
        raise RuntimeError("something actually broke in the SQL bridge")

    monkeypatch.setattr("yadgar.backend.admin_exec.run_admin_op_async", _impl)

    resp = _client.post("/admin", json={"op": "create_task_row", "payload": {}})

    assert resp.status_code == 500, (
        "a genuine fault must remain a 500 — only TaskEdgePartialStateError "
        "(and its sister AdminRefusal subclasses) is re-filed as a 409"
    )


# ── Behavioral: create_task_row MUST raise on partial state, not swallow ───────


def test_create_task_row_raises_partial_state_when_edges_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ledger op must re-raise ``TaskEdgePartialStateError`` instead of
    swallowing it into a 200 + ok:False envelope.

    Pre-C10: ``create_task_row`` caught ``Exception`` around the entire body
    and returned ``{"ok": False, "error": "..."}``. C10 narrows the catch to
    ONLY suppress storage-level faults for the row-create itself; the edge
    sync path raises a typed refusal that propagates to the /admin route.
    """
    from yadgar.backend.admin_exec import ledger
    from yadgar.backend.admin_exec.ledger import (
        TaskEdgePartialStateError,
        create_task_row,
    )

    fake_storage = MagicMock()
    fake_storage.create_task_row = AsyncMock(return_value={"id": 7})
    fake_storage.list_task_blocks = AsyncMock(return_value=[])
    fake_storage.list_task_blocked_by = AsyncMock(return_value=[])
    fake_storage.add_task_blocked_by = AsyncMock(side_effect=RuntimeError("simulated FK violation"))

    monkeypatch.setattr(ledger, "_get_sql_storage", lambda: fake_storage)

    with pytest.raises(TaskEdgePartialStateError) as excinfo:
        import asyncio

        asyncio.run(
            create_task_row(
                {
                    "project_id": "m-agahi/yadgar",
                    "title": "t",
                    "blocked_by": [1, 2],
                }
            )
        )

    exc = excinfo.value
    assert exc.reason == "task_edge_partial_state"
    # task_id MUST be the row that WAS created (caller decides whether to roll back).
    assert exc.task_id == 7
    assert exc.edge_kind == "blocked_by"


def test_update_task_row_raises_partial_state_when_edges_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same wiring on the UPDATE path."""
    from yadgar.backend.admin_exec import ledger
    from yadgar.backend.admin_exec.ledger import (
        TaskEdgePartialStateError,
        update_task_row,
    )

    fake_storage = MagicMock()
    fake_storage.update_task_row = AsyncMock()
    fake_storage.list_task_blocks = AsyncMock(return_value=[])
    fake_storage.list_task_blocked_by = AsyncMock(return_value=[])
    fake_storage.add_task_blocked_by = AsyncMock(side_effect=RuntimeError("simulated FK violation"))

    monkeypatch.setattr(ledger, "_get_sql_storage", lambda: fake_storage)

    with pytest.raises(TaskEdgePartialStateError) as excinfo:
        import asyncio

        asyncio.run(
            update_task_row(
                {
                    "id": 11,
                    "blocked_by": [3, 4],
                }
            )
        )

    exc = excinfo.value
    assert exc.reason == "task_edge_partial_state"
    assert exc.task_id == 11
    assert exc.edge_kind == "blocked_by"
