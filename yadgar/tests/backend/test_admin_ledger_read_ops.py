"""Car B — backend ledger read ops registered in ``_ADMIN_OPS``.

Adds six READ op bodies to the backend admin dispatch:

  * list_task_rows, get_task_row, list_task_rows_all_projects
  * list_adr_rows, get_adr_row
  * list_agent_prompt_rows

Each one is an ``async`` body that calls a corresponding ``MariaStorageEngine``
method (the chokepoint, D20) and returns ``{"rows": [...]}`` / ``{"row": ...}``.

Plus two SYNC config read ops (get_config_row, list_config_rows) that hit the
SurrealDB StorageEngine via ``_RuntimeConfigMixin``.

These tests pin four properties the dispatch must hold:
  1. unknown op → KeyError (existing dispatch behaviour, unchanged);
  2. each op dispatches to its registered body;
  3. async ledger ops are awaited (not run on the event loop blocking style);
  4. the bodies reach the chokepoint (storage methods called with right args).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from yadgar.backend import admin_exec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_sql_storage(
    *,
    task_rows: list[dict] | None = None,
    task_rows_all: list[dict] | None = None,
    task_row: dict | None = None,
    adr_rows: list[dict] | None = None,
    adr_row: dict | None = None,
    agent_prompt_rows: list[dict] | None = None,
) -> MagicMock:
    """Return a MagicMock standing in for ``MariaStorageEngine``.

    All read methods are ``AsyncMock`` so awaiting them in the admin op bodies
    works without a live DB.
    """
    storage = MagicMock()
    storage.list_task_rows = AsyncMock(return_value=task_rows or [])
    storage.list_task_rows_all_projects = AsyncMock(return_value=task_rows_all or [])
    storage.get_task_row = AsyncMock(return_value=task_row)
    storage.list_adr_rows = AsyncMock(return_value=adr_rows or [])
    storage.get_adr_row = AsyncMock(return_value=adr_row)
    storage.list_agent_prompt_rows = AsyncMock(return_value=agent_prompt_rows or [])
    return storage


def _make_fake_surreal_storage(
    *,
    config_row: dict | None = None,
    config_rows: list[dict] | None = None,
) -> MagicMock:
    """Return a MagicMock standing in for SurrealDB ``StorageEngine``.

    ``get_config_row`` / ``list_config_rows`` are SYNC (SurrealDB sync), so the
    mock methods are plain MagicMock returning canned values.
    """
    storage = MagicMock()
    storage.get_config_row = MagicMock(return_value=config_row)
    storage.list_config_rows = MagicMock(return_value=config_rows or [])
    return storage


# ---------------------------------------------------------------------------
# 1. Unknown op → KeyError (existing behaviour, unchanged)
# ---------------------------------------------------------------------------


class TestUnknownOpRaisesKeyError:
    def test_unknown_op_raises_keyerror_sync(self) -> None:
        with pytest.raises(KeyError):
            admin_exec.run_admin_op("definitely_not_a_registered_op", {})

    async def test_unknown_op_raises_keyerror_async(self) -> None:
        with pytest.raises(KeyError):
            await admin_exec.run_admin_op_async("definitely_not_a_registered_op", {})


# ---------------------------------------------------------------------------
# 2. Each ledger op dispatches to its registered body
# ---------------------------------------------------------------------------


class TestLedgerOpsRegistered:
    """Verify the seven new ledger/config op names are in ``_ADMIN_OPS``."""

    @pytest.mark.parametrize(
        "op_name",
        [
            "list_task_rows",
            "get_task_row",
            "list_task_rows_all_projects",
            "list_adr_rows",
            "get_adr_row",
            "list_agent_prompt_rows",
            "get_config_row",
            "list_config_rows",
        ],
    )
    def test_op_registered(self, op_name: str) -> None:
        assert op_name in admin_exec._ADMIN_OPS


# ---------------------------------------------------------------------------
# 3. Async ledger ops are awaited, return rows from MariaStorageEngine
# ---------------------------------------------------------------------------


class TestLedgerAsyncOps:
    async def test_list_task_rows_returns_rows_from_chokepoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [{"id": 1, "title": "t1"}]
        sql_storage = _make_fake_sql_storage(task_rows=rows)
        monkeypatch.setattr(
            "yadgar.backend.admin_exec.ledger._get_sql_storage",
            lambda: sql_storage,
        )
        result = await admin_exec.run_admin_op_async(
            "list_task_rows", {"project_id": "m-agahi/yadgar"}
        )
        assert result == {"rows": rows}
        # ``summary`` defaults to False — a payload from an older core image
        # carries no such key and must keep getting every column.
        sql_storage.list_task_rows.assert_awaited_once_with(
            project_id="m-agahi/yadgar", status=None, summary=False
        )

    async def test_list_task_rows_forwards_the_summary_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lean projection is opt-in, and the op is what carries the opt-in."""
        sql_storage = _make_fake_sql_storage(task_rows=[])
        monkeypatch.setattr(
            "yadgar.backend.admin_exec.ledger._get_sql_storage",
            lambda: sql_storage,
        )
        await admin_exec.run_admin_op_async(
            "list_task_rows", {"project_id": "m-agahi/yadgar", "summary": True}
        )
        sql_storage.list_task_rows.assert_awaited_once_with(
            project_id="m-agahi/yadgar", status=None, summary=True
        )

    async def test_get_task_row_returns_row_or_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sql_storage = _make_fake_sql_storage(task_row={"id": 7, "title": "t7"})
        monkeypatch.setattr(
            "yadgar.backend.admin_exec.ledger._get_sql_storage",
            lambda: sql_storage,
        )
        result = await admin_exec.run_admin_op_async("get_task_row", {"id": 7})
        assert result == {"row": {"id": 7, "title": "t7"}}
        sql_storage.get_task_row.assert_awaited_once_with(7)

    async def test_get_task_row_missing_returns_none_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sql_storage = _make_fake_sql_storage(task_row=None)
        monkeypatch.setattr(
            "yadgar.backend.admin_exec.ledger._get_sql_storage",
            lambda: sql_storage,
        )
        result = await admin_exec.run_admin_op_async("get_task_row", {"id": 999})
        assert result == {"row": None}

    async def test_list_task_rows_all_projects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [{"id": 1}, {"id": 2}]
        sql_storage = _make_fake_sql_storage(task_rows_all=rows)
        monkeypatch.setattr(
            "yadgar.backend.admin_exec.ledger._get_sql_storage",
            lambda: sql_storage,
        )
        result = await admin_exec.run_admin_op_async("list_task_rows_all_projects", {})
        assert result == {"rows": rows}
        sql_storage.list_task_rows_all_projects.assert_awaited_once_with(status=None, summary=False)

    async def test_list_adr_rows_returns_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [{"id": 11, "title": "a1"}]
        sql_storage = _make_fake_sql_storage(adr_rows=rows)
        monkeypatch.setattr(
            "yadgar.backend.admin_exec.ledger._get_sql_storage",
            lambda: sql_storage,
        )
        result = await admin_exec.run_admin_op_async(
            "list_adr_rows", {"project_id": "m-agahi/yadgar"}
        )
        assert result == {"rows": rows}
        # Car H (0047 §7 D27/D28): ``list_adr_rows`` forwards the optional
        # ``tier`` and ``subsystem`` filters; both default to ``None`` when
        # absent from the payload (no WHERE-clause narrowing).
        sql_storage.list_adr_rows.assert_awaited_once_with(
            project_id="m-agahi/yadgar", status=None, tier=None, subsystem=None
        )

    async def test_get_adr_row_returns_row_or_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sql_storage = _make_fake_sql_storage(adr_row={"id": 22, "title": "a22"})
        monkeypatch.setattr(
            "yadgar.backend.admin_exec.ledger._get_sql_storage",
            lambda: sql_storage,
        )
        result = await admin_exec.run_admin_op_async("get_adr_row", {"id": 22})
        assert result == {"row": {"id": 22, "title": "a22"}}
        sql_storage.get_adr_row.assert_awaited_once_with(22)

    async def test_list_agent_prompt_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [{"name": "dispatch-fix-bug"}]
        sql_storage = _make_fake_sql_storage(agent_prompt_rows=rows)
        monkeypatch.setattr(
            "yadgar.backend.admin_exec.ledger._get_sql_storage",
            lambda: sql_storage,
        )
        result = await admin_exec.run_admin_op_async("list_agent_prompt_rows", {})
        assert result == {"rows": rows}
        sql_storage.list_agent_prompt_rows.assert_awaited_once_with()


# ---------------------------------------------------------------------------
# 4. Config read ops are SYNC (SurrealDB), dispatched via _get_storage
# ---------------------------------------------------------------------------


class TestConfigOpsSync:
    def test_get_config_row_dispatches_sync(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = {"key": "k", "value": 42, "directory": None}
        surreal = _make_fake_surreal_storage(config_row=row)
        monkeypatch.setattr(
            "yadgar.backend.admin_exec.runtime_config._get_storage",
            lambda: surreal,
        )
        result = admin_exec.run_admin_op("get_config_row", {"key": "k", "directory": None})
        assert result == {"row": row}
        surreal.get_config_row.assert_called_once_with("k", directory=None)

    def test_list_config_rows_dispatches_sync(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [{"key": "a"}, {"key": "b"}]
        surreal = _make_fake_surreal_storage(config_rows=rows)
        monkeypatch.setattr(
            "yadgar.backend.admin_exec.runtime_config._get_storage",
            lambda: surreal,
        )
        # Pass a sentinel-ish value: list_config_rows treats no directory as
        # the "ALL rows" branch (uses an object sentinel internally).
        result = admin_exec.run_admin_op("list_config_rows", {})
        assert result == {"rows": rows}
        surreal.list_config_rows.assert_called_once()
