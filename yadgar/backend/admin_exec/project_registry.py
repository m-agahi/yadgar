"""Backend execution body for the ``project`` registry guard (Car A0 of 0047 spine).

A read-only existence check against the ``project`` table. The single
purpose of this guard is to make every write that stamps
``task.project_id`` or ``adr.project_id`` REJECT unknown values with a
structured error before the write reaches the FK.

§16.5 — FAIL LOUD, NOT INSERT OR IGNORE
---------------------------------------

Car A's ``_LedgerMixin`` write path stamps ``project_id`` on every new
``task`` / ``adr`` row. The ``task.project_id`` and ``adr.project_id``
columns carry an FK to ``project.key`` (defined in alembic revision
``003_project_registry``, this car). The FK alone is not the registry
check: the FK catches a missing row at INSERT time, but it surfaces as
an opaque SQL error — and a typo in the caller would still produce a
row with the wrong key (or no row at all). The guard runs BEFORE the
INSERT and translates "no such project_id" into a structured
``UnknownProjectError`` carrying the offending value, so the caller
logs the typo at the right call site rather than chasing a foreign-key
error later.

INSERT OR IGNORE was rejected for this reason: auto-creating the row
would manufacture phantom namespaces (every typo would create a
phantom project that nobody curates), the exact failure ADR-0202 says
the registry exists to prevent.

SYNC + ASYNC PAIR
-----------------

Engine #2 (MariaDB) is async-only (SQLAlchemy ``AsyncEngine``); the
write path in Car A's ``_LedgerMixin`` is synchronous. The split:

  * ``_ensure_project_exists_async`` — the actual query, awaits the
    engine. Car A's write path calls this from inside
    ``asyncio.to_thread`` (a single await per row).
  * ``_ensure_project_exists_sync`` — thin sync wrapper that runs the
    async impl under ``asyncio.run``. Use this when the caller has
    no running loop.

ENGINE #2 ABSENT — RAISES (C6)
------------------------------

This branch used to ``return`` silently, on the reasoning that with no
engine there is no registry and therefore nothing to check. That made
the guard a NO-OP even once wired: on any deployment without engine #2
every project_id would pass a "check" that never ran, and the first
symptom would be a phantom namespace nobody could trace back to a
missing dependency.

It now raises ``ProjectRegistryUnavailableError`` — a DIFFERENT class
from ``UnknownProjectError``, because "could not check" and "checked and
rejected" call for different fixes (repair the deployment vs correct the
project_id) and only one of them is the caller's fault.

Nothing regresses on a real deployment: the compose file composes engine
#2 unconditionally, and every ledger write path already returns
``{"ok": False, "error": "engine #2 not composed"}`` when the slot is
empty (``admin_exec/ledger.py``), so a host reaching this branch was
already unable to write a ``task`` or ``adr`` row. This function had
ZERO call sites before C6, so flipping it breaks no existing caller.

The IN-ENGINE half of the guard —
``MariaStorageEngine.assert_project_registered`` — cannot reach this
case at all: it is dispatched through ``self``, so an engine that does
not exist cannot call it.

STILL ZERO CALL SITES, AND WHY THAT IS STRUCTURAL (Car 5, 2026-08-20)
---------------------------------------------------------------------
C6 noted "this function had ZERO call sites before C6" as a reason
flipping its engine-absent branch broke nothing. Car 5 measured the same
thing afterwards and found the count unchanged — and the reason is not
that nobody got round to it. **Both halves of this module are unusable
from every process that would want them:**

  * ``_ensure_project_exists_sync`` runs ``asyncio.run``. Its only
    plausible callers are the sync write paths — chiefly
    ``QueueDrainer``, a bare ``threading.Thread``. ``MariaStorageEngine``
    builds its ``AsyncEngine`` with the default
    ``AsyncAdaptedQueuePool``, so a private loop per call would cache
    connections bound to a loop that dies with the thread. That hazard is
    written down three times in this repo
    (``backend/retrieval/superseded.py`` — *"Do not move this call
    downstream"*; ``admin_exec/invariants_cross_engine.py``;
    ``embed_service/embed_service_lifecycle.py``).
  * ``_ensure_project_exists_async`` needs a loop AND an engine handle.
    The core process has neither: ``init_engines(sql_storage=False)`` is
    the default and only ``embed_service._ensure_recall_engines`` passes
    ``True``, so ``_st._sql_storage`` is always ``None`` core-side.

Meanwhile ~12 docstrings across ``core/server/tools/`` asserted that this
function enforced the registry "at the backend write path". It did not,
and ``memory.project_id`` / ``wiki_page.project_id`` had no registry
check on ANY writer. Car 5 corrected those docstrings and wired the real
check where it can run: ``core/server/tools/_project_registry.py``, which
forwards ``list_project_rows`` to the backend ``/admin`` route (the query
executes on the backend's own event loop) and caches the key set. This
module is kept — it is the correct shape for a caller that already holds
an engine handle and a loop, e.g. a future async admin op — but it must
not be cited as the thing that guards a write until something calls it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from yadgar._shared.observability.observe import observe

# C6: the error classes moved to a STDLIB-ONLY module under ``_shared`` so
# ``MariaStorageEngine`` can raise the SAME class objects this guard raises.
# Identity matters — every ``except UnknownProjectError`` binds on it, and two
# same-named classes would let a real rejection slip through an except block
# that looks correct. The target module imports nothing but ``__future__``, so
# this re-export does not put ``project_registry`` on the ``sql`` extra (see
# the module docstring's promise, and ``test_errors_module_is_stdlib_only``).
from yadgar._shared.storage.sql.errors import (
    ProjectRegistryUnavailableError,
    UnknownProjectError,
)

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="backend.admin._ensure_project_exists_async")
async def _ensure_project_exists_async(project_id: str, *, engine: Any = None) -> None:
    """Async registry check — REJECT unknown project_id with a structured error.

    Args:
        project_id: The ``owner/repo`` (or ``local/<basename>``) key to verify.
        engine:    A ``MariaStorageEngine`` (or anything exposing
                   ``row_exists``). When ``None``, falls through to the
                   live ``_sql_storage`` slot.

    Raises:
        UnknownProjectError: when no row matches ``project_id``.
        ProjectRegistryUnavailableError: when engine #2 is not composed, so
            the check could not run at all (see module docstring — this used
            to return silently, which made the guard a no-op).

    STALENESS REFRESH (Car C11-#88 / task #88)
    ------------------------------------------
    On a successful registry check, the row's ``last_validated_at`` is
    bumped to CURRENT_TIMESTAMP. The bump runs in a try/except so a stale
    refresh can NEVER break the guard — the contract here is read-only,
    and the threshold query (CLI ``yadgar project list --stale``) only
    reads ``last_validated_at``, never relies on it being fresh.

    A bump failure is logged at WARNING and swallowed: the registry check
    has already passed, so the caller still sees the project as
    registered. The next call bumps again.
    """
    if engine is None:
        engine = _live_engine()
    if engine is None:
        # Engine #2 absent — the guard CANNOT run. Raising rather than
        # returning is the whole point: a silent pass here is a guard that
        # protects nothing on exactly the deployments that need it most.
        raise ProjectRegistryUnavailableError(project_id)

    present = await engine.row_exists(  # type: ignore[attr-defined]
        table="project", key_column="key", key_value=project_id
    )
    if not present:
        raise UnknownProjectError(project_id)

    # Staleness refresh — fires only on a confirmed-present row.
    try:
        from sqlalchemy import text as _sa_text

        async with engine._engine.begin() as _conn:  # type: ignore[attr-defined]
            await _conn.execute(
                _sa_text(
                    "UPDATE project SET last_validated_at = CURRENT_TIMESTAMP WHERE `key` = :key"
                ),
                {"key": project_id},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "project_registry: last_validated_at refresh failed for %s: %s",
            project_id,
            exc,
        )


@observe(tier="boundary", metric="backend.admin._ensure_project_exists_sync")
def _ensure_project_exists_sync(project_id: str, *, engine: Any = None) -> None:
    """Sync wrapper around ``_ensure_project_exists_async``.

    Runs the async impl under ``asyncio.run``. Use from synchronous
    callers (carries the private-loop cost — do not call from inside a
    running event loop, that raises ``RuntimeError``).

    Args:
        project_id: The ``owner/repo`` (or ``local/<basename>``) key to verify.
        engine:    Optional ``MariaStorageEngine``. ``None`` pulls from
                   the live ``_sql_storage`` slot.

    Raises:
        UnknownProjectError: when no row matches ``project_id``.
    """
    asyncio.run(_ensure_project_exists_async(project_id, engine=engine))


def _live_engine() -> Any:
    """Return the live engine #2 slot, or ``None`` when not composed.

    Lazy import keeps this module off the ``sql`` extra on hosts that
    never import engine #2.
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    return _st._sql_storage


__all__ = [
    "ProjectRegistryUnavailableError",
    "UnknownProjectError",
    "_ensure_project_exists_async",
    "_ensure_project_exists_sync",
]
