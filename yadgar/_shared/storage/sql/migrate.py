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
def describe_dbapi_error(exc: BaseException) -> dict[str, object]:
    """Pull the DRIVER's errno + message out of a SQLAlchemy wrapper exception.

    THE DIAGNOSIS THIS EXISTS FOR. A failed engine-#2 migration used to log

        {"level":"ERROR","event":"engine #2 migration FAILED …",
         "error":"OperationalError","traceback":"… [truncated]"}

    and nothing else. ``OperationalError`` is the same class for "access
    denied", "server has gone away" and "unknown database"; the one string that
    says WHICH — ``(1142, "CREATE command denied to user 'yadgar_app'@'localhost'
    for table `yadgar`.`task`")`` — lives on ``exc.orig.args`` and was never
    recorded. That cost an entire diagnosis cycle.

    ``exc.orig.args`` rather than ``str(exc)``: SQLAlchemy's ``str`` appends the
    full failing statement and, on a DML path, its bound parameters. The DBAPI
    args are the errno and the server's own message and carry neither. Nothing
    here can reach a password: the credential never enters this process (asyncmy
    reads it straight out of the 0600 option file via ``read_default_file``).

    IT LIVES HERE AND NOT IN ``errors.py``, which would otherwise be its
    natural home. That module is STDLIB-ONLY by contract
    (``test_errors_module_is_stdlib_only``) so the registry guard can import it
    without dragging in the ``sql`` extra — and I33 requires an ``@observe``
    decorator, whose import would break exactly that guarantee. This module
    already carries ``observe`` and stays importable without the extra (every
    alembic/sqlalchemy import in it is function-local).

    Args:
        exc: the exception as caught — a SQLAlchemy ``DBAPIError`` wrapper, or
            any exception at all.

    Returns:
        ``{"error": <class name>}``, plus ``error_code`` / ``error_message``
        when a DBAPI ``(errno, message)`` pair could be read. Never raises: a
        logging helper that can throw inside an ``except`` block would replace
        the failure it was meant to describe.
    """
    described: dict[str, object] = {"error": type(exc).__name__}
    orig = getattr(exc, "orig", None) or exc
    described["error_class"] = type(orig).__name__
    args = getattr(orig, "args", None)
    if isinstance(args, tuple) and len(args) >= 2 and isinstance(args[0], int):
        described["error_code"] = args[0]
        described["error_message"] = str(args[1])
    elif args:
        described["error_message"] = str(args[0])
    return described


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
async def upgrade_to_head_as_migrator(option_file: Path | str | None = None) -> str:
    """Run the chain on a THROWAWAY engine built from the migration credentials.

    THE PRIVILEGE SPLIT, IN ONE FUNCTION. The runtime engine authenticates as
    the DDL-less app account (D19), so it cannot create a table — a fact this
    train exists because nothing enforced. Migrations get their own account and
    their own engine, and the engine is disposed the moment the chain is done
    so no pooled DDL-capable connection outlives the migration.

    Args:
        option_file: the migration account's option file;
            ``default_migrate_option_file_path()`` when omitted.

    Returns:
        The head revision id the chain was brought to.
    """
    from yadgar._shared.storage.sql.config import (  # noqa: PLC0415 — `sql` extra
        default_migrate_option_file_path,
    )
    from yadgar._shared.storage.sql.mariadb import MariaStorageEngine  # noqa: PLC0415

    resolved = Path(option_file) if option_file is not None else default_migrate_option_file_path()
    # pool_size=1: one connection, one chain, then gone.
    migrator = MariaStorageEngine.from_option_file(resolved, pool_size=1, max_overflow=0)
    try:
        return await upgrade_to_head(migrator.engine)
    finally:
        await migrator.dispose()


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


@observe(tier="boundary")
def main(argv: list[str] | None = None) -> int:
    """``python3 -m yadgar._shared.storage.sql.migrate`` — the entrypoint's step.

    WHY THE ENTRYPOINT RUNS THIS AND NOT ONLY THE LIFESPAN. MariaDB rejects a
    table-level ``GRANT`` on a table that does not exist (``1146``), and
    ``entrypoint-backend.sh`` applies the app account's per-table grants from a
    heredoc with no ``--force``, so the first such statement aborts every one
    after it. The tables must therefore exist BETWEEN account creation and
    grant narrowing — an ordering only the process that owns both steps can
    honour. See ``_migrate_engine_two_schema`` in the entrypoint.

    Reads no argument but the optional option-file path; credentials come from
    the 0600 file, never from a flag (a password in ``argv`` lands in
    ``/proc/<pid>/cmdline``).

    Returns:
        0 on success; 1 on failure, with the driver's errno and message on
        stderr so the container log names the actual cause.
    """
    import asyncio  # noqa: PLC0415
    import json  # noqa: PLC0415
    import sys  # noqa: PLC0415

    args = sys.argv[1:] if argv is None else argv
    option_file = args[0] if args else None
    try:
        head = asyncio.run(upgrade_to_head_as_migrator(option_file))
    except Exception as exc:  # noqa: BLE001 — a CLI boundary reports, never propagates
        print(
            "engine #2 migration FAILED: " + json.dumps(describe_dbapi_error(exc), default=str),
            file=sys.stderr,
        )
        return 1
    print(f"engine #2 migrated to alembic head {head}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised as a subprocess
    raise SystemExit(main())
