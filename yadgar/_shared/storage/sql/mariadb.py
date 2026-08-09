"""``MariaStorageEngine`` — the engine-#2 handle (ADR-0195 car C).

The SECOND of the two concrete storage classes. ``StorageEngine``
(``_shared/storage/__init__.py:218``) keeps graph, memory, wiki bodies and
embeddings in SurrealDB; this one owns the relational set in MariaDB. ADR-0195
is explicit that the two are CONCRETE classes selected at the existing
composition root — no ABC, no general seam. ``StorageProtocol``
(``_shared/contracts/protocols.py:167``) is read-only, describes the retrieval
surface and has zero non-test consumers, so it is not in this picture.

They also do not share a base class or a mixin list, and that is the point.
PR #32 died partly because a MariaDB ``_LedgerMixin`` sat BEHIND SurrealDB's
``_RuntimeConfigMixin`` in the ``StorageEngine`` MRO: SurrealDB silently won
every ``set_config_row`` call and the MariaDB implementations were dead code
with passing tests. Two unrelated classes make that failure unrepresentable.

WHAT THIS CAR DOES AND DOES NOT DO
----------------------------------
Connect, verify, expose the handle. Nothing more. No tables, no migrations, no
rows — car D owns the schema (``config``, schema-only, zero rows per ADR-0203)
and the knob train owns the first row. Config READS are untouched: they are
core-in-process today (``core/server/tools/_runtime_config.py:23-26``) and
ADR-0200 forbids core touching either database, so repointing them needs the
backend read-op plus the backend PTC — that is the knob train's build.

ASYNC, AND WHY CONSTRUCTION DOES NOT CONNECT
--------------------------------------------
``create_async_engine`` with ``mysql+asyncmy://``. The driver is async-only, so
a sync ``create_engine`` around it fails at runtime — PR #32 paired the two and
did exactly that. Car B made backend admin-op dispatch async-capable
(``run_admin_op_async`` awaits coroutine ops) so an engine-#2 op can be
``async def`` and reach this handle from the event loop.

The constructor is nonetheless SYNC and CONNECTIONLESS. ``init_engines`` is a
sync function, and on the backend boot path it runs inside a worker thread
(``asyncio.to_thread(_start_queue_drainer)`` → ``_ensure_recall_engines``).
Building the ``AsyncEngine`` there is safe because SQLAlchemy defers all loop
binding to first connect; actually CONNECTING there would not be — it would
need a private event loop, and ``AsyncAdaptedQueuePool`` would then cache a
connection bound to a loop that dies with the thread, breaking every later
connect from the real loop. So verification is a separate coroutine.

CREDENTIALS
-----------
``read_default_file`` points the driver at car A's 0600 option file and the
password goes straight from that file into ``asyncmy`` — it is never held by
this process. Explicit ``user``/``database`` still win: asyncmy's ``_config``
helper only falls back to the file when an argument is falsy
(``asyncmy/connection.pyx:378-393``), which is what lets us name the user from
the parsed file rather than hardcoding ``yadgar_app``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.sql.config import (
    CLIENT_GROUP,
    MariaClientConfig,
    read_client_option_file,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

_log = logging.getLogger(__name__)

DRIVER = "mysql+asyncmy"

# Socket-local engine against a single container-local mysqld: a small pool is
# plenty and keeps idle RSS honest (ADR-0205 measured 86.6 MB idle, and calls
# that a FLOOR).
DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 5

# Bare unquoted SQL identifier. Guards ``count_rows``, whose table name cannot be
# a bind parameter.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class MariaStorageEngine:
    """Engine-#2 handle: an async SQLAlchemy engine over a local MariaDB socket."""

    def __init__(
        self,
        config: MariaClientConfig,
        *,
        pool_size: int = DEFAULT_POOL_SIZE,
        max_overflow: int = DEFAULT_MAX_OVERFLOW,
        echo: bool = False,
    ) -> None:
        # Lazy — see the composition root's own lazy import. Keeping sqlalchemy
        # out of module scope means this module itself stays importable without
        # the `sql` extra, so the credential half and the wiring tests run on
        # the yadgar-ci image before it is rebuilt.
        from sqlalchemy.engine import URL  # noqa: PLC0415
        from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

        self._config = config
        # No host and no password in the URL: the transport is a unix socket
        # (car A runs mysqld --skip-networking, so there is no TCP listener to
        # address) and the password comes from the option file.
        self._url = URL.create(DRIVER, username=config.user, database=config.database)
        self._connect_args: dict[str, Any] = {
            "unix_socket": config.unix_socket,
            "read_default_file": str(config.option_file),
            "read_default_group": CLIENT_GROUP,
        }
        self._engine: AsyncEngine = create_async_engine(
            self._url,
            connect_args=self._connect_args,
            pool_size=pool_size,
            max_overflow=max_overflow,
            # mysqld is started by entrypoint-backend.sh and every failure there
            # is non-fatal, so it can restart under a live pool. Pre-ping turns
            # a stale pooled handle into a transparent reconnect.
            pool_pre_ping=True,
            echo=echo,
        )

    # ── constructors ─────────────────────────────────────────────────────

    @classmethod
    def from_option_file(cls, path: Path | str | None = None, **kwargs: Any) -> MariaStorageEngine:
        """Build from car A's option file (``default_option_file_path()`` when omitted)."""
        return cls(read_client_option_file(path), **kwargs)

    # ── handle ───────────────────────────────────────────────────────────

    @property
    def config(self) -> MariaClientConfig:
        return self._config

    @property
    def engine(self) -> AsyncEngine:
        """The SQLAlchemy ``AsyncEngine``. Car D's Alembic run takes it from here."""
        return self._engine

    @property
    def connect_args(self) -> dict[str, Any]:
        """Copy of the driver kwargs. Never contains the password."""
        return dict(self._connect_args)

    @property
    def url(self) -> str:
        """Rendered URL. Password-free by construction, hidden anyway."""
        return self._url.render_as_string(hide_password=True)

    def __repr__(self) -> str:
        return f"<MariaStorageEngine {self.url} socket={self._config.unix_socket}>"

    # ── verification ─────────────────────────────────────────────────────

    @observe(tier="boundary")
    async def verify(self) -> dict[str, str]:
        """Open a connection and report which database and account we landed on.

        Returns ``{"database": ..., "user": ...}``. ``user`` is
        ``CURRENT_USER()`` — the account the SERVER authenticated, which is what
        proves the option-file credential path actually worked rather than some
        fallback quietly succeeding.
        """
        from sqlalchemy import text  # noqa: PLC0415

        async with self._engine.connect() as conn:
            row = (await conn.execute(text("SELECT DATABASE(), CURRENT_USER()"))).one()
        return {"database": str(row[0]), "user": str(row[1])}

    @observe(tier="boundary")
    async def list_tables(self) -> list[str]:
        """Table names in the engine-#2 schema, sorted.

        Read-only introspection. Car D creates the first table; until then this
        is expected to return ``[]``, which is the assertion exit criterion 1
        wants made ENGINE-DIRECT rather than inferred from ``config_list()``.
        """
        from sqlalchemy import text  # noqa: PLC0415

        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() ORDER BY table_name"
                )
            )
            return [str(r[0]) for r in result]

    @observe(tier="boundary")
    async def count_rows(self, table: str) -> int:
        """Row count for one engine-#2 table. Read-only.

        Engine-#2 car H's config-baseline assertion needs an EXACT count, in both
        directions, engine-direct (``config_list()`` returned ``[]`` before this
        train existed and cannot tell the engines apart).

        A table name cannot be parameterised in SQL, so it is validated as a bare
        identifier and quoted with backticks rather than interpolated raw. Callers
        pass module constants today; the guard is what keeps that from becoming a
        latent injection point the first time one does not.
        """
        from sqlalchemy import text  # noqa: PLC0415

        if not _IDENTIFIER_RE.fullmatch(table):
            raise ValueError(f"not a bare SQL identifier: {table!r}")
        async with self._engine.connect() as conn:
            result = await conn.execute(text(f"SELECT COUNT(*) FROM `{table}`"))  # noqa: S608
            return int(result.scalar_one())

    @observe(tier="stage")
    async def dispose(self) -> None:
        """Release pooled connections. Safe when nothing ever connected."""
        await self._engine.dispose()

    @observe(tier="stage")
    async def row_exists(
        self,
        table: str,
        key_column: str,
        key_value: str,
        *,
        limit: int = 1,
    ) -> bool:
        """Return True when ``table.key_column = key_value`` has at least one row.

        Car A0 of 0047 spine train — the ``project`` registry guard. Pure
        read-only existence check; never writes. The key column is a
        single TEXT/VARCHAR primary key (the ``project.key`` schema); a
        LIMIT 1 query is the cheapest way to confirm presence.

        Validation: ``table`` and ``key_column`` are checked as bare
        SQL identifiers and backtick-quoted (defence-in-depth, see
        ``count_rows`` for the pattern); ``key_value`` is bound as a
        parameter, never interpolated.
        """
        from sqlalchemy import text  # noqa: PLC0415

        if not _IDENTIFIER_RE.fullmatch(table):
            raise ValueError(f"not a bare SQL identifier: {table!r}")
        if not _IDENTIFIER_RE.fullmatch(key_column):
            raise ValueError(f"not a bare SQL identifier: {key_column!r}")
        # ``limit`` is an int literal we control — same defence pattern.
        if not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit must be a positive int: {limit!r}")
        quoted_table = f"`{table}`"
        quoted_key = f"`{key_column}`"
        # LIMIT cannot be a parameter on every MySQL/MariaDB driver — bind
        # only ``key_value`` and inject the validated int into the SQL.
        sql = (
            f"SELECT 1 FROM {quoted_table} "  # noqa: S608 — identifiers validated above
            f"WHERE {quoted_key} = :key_value LIMIT {limit}"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(text(sql), {"key_value": key_value})
            return result.first() is not None

    # ── ledger chokepoint (D20) ────────────────────────────────────────────
    #
    # Car A of 0047 spine train — every row access to the engine-#2 ledger
    # (task / adr / agent_pattern / agent_discipline + 3 join tables) flows
    # through these methods. ``scripts/check_ledger_chokepoint.py`` is the
    # AST guard that enforces this rule on every commit.
    #
    # Per-entity tools over a generic record interface (D1). Return shapes
    # are keyed on ``id`` (AUTO_INCREMENT PK, ADR-0197 / §14.1) — never on
    # the retired ``number`` column.

    # ── task ─────────────────────────────────────────────────────────────

    @observe(tier="boundary", metric="backend.sql.task.create")
    async def create_task_row(
        self,
        *,
        project_id: str,
        title: str,
        status: str = "pending",
        state: str | None = "open",
        active_form: str | None = None,
        plan_path: str | None = None,
        body_slug: str | None = None,
    ) -> dict:
        """Insert one ``task`` row, return the inserted PK + inserted fields.

        ``id`` is the AUTO_INCREMENT PK (ADR-0197) and is the row's
        identifier — ``number`` is retired (§14.1). MySQL's
        ``LAST_INSERT_ID()`` returns it without a follow-up SELECT.
        """
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "INSERT INTO task "
            "(project_id, title, status, state, active_form, plan_path, body_slug) "
            "VALUES (:project_id, :title, :status, :state, :active_form, "
            ":plan_path, :body_slug)"
        )
        params = {
            "project_id": project_id,
            "title": title,
            "status": status,
            "state": state,
            "active_form": active_form,
            "plan_path": plan_path,
            "body_slug": body_slug,
        }
        async with self._engine.begin() as conn:
            result = await conn.execute(sql, params)
            inserted_id = result.lastrowid
        return {"id": inserted_id, **params}

    @observe(tier="boundary", metric="backend.sql.task.list")
    async def list_task_rows(
        self,
        *,
        project_id: str,
        status: str | None = None,
    ) -> list[dict]:
        """Project-scoped ``task`` read, optionally filtered by status.

        Always filters on ``project_id`` — never returns rows from other
        projects. Returns dicts keyed on ``id`` (AUTO_INCREMENT PK).
        """
        from sqlalchemy import text  # noqa: PLC0415

        params: dict[str, Any] = {"project_id": project_id}
        where_extra = ""
        if status is not None:
            where_extra = " AND status = :status"
            params["status"] = status
        sql = text(
            "SELECT id, project_id, title, status, state, active_form, "
            "plan_path, body_slug, created_at, updated_at "
            "FROM task WHERE project_id = :project_id" + where_extra + " ORDER BY id ASC"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, params)
            return [dict(row._mapping) for row in result]

    @observe(tier="boundary", metric="backend.sql.task.list_all")
    async def list_task_rows_all_projects(
        self,
        *,
        status: str | None = None,
    ) -> list[dict]:
        """Cross-project ``task`` read — used by ops / dashboards, not users.

        The ``task_list`` tool per project goes through ``list_task_rows``
        (which scopes by ``project_id``); this method is the cross-project
        variant for ops surfaces that legitimately span projects.
        """
        from sqlalchemy import text  # noqa: PLC0415

        params: dict[str, Any] = {}
        where_extra = ""
        if status is not None:
            where_extra = " WHERE status = :status"
            params["status"] = status
        sql = text(
            "SELECT id, project_id, title, status, state, active_form, "
            "plan_path, body_slug, created_at, updated_at "
            "FROM task" + where_extra + " ORDER BY id ASC"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, params)
            return [dict(row._mapping) for row in result]

    @observe(tier="boundary", metric="backend.sql.task.get")
    async def get_task_row(self, task_id: int) -> dict | None:
        """Single-row ``task`` lookup by ``id``. ``None`` when absent."""
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "SELECT id, project_id, title, status, state, active_form, "
            "plan_path, body_slug, created_at, updated_at "
            "FROM task WHERE id = :id"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, {"id": task_id})
            row = result.first()
        return None if row is None else dict(row._mapping)

    @observe(tier="boundary", metric="backend.sql.task.update")
    async def update_task_row(self, task_id: int, **fields: Any) -> None:
        """Patch the named columns on one ``task`` row.

        ``**fields`` accepts the same column names ``create_task_row``
        writes (minus ``id`` / ``created_at`` / ``updated_at`` — those are
        owned by MySQL). An empty ``fields`` is a no-op, NOT an error —
        the caller has nothing to update.
        """
        from sqlalchemy import text  # noqa: PLC0415

        if not fields:
            return
        # Whitelist the updatable columns — defence-in-depth against an
        # ``update_task_row(task_id, id=999)`` smuggling a PK change.
        allowed = {
            "project_id",
            "title",
            "status",
            "state",
            "active_form",
            "plan_path",
            "body_slug",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown task columns: {sorted(unknown)}")
        set_clause = ", ".join(f"`{col}` = :{col}" for col in fields)
        params: dict[str, Any] = dict(fields)
        params["id"] = task_id
        sql = text(f"UPDATE task SET {set_clause} WHERE id = :id")  # noqa: S608 — column whitelist
        async with self._engine.begin() as conn:
            await conn.execute(sql, params)

    @observe(tier="boundary", metric="backend.sql.task.set_body_slug")
    async def set_task_body_slug(self, task_id: int, body_slug: str) -> None:
        """Stamp the wiki ``body_slug`` on one ``task`` row.

        Car D writes the wiki page and then sets this slug so the next
        read can locate the body without scanning all pages.
        """
        from sqlalchemy import text  # noqa: PLC0415

        sql = text("UPDATE task SET body_slug = :body_slug WHERE id = :id")
        async with self._engine.begin() as conn:
            await conn.execute(sql, {"id": task_id, "body_slug": body_slug})

    @observe(tier="boundary", metric="backend.sql.task.add_blocked_by")
    async def add_task_blocked_by(self, task_id: int, blocked_by_id: int) -> None:
        """Insert one ``task_blocked_by`` row — task_id is blocked by blocked_by_id."""
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "INSERT INTO task_blocked_by (task_id, blocked_by_id) VALUES (:task_id, :blocked_by_id)"
        )
        async with self._engine.begin() as conn:
            await conn.execute(sql, {"task_id": task_id, "blocked_by_id": blocked_by_id})

    @observe(tier="boundary", metric="backend.sql.task.list_blocked_by")
    async def list_task_blocked_by(self, task_id: int) -> list[int]:
        """Return the list of task ids blocking ``task_id``, ordered ASC."""
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "SELECT blocked_by_id FROM task_blocked_by "
            "WHERE task_id = :task_id ORDER BY blocked_by_id ASC"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, {"task_id": task_id})
            return [int(row[0]) for row in result]

    # ── adr ──────────────────────────────────────────────────────────────

    @observe(tier="boundary", metric="backend.sql.adr.create")
    async def create_adr_row(
        self,
        *,
        project_id: str,
        title: str,
        status: str = "open",
        decided_on: str | None = None,
        subsystem: str | None = None,
        tier: str | None = None,
        body_slug: str | None = None,
    ) -> dict:
        """Insert one ``adr`` row, return the inserted PK + inserted fields."""
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "INSERT INTO adr "
            "(project_id, title, status, decided_on, subsystem, tier, body_slug) "
            "VALUES (:project_id, :title, :status, :decided_on, :subsystem, "
            ":tier, :body_slug)"
        )
        params = {
            "project_id": project_id,
            "title": title,
            "status": status,
            "decided_on": decided_on,
            "subsystem": subsystem,
            "tier": tier,
            "body_slug": body_slug,
        }
        async with self._engine.begin() as conn:
            result = await conn.execute(sql, params)
            inserted_id = result.lastrowid
        return {"id": inserted_id, **params}

    @observe(tier="boundary", metric="backend.sql.adr.list")
    async def list_adr_rows(
        self,
        *,
        project_id: str,
        status: str | None = None,
    ) -> list[dict]:
        """Project-scoped ``adr`` read, optionally filtered by status."""
        from sqlalchemy import text  # noqa: PLC0415

        params: dict[str, Any] = {"project_id": project_id}
        where_extra = ""
        if status is not None:
            where_extra = " AND status = :status"
            params["status"] = status
        sql = text(
            "SELECT id, project_id, title, status, decided_on, subsystem, "
            "tier, body_slug, created_at, updated_at "
            "FROM adr WHERE project_id = :project_id" + where_extra + " ORDER BY id ASC"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, params)
            return [dict(row._mapping) for row in result]

    @observe(tier="boundary", metric="backend.sql.adr.get")
    async def get_adr_row(self, adr_id: int) -> dict | None:
        """Single-row ``adr`` lookup by ``id``."""
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "SELECT id, project_id, title, status, decided_on, subsystem, "
            "tier, body_slug, created_at, updated_at "
            "FROM adr WHERE id = :id"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, {"id": adr_id})
            row = result.first()
        return None if row is None else dict(row._mapping)

    @observe(tier="boundary", metric="backend.sql.adr.set_body_slug")
    async def set_adr_body_slug(self, adr_id: int, body_slug: str) -> None:
        """Stamp the wiki ``body_slug`` on one ``adr`` row."""
        from sqlalchemy import text  # noqa: PLC0415

        sql = text("UPDATE adr SET body_slug = :body_slug WHERE id = :id")
        async with self._engine.begin() as conn:
            await conn.execute(sql, {"id": adr_id, "body_slug": body_slug})

    @observe(tier="boundary", metric="backend.sql.adr.add_supersedes")
    async def add_adr_supersedes(self, adr_id: int, supersedes_id: int) -> None:
        """Insert one ``adr_supersedes`` row — ``adr_id`` supersedes ``supersedes_id``."""
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "INSERT INTO adr_supersedes (adr_id, supersedes_id) VALUES (:adr_id, :supersedes_id)"
        )
        async with self._engine.begin() as conn:
            await conn.execute(sql, {"adr_id": adr_id, "supersedes_id": supersedes_id})

    # ── agent_pattern ────────────────────────────────────────────────────

    @observe(tier="boundary", metric="backend.sql.agent_pattern.save")
    async def save_agent_prompt(
        self,
        *,
        name: str,
        body_slug: str,
        content_hash: str,
        purpose: str | None = None,
        status: str = "active",
        baseline_hash: str | None = None,
    ) -> dict:
        """Upsert one ``agent_pattern`` row by ``name``.

        PR #32 Fix 8 — a second save of the same ``name`` must UPDATE,
        not violate the UNIQUE constraint. ``INSERT ... ON DUPLICATE KEY
        UPDATE`` is the canonical MySQL upsert; the ``name`` column is
        the conflict target via the ``uq_agent_pattern_name`` constraint.
        """
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "INSERT INTO agent_pattern "
            "(name, body_slug, purpose, status, baseline_hash, content_hash) "
            "VALUES (:name, :body_slug, :purpose, :status, "
            ":baseline_hash, :content_hash) "
            "ON DUPLICATE KEY UPDATE "
            "body_slug = VALUES(body_slug), "
            "purpose = VALUES(purpose), "
            "status = VALUES(status), "
            "baseline_hash = VALUES(baseline_hash), "
            "content_hash = VALUES(content_hash)"
        )
        params = {
            "name": name,
            "body_slug": body_slug,
            "purpose": purpose,
            "status": status,
            "baseline_hash": baseline_hash,
            "content_hash": content_hash,
        }
        async with self._engine.begin() as conn:
            await conn.execute(sql, params)
        return await self.get_agent_prompt_row(name) or {"name": name, **params}

    @observe(tier="boundary", metric="backend.sql.agent_pattern.list")
    async def list_agent_prompt_rows(self) -> list[dict]:
        """List every ``agent_pattern`` row, ordered by name."""
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "SELECT id, name, body_slug, purpose, status, baseline_hash, "
            "content_hash, uses, created_at, updated_at "
            "FROM agent_pattern ORDER BY name ASC"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql)
            return [dict(row._mapping) for row in result]

    @observe(tier="boundary", metric="backend.sql.agent_pattern.get")
    async def get_agent_prompt_row(self, name: str) -> dict | None:
        """Single ``agent_pattern`` lookup by ``name``."""
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "SELECT id, name, body_slug, purpose, status, baseline_hash, "
            "content_hash, uses, created_at, updated_at "
            "FROM agent_pattern WHERE name = :name"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql, {"name": name})
            row = result.first()
        return None if row is None else dict(row._mapping)

    @observe(tier="boundary", metric="backend.sql.agent_pattern.increment_uses")
    async def increment_agent_prompt_uses(self, name: str) -> None:
        """Atomically ``SET uses = uses + 1`` for one ``agent_pattern`` row.

        D40 — the SQL is atomic at the storage layer, not read-modify-
        write in Python. Two concurrent callers both reading ``uses=3``
        and writing ``uses=4`` would lose a count; this method avoids
        the race by issuing a single UPDATE.
        """
        from sqlalchemy import text  # noqa: PLC0415

        sql = text("UPDATE agent_pattern SET uses = uses + 1 WHERE name = :name")
        async with self._engine.begin() as conn:
            await conn.execute(sql, {"name": name})

    # ── agent_discipline ─────────────────────────────────────────────────

    @observe(tier="boundary", metric="backend.sql.agent_discipline.save")
    async def save_agent_discipline(
        self,
        *,
        name: str,
        body_slug: str,
        content_hash: str,
        baseline_hash: str | None = None,
        meta: dict | None = None,
    ) -> dict:
        """Upsert one ``agent_discipline`` row by ``name`` (PR #32 Fix 8).

        Optional presentation columns (``purpose``, ``always_applied``,
        ``position``, ``status``) ride inside the ``meta`` dict rather
        than as separate kwargs — the I30 cap is HARD=8 params and the
        discipline row legitimately has more presentation knobs than
        ``agent_pattern`` (which only carries the prompt). Consumers that
        do not care about ordering or visibility may pass ``meta=None``
        and the defaults match the schema's column defaults.
        """
        from sqlalchemy import text  # noqa: PLC0415

        meta = meta or {}
        purpose = meta.get("purpose")
        always_applied = bool(meta.get("always_applied", False))
        position = int(meta.get("position", 0))
        status = str(meta.get("status", "active"))

        sql = text(
            "INSERT INTO agent_discipline "
            "(name, body_slug, purpose, always_applied, position, status, "
            "baseline_hash, content_hash) "
            "VALUES (:name, :body_slug, :purpose, :always_applied, "
            ":position, :status, :baseline_hash, :content_hash) "
            "ON DUPLICATE KEY UPDATE "
            "body_slug = VALUES(body_slug), "
            "purpose = VALUES(purpose), "
            "always_applied = VALUES(always_applied), "
            "position = VALUES(position), "
            "status = VALUES(status), "
            "baseline_hash = VALUES(baseline_hash), "
            "content_hash = VALUES(content_hash)"
        )
        params = {
            "name": name,
            "body_slug": body_slug,
            "purpose": purpose,
            "always_applied": always_applied,
            "position": position,
            "status": status,
            "baseline_hash": baseline_hash,
            "content_hash": content_hash,
        }
        async with self._engine.begin() as conn:
            await conn.execute(sql, params)
        return {"name": name, **params}

    @observe(tier="boundary", metric="backend.sql.agent_discipline.list")
    async def list_agent_discipline_rows(self) -> list[dict]:
        """List every ``agent_discipline`` row, ordered by ``position`` then name."""
        from sqlalchemy import text  # noqa: PLC0415

        sql = text(
            "SELECT id, name, body_slug, purpose, always_applied, position, "
            "status, baseline_hash, content_hash, created_at, updated_at "
            "FROM agent_discipline ORDER BY position ASC, name ASC"
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(sql)
            return [dict(row._mapping) for row in result]

    # ── agent_pattern_composes ───────────────────────────────────────────

    @observe(tier="boundary", metric="backend.sql.agent_pattern_composes.set")
    async def set_pattern_composes(
        self,
        pattern_name: str,
        composes: list[tuple[str, int]],
    ) -> None:
        """Replace the composition list for one ``agent_pattern``.

        ``composes`` is ``[(discipline_name, position), ...]``. The
        implementation deletes the existing rows for ``pattern_name`` and
        reinserts; partial failure leaves the DB in an inconsistent state,
        but the call is wrapped in a single transaction so the failure
        mode is rollback, not half-write.
        """
        from sqlalchemy import text  # noqa: PLC0415

        async with self._engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM agent_pattern_composes WHERE pattern_name = :pattern_name"),
                {"pattern_name": pattern_name},
            )
            for discipline_name, position in composes:
                await conn.execute(
                    text(
                        "INSERT INTO agent_pattern_composes "
                        "(pattern_name, discipline_name, position) "
                        "VALUES (:pattern_name, :discipline_name, :position)"
                    ),
                    {
                        "pattern_name": pattern_name,
                        "discipline_name": discipline_name,
                        "position": position,
                    },
                )

    # ── D17 — no-op scope filter hook ────────────────────────────────────

    @staticmethod
    def apply_scope_filter(query: Any, *, project_id: str | None) -> Any:
        """D17 — tenancy scope filter. No-op today.

        The ``owner_kind`` / ``owner_id`` / ``reach`` columns were retired
        (§14.1); tenancy is enforced at the registry guard
        (car A0's ``_ensure_project_exists``). The hook is the future
        seam: when a multi-tenant query shape lands, this method becomes
        a non-no-op and every read path gets filtered through it.

        Static method today — no need for the engine handle until the
        tenancy column is restored.
        """
        return query
