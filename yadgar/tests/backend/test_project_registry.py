"""Car A0 of 0047 spine train — ``_ensure_project_exists`` backend guard.

The function REJECTS unknown ``project_id`` values with a structured
error (FAIL LOUD, §16.5). NOT ``INSERT OR IGNORE`` — auto-creating the
row would manufacture phantom namespaces, the exact failure the
``project`` registry exists to prevent.

Tests pin four properties separately so a regression localises:

  * ``UnknownProjectError`` is structured (carries project_id, RuntimeError)
  * unknown project_id → error raised
  * known project_id → no raise, no DML issued
  * NEVER inserts into the ``project`` table (no INSERT/UPDATE/DELETE)
  * SQL storage absent → guard is a no-op (registry table absent too)
"""

from __future__ import annotations

import pytest

import yadgar._shared.runtime.state as _st
import yadgar.backend.admin_exec.project_registry as project_registry

# ── exception type exists and is structured ─────────────────────────────────


def test_unknown_project_error_carries_project_id():
    """``UnknownProjectError`` must hold the offending project_id verbatim.

    Car A wires this error into the write path; the payload is what the
    structured-error path returns to the caller, so the field names are
    load-bearing (not just an opaque message).
    """
    exc = project_registry.UnknownProjectError("m-agahi/yadgar")

    assert exc.project_id == "m-agahi/yadgar"
    assert "m-agahi/yadgar" in str(exc)


def test_unknown_project_error_is_subclass_of_runtime_error():
    """Caller code expects ``except RuntimeError`` to catch the failure.

    The base matches the existing backend structured-error pattern
    (``RestoreVerificationError(RuntimeError)`` in
    ``admin_exec/restore_sql.py``).
    """
    assert issubclass(project_registry.UnknownProjectError, RuntimeError)


# ── the guard itself (sync + async dispatch) ───────────────────────────────


class _FakeEngine:
    """A ``MariaStorageEngine`` stub that records ``row_exists`` calls.

    Mirrors the public surface the registry guard depends on (the new
    ``row_exists(table, key_column, key_value)`` method on
    ``MariaStorageEngine``). Records every call so the NEVER-INSERT
    assertion can inspect what was issued.
    """

    def __init__(self, *, present: bool) -> None:
        self._present = present
        self.calls: list[tuple[str, str, str]] = []

    async def row_exists(self, *, table: str, key_column: str, key_value: str) -> bool:
        self.calls.append((table, key_column, key_value))
        return self._present


# ── behaviour: async path ─────────────────────────────────────────────────


async def test_ensure_project_exists_raises_when_row_absent_async():
    """Unknown project_id MUST raise ``UnknownProjectError`` (async)."""
    engine = _FakeEngine(present=False)

    with pytest.raises(project_registry.UnknownProjectError) as excinfo:
        await project_registry._ensure_project_exists_async("unknown/proj", engine=engine)

    assert excinfo.value.project_id == "unknown/proj"


async def test_ensure_project_exists_passes_silently_when_row_present_async():
    """Known project_id → no raise, single row_exists call against ``project``."""
    engine = _FakeEngine(present=True)

    await project_registry._ensure_project_exists_async("m-agahi/yadgar", engine=engine)

    assert len(engine.calls) == 1
    table, key_column, key_value = engine.calls[0]
    assert table == "project"
    assert key_column == "key"
    assert key_value == "m-agahi/yadgar"


async def test_ensure_project_exists_queries_only_project_table():
    """The query must target the ``project`` table — never some other table.

    A guard that queried ``task.project_id`` (or any other column) would
    be the wrong check.
    """
    engine = _FakeEngine(present=True)

    await project_registry._ensure_project_exists_async("m-agahi/yadgar", engine=engine)

    table, _, _ = engine.calls[0]
    assert table == "project"


async def test_ensure_project_exists_never_writes():
    """The guard MUST NOT call any write API on the engine.

    INSERT OR IGNORE would manufacture phantom namespaces (§16.5). The
    structural pin: the fake engine ONLY implements ``row_exists`` — any
    other method call (``execute``, ``insert``, ``upsert``, …) would
    raise ``AttributeError`` before the test could pass, so the absence
    of a write method is itself the proof.
    """
    engine = _FakeEngine(present=False)

    # This MUST raise UnknownProjectError; the absence of write methods
    # on the fake makes any accidental INSERT/UPDATE/DELETE a hard error.
    with pytest.raises(project_registry.UnknownProjectError):
        await project_registry._ensure_project_exists_async("unknown/proj", engine=engine)

    # Belt: only row_exists was called.
    assert len(engine.calls) == 1


# ── behaviour: sync dispatch wrapper ──────────────────────────────────────


def test_ensure_project_exists_sync_dispatches_to_async():
    """Sync wrapper calls the async impl via ``asyncio.run``.

    The Car A write path runs sync; the engine is async-only; this
    wrapper is the bridge.
    """
    engine = _FakeEngine(present=False)

    with pytest.raises(project_registry.UnknownProjectError):
        project_registry._ensure_project_exists_sync("unknown/proj", engine=engine)


def test_ensure_project_exists_sync_passes_when_row_present():
    """Sync wrapper passes silently when the row exists."""
    engine = _FakeEngine(present=True)

    project_registry._ensure_project_exists_sync("m-agahi/yadgar", engine=engine)

    assert len(engine.calls) == 1


# ── the default-engine path ───────────────────────────────────────────────


def test_ensure_project_exists_pulls_engine_from_live_slot(monkeypatch):
    """Calling without an explicit ``engine`` must pull from ``_sql_storage``.

    The runtime hook is what Car A's ``_LedgerMixin`` uses; this test
    pins the seam so a future refactor cannot silently swap to the
    non-SQL slot.
    """
    engine = _FakeEngine(present=True)
    _st._sql_storage = engine

    try:
        project_registry._ensure_project_exists_sync("m-agahi/yadgar", engine=None)
        assert len(engine.calls) == 1
    finally:
        _st._sql_storage = None


def test_ensure_project_exists_no_op_when_sql_storage_absent():
    """Engine #2 absent → the guard is a no-op (the registry doesn't exist yet).

    Engine #2 is opt-in (ADR-0195). On hosts where MariaDB did not come
    up, the registry table is absent too — and the write path that
    calls this guard will fail later on its own terms (the FK on
    ``task.project_id`` will reject the insert). The guard itself
    cannot enforce a registry that does not exist, so it must pass
    through rather than raise.
    """
    _st._sql_storage = None

    # Must NOT raise. The caller decides what to do without engine #2.
    project_registry._ensure_project_exists_sync("m-agahi/yadgar", engine=None)
