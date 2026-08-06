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

    @observe(tier="stage")
    async def dispose(self) -> None:
        """Release pooled connections. Safe when nothing ever connected."""
        await self._engine.dispose()
