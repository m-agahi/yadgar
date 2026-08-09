"""``agent_pattern_model`` + ``client`` tables (Car I of 0047 spine train, §3.3).

Revision ID: 004_agent_pattern_model_client
Revises:     003_project_registry
Create Date: 2026-08-09

Closes task 0094. Model tier is a property of (pattern × client), never of the
pattern — the seed corpus's hardcoded ``DISPATCH: model=opus`` lines assume
Claude Code tiers and are wrong for every other client.

SHAPE — from the spine schema (§3.3, task 0047):

  agent_pattern_model    — pattern_name (composite PK col 1, FK →
                           agent_pattern.name, CASCADE) · client
                           (composite PK col 2, FK → client.name, CASCADE)
                           · model VARCHAR(64) NOT NULL · fallback
                           VARCHAR(64) NULL · created_at · updated_at.

  client                 — name VARCHAR(32) PK · display_name VARCHAR(64)
                           NULL · created_at. Lookup mirroring the existing
                           CLIENT_REGISTRY so ``claude-code`` vs
                           ``claude_code`` cannot become a silent miss.

Reach-global: neither table carries ``project_id`` (D3). The
``fk_pattern_composes_pattern`` CASCADE mirrors the §16.11 reach-global
rule — delete the pattern, delete its model tier rows.

WHY CASCADE (NOT RESTRICT) ON ``agent_pattern_model.pattern_name``
-----------------------------------------------------------------
A RESTRICT would make renaming an agent_pattern leave a dangling row that
no resolver could reach — the table's whole reason to exist is the model
tier lookup. The seed corpus's contract path is ``(pattern, '*')`` → the
default tier; deleting the pattern deletes its tiers and the resolver
falls back to unset.

ORDER — load-bearing for the alembic chain
------------------------------------------
``agent_pattern_model`` is created FIRST so its FK references both
``agent_pattern`` (Car A's 002) and ``client`` (this revision) cleanly.
``client`` has no FKs and is created SECOND; the order is reversed in
downgrade so each table drops before the one it depends on.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "004_agent_pattern_model_client"
down_revision: str | None = "003_project_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENT_PATTERN_MODEL_TABLE = "agent_pattern_model"
CLIENT_TABLE = "client"

# Same MySQL table options as Car A's ledger (InnoDB + utf8mb4 + the spine's
# collate). Single source of truth mirrors 002_ledger_tables._MYSQL so the
# chain renders consistently.
_MYSQL: Any = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def _created_updated() -> tuple[sa.Column, sa.Column]:
    """Same (created_at, updated_at) shape as the rest of the spine schema."""
    return (
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
    )


def upgrade() -> None:
    """Create ``agent_pattern_model`` + ``client`` (schema only, zero rows).

    Order: ``agent_pattern_model`` FIRST (it has FKs into both parent
    tables), then ``client`` (no FKs). Downgrade reverses the order so a
    drop never trips over a live FK reference.
    """
    created_at, updated_at = _created_updated()

    op.create_table(
        AGENT_PATTERN_MODEL_TABLE,
        sa.Column("pattern_name", sa.String(length=128), nullable=False),
        sa.Column("client", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("fallback", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["pattern_name"],
            ["agent_pattern.name"],
            name="fk_agent_pattern_model_pattern",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client"],
            [f"{CLIENT_TABLE}.name"],
            name="fk_agent_pattern_model_client",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("pattern_name", "client"),
        created_at,
        updated_at,
        sa.Index("ix_agent_pattern_model_client", "client"),
        **_MYSQL,
    )

    op.create_table(
        CLIENT_TABLE,
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=True),
        created_at,
        sa.PrimaryKeyConstraint("name"),
        **_MYSQL,
    )


def downgrade() -> None:
    """Drop ``client`` + ``agent_pattern_model`` in reverse FK order.

    ``agent_pattern_model`` first (it references ``client``), then
    ``client``. Reversible only while empty — once rows land (Car I ships
    the seed separately), a downgrade destroys data, which is the
    ordinary property of a table-creating revision and why the backup
    arm (car F) exists.
    """
    op.drop_table(AGENT_PATTERN_MODEL_TABLE)
    op.drop_table(CLIENT_TABLE)
