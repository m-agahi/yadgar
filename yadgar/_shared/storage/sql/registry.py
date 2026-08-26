"""``project`` registry row surface — the mixin ``MariaStorageEngine`` inherits.

C6 of the 0047 spine train. ``003_project_registry`` creates the table;
``002_ledger_tables`` ships zero rows; before this car nothing in the tree
could put a row IN it, so the first ``create_task_row`` died on
``fk_task_project``. These three methods are the registry's whole row
surface: one writer, one reader, one guard.

WHY A MIXIN, AND WHY IT IS NOT PR #32's MIXIN
---------------------------------------------
``mariadb.py`` is at I13's HARD 1000-LOC file cap, so the registry surface
lives in its own module. The mixin is mixed into exactly ONE class
(``MariaStorageEngine``) and defines names that exist nowhere else in that
MRO — so it cannot reproduce PR #32's failure, where a MariaDB
``_LedgerMixin`` sat behind SurrealDB's ``_RuntimeConfigMixin`` in the
``StorageEngine`` MRO and every call silently resolved to the wrong half.
The two storage classes still share no base.

``self._engine`` and ``self.row_exists`` are supplied by the host class; the
mixin is not independently constructible and is private for that reason.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

# Legal values of ``project.kind`` — mirrors the ENUM('git','local') in alembic
# revision ``003_project_registry`` and §16.2's resolution chain. Validated in
# ``create_project_row`` so the rejection names the column's legal values;
# MySQL's own ENUM error names neither them nor the caller.
_PROJECT_KINDS = frozenset({"git", "local"})


class _ProjectRegistryMixin:
    """The ``project`` table's row surface. Mixed into ``MariaStorageEngine``."""

    # Supplied by the host class — declared so the mixin type-checks alone.
    _engine: AsyncEngine

    if TYPE_CHECKING:

        async def row_exists(
            self, table: str, key_column: str, key_value: str, *, limit: int = 1
        ) -> bool: ...

    @observe(tier="boundary", metric="backend.sql.project.create")
    async def create_project_row(
        self,
        *,
        key: str,
        kind: str,
        display_name: str | None = None,
        remote_url: str | None = None,
    ) -> dict:
        """INSERT one ``project`` registry row. FAIL LOUD on a duplicate key.

        DELIBERATELY NOT ``INSERT OR IGNORE`` / ``ON DUPLICATE KEY UPDATE``.
        ADR-0202's consequences make the registry check load-bearing on write
        *because* project_id arrives as a caller-supplied free string — and
        auto-creating a row on collision is how ``memorize(project="typo")``
        mints a phantom namespace. A duplicate is surfaced as
        ``DuplicateProjectError`` carrying the key, so an operator re-running
        the seed sees exactly which row already existed rather than a silent
        "success" that may or may not have written anything.

        ``kind`` mirrors §16.2's resolution chain and the ENUM in 003:
        ``git`` for a resolvable remote (``owner/repo``), ``local`` for a
        directory with none (``local/<basename>``). Validated here rather
        than left to MySQL, whose ENUM rejection message names neither the
        column's legal values nor the caller.

        ``created_at`` is NOT NULL with no server default on 003, so it is
        supplied as ``CURRENT_TIMESTAMP`` in the statement.

        Raises:
            ValueError: ``kind`` is not ``git`` or ``local``, or ``key`` is empty.
            DuplicateProjectError: the key is already registered.
        """
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        from yadgar._shared.storage.sql.errors import DuplicateProjectError

        if not key:
            raise ValueError("project key must be a non-empty string")
        if kind not in _PROJECT_KINDS:
            raise ValueError(f"project kind must be one of {sorted(_PROJECT_KINDS)}: {kind!r}")

        sql = text(
            "INSERT INTO project "
            "(`key`, display_name, kind, remote_url, created_at, last_validated_at) "
            "VALUES (:key, :display_name, :kind, :remote_url, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        params = {
            "key": key,
            "display_name": display_name,
            "kind": kind,
            "remote_url": remote_url,
        }
        try:
            async with self._engine.begin() as conn:
                await conn.execute(sql, params)
        except IntegrityError as exc:
            raise DuplicateProjectError(key) from exc
        return dict(params)

    @observe(tier="boundary", metric="backend.sql.project.list")
    async def list_project_rows(self) -> list[dict]:
        """Return every registered project row. Read-only.

        The registry is small by construction (one row per project the user
        owns), so there is no pagination and no filter. Its caller is the C6
        backfill, which validates a host-supplied ``directory_context →
        project_id`` mapping against the registry BEFORE applying anything —
        an unregistered target in the mapping is a manifest-review failure,
        not a per-row FK error discovered halfway through the apply.

        ``last_validated_at`` is deliberately NOT in the projection (task
        384). This SELECT is what ``core/server/tools/_project_registry``
        forwards to answer ``assert_project_registered_for_create`` on EVERY
        ``memorize`` / ``wiki_add``, so naming an optional column here couples
        the create gate to it: after ``005``'s ``downgrade()`` the statement
        fails with MySQL 1054, the forward returns an error, and the gate
        degrades to a shape check — silently, and with nothing in the symptom
        pointing at a dropped column. ``list_stale_projects`` selects the
        column itself and is the surface that ages rows.
        """
        from sqlalchemy import text

        sql = text(
            "SELECT `key`, display_name, kind, remote_url, created_at "
            "FROM project ORDER BY `key` ASC"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql)
            return [dict(row._mapping) for row in result]

    @observe(tier="stage", metric="backend.sql.project.list_stale")
    async def list_stale_projects(self, threshold_days: int) -> dict:
        """Return project rows whose ``last_validated_at`` is older than *threshold_days*.

        Car C11-#88 (task #88). Powers ``yadgar project list --stale`` so an
        operator can see how many rows have drifted past the configured
        threshold without scanning ``list_project_rows`` by hand. NULL is
        included because "never validated" is the failure mode — a row that
        pre-dates the column cannot be older than anything but it IS stale
        in the operator's intent.

        Args:
            threshold_days: rows with ``last_validated_at`` older than this
                are surfaced. Comes from ``Settings.PROJECT_STALENESS_DAYS``
                (env: ``YADGAR_PROJECT_STALENESS_DAYS``, default 90).

        Returns:
            ``{"projects": [...], "threshold_days": int, "count": int}`` —
            one row per stale project, sorted by ``key`` for stable output.
            ``threshold_days`` is echoed so the CLI can print "stale since N
            days" alongside the row count without re-reading settings.
        """
        from sqlalchemy import text

        sql = text(
            "SELECT `key`, display_name, kind, remote_url, created_at, "
            "last_validated_at "
            "FROM project "
            "WHERE last_validated_at IS NULL "
            "   OR last_validated_at < (CURRENT_TIMESTAMP - INTERVAL :days DAY) "
            "ORDER BY `key` ASC"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, {"days": int(threshold_days)})
            rows = [dict(row._mapping) for row in result]
        return {"projects": rows, "threshold_days": int(threshold_days), "count": len(rows)}

    @observe(tier="boundary", metric="backend.sql.project.assert_registered")
    async def assert_project_registered(self, project_id: str, *, refresh: bool = True) -> None:
        """Raise unless *project_id* is a registered project; refresh its clock.

        The registry guard. It sits inside the chokepoint methods, so no
        caller of ``create_task_row`` / ``create_adr_row`` can bypass it —
        including the two that call the engine directly rather than through
        the admin op (``adr_seed``, ``seed``).

        Engine-absent is NOT a case here: the method is reached through
        ``self``, so an engine that does not exist cannot dispatch it.

        STALENESS REFRESH (task #88, re-homed by task 384)
        --------------------------------------------------
        A confirmed-present row has its ``last_validated_at`` bumped to
        CURRENT_TIMESTAMP. Task #88 put this bump on the standalone
        ``admin_exec/project_registry`` guard, which had no call site and
        has since been deleted — so nothing ever bumped the column, and
        after ``005``'s day-zero backfill every row would have crossed
        ``PROJECT_STALENESS_DAYS`` on the same day and stayed there:
        ``yadgar project list --stale`` would report EVERY project stale,
        permanently, which is the exact inverse of the signal it exists to
        carry. The bump belongs on the guard that actually runs.

        It runs in its OWN transaction, AFTER the check, inside a
        try/except: this method's contract to its callers is the raise, and
        a write added for observability must never be able to fail one of
        their ledger inserts. A bump failure is logged at WARNING and
        swallowed — the check has already passed, and the next call bumps
        again. Opening the transaction here is safe because every call site
        invokes this BEFORE opening its own ``begin()``.

        SCOPE — this is a LEDGER-write clock, not a project-activity clock.
        Only ``create_task_row`` / ``create_adr_row`` reach it. The memory /
        wiki create gate (``assert_project_registered_for_create``) forwards
        a cached read and does not bump, so a project that only stores
        memories still ages past the threshold.

        ``refresh=False`` — A PREVIEW CHECKS, IT DOES NOT STAMP
        ------------------------------------------------------
        The dry-run preflights (``admin_exec/identity_stamp`` and
        ``admin_exec/adr_seed``, both ``_preflight_write_guards``) call this
        guard so a preview reaches the same verdict the apply would. They
        pass ``refresh=False``: a ``--dry-run`` that writes is the same
        defect ledger task 385 fixed in ``verify-hooks``, and it would make
        the flag mean something different here than everywhere else in the
        CLI. Parity is preserved where parity is owed — the check runs, the
        refusal is identical, the preview fidelity is unchanged. Only the
        side effect is withheld, and a side effect is precisely what a
        preview must not have.

        Args:
            project_id: the ``owner/repo`` key to verify.
            refresh: bump ``last_validated_at`` on a present row. ``False``
                for preview/dry-run callers.

        Raises:
            UnknownProjectError: no ``project`` row matches *project_id*.
        """
        from sqlalchemy import text

        from yadgar._shared.storage.sql.errors import UnknownProjectError

        present = await self.row_exists(table="project", key_column="key", key_value=project_id)
        if not present:
            raise UnknownProjectError(project_id)

        if not refresh:
            return

        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE project SET last_validated_at = CURRENT_TIMESTAMP "
                        "WHERE `key` = :key"
                    ),
                    {"key": project_id},
                )
        except Exception as exc:  # noqa: BLE001 — never fail the guard on a refresh
            logger.warning(
                "project registry: last_validated_at refresh failed for %s: %s",
                project_id,
                exc,
            )
