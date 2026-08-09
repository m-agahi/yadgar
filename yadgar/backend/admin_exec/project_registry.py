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

ENGINE #2 ABSENT — NO-OP
------------------------

When engine #2 is not composed (``_sql_storage is None``), the
``project`` table does not exist either — so the registry check
cannot fail, and we cannot tell "missing" from "present". The guard
passes through silently; the caller decides what to do without
engine #2 (carries its own consequences in the write path).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


class UnknownProjectError(RuntimeError):
    """The given project_id is not present in the ``project`` registry.

    Carries the offending ``project_id`` verbatim so the structured-error
    path can surface it in the response payload. Subclasses ``RuntimeError``
    to match the existing backend structured-error pattern
    (``RestoreVerificationError`` in ``admin_exec/restore_sql.py``).
    """

    def __init__(self, project_id: str) -> None:
        super().__init__(f"unknown project_id: {project_id!r}")
        self.project_id = project_id


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

    MUST NOT issue any INSERT/UPDATE/DELETE — this guard is read-only by
    contract (see module docstring).
    """
    if engine is None:
        engine = _live_engine()
    if engine is None:
        # Engine #2 absent — guard cannot run. Pass through; the caller
        # owns the consequences.
        return

    present = await engine.row_exists(  # type: ignore[attr-defined]
        table="project", key_column="key", key_value=project_id
    )
    if not present:
        raise UnknownProjectError(project_id)


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
    "UnknownProjectError",
    "_ensure_project_exists_async",
    "_ensure_project_exists_sync",
]
