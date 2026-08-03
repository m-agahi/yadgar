# SPDX-License-Identifier: Apache-2.0
"""Car K — nightly archive sweep.

Policy-dispatched archive of completed tasks and stale agent prompts.
Archived rows persist in the ledger table (D7 — never delete, archive
only) but are excluded from recall (D22) and from the default
task_list read (D37).

Policy:
  - task: completed → archived after 90 days
  - task: completed → archived immediately if body_slug is null
  - agent_prompt: archived after 365 days of no use (future car)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)

_TASK_ARCHIVE_AFTER_DAYS = 90
_NO_BODY_IMMEDIATE_ARCHIVE = True


@observe(tier="stage")
def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO datetime string. Returns None on failure."""
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):  # fmt: skip
        return None


@observe(tier="stage")
def should_archive_completed_task(
    *,
    completed_at: str | None,
    body_slug: str | None = None,
) -> bool:
    """Return True if a completed task should be archived.

    Policy:
      - completed > 90 days ago → archive
      - no body_slug → archive immediately (D38: body is retained
        for context but the row is archived; recall excludes it)
      - otherwise → keep
    """
    if body_slug is None and _NO_BODY_IMMEDIATE_ARCHIVE:
        return True
    dt = _parse_dt(completed_at)
    if dt is None:
        return False
    age = datetime.now(UTC) - dt
    return age.days >= _TASK_ARCHIVE_AFTER_DAYS


@observe(tier="boundary", metric="backend.archive_sweep.run")
def run_archive_sweep() -> dict:
    """Run the nightly archive sweep.

    Returns:
        {archived: N, scanned: M, skipped: K}
    """
    storage = _get_storage()
    # Read completed tasks across all projects (Car K cross-project sweep).
    completed = storage.list_task_rows_all_projects(status=["completed"])

    archived = 0
    skipped = 0
    for row in completed:
        if should_archive_completed_task(
            completed_at=row.get("modified_at"),
            body_slug=row.get("body_slug"),
        ):
            try:
                storage.update_task_status(
                    project_id=row["project_id"],
                    number=row["id"],
                    status="archived",
                )
                archived += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("archive_sweep failed for task %s: %s", row.get("id"), exc)
                skipped += 1
        else:
            skipped += 1

    return {
        "archived": archived,
        "scanned": len(completed),
        "skipped": skipped,
    }
