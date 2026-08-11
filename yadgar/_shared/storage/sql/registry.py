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

from typing import TYPE_CHECKING

from yadgar._shared.observability.observe import observe

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
        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

        from yadgar._shared.storage.sql.errors import DuplicateProjectError  # noqa: PLC0415

        if not key:
            raise ValueError("project key must be a non-empty string")
        if kind not in _PROJECT_KINDS:
            raise ValueError(f"project kind must be one of {sorted(_PROJECT_KINDS)}: {kind!r}")

        sql = text(
            "INSERT INTO project (`key`, display_name, kind, remote_url, created_at) "
            "VALUES (:key, :display_name, :kind, :remote_url, CURRENT_TIMESTAMP)"
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
        """
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "SELECT `key`, display_name, kind, remote_url, created_at "
            "FROM project ORDER BY `key` ASC"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql)
            return [dict(row._mapping) for row in result]

    @observe(tier="boundary", metric="backend.sql.project.assert_registered")
    async def assert_project_registered(self, project_id: str) -> None:
        """Raise unless *project_id* is a registered project. Read-only.

        The IN-ENGINE half of the registry guard. Its sibling —
        ``admin_exec/project_registry._ensure_project_exists_async`` — is the
        same check for callers that hold an engine handle and want the guard
        WITHOUT going through a ledger write. Both run the same ``row_exists``
        query; they exist at two seams because they protect different things:

          * this one sits inside the chokepoint methods, so no caller of
            ``create_task_row`` / ``create_adr_row`` can bypass it — including
            the two that call the engine directly rather than through the
            admin op (``adr_seed``, ``seed``);
          * the standalone one is callable before a write is composed.

        Engine-absent is NOT a case here: the method is reached through
        ``self``, so an engine that does not exist cannot dispatch it.

        Raises:
            UnknownProjectError: no ``project`` row matches *project_id*.
        """
        from yadgar._shared.storage.sql.errors import UnknownProjectError  # noqa: PLC0415

        present = await self.row_exists(table="project", key_column="key", key_value=project_id)
        if not present:
            raise UnknownProjectError(project_id)
