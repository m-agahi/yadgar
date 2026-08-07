"""config table — engine #2's first revision, SCHEMA ONLY, ZERO ROWS.

Revision ID: 0001_config
Revises:
Create Date: 2026-08-06

ADR-0203 makes this the pilot table for the four operational arms, and makes
"zero rows" load-bearing rather than incidental: the knob plan puts task 0095's
free-re-key window behind the first ``config_set``, NOT behind the schema. An
empty table exercises every arm while leaving that window open. Seeding is the
knob train's job, so THIS FILE MUST NEVER INSERT A ROW — and a no-database test
asserts that mechanically by rendering the chain and failing on any ``INSERT``.
(``alembic_version`` rows are expected; that gate is scoped to ``config``.)

SHAPE — from the spine schema (docs/plans/task-table-refactor-2026-07-29.md §3.1),
which ADR-0198 decided table by table. Four columns, no more:

    key            VARCHAR(64)  NOT NULL  PRIMARY KEY
    value          TEXT         NOT NULL
    default_value  TEXT         NOT NULL
    updated_at     DATETIME     NOT NULL  DEFAULT now / ON UPDATE now

WHY THERE IS NO ``directory`` COLUMN, AND NO SURROGATE ``id``
------------------------------------------------------------
Both are deliberate removals, not omissions (ADR-0198, ADR-0207/D2). All knobs
are global. The old ``UNIQUE(key, directory)`` never bound the global rows at
all, because MariaDB unique indexes permit unlimited NULLs — two concurrent
global writes produced duplicate rows, and every later read then failed on
``MultipleResultsFound`` with no tool able to repair it, since ``delete_config_row``
resolves through the same ``.one_or_none()``. Key-as-PK makes the duplicate
structurally unrepresentable and reduces the write to one
``INSERT ... ON DUPLICATE KEY UPDATE`` with no read-then-write race.

``value`` and ``default_value`` are TEXT holding JSON, not a JSON column: the
existing write path already JSON-encodes (``_shared/storage/runtime_config.py:101``)
and ADR-0207 widens the accepted types to include float, which round-trips as a
plain JSON number. ``default_value`` is DERIVED — re-synced from ``Settings`` at
every backend boot — so it is NOT NULL with no server default, which means an
INSERT must supply it. Both the seed and the boot re-sync belong to the knob train.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_config"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "config"

# MariaDB has no DDL-level "ON UPDATE" outside the column default clause, and
# SQLAlchemy's ``server_onupdate=`` is informational only — it emits nothing. The
# whole clause therefore has to travel in ``server_default`` to reach the DDL.
_UPDATED_AT_DEFAULT = "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        # ``key`` is a MySQL reserved word; SQLAlchemy quotes it automatically.
        # 64 is sized off the real corpus — the longest Settings-derived key in
        # the tree today is 49 characters (spine schema §3.1).
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("default_value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text(_UPDATED_AT_DEFAULT),
        ),
        sa.PrimaryKeyConstraint("key"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )


def downgrade() -> None:
    """Drop the table.

    Reversible in the full sense, which is only true while the table is empty —
    the state ADR-0203 requires this train to leave it in. Once the knob train
    seeds it, a downgrade destroys those rows; that is an ordinary property of a
    table-creating revision and is why the backup arm exists.
    """
    op.drop_table(TABLE_NAME)
