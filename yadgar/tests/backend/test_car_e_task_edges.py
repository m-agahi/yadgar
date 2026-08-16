"""Car E — ``task_blocked_by`` edges reach the READ path, and edge-write
failures reach the caller.

Three properties, none of which held before this car:

1. ``get_task_row`` returns ``blocked_by`` / ``blocks``; ``list_task_rows``
   returns them ONLY when the payload asks (``with_edges``). The join table
   was writable and unreadable — ``list_task_blocked_by`` had no admin op, no
   MCP tool and no inverse, so ``task_get`` answered with columns only.
2. A failed edge write is reported as ``{"ok": False, ...}``. It used to be a
   ``logger.warning`` followed by the success envelope, which made "six edges
   written" and "six edges silently dropped" the same answer (the 2026-08-15
   backfill hit exactly this).
3. The ``blocks`` direction is reconciled at all. It was accepted by
   ``task_write``, stripped out of the column payload by the admin op, and
   then dropped.

The storage double here is not a bare mock: it holds a real edge set and
answers reads from it, so a test can write edges and read back the ACTUAL ids
rather than a canned return value. The SQL those methods emit is settled
against a live server by
``yadgar/tests/integration/test_task_edges_and_paging.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from yadgar.backend.admin_exec import ledger as ledger_ops


class FakeEdgeStore:
    """A storage double that really stores ``task_blocked_by`` pairs.

    ``fail_on`` names a method that raises instead of running, which is how the
    failure-surfacing tests drive the error path without a broken server.
    """

    def __init__(self, rows: list[dict] | None = None, *, fail_on: str = "") -> None:
        self.rows = rows if rows is not None else []
        self.edges: set[tuple[int, int]] = set()
        self.fail_on = fail_on
        self.list_calls: list[dict] = []

    def _guard(self, name: str) -> None:
        if self.fail_on == name:
            raise RuntimeError(f"{name} exploded")

    async def list_task_rows(self, **kwargs: Any) -> list[dict]:
        self.list_calls.append(kwargs)
        return [dict(r) for r in self.rows]

    async def list_task_rows_all_projects(self, **kwargs: Any) -> list[dict]:
        self.list_calls.append(kwargs)
        return [dict(r) for r in self.rows]

    async def get_task_row(self, task_id: int) -> dict | None:
        for row in self.rows:
            if int(row["id"]) == int(task_id):
                return dict(row)
        return None

    async def create_task_row(self, **kwargs: Any) -> dict:
        self._guard("create_task_row")
        return {"id": 231, **kwargs}

    async def update_task_row(self, task_id: int, **fields: Any) -> None:
        self._guard("update_task_row")

    async def add_task_blocked_by(self, task_id: int, blocked_by_id: int) -> None:
        self._guard("add_task_blocked_by")
        self.edges.add((int(task_id), int(blocked_by_id)))

    async def remove_task_blocked_by(self, task_id: int, blocked_by_id: int) -> None:
        self._guard("remove_task_blocked_by")
        self.edges.discard((int(task_id), int(blocked_by_id)))

    async def list_task_blocked_by(self, task_id: int) -> list[int]:
        self._guard("list_task_blocked_by")
        return sorted(b for t, b in self.edges if t == int(task_id))

    async def list_task_blocks(self, task_id: int) -> list[int]:
        self._guard("list_task_blocks")
        return sorted(t for t, b in self.edges if b == int(task_id))

    async def list_task_edges(self, task_ids: list[int]) -> dict[int, dict[str, list[int]]]:
        self._guard("list_task_edges")
        out: dict[int, dict[str, list[int]]] = {
            int(i): {"blocked_by": [], "blocks": []} for i in task_ids
        }
        for blocked, blocker in sorted(self.edges):
            if blocked in out:
                out[blocked]["blocked_by"].append(blocker)
            if blocker in out:
                out[blocker]["blocks"].append(blocked)
        return out


@pytest.fixture
def store():
    """Patch the admin-op storage seam with a ``FakeEdgeStore``."""
    fake = FakeEdgeStore(rows=[{"id": 1, "title": "a"}, {"id": 2, "title": "b"}])
    with patch.object(ledger_ops, "_get_sql_storage", return_value=fake):
        yield fake


# ── 1. the read path ─────────────────────────────────────────────────────────


class TestGetTaskRowCarriesEdges:
    async def test_get_returns_the_actual_written_ids(self, store) -> None:
        """Write two edges, read the row: the ids come back, not ``[]``."""
        await store.add_task_blocked_by(1, 2)
        await store.add_task_blocked_by(1, 7)

        out = await ledger_ops.get_task_row({"id": 1})

        assert out["row"]["blocked_by"] == [2, 7]

    async def test_get_returns_the_inverse_direction_too(self, store) -> None:
        await store.add_task_blocked_by(2, 1)

        out = await ledger_ops.get_task_row({"id": 1})

        assert out["row"]["blocks"] == [2]
        assert out["row"]["blocked_by"] == []

    async def test_get_returns_empty_lists_when_there_are_no_edges(self, store) -> None:
        """Present-and-empty, never absent — the caller must be able to tell."""
        out = await ledger_ops.get_task_row({"id": 1})

        assert out["row"]["blocked_by"] == []
        assert out["row"]["blocks"] == []

    async def test_an_absent_row_is_still_none(self, store) -> None:
        assert (await ledger_ops.get_task_row({"id": 99}))["row"] is None

    async def test_an_edge_read_failure_fails_the_op(self, store) -> None:
        """Empty lists on error would read as "this task has no dependencies"."""
        store.fail_on = "list_task_blocked_by"

        out = await ledger_ops.get_task_row({"id": 1})

        assert out["ok"] is False
        assert "exploded" in out["error"]


class TestListTaskRowsEdgesAreOptIn:
    async def test_absent_with_edges_returns_no_edge_keys(self, store) -> None:
        """Car A's projection win must not be spent on a join nobody asked for."""
        await store.add_task_blocked_by(1, 2)

        rows = (await ledger_ops.list_task_rows({"project_id": "p"}))["rows"]

        assert all("blocked_by" not in r and "blocks" not in r for r in rows)

    async def test_with_edges_true_attaches_both_directions(self, store) -> None:
        await store.add_task_blocked_by(1, 2)

        rows = (await ledger_ops.list_task_rows({"project_id": "p", "with_edges": True}))["rows"]

        by_id = {r["id"]: r for r in rows}
        assert by_id[1]["blocked_by"] == [2]
        assert by_id[1]["blocks"] == []
        assert by_id[2]["blocks"] == [1]

    async def test_all_projects_reader_honours_with_edges_too(self, store) -> None:
        await store.add_task_blocked_by(2, 1)

        rows = (await ledger_ops.list_task_rows_all_projects({"with_edges": True}))["rows"]

        assert {r["id"]: r["blocked_by"] for r in rows} == {1: [], 2: [1]}


# ── 2. failed edge writes surface ────────────────────────────────────────────


class TestEdgeWriteFailuresAreNotSuccess:
    async def test_create_reports_ok_false_when_the_edge_write_fails(self, store) -> None:
        store.fail_on = "add_task_blocked_by"

        out = await ledger_ops.create_task_row({"project_id": "p", "title": "t", "blocked_by": [2]})

        assert out["ok"] is False
        assert "blocked_by" in out["error"]

    async def test_create_still_names_the_row_that_survived(self, store) -> None:
        """The row IS inserted; a caller that loses its id cannot clean up."""
        store.fail_on = "add_task_blocked_by"

        out = await ledger_ops.create_task_row({"project_id": "p", "title": "t", "blocked_by": [2]})

        assert out["id"] == 231
        assert "231" in out["error"]

    async def test_update_reports_ok_false_when_the_edge_write_fails(self, store) -> None:
        store.fail_on = "add_task_blocked_by"

        out = await ledger_ops.update_task_row({"id": 1, "blocked_by": [2]})

        assert out["ok"] is False

    async def test_a_clean_create_still_returns_the_row(self, store) -> None:
        out = await ledger_ops.create_task_row({"project_id": "p", "title": "t", "blocked_by": [2]})

        assert out.get("ok") is not False
        assert out["id"] == 231
        assert store.edges == {(231, 2)}

    async def test_a_clean_update_still_returns_the_patched_columns(self, store) -> None:
        out = await ledger_ops.update_task_row({"id": 1, "title": "t2", "blocked_by": [2]})

        assert out == {"id": 1, "title": "t2"}
        assert store.edges == {(1, 2)}


# ── 3. both directions reconcile ─────────────────────────────────────────────


class TestBothDirectionsReconcile:
    async def test_blocks_on_update_writes_the_inverse_edge(self, store) -> None:
        """``blocks=[2]`` was accepted, stripped and dropped before Car E."""
        await ledger_ops.update_task_row({"id": 1, "blocks": [2]})

        assert store.edges == {(2, 1)}

    async def test_blocks_on_create_writes_the_inverse_edge(self, store) -> None:
        await ledger_ops.create_task_row({"project_id": "p", "title": "t", "blocks": [5]})

        assert store.edges == {(5, 231)}

    async def test_update_removes_edges_no_longer_desired(self, store) -> None:
        await store.add_task_blocked_by(1, 2)
        await store.add_task_blocked_by(1, 3)

        await ledger_ops.update_task_row({"id": 1, "blocked_by": [3]})

        assert store.edges == {(1, 3)}

    async def test_update_removes_inverse_edges_no_longer_desired(self, store) -> None:
        await store.add_task_blocked_by(2, 1)
        await store.add_task_blocked_by(3, 1)

        await ledger_ops.update_task_row({"id": 1, "blocks": [2]})

        assert store.edges == {(2, 1)}

    async def test_an_absent_key_leaves_the_edges_alone(self, store) -> None:
        """Absent means "not mentioned", never "clear them"."""
        await store.add_task_blocked_by(1, 2)

        await ledger_ops.update_task_row({"id": 1, "title": "t2"})

        assert store.edges == {(1, 2)}

    async def test_an_empty_list_does_clear_them(self, store) -> None:
        """``[]`` is a stated set, unlike absence."""
        await store.add_task_blocked_by(1, 2)

        await ledger_ops.update_task_row({"id": 1, "blocked_by": []})

        assert store.edges == set()
