"""Car D's deliverable has a NAMED CALLER in the running system.

The plan's standing acceptance rule exists because PR #32 shipped five modules
with tests and no invocation path — every test green, nothing ever run. So these
assert the wiring itself, not the migration: that the lifespan calls the boot
step and the teardown step, and that each behaves correctly when engine #2 is
absent (the ordinary case on any host without MariaDB) and when it is present.

Pure stdlib + stubs — no sqlalchemy, no alembic, no database. This file must keep
passing on the yadgar-ci image, which bakes neither the `sql` extra nor a server,
exactly as car C's ``test_mariadb_composition.py`` does for its half.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

import yadgar._shared.runtime.lifecycle as _lifecycle
import yadgar._shared.runtime.state as _st

# Reached through the CANONICAL submodule, not the sibling that defines them:
# both helpers live in ``embed_service_lifecycle`` and are re-exported at the
# bottom of ``embed_service`` — which is also how ``lifespan`` resolves them at
# call time. Importing them from here proves that re-export exists.
from yadgar.backend.embed_service.embed_service import (
    _cancel_lifespan_task,
    _dispose_engine_two,
    _migrate_engine_two,
    lifespan,
)


@pytest.fixture(autouse=True)
def _preserve_sql_storage_slot():
    """The slot is a process global; never let a test leak into the next."""
    before = _st._sql_storage
    yield
    _st._sql_storage = before


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1


class _FakeSqlStorage:
    """Stands in for ``MariaStorageEngine`` — only ``engine`` + ``dispose``."""

    def __init__(self, *, dispose_raises: bool = False) -> None:
        self.engine = _FakeEngine()
        self.disposed = 0
        self._dispose_raises = dispose_raises

    async def dispose(self) -> None:
        self.disposed += 1
        if self._dispose_raises:
            raise RuntimeError("pool teardown exploded")


# ── the lifespan names both steps ────────────────────────────────────────


def _lifespan_call_names() -> list[str]:
    """Every bare name referenced in the lifespan body, in SOURCE order.

    Names, not just ``Call.func``: the drainer steps are handed to a thread
    (``asyncio.to_thread(_start_queue_drainer)``), so they appear as arguments
    rather than call targets. ``ast.walk`` yields breadth-first, so the nodes are
    re-sorted by position — the ordering assertions below depend on it.
    """
    source = inspect.getsource(lifespan)
    nodes = [n for n in ast.walk(ast.parse(source.lstrip())) if isinstance(n, ast.Name)]
    nodes.sort(key=lambda n: (n.lineno, n.col_offset))
    return [n.id for n in nodes]


def test_the_lifespan_calls_the_migration_step():
    """Car D is not done until ``alembic upgrade head`` has a caller."""
    assert "_migrate_engine_two" in _lifespan_call_names()


def test_the_lifespan_calls_the_dispose_step():
    """The gap car C flagged: the pool was never released."""
    assert "_dispose_engine_two" in _lifespan_call_names()


def test_the_migration_runs_after_the_engine_is_composed():
    """Ordering is load-bearing — ``_start_queue_drainer`` is what composes it.

    ``_ensure_recall_engines`` (reached through ``_start_queue_drainer``) calls
    ``init_engines(sql_storage=True)``. Migrating before that would find the slot
    empty and silently skip.
    """
    names = _lifespan_call_names()
    assert names.index("_start_queue_drainer") < names.index("_migrate_engine_two")


def test_dispose_runs_after_the_writers_stop():
    names = _lifespan_call_names()
    assert names.index("_stop_queue_drainer") < names.index("_dispose_engine_two")


def test_dispose_is_not_wired_into_the_sync_shutdown():
    """``lifecycle.shutdown`` is sync; ``dispose`` is a coroutine.

    Disposing there would need ``asyncio.run`` — a private event loop tearing
    down a pool bound to the server's. This pins the placement so a later
    "tidy-up" cannot move it back.

    Scoped to ``shutdown``'s OWN body via the AST rather than a substring slice
    to end-of-file, so an unrelated ``dispose`` added anywhere below it cannot
    fail this test with a misleading message. Resolved off the imported module's
    ``__file__``, not a repo-relative path, so it does not depend on the cwd.
    """
    source = Path(_lifecycle.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    shutdown = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == "shutdown"
    )
    body = ast.get_source_segment(source, shutdown) or ""
    assert "dispose" not in body


# ── behaviour with engine #2 absent (the ordinary case) ──────────────────


async def test_migration_skips_silently_when_engine_two_is_absent():
    _st._sql_storage = None
    assert await _migrate_engine_two() is None


async def test_dispose_is_a_no_op_when_engine_two_is_absent():
    _st._sql_storage = None
    await _dispose_engine_two()  # must not raise


# ── behaviour with engine #2 present ─────────────────────────────────────


async def test_migration_upgrades_the_composed_engine(monkeypatch):
    fake = _FakeSqlStorage()
    _st._sql_storage = fake
    seen: list[object] = []

    async def _fake_upgrade(engine):
        seen.append(engine)
        return "0001_config"

    import yadgar._shared.storage.sql.migrate as _migrate

    monkeypatch.setattr(_migrate, "upgrade_to_head", _fake_upgrade)
    assert await _migrate_engine_two() == "0001_config"
    assert seen == [fake.engine], "the composed engine must be the one migrated"


async def test_migration_failure_is_non_fatal_and_logged(monkeypatch, caplog):
    """Non-fatal matches cars A and C — but it is LOGGED with its traceback.

    PR #32's review flagged the silently-swallowed version: the server started
    with no schema and every op failed later with "table doesn't exist" instead
    of one clear boot error.
    """
    _st._sql_storage = _FakeSqlStorage()

    async def _boom(engine):
        raise RuntimeError("mysqld went away")

    import yadgar._shared.storage.sql.migrate as _migrate

    monkeypatch.setattr(_migrate, "upgrade_to_head", _boom)
    with caplog.at_level("ERROR"):
        assert await _migrate_engine_two() is None
    assert any("migration FAILED" in r.message for r in caplog.records)


async def test_dispose_releases_the_pool():
    fake = _FakeSqlStorage()
    _st._sql_storage = fake
    await _dispose_engine_two()
    assert fake.disposed == 1


async def test_dispose_failure_never_blocks_shutdown(caplog):
    fake = _FakeSqlStorage(dispose_raises=True)
    _st._sql_storage = fake
    with caplog.at_level("WARNING"):
        await _dispose_engine_two()  # must not raise
    assert any("dispose failed" in r.message for r in caplog.records)


# ── the cancel helper the two engine-#2 awaits paid for ──────────────────
#
# ``embed_service.py`` sat at 990 of the I30 hard 1000-line cap and ``lifespan``
# at 149 of 150, so the two awaits above broke both. The snapshot and warmup
# cancels were two byte-identical five-line blocks; folding them is what bought
# the room, the same technique as the earlier ``_shutdown_tracing_bounded``
# extraction. Behaviour-preserving, so it is pinned rather than assumed.


async def test_cancel_helper_cancels_a_running_task():
    async def _forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(_forever())
    await asyncio.sleep(0)  # let it start
    await _cancel_lifespan_task(task)
    assert task.cancelled()


async def test_cancel_helper_swallows_a_task_that_raised():
    """The old inline blocks caught ``Exception`` too — a failed warmup task
    must not take shutdown down."""

    async def _boom():
        raise RuntimeError("warmup exploded")

    task = asyncio.create_task(_boom())
    await _cancel_lifespan_task(task)  # must not raise


async def test_cancel_helper_is_a_no_op_on_a_finished_task():
    """``_warmup_task`` is routinely already done by shutdown."""

    async def _done():
        return 42

    task = asyncio.create_task(_done())
    await task
    await _cancel_lifespan_task(task)  # must not raise
