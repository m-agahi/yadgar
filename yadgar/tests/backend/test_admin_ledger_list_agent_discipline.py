"""Bug-bag-2 train 2026-08-23, C5 — ``list_agent_discipline_rows`` admin op tests.

``save_agent_discipline_row`` landed in Car I but the READ counterpart never
did: neither the body function in ``yadgar/backend/admin_exec/ledger_agent.py``
(``ledger.py`` until the ledger-task-402 split) nor
an ``_ADMIN_OPS`` entry existed, so ``run_admin_op("list_agent_discipline_rows",
{})`` raised ``KeyError`` on the op NAME — the dispatch table simply had no such
key. C5 adds both halves so the op returns ``{"rows": ...}`` the way the sister
``list_agent_prompt_rows`` op already does.

The op name was NOT dangling before this car, and it could not have been:
``_ADMIN_OPS`` is a dict literal whose values are direct attribute references
(``"list_agent_discipline_rows": ledger_agent.list_agent_discipline_rows``), so an
entry naming a symbol the module does not define raises ``AttributeError`` at
IMPORT — the whole backend fails to load, loudly. ``KeyError`` at call time is
the signature of a MISSING entry, which is what this was.

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
    """The module the op body lives in.

    ``ledger_agent`` since ledger task 402 split ``ledger.py`` by table family
    (it was 998 of the I13 HARD file_loc cap of 1000). The ``_get_sql_storage``
    seam moved WITH the body — each ledger module owns its own — so this is
    also the module to monkeypatch.
    """
    from yadgar.backend.admin_exec import ledger_agent

    return ledger_agent


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
