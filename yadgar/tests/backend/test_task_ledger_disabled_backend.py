"""yaadgaar #699 — the backend half of ``tasks.ledger_disabled``.

The core gate refuses ``task_write`` first. These tests pin the backend
defence in depth:

* ``create_task_row`` / ``update_task_row`` refuse (``AdminRefusal`` → 409)
  when the key is true, so a raw ``POST /admin`` caller (backfill CLI,
  scripts) cannot write the frozen ledger either;
* the nightly archive sweep neither counts nor archives task rows when the
  key is true (the frozen ledger is read-only history), and still sweeps ADRs;
* the key is off when absent, ``false``, malformed or unreadable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from yadgar.backend.admin_exec import ledger, nightly_sweep
from yadgar.backend.admin_exec import runtime_config as backend_rc
from yadgar.backend.admin_exec.refusals import TaskLedgerDisabledError
from yadgar.tests.backend.test_archive_sweep_car_k import (
    _adr_row,
    _body_page,
    _FakeSqlStorage,
    _FakeSurrealStorage,
    _task_row,
)

_KEY = "tasks.ledger_disabled"


def _surreal_with(value: object | None) -> MagicMock:
    """A SurrealDB stand-in whose global ``tasks.ledger_disabled`` row is ``value``."""
    storage = MagicMock()
    row = None if value is None else {"key": _KEY, "directory": None, "value": value}
    storage.get_config_row = MagicMock(return_value=row)
    return storage


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch):
    """Point the backend reader's default storage at a settable fake."""
    state: dict = {"storage": _surreal_with(None)}
    monkeypatch.setattr(backend_rc, "_get_storage", lambda: state["storage"])
    return state


# ── the reader ───────────────────────────────────────────────────────────────


def test_backend_key_matches_core_key() -> None:
    from yadgar.core.task_ledger_gate import LEDGER_DISABLED_KEY

    assert backend_rc.LEDGER_DISABLED_KEY == LEDGER_DISABLED_KEY


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), (None, False), ("true", False), (1, False)],
)
def test_reader_is_on_only_for_json_true(config, value: object, expected: bool) -> None:
    config["storage"] = _surreal_with(value)
    assert backend_rc.task_ledger_disabled() is expected
    config["storage"].get_config_row.assert_called_once_with(_KEY, directory=None)


def test_reader_failed_read_is_off_and_warns(config, caplog: pytest.LogCaptureFixture) -> None:
    config["storage"] = MagicMock()
    config["storage"].get_config_row = MagicMock(side_effect=RuntimeError("surreal down"))
    assert backend_rc.task_ledger_disabled() is False
    assert any(_KEY in r.getMessage() for r in caplog.records)


def test_reader_storage_unavailable_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise RuntimeError("storage not initialised")

    monkeypatch.setattr(backend_rc, "_get_storage", _boom)
    assert backend_rc.task_ledger_disabled() is False


# ── the ledger write ops ─────────────────────────────────────────────────────


def _fake_sql(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    sql = MagicMock()
    sql.create_task_row = AsyncMock(return_value={"id": 7})
    sql.update_task_row = AsyncMock()
    sql.list_task_blocks = AsyncMock(return_value=[])
    sql.list_task_blocked_by = AsyncMock(return_value=[])
    monkeypatch.setattr(ledger, "_get_sql_storage", lambda: sql)
    return sql


def test_create_task_row_refuses_when_disabled(monkeypatch, config) -> None:
    sql = _fake_sql(monkeypatch)
    config["storage"] = _surreal_with(True)
    with pytest.raises(TaskLedgerDisabledError) as excinfo:
        asyncio.run(ledger.create_task_row({"project_id": "x/y", "title": "t"}))
    assert excinfo.value.reason == "external_task_ledger"
    assert excinfo.value.refusal_report()["project_id"] == "x/y"
    assert excinfo.value.refusal_report()["ledger"] == "yaadgaar"
    sql.create_task_row.assert_not_called()


def test_update_task_row_refuses_when_disabled(monkeypatch, config) -> None:
    sql = _fake_sql(monkeypatch)
    config["storage"] = _surreal_with(True)
    with pytest.raises(TaskLedgerDisabledError) as excinfo:
        asyncio.run(ledger.update_task_row({"id": 5, "status": "completed"}))
    assert excinfo.value.refusal_report()["task_id"] == 5
    sql.update_task_row.assert_not_called()


@pytest.mark.parametrize("value", [None, False, "true"])
def test_write_ops_unchanged_when_not_disabled(monkeypatch, config, value) -> None:
    sql = _fake_sql(monkeypatch)
    config["storage"] = _surreal_with(value)
    assert asyncio.run(ledger.create_task_row({"project_id": "x/y", "title": "t"})) == {"id": 7}
    assert asyncio.run(ledger.update_task_row({"id": 5, "status": "completed"})) == {
        "id": 5,
        "status": "completed",
    }
    sql.create_task_row.assert_awaited_once()
    sql.update_task_row.assert_awaited_once()


def test_refusal_renders_as_409_on_the_admin_route(monkeypatch, config) -> None:
    from yadgar.backend.embed_service.embed_service import app

    _fake_sql(monkeypatch)
    config["storage"] = _surreal_with(True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/admin", json={"op": "create_task_row", "payload": {"project_id": "x/y", "title": "t"}}
    )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["refused"] is True
    assert body["reason"] == "external_task_ledger"
    assert body["ledger"] == "yaadgaar"


# ── the nightly sweep ────────────────────────────────────────────────────────


class _SurrealWithConfig(_FakeSurrealStorage):
    def __init__(self, *, disabled: object | None, pages: list[dict]) -> None:
        super().__init__(pages=pages)
        self._disabled = disabled

    def get_config_row(self, key: str, *, directory: str | None) -> dict | None:
        if key == _KEY and directory is None and self._disabled is not None:
            return {"key": key, "directory": None, "value": self._disabled}
        return None


async def _sweep(monkeypatch, *, disabled: object | None, limit: int = 500):
    task = _task_row(id_=42, body_slug="p_r_task-42")
    adr = _adr_row(id_=7, body_slug="p_r_adr-7")
    sql = _FakeSqlStorage(task_rows=[task], adr_rows=[adr])
    surreal = _SurrealWithConfig(
        disabled=disabled,
        pages=[
            _body_page(id_=100, slug="p_r_task-42", page_type="task"),
            _body_page(id_=101, slug="p_r_adr-7", page_type="adr"),
        ],
    )
    monkeypatch.setattr(nightly_sweep, "_get_sql_storage", lambda: sql)
    monkeypatch.setattr(nightly_sweep, "_get_storage", lambda: surreal)
    monkeypatch.setattr(backend_rc, "_get_storage", lambda: surreal)
    result = await nightly_sweep.run_nightly_archive_sweep(
        {
            "retention_days": 90,
            "now": datetime.now(UTC).timestamp(),
            "circuit_breaker_limit": limit,
        }
    )
    return result, sql


async def test_sweep_skips_tasks_but_archives_adrs_when_disabled(monkeypatch) -> None:
    result, sql = await _sweep(monkeypatch, disabled=True)
    assert result["archived_tasks"] == 0
    assert sql.task_updates == []
    assert result["archived_adrs"] == 1


async def test_sweep_archives_tasks_when_not_disabled(monkeypatch) -> None:
    result, sql = await _sweep(monkeypatch, disabled=False)
    assert result["archived_tasks"] == 1
    assert sql.task_updates == [(42, {"status": "archived", "state": None})]


async def test_frozen_tasks_do_not_trip_the_circuit_breaker(monkeypatch) -> None:
    # One task + one ADR candidate; a limit of 1 trips only if tasks are counted.
    result, _ = await _sweep(monkeypatch, disabled=True, limit=1)
    assert result["circuit_breaker_hit"] is False
    assert result["archived_adrs"] == 1
