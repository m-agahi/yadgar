"""Bug-bag-2 train 2026-08-23, C5 — ``list_agent_discipline_rows`` admin op tests.

The admin op wrapper at ``yadgar/backend/admin_exec/ledger.py`` registered a
``list_agent_discipline_rows`` op name but the body function was MISSING. The
``_ADMIN_OPS`` dispatch table carried a reference to a symbol that did not
exist on the module — a soft no-op error path that surfaces as ``KeyError``
only on the actual call, not on import. C5's fix adds the body and registers
it so ``run_admin_op("list_agent_discipline_rows", {})`` returns ``{"rows": ...}``
the way the sister ``list_agent_prompt_rows`` op already does.

Pins:

  * the op is registered in ``_ADMIN_OPS`` (the soft no-op class);
  * the body returns ``{"rows": [...]}`` on success and the standard
    ``{"ok": False, "error": ...}`` envelope on storage failure;
  * an absent ``_get_sql_storage`` returns the same engine-uncomposed error
    message every other op in this module returns (string-equality).
"""

from __future__ import annotations

import pytest


def _admin_ops():
    from yadgar.backend.admin_exec import _ADMIN_OPS

    return _ADMIN_OPS


def _ledger_module():
    from yadgar.backend.admin_exec import ledger

    return ledger


class TestListAgentDisciplineRowsRegistered:
    def test_op_name_in_dispatch_table(self):
        # The dispatch table is the only thing that decides whether the route
        # even accepts the op name — a missing entry was the entire defect.
        assert "list_agent_discipline_rows" in _admin_ops()

    def test_dispatch_target_is_coroutine_function(self):
        # Engine-#2 ledger ops are async (asyncmy) and the dispatcher routes
        # them through run_admin_op_async; sync bodies misroute under the
        # wrong pathway.
        import inspect

        impl = _admin_ops()["list_agent_discipline_rows"]
        assert inspect.iscoroutinefunction(impl)

    def test_ledger_module_exposes_function(self):
        # Symbol existence — the module did not even define this name before
        # C5, so any import that expected to ``from ... import
        # list_agent_discipline_rows`` would fail with ImportError.
        assert hasattr(_ledger_module(), "list_agent_discipline_rows")


class TestListAgentDisciplineRowsReturnsRows:
    @pytest.mark.asyncio
    async def test_returns_rows_envelope_on_success(self, monkeypatch):
        storage = _FakeStorage(rows=[{"name": "d1", "position": 0}])
        monkeypatch.setattr(
            _ledger_module(),
            "_get_sql_storage",
            lambda: storage,
        )
        result = await _ledger_module().list_agent_discipline_rows({})
        assert result == {"rows": [{"name": "d1", "position": 0}]}

    @pytest.mark.asyncio
    async def test_returns_engine_uncomposed_when_storage_none(self, monkeypatch):
        monkeypatch.setattr(
            _ledger_module(),
            "_get_sql_storage",
            lambda: None,
        )
        result = await _ledger_module().list_agent_discipline_rows({})
        # String-equality with the message every other ledger op returns —
        # a divergent copy here is the kind of drift the harness would never
        # otherwise catch.
        assert result == {
            "ok": False,
            "error": "engine #2 not composed (MariaStorageEngine is None)",
        }

    @pytest.mark.asyncio
    async def test_returns_error_envelope_on_storage_exception(self, monkeypatch):
        storage = _FakeStorage(raise_on_list=RuntimeError("boom"))
        monkeypatch.setattr(
            _ledger_module(),
            "_get_sql_storage",
            lambda: storage,
        )
        result = await _ledger_module().list_agent_discipline_rows({})
        assert result == {"ok": False, "error": "boom"}


class _FakeStorage:
    """Tiny stand-in — only ``list_agent_discipline_rows`` is exercised."""

    def __init__(self, *, rows: list[dict] | None = None, raise_on_list: Exception | None = None):
        self._rows = rows or []
        self._raise = raise_on_list

    async def list_agent_discipline_rows(self):
        if self._raise is not None:
            raise self._raise
        return list(self._rows)
