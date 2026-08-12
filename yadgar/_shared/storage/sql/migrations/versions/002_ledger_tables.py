"""Engine-#2 ledger schema — task, adr, agent_pattern, agent_discipline + 3 joins.

Revision ID: 002_ledger_tables
Revises:     0001_config
Create Date: 2026-08-09

Car A of 0047 spine train (§16). Car A0's ``003_project_registry`` chains
off this revision and adds the ``project`` table + the FKs to
``task.project_id`` / ``adr.project_id``; this revision ships the columns
WITHOUT a FK so the alembic chain compiles regardless of FK target order.

WHAT THIS REVISION CREATES
--------------------------
Seven tables (three rows of four below, then the three join tables):

  task                — title, status, state, active_form, plan_path,
                        body_slug, completed_at, project_id (plain column,
                        NO FK), id AUTO_INCREMENT PK (ADR-0197; ``id`` IS
                        the number, no separate ``number`` column).
                        UNIQUE(project_id, body_slug) — see below.
  adr                 — title, status, decided_on, subsystem, tier,
                        body_slug, superseded_at, project_id (plain column,
                        NO FK), id AUTO_INCREMENT PK.
  agent_pattern       — name (PK-like unique), body_slug, purpose, status,
                        baseline_hash, content_hash, uses, id AUTO_INCREMENT
                        PK. ``content_hash NOT NULL`` + ``baseline_hash
                        NULL`` are the cross-engine desync arm's substrate
                        (ADR-0209 / §14.3).
  agent_discipline    — same shape as ``agent_pattern`` plus ``position`` and
                        ``always_applied``.

  task_blocked_by     — composite (task_id, blocked_by_id) PK.
  adr_supersedes      — composite (adr_id, supersedes_id) PK.
  agent_pattern_composes
                      — composite (pattern_name, discipline_name, position) PK.

WHY ``project_id`` HAS NO FOREIGN KEY HERE
------------------------------------------
The FK target (``project.key``) is created by car A0's
``003_project_registry``, which descends from THIS revision. Car A0's
revision adds the FKs after the parent table exists. A FK declared here
would reference a not-yet-existent table and the alembic chain would fail
to compile (``foreign key target ``project`` is not present``).

WHY ``id`` IS THE NUMBER, AND WHY ``number`` IS RETIRED
--------------------------------------------------------
ADR-0197 + §14.1. The historical ``number`` column was wrapped in
``UNIQUE(project_id, origin, number)`` and assigned via ``MAX+1 FOR UPDATE``;
that construct was retired in favour of ``id BIGINT UNSIGNED AUTO_INCREMENT``.
PR #32 §13.2 blocker 2 — surfaces that ledger method return shapes MUST be
keyed on ``id``, not ``number``. This revision makes ``id`` the only
identifier and never creates ``number``.

THE TWO RETIREMENT CLOCKS — ``completed_at`` / ``superseded_at`` (C15a)
------------------------------------------------------------------------
Both added IN PLACE on this unreleased revision (§0 licence — this revision
CREATES both tables and has never run anywhere), for the same reason and
with the same shape: ``DATETIME NULL``.

The nightly archive sweep retires a row 90 days after it STOPS being live,
and neither table previously carried the instant at which that happened.
Car K therefore aged tasks off ``updated_at`` and ADRs off ``created_at``,
and both are the wrong interval:

  * ``updated_at`` bumps on every edit, so touching a completed task reset
    its 90-day clock — the regression §14.2 named explicitly.
  * ``created_at`` measures age-since-authoring, so an ADR written 120 days
    ago and superseded today was archived on the very NEXT sweep, with no
    grace period whatsoever.

Both are NULLable, and the sweep reads NULL as "this clock never started"
rather than "infinitely old" — a row that has not completed cannot have run
out of retention. ``superseded_at`` additionally stays NULL for the
``rejected`` / ``deprecated`` rows the sweep also collects (nothing
supersedes them); those fall back to ``created_at``, which is why the
column can be added without changing their behaviour at all.

UNIQUE(project_id, body_slug) ON ``task``
-----------------------------------------
Parent-plan D8: one ledger row ↔ one wiki page is the only genuine 1:1, so
uniqueness belongs on ``body_slug``, NOT on the title (two tasks may share
a name). Scoped by ``project_id`` because the slug is only unique within a
project. MySQL permits repeated NULLs in a UNIQUE index, so this does not
block the D4 state where ``body_slug`` is NULL until the body page exists.

ZERO ROWS — D35a
----------------
``op.create_table`` calls only. The first ledger row is the seed (a
separate one-shot admin op, NOT a migration step). The no-database test
``yadgar/tests/_shared/test_mariadb_migrations.py::test_the_chain_inserts_nothing``
renders the chain offline and fails on any ``INSERT``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql as mysql_dialect

revision: str = "002_ledger_tables"
down_revision: str | None = "0001_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ── column type constants (one source of truth for tests + DDL) ─────────────

# ``project_id`` is a plain VARCHAR(256) NOT NULL — no FK on this revision
# (car A0's 003 adds the FK after ``project`` exists).
#
# WHY 256 AND NOT 255 (C6 / #17). Two DIFFERENT invariants land on the same
# number and it is worth keeping them apart:
#
#   * ``_SLUG`` below holds a slug, and ADR-0202 caps slugs at 256 chars with
#     a hash suffix on overflow (``reslug.cap_slug``). 256 is that cap; the
#     two must move together or an over-long slug turns into an INSERT
#     failure instead of a hashed one.
#   * ``_PROJECT_ID`` holds a project_id, which ADR-0202 gives NO cap. The
#     constraint on it is only that ``project.key`` (003) and the two
#     referencing columns AGREE — a FK across mismatched VARCHAR widths is a
#     latent truncation bug. 256 is chosen so the identity columns and the
#     slug columns carry one number rather than two adjacent ones that a
#     later reader would try to reconcile.
#
# The 255 → 256 change is made IN PLACE on this unreleased revision rather
# than as a follow-up ALTER, the same licence C14 takes for its own 002
# changes (§5.C14). InnoDB's index-prefix limit is not in play: utf8mb4 at
# 255 chars is already 1020 bytes, so if 255 fits, 256 does.
_PROJECT_ID = sa.String(length=256)

# Slug columns (``body_slug``). Sized to ADR-0202's slug cap — see above.
_SLUG = sa.String(length=256)

# ``content_hash`` and ``baseline_hash`` are CHAR(64) — sha256 hex shape,
# fixed width so MySQL's row format does not depend on the actual value
# length and the cross-engine desync arm reads back the exact stored bytes.
_HEX_64 = sa.CHAR(length=64)

# ``id`` is the only identifier — BIGINT UNSIGNED AUTO_INCREMENT. The
# ``MySQLDialect`` has a built-in ``BigInteger(unsigned=True)`` via the
# ``with_variant`` form; ``sa.BigInteger(unsigned=True)`` directly raises
# (``BigInteger() takes no arguments``), so the variant is the right shape.
_ID = sa.BigInteger().with_variant(mysql_dialect.BIGINT(unsigned=True), "mysql")

# Shared MySQL table options — every ledger table is InnoDB + utf8mb4.
# Keeping one constant shrinks the upgrade function below I30's HARD=150
# LOC cap without losing legibility. Typed as ``Any`` so unpacking via
# ``**_MYSQL`` matches ``op.create_table``'s ``**kw: Any`` signature.
_MYSQL: Any = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def _created_updated() -> tuple[sa.Column, sa.Column]:
    """Return the standard ``(created_at, updated_at)`` column pair.

    Every ledger table carries the same timestamp pair so the engine-side
    ``updated_at`` mirror stays uniform across the schema.
    """
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
    """Create the seven ledger tables — schema only, zero rows."""
    created_at, updated_at = _created_updated()

    op.create_table(
        "task",
        sa.Column("id", _ID, nullable=False, autoincrement=True),
        sa.Column("project_id", _PROJECT_ID, nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("state", sa.String(length=32), nullable=True),
        sa.Column("active_form", sa.String(length=512), nullable=True),
        sa.Column("plan_path", sa.String(length=512), nullable=True),
        sa.Column("body_slug", _SLUG, nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "body_slug", name="uq_task_project_body_slug"),
        sa.Index("ix_task_project_id", "project_id"),
        sa.Index("ix_task_status", "status"),
        **_MYSQL,
    )

    op.create_table(
        "adr",
        sa.Column("id", _ID, nullable=False, autoincrement=True),
        sa.Column("project_id", _PROJECT_ID, nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("decided_on", sa.Date(), nullable=True),
        sa.Column("subsystem", sa.String(length=128), nullable=True),
        sa.Column("tier", sa.String(length=32), nullable=True),
        sa.Column("body_slug", _SLUG, nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_adr_project_id", "project_id"),
        sa.Index("ix_adr_status", "status"),
        **_MYSQL,
    )

    op.create_table(
        "agent_pattern",
        sa.Column("id", _ID, nullable=False, autoincrement=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("body_slug", _SLUG, nullable=False),
        sa.Column("purpose", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("baseline_hash", _HEX_64, nullable=True),
        sa.Column("content_hash", _HEX_64, nullable=False),
        sa.Column("uses", sa.BigInteger(), nullable=False, server_default="0"),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_agent_pattern_name"),
        sa.Index("ix_agent_pattern_status", "status"),
        **_MYSQL,
    )

    op.create_table(
        "agent_discipline",
        sa.Column("id", _ID, nullable=False, autoincrement=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("body_slug", _SLUG, nullable=False),
        sa.Column("purpose", sa.String(length=512), nullable=True),
        sa.Column("always_applied", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("baseline_hash", _HEX_64, nullable=True),
        sa.Column("content_hash", _HEX_64, nullable=False),
        created_at,
        updated_at,
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_agent_discipline_name"),
        sa.Index("ix_agent_discipline_status", "status"),
        **_MYSQL,
    )

    # ── join tables ──────────────────────────────────────────────────────
    # Composite PKs only — no surrogate id. The FKs are added in their
    # natural order; the parent tables exist by the time this revision
    # runs.

    op.create_table(
        "task_blocked_by",
        sa.Column("task_id", _ID, nullable=False),
        sa.Column("blocked_by_id", _ID, nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], name="fk_task_blocked_by_task"),
        sa.ForeignKeyConstraint(
            ["blocked_by_id"], ["task.id"], name="fk_task_blocked_by_blocked_by"
        ),
        sa.PrimaryKeyConstraint("task_id", "blocked_by_id"),
        **_MYSQL,
    )

    op.create_table(
        "adr_supersedes",
        sa.Column("adr_id", _ID, nullable=False),
        sa.Column("supersedes_id", _ID, nullable=False),
        sa.ForeignKeyConstraint(["adr_id"], ["adr.id"], name="fk_adr_supersedes_adr"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["adr.id"], name="fk_adr_supersedes_supersedes"),
        sa.PrimaryKeyConstraint("adr_id", "supersedes_id"),
        **_MYSQL,
    )

    op.create_table(
        "agent_pattern_composes",
        sa.Column("pattern_name", sa.String(length=128), nullable=False),
        sa.Column("discipline_name", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["pattern_name"], ["agent_pattern.name"], name="fk_pattern_composes_pattern"
        ),
        sa.ForeignKeyConstraint(
            ["discipline_name"],
            ["agent_discipline.name"],
            name="fk_pattern_composes_discipline",
        ),
        sa.PrimaryKeyConstraint("pattern_name", "discipline_name"),
        **_MYSQL,
    )


def downgrade() -> None:
    """Drop the seven tables in reverse FK order.

    Join tables first (they reference the parent tables), then the parents.
    """
    op.drop_table("agent_pattern_composes")
    op.drop_table("adr_supersedes")
    op.drop_table("task_blocked_by")
    op.drop_table("agent_discipline")
    op.drop_table("agent_pattern")
    op.drop_table("adr")
    op.drop_table("task")
