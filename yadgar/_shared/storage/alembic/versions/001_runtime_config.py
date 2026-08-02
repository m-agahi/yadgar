# SPDX-License-Identifier: Apache-2.0
"""create runtime_config table

Revision ID: 001_runtime_config
Revises:
Create Date: 2026-08-02 00:00:00

Spine Car A — runtime_config moves from SurrealDB to MariaDB (task #0119).
This is the FIRST Alembic revision per D33(a): the ledger tables depend on
runtime_config existing first (D15's project.key_override is read at write
time, and the read must tolerate absence — falls back to derived key).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from yadgar._shared.observability.observe import observe

# revision identifiers, used by Alembic.
revision: str = "001_runtime_config"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


@observe(tier="stage")
def upgrade() -> None:
    op.create_table(
        "runtime_config",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("directory", sa.String(length=1024), nullable=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", "directory", name="runtime_config_key_dir_idx"),
    )


@observe(tier="stage")
def downgrade() -> None:
    op.drop_table("runtime_config")
