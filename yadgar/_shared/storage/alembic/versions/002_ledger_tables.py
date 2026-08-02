# SPDX-License-Identifier: Apache-2.0
"""create task, adr, agent_prompt tables

Revision ID: 002_ledger_tables
Revises: 001_runtime_config
Create Date: 2026-08-02 00:00:01

Spine Car A — ledger tables for the relational set. Bodies stay as wiki
pages in SurrealDB (D4, non-negotiable); these tables hold metadata only.

Identity: id is the AUTO_INCREMENT PK and also the semantic number.
No separate number column — per-project numbering is not needed; global
uniqueness across all projects is sufficient.

Schema per §3 of task-table-refactor-2026-07-29.md.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from yadgar._shared.observability.observe import observe

# revision identifiers, used by Alembic.
revision: str = "002_ledger_tables"
down_revision: str | None = "001_runtime_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


@observe(tier="stage")
def upgrade() -> None:
    # task table
    op.create_table(
        "task",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("origin", sa.String(length=64), nullable=False),
        sa.Column("owner_kind", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("owner_id", sa.String(length=255), nullable=True),
        sa.Column("reach", sa.String(length=16), nullable=False, server_default="project"),
        sa.Column("title", sa.String(length=200), nullable=False),  # D12
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="open"),  # D36
        sa.Column("body_slug", sa.String(length=255), nullable=True),  # D4
        sa.Column("active_form", sa.String(length=200), nullable=True),
        sa.Column("plan_path", sa.String(length=512), nullable=True),  # D36
        sa.Column("blocked_by", sa.JSON(), nullable=True),  # D39
        sa.Column("blocks", sa.JSON(), nullable=True),  # D39
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "modified_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("task_project_status_idx", "task", ["project_id", "status"])

    # adr table
    op.create_table(
        "adr",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("origin", sa.String(length=64), nullable=False),
        sa.Column("owner_kind", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("owner_id", sa.String(length=255), nullable=True),
        sa.Column("reach", sa.String(length=16), nullable=False, server_default="project"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("body_slug", sa.String(length=255), nullable=True),  # D4: set after wiki write
        sa.Column("date", sa.String(length=32), nullable=True),
        sa.Column("subsystem", sa.String(length=128), nullable=True),  # D28
        sa.Column("tier", sa.String(length=32), nullable=True),  # D27
        sa.Column("supersedes", sa.JSON(), nullable=True),
        sa.Column("superseded_by", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "modified_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("adr_project_status_idx", "adr", ["project_id", "status"])

    # agent_prompt table (reach always global — D3)
    op.create_table(
        "agent_prompt",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False, server_default="global"),
        sa.Column("origin", sa.String(length=64), nullable=False),
        sa.Column("owner_kind", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("owner_id", sa.String(length=255), nullable=True),
        sa.Column("reach", sa.String(length=16), nullable=False, server_default="global"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("body_slug", sa.String(length=255), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=500), nullable=True),
        sa.Column("composes", sa.JSON(), nullable=True),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),  # D40
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "modified_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("title", name="agent_prompt_title_unique"),
    )


@observe(tier="stage")
def downgrade() -> None:
    op.drop_table("agent_prompt")
    op.drop_index("adr_project_status_idx", table_name="adr")
    op.drop_table("adr")
    op.drop_index("task_project_status_idx", table_name="task")
    op.drop_table("task")
