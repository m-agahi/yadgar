# SPDX-License-Identifier: Apache-2.0
"""Alembic env — spine MariaDB migrations (D34).

Connects to MariaDB via the URL resolved by Yadgar's config system
(YADGAR_MARIADB_URL env > config.yaml > default). Two migration systems
are separate: SurrealDB stays in migrations.py, MariaDB lives here.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from yadgar._shared.storage.alembic_models import Base

config = context.config  # type: ignore[attr-defined]

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from Yadgar config resolution.
# D33(b): migration-time reads nothing from the config store — env var only.
# The full config resolver (env > yaml > default) is invoked at write time.
_mariadb_url = os.environ.get("YADGAR_MARIADB_URL", "")

if _mariadb_url:
    config.set_main_option("sqlalchemy.url", _mariadb_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
