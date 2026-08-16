"""Car D — ``limit`` / ``offset`` are forwarded instead of being decorative.

They were accepted by ``task_list``, forwarded only when non-default, read by
no admin op, and never reached a ``LIMIT`` clause: ``limit=5`` returned all 77
rows on the live corpus (confirmed 2026-08-16). This file pins the two seams
above the SQL — the tool putting them on the wire and the admin op handing them
to storage. That the clause actually caps the row count is settled against a
real server by ``yadgar/tests/integration/test_task_edges_and_paging.py``,
because only a server can count rows.

The DEFAULT is the part worth guarding hardest. ``limit`` defaults to ``None``
(no cap), not to the ``100`` the docstring used to claim: implementing the
parameter while keeping ``100`` would have converted a decorative argument into
a silent truncation at row 101 for the session-restore read and the harness
seeder, both of which need the complete open set.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture
def _forward_capture():
    from yadgar.core.server.tools import task as task_mod

    captured: dict = {}

    def fake_forward(op: str, payload: dict, timeout_s: float = 30.0) -> dict:  # noqa: ARG001
        captured["op"] = op
        captured["payload"] = payload
        return {"rows": []}

    with patch.object(task_mod, "_forward_admin", side_effect=fake_forward):
        yield captured


class TestTaskListForwardsPaging:
    def test_the_default_is_no_cap_on_the_wire(self, _forward_capture: dict) -> None:
        """``None``, not 100 — an unpaged caller must not be silently truncated."""
        from yadgar.core.server.tools.task import task_list

        task_list(project_id="m-agahi/yadgar")

        assert _forward_capture["payload"]["limit"] is None

    def test_a_stated_limit_is_forwarded(self, _forward_capture: dict) -> None:
        from yadgar.core.server.tools.task import task_list

        task_list(project_id="m-agahi/yadgar", limit=5)

        assert _forward_capture["payload"]["limit"] == 5

    def test_limit_100_is_forwarded_too(self, _forward_capture: dict) -> None:
        """The old code dropped exactly this value — it was the default sentinel."""
        from yadgar.core.server.tools.task import task_list

        task_list(project_id="m-agahi/yadgar", limit=100)

        assert _forward_capture["payload"]["limit"] == 100

    def test_offset_is_always_present(self, _forward_capture: dict) -> None:
        from yadgar.core.server.tools.task import task_list

        task_list(project_id="m-agahi/yadgar")

        assert _forward_capture["payload"]["offset"] == 0

    def test_a_stated_offset_is_forwarded(self, _forward_capture: dict) -> None:
        from yadgar.core.server.tools.task import task_list

        task_list(project_id="m-agahi/yadgar", limit=2, offset=10)

        assert _forward_capture["payload"]["offset"] == 10

    def test_the_signature_default_is_none(self) -> None:
        """A schema-level guard: the MCP surface must not advertise a cap."""
        import inspect

        from yadgar.core.server.tools.task import task_list

        assert inspect.signature(task_list).parameters["limit"].default is None


class TestAdminOpForwardsPaging:
    async def test_list_task_rows_hands_paging_to_storage(self) -> None:
        from yadgar.backend.admin_exec import ledger as ledger_ops

        seen: dict[str, Any] = {}

        class _Store:
            async def list_task_rows(self, **kwargs: Any) -> list[dict]:
                seen.update(kwargs)
                return []

        with patch.object(ledger_ops, "_get_sql_storage", return_value=_Store()):
            await ledger_ops.list_task_rows({"project_id": "p", "limit": 5, "offset": 2})

        assert seen["limit"] == 5
        assert seen["offset"] == 2

    async def test_absent_paging_reaches_storage_as_none(self) -> None:
        """``None`` is the storage layer's "emit no clause" — not a default number."""
        from yadgar.backend.admin_exec import ledger as ledger_ops

        seen: dict[str, Any] = {}

        class _Store:
            async def list_task_rows(self, **kwargs: Any) -> list[dict]:
                seen.update(kwargs)
                return []

        with patch.object(ledger_ops, "_get_sql_storage", return_value=_Store()):
            await ledger_ops.list_task_rows({"project_id": "p"})

        assert seen["limit"] is None
        assert seen["offset"] is None

    async def test_the_cross_project_op_forwards_paging_too(self) -> None:
        from yadgar.backend.admin_exec import ledger as ledger_ops

        seen: dict[str, Any] = {}

        class _Store:
            async def list_task_rows_all_projects(self, **kwargs: Any) -> list[dict]:
                seen.update(kwargs)
                return []

        with patch.object(ledger_ops, "_get_sql_storage", return_value=_Store()):
            await ledger_ops.list_task_rows_all_projects({"limit": 3})

        assert seen["limit"] == 3


class TestPagingTailShape:
    """The clause builder, without a server. Row counts are the live file's job."""

    def test_neither_stated_emits_nothing(self) -> None:
        pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")
        from yadgar._shared.storage.sql.mariadb import _paging_tail

        params: dict = {}

        assert _paging_tail(params, None, None) == ""
        assert params == {}

    def test_a_limit_binds_rather_than_interpolates(self) -> None:
        pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")
        from yadgar._shared.storage.sql.mariadb import _paging_tail

        params: dict = {}

        assert _paging_tail(params, 5, None) == " LIMIT :limit"
        assert params == {"limit": 5}

    def test_offset_without_limit_gets_the_maximal_row_count(self) -> None:
        """MariaDB rejects a bare ``OFFSET``; the idiom is a maximal ``LIMIT``."""
        pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")
        from yadgar._shared.storage.sql.mariadb import _MAX_ROWS, _paging_tail

        params: dict = {}

        assert _paging_tail(params, None, 7) == f" LIMIT {_MAX_ROWS} OFFSET :offset"
        assert params == {"offset": 7}

    def test_a_zero_offset_adds_no_clause(self) -> None:
        pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")
        from yadgar._shared.storage.sql.mariadb import _paging_tail

        params: dict = {}

        assert _paging_tail(params, None, 0) == ""

    def test_negatives_are_rejected_not_sent(self) -> None:
        pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")
        from yadgar._shared.storage.sql.mariadb import _paging_tail

        with pytest.raises(ValueError, match="limit"):
            _paging_tail({}, -1, None)
        with pytest.raises(ValueError, match="offset"):
            _paging_tail({}, None, -1)
