"""``project`` registry table + FKs (Car A0 of 0047 spine train, §16.5).

Revision ID: 003_project_registry
Revises:     002_ledger_tables
Create Date: 2026-08-09

The registry exists so every write that stamps ``task.project_id`` or
``adr.project_id`` can be REJECTED at the boundary when the value is
not a known project (FAIL LOUD, NOT INSERT OR IGNORE — §16.5). The
guarded writes are the ``_LedgerMixin`` paths car A wires; the guard
itself is ``yadgar/backend/admin_exec/project_registry.py``.

Car A's ``002_ledger_tables`` ships the ``task`` and ``adr`` tables
WITHOUT the FK on ``project_id`` — that is intentional: the FK chain
needs the ``project`` table to exist before alembic will compile it.
This revision (003) creates the ``project`` table and then attaches
the FKs.

The kind column is an ENUM('git', 'local'), mirroring the §16.2
resolution chain: ``owner/repo`` for git remotes, ``local/<basename>``
for non-git directories. ``created_at`` carries the row's insertion
time — same DATETIME shape as the ledger tables it sits beside.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003_project_registry"
down_revision: str | None = "002_ledger_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "project"
FK_TASK = "fk_task_project"
FK_ADR = "fk_adr_project"


def upgrade() -> None:
    """Create the ``project`` table and wire the FKs from car A's ledger tables.

    Order is load-bearing:
      1. CREATE TABLE project  — the table must exist before the FKs can
         reference it.
      2. CREATE FK on task.project_id  — car A's 002 made this column without
        an FK precisely because 003 ships the parent table.
      3. CREATE FK on adr.project_id   — same.
    """
    op.create_table(
        TABLE_NAME,
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=True),
        sa.Column(
            "kind",
            sa.Enum("git", "local", name="project_kind"),
            nullable=False,
        ),
        sa.Column("remote_url", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_foreign_key(
        FK_TASK,
        "task",
        TABLE_NAME,
        ["project_id"],
        ["key"],
    )
    op.create_foreign_key(
        FK_ADR,
        "adr",
        TABLE_NAME,
        ["project_id"],
        ["key"],
    )


def downgrade() -> None:
    """Reverse cleanly: drop FKs first, then the table.

    Dropping the table while FKs reference it would be a DDL error on
    a populated DB; ordering matters. The two ``drop_constraint`` calls
    run before the ``drop_table`` call.
    """
    op.drop_constraint(FK_TASK, "task", type_="foreignkey")
    op.drop_constraint(FK_ADR, "adr", type_="foreignkey")
    op.drop_table(TABLE_NAME)
