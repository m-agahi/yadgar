"""Alembic environment for engine #2 (MariaDB) — ADR-0195 / ADR-0203 car D.

TWO SUPPORTED INVOCATIONS, AND NO THIRD
---------------------------------------
1. **Programmatic, with a caller-supplied connection.** ``migrate.py`` puts a
   live sync ``Connection`` into ``config.attributes["connection"]`` from inside
   ``AsyncConnection.run_sync``. This is the boot path and the only one that
   touches a real database.
2. **Offline ``--sql`` rendering.** No connection, no driver, no server — the
   dialect name alone is enough to render DDL, which is what lets the no-database
   tests assert the shape of the ``config`` table on the yadgar-ci image.

Anything else raises. In particular there is deliberately NO "build my own engine
from ``sqlalchemy.url``" fallback, because engine #2's driver (``asyncmy``) is
async-only: a shell ``alembic upgrade head`` would construct a sync engine over an
async driver and fail at connect time. PR #32 shipped exactly that pairing. A
clear error at the top beats a confusing one at the bottom.

``target_metadata`` IS NONE ON PURPOSE
--------------------------------------
No ORM models, so no ``--autogenerate``. Revisions are hand-written. Declaring
metadata here would create a second source of truth for the schema alongside the
revision files, and which one is authoritative would then be a question rather
than a fact. The spine train (task 0047) owns whether that ever changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import context

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

# Hand-written revisions only — see the module docstring.
target_metadata = None

# Rendered when no connection is supplied. MariaDB speaks the MySQL wire
# protocol, so the MySQL dialect is the correct renderer (ADR-0195).
OFFLINE_DIALECT = "mysql"


def run_migrations_offline() -> None:
    """Render the chain as SQL text — no database, no driver."""
    context.configure(
        dialect_name=OFFLINE_DIALECT,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_with_connection(connection: Connection) -> None:
    """Run the chain against a connection the caller already opened.

    The transaction is alembic's: on MySQL ``transactional_ddl`` is False, so the
    outer ``begin_transaction()`` is a no-op and alembic opens and COMMITS one
    real transaction per migration. That commit is what makes ``alembic_version``
    durable, which is in turn what makes a second ``upgrade head`` a no-op rather
    than a re-run that trips over an existing table.
    """
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Dispatch to the caller-supplied connection, or refuse."""
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError(
            "engine-#2 alembic requires a caller-supplied connection: "
            "yadgar._shared.storage.sql.migrate.upgrade_to_head(engine) puts one in "
            "config.attributes['connection']. There is no URL fallback because the "
            "engine-#2 driver (asyncmy) is async-only and cannot back a sync engine."
        )
    run_migrations_with_connection(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
