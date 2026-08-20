"""Car B2 — ledger task 195: the ADR read ops must attach the supersede edges.

``adr`` has no ``supersedes`` / ``superseded_by`` COLUMN (migration 002); the
relation lives only in the ``adr_supersedes`` join table, which
``add_adr_supersedes`` has written since Car F and which nothing has ever read.
So every consumer of the 7-key ADR shape — ``adr_list``, ``adr_get``, and the
(dormant) ``adr_render._assemble_index_rows``, all three of which reach the
ledger through THESE ops — renders ``supersedes: "none"`` and
``superseded_by: "-"`` unconditionally.

THE ATTACH LIVES HERE, NOT IN CORE.  All three consumers forward
``list_adr_rows`` / ``get_adr_row``; attaching in core's ``adr_list`` would fix
two of them and leave the third silently broken.  This is the same placement
``list_task_rows`` already uses for ``task_blocked_by`` (``_attach_edges``).

ALWAYS-ON, unlike ``list_task_rows``' ``with_edges`` flag.  The task op's flag
exists because its lean projection deliberately omits the columns; the ADR
7-key shape ALWAYS emits ``supersedes`` / ``superseded_by``, so they are always
wrong today and an opt-in would leave every existing caller wrong.  It costs one
extra query per read against a ~237-row corpus.

BEST-EFFORT, deliberately.  A failure in the newly-added edge read degrades to
the empty lists — i.e. to exactly today's behaviour — rather than failing the
whole ADR read.  A regression in an additive enrichment must not be able to
take out ``adr_list``.

The SQL half (does the join select the right edges?) is pinned in
``yadgar/tests/_shared/test_adr_car_b2_ledger_sql.py``, which executes the real
statement against a two-project fixture.  This file measures PLUMBING: that the
op reaches the reader with the project scope it was handed and folds the result
onto the right rows.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from yadgar.backend import admin_exec

_YADGAR = "m-agahi/yadgar"
_FLUX = "quinyx/flux"

_ROWS: tuple[dict[str, Any], ...] = (
    {"id": 16, "project_id": _FLUX, "title": "flux 16", "status": "superseded"},
    {"id": 23, "project_id": _YADGAR, "title": "yadgar 23", "status": "superseded"},
    {"id": 24, "project_id": _YADGAR, "title": "yadgar 24", "status": "superseded"},
    {"id": 25, "project_id": _YADGAR, "title": "yadgar 25", "status": "accepted"},
    {"id": 26, "project_id": _YADGAR, "title": "yadgar 26", "status": "accepted"},
)

#: What ``list_adr_supersedes(project_id="m-agahi/yadgar")`` returns for the
#: above: 25 supersedes 23 and 24.  26 has no edge and is therefore ABSENT from
#: the map — the op, not the reader, is what turns that into empty lists.
_EDGES: dict[int, dict[str, list[int]]] = {
    25: {"supersedes": [23, 24], "superseded_by": []},
    23: {"supersedes": [], "superseded_by": [25]},
    24: {"supersedes": [], "superseded_by": [25]},
}


class _EdgeStorage:
    """Stands in for ``MariaStorageEngine``; records the scope it RECEIVES."""

    def __init__(self, *, edges_raise: bool = False) -> None:
        self.edge_scopes: list[str] = []
        self._edges_raise = edges_raise

    async def list_adr_rows(self, *, project_id: str, **_: Any) -> list[dict]:
        return [dict(r) for r in _ROWS if r["project_id"] == project_id]

    async def get_adr_row(self, adr_id: int, *, project_id: str | None = None) -> dict | None:
        for row in _ROWS:
            if row["id"] == adr_id and (project_id is None or row["project_id"] == project_id):
                return dict(row)
        return None

    async def list_adr_supersedes(self, *, project_id: str) -> dict[int, dict[str, list[int]]]:
        self.edge_scopes.append(project_id)
        if self._edges_raise:
            raise RuntimeError("adr_supersedes read blew up")
        if project_id != _YADGAR:
            return {}
        return {k: {kk: list(vv) for kk, vv in v.items()} for k, v in _EDGES.items()}


def _install(monkeypatch: pytest.MonkeyPatch, storage: _EdgeStorage) -> _EdgeStorage:
    holder = MagicMock()
    holder.list_adr_rows = storage.list_adr_rows
    holder.get_adr_row = storage.get_adr_row
    holder.list_adr_supersedes = storage.list_adr_supersedes
    monkeypatch.setattr(admin_exec.ledger, "_get_sql_storage", lambda: holder)
    return storage


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> _EdgeStorage:
    return _install(monkeypatch, _EdgeStorage())


def _by_id(rows: list[dict]) -> dict[int, dict]:
    return {int(r["id"]): r for r in rows}


class TestListAdrRowsAttachesSupersedes:
    async def test_superseder_row_carries_its_targets(self, storage: _EdgeStorage) -> None:
        result = await admin_exec.run_admin_op_async("list_adr_rows", {"project_id": _YADGAR})
        assert _by_id(result["rows"])[25]["supersedes"] == [23, 24]

    async def test_target_row_carries_the_reverse_pointer(self, storage: _EdgeStorage) -> None:
        result = await admin_exec.run_admin_op_async("list_adr_rows", {"project_id": _YADGAR})
        assert _by_id(result["rows"])[23]["superseded_by"] == [25]

    async def test_edgeless_row_gets_empty_lists_not_missing_keys(
        self, storage: _EdgeStorage
    ) -> None:
        """ "Has none" and "was never looked up" must not be the same value —
        the reader's map is sparse, so the op is what fills the gap."""
        row = _by_id(
            (await admin_exec.run_admin_op_async("list_adr_rows", {"project_id": _YADGAR}))["rows"]
        )[26]
        assert row["supersedes"] == []
        assert row["superseded_by"] == []

    async def test_the_edge_read_is_scoped_to_the_same_project(self, storage: _EdgeStorage) -> None:
        await admin_exec.run_admin_op_async("list_adr_rows", {"project_id": _FLUX})
        assert storage.edge_scopes == [_FLUX]

    async def test_a_failing_edge_read_degrades_instead_of_failing_the_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _EdgeStorage(edges_raise=True))
        result = await admin_exec.run_admin_op_async("list_adr_rows", {"project_id": _YADGAR})
        assert result.get("ok") is not False, "an additive enrichment took out adr_list"
        assert _by_id(result["rows"])[25]["supersedes"] == []


class TestGetAdrRowAttachesSupersedes:
    async def test_single_row_carries_its_targets(self, storage: _EdgeStorage) -> None:
        result = await admin_exec.run_admin_op_async(
            "get_adr_row", {"id": 25, "project_id": _YADGAR}
        )
        assert result["row"]["supersedes"] == [23, 24]

    async def test_single_row_carries_the_reverse_pointer(self, storage: _EdgeStorage) -> None:
        result = await admin_exec.run_admin_op_async(
            "get_adr_row", {"id": 23, "project_id": _YADGAR}
        )
        assert result["row"]["superseded_by"] == [25]

    async def test_edgeless_single_row_gets_empty_lists(self, storage: _EdgeStorage) -> None:
        result = await admin_exec.run_admin_op_async(
            "get_adr_row", {"id": 26, "project_id": _YADGAR}
        )
        assert result["row"]["supersedes"] == []
        assert result["row"]["superseded_by"] == []

    async def test_a_missing_row_stays_none(self, storage: _EdgeStorage) -> None:
        """The attach must not resurrect a row the scoped lookup refused."""
        result = await admin_exec.run_admin_op_async(
            "get_adr_row", {"id": 16, "project_id": _YADGAR}
        )
        assert result["row"] is None

    async def test_a_failing_edge_read_degrades_instead_of_failing_the_get(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _EdgeStorage(edges_raise=True))
        result = await admin_exec.run_admin_op_async(
            "get_adr_row", {"id": 25, "project_id": _YADGAR}
        )
        assert result.get("ok") is not False
        assert result["row"]["supersedes"] == []
