"""Engine-#2 migration runner — Alembic, driven programmatically (car D).

WHY ALEMBIC AT ALL, AND WHY IT IS A SECOND SYSTEM
-------------------------------------------------
SurrealDB keeps its own hand-rolled chain (``_shared/storage/migrations.py``, 25
functions behind a ``schema_version`` table). Engine #2 gets Alembic, and the two
are deliberately NOT merged (spine schema D34): one ordered list spanning two
engines has no meaningful "version N" and deadlocks the first time a revision
needs both. MariaDB is MySQL-wire, so ``mysql+asyncmy://`` is a first-class
SQLAlchemy 2.0 async dialect and Alembic works with no adapter — which is the
whole reason task 0051's surrealmigrate fork is moot.

WHY THERE IS NO ``alembic.ini``
-------------------------------
The ``Config`` is built here in code. An ini file would only carry two settings
(``script_location`` and a URL), one of which must be a package-relative path
resolved at runtime and the other of which cannot exist — see ``env.py`` on why
there is no URL fallback. Shipping an ini that is wrong in a container and
unusable from a shell would be a liability, not ergonomics.

ASYNC, AND THE ONE-LINE RECIPE THAT MAKES IT WORK
-------------------------------------------------
``alembic.command.upgrade`` is synchronous and wants a sync ``Connection``.
``AsyncConnection.run_sync`` hands one over — a greenlet-backed proxy onto the
same async connection — so alembic runs unmodified on the event loop's
connection. This is alembic's own documented async recipe.

``connect()``, not ``begin()``: alembic owns the transaction. On MySQL
``transactional_ddl`` is False, so alembic opens and COMMITS one real transaction
PER MIGRATION (``MigrationContext.begin_transaction``). Wrapping it in an outer
``begin()`` would double-begin. The per-migration commit is what makes the
``alembic_version`` row durable, and therefore what makes a second
``upgrade head`` a no-op instead of a re-run that trips over an existing table.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from yadgar._shared.observability.observe import observe

if TYPE_CHECKING:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncEngine

# The alembic environment ships INSIDE the package (``packages = ["yadgar"]`` in
# pyproject.toml puts every file under ``yadgar/`` in the wheel), so the chain
# travels with an installed yadgar rather than depending on a repo checkout.
SCRIPT_LOCATION: Path = Path(__file__).resolve().parent / "migrations"

# Revision the boot path upgrades to. Named rather than inlined so the tests and
# the runner cannot drift apart on it.
HEAD = "head"


@observe(tier="stage")
def build_alembic_config(script_location: Path | str | None = None) -> Config:
    """Build the ``Config`` in code — no ini file (see the module docstring)."""
    from alembic.config import Config as _Config  # noqa: PLC0415 — `sql` extra

    cfg = _Config()
    cfg.set_main_option("script_location", str(script_location or SCRIPT_LOCATION))
    # Revision ids are hand-authored (``0001_config``), so the sortable-filename
    # template alembic would otherwise apply is not wanted.
    cfg.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    return cfg


@observe(tier="stage")
def script_directory(cfg: Config | None = None) -> ScriptDirectory:
    """The parsed revision chain. Used by the no-database head/shape assertions."""
    from alembic.script import ScriptDirectory as _ScriptDirectory  # noqa: PLC0415

    return _ScriptDirectory.from_config(cfg or build_alembic_config())


@observe(tier="stage")
def heads(cfg: Config | None = None) -> tuple[str, ...]:
    """Head revision ids. More than one means the chain forked — a build error."""
    return tuple(script_directory(cfg).get_heads())


@observe(tier="stage")
def render_sql(revision: str = f"base:{HEAD}", *, downgrade: bool = False) -> str:
    """Render the chain as SQL text WITHOUT a database, driver or server.

    ``alembic upgrade --sql`` in library form. Needs neither a connection nor an
    installed DBAPI: ``env.py`` configures the offline context by dialect NAME.
    That is what lets the DDL shape — and the zero-rows rule — be asserted on the
    yadgar-ci image, which has no MariaDB.
    """
    import io  # noqa: PLC0415
    from contextlib import redirect_stdout  # noqa: PLC0415

    from alembic import command  # noqa: PLC0415 — `sql` extra

    cfg = build_alembic_config()
    buffer = io.StringIO()
    cfg.stdout = buffer
    with redirect_stdout(buffer):
        if downgrade:
            command.downgrade(cfg, revision, sql=True)
        else:
            command.upgrade(cfg, revision, sql=True)
    return buffer.getvalue()


def _upgrade_on_connection(connection: Connection, cfg: Config) -> None:
    """Body handed to ``run_sync``: alembic against an already-open connection.

    Not decorated: this runs inside greenlet-adapted sync context under
    ``AsyncConnection.run_sync``, where a span would be attributed to the
    greenlet rather than the awaiting task. ``upgrade_to_head`` carries the span
    for the whole operation.
    """
    from alembic import command  # noqa: PLC0415 — `sql` extra

    cfg.attributes["connection"] = connection
    command.upgrade(cfg, HEAD)


@observe(tier="boundary")
async def upgrade_to_head(engine: AsyncEngine) -> str:
    """Run ``alembic upgrade head`` against a live engine. Returns the head id.

    Idempotent by construction: alembic skips any revision already recorded in
    ``alembic_version``, so a second call is a no-op and changes nothing.

    Args:
        engine: the ``AsyncEngine`` from ``MariaStorageEngine.engine``.

    Returns:
        The head revision id the chain was brought to.
    """
    cfg = build_alembic_config()
    async with engine.connect() as conn:
        await conn.run_sync(_upgrade_on_connection, cfg)
    return heads(cfg)[0]


@observe(tier="boundary")
async def current_revision(engine: AsyncEngine) -> str | None:
    """The revision the database is stamped at, or None when never migrated.

    Reads ``alembic_version`` through alembic's own ``MigrationContext`` rather
    than by querying the table, so "the table does not exist yet" is a clean None
    instead of a driver error.
    """

    def _read(connection: Connection) -> str | None:
        from alembic.runtime.migration import MigrationContext  # noqa: PLC0415

        return MigrationContext.configure(connection).get_current_revision()

    async with engine.connect() as conn:
        result: Any = await conn.run_sync(_read)
    return None if result is None else str(result)
