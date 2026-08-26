"""Car C11-#88: add ``last_validated_at`` column to ``project`` table (task #88).

Project rows are created once and never re-validated. After 81 days of silent
drift on the canonical repo, this column makes the staleness computable.

The column is added NULLABLE, then immediately backfilled — so no row is left
NULL by THIS migration. NULL remains meaningful afterwards ("never validated")
and ``list_stale_projects`` still treats it as stale, but the only way to
reach it now is a row inserted outside ``create_project_row``.

BACKFILL (deliberate — the column is nullable, the rows are not left NULL):

  UPDATE project SET last_validated_at = CURRENT_TIMESTAMP WHERE last_validated_at IS NULL

Treat every existing project as freshly validated NOW so the threshold does
NOT trip on day-zero after deploy. Stale drift was the SYMPTOM; the threshold
is what kills the *next* cycle of drift.

WHAT REFRESHES THE COLUMN (corrected by task 384)
-------------------------------------------------
New rows are stamped by ``create_project_row``. Refreshes come from
``MariaStorageEngine.assert_project_registered`` — the registry guard inside
``create_task_row`` / ``create_adr_row``. This docstring originally named
``admin_exec.project_registry._ensure_project_exists_async``, which had no
call site and has since been deleted; had it shipped that way, the backfill
below would have been the LAST write to the column and every project would
have reported stale from day 91 onward, forever.

NEW READ SURFACE: ``yadgar project list --stale`` (C11) — also routed through
``list_stale_projects`` admin op, which filters on this column.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "005_project_last_validated"
down_revision: str | None = "004_agent_pattern_model_client"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column, then backfill existing rows with CURRENT_TIMESTAMP.

    Order is load-bearing: the backfill assumes the column exists. A second
    ``UPDATE`` on a not-yet-existent column would error 1054.
    """
    op.execute("ALTER TABLE project ADD COLUMN last_validated_at DATETIME NULL AFTER created_at")
    op.execute(
        "UPDATE project SET last_validated_at = CURRENT_TIMESTAMP WHERE last_validated_at IS NULL"
    )


def downgrade() -> None:
    """Drop the column. Reversible on an empty / non-running deployment only.

    Reversible in the ordinary sense — a column drop does not destroy data
    that lives elsewhere. The backfill values are dropped with the column;
    re-running ``upgrade`` then re-stamps them.
    """
    op.execute("ALTER TABLE project DROP COLUMN last_validated_at")
