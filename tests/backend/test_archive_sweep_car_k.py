# SPDX-License-Identifier: Apache-2.0
"""RED tests for Car K — nightly archive sweep.

Spine task-table-refactor-2026-07-29, Car K: nightly archive sweep,
policy-dispatched. Archived rows persist in the table but are
excluded from recall (D22 + D38) and from the default task_list
read (D37).

Policy:
  - task: completed → archived after 90 days
  - task: completed → archived immediately if body_slug is null
  - agent_prompt: archived after 365 days of no use
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


def test_k_archive_completed_tasks_after_90_days() -> None:
    """Completed tasks older than 90 days are archived."""
    from yadgar.backend.consolidation.archive_sweep import (
        should_archive_completed_task,
    )

    old_date = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    recent_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    # body_slug must be provided to skip the immediate-archive rule
    assert should_archive_completed_task(completed_at=old_date, body_slug="some-slug") is True
    assert should_archive_completed_task(completed_at=recent_date, body_slug="some-slug") is False


def test_k_archive_completed_task_no_body_immediately() -> None:
    """Completed tasks with no body_slug are archived immediately (D38)."""
    from yadgar.backend.consolidation.archive_sweep import (
        should_archive_completed_task,
    )

    # No completed_at but body_slug is None → archive immediately
    assert should_archive_completed_task(completed_at=None, body_slug=None) is True


def test_k_archive_sweep_updates_status() -> None:
    """The sweep calls storage to flip status to 'archived'."""
    from yadgar.backend.consolidation.archive_sweep import run_archive_sweep

    mock_storage = MagicMock()
    mock_storage.list_task_rows_all_projects.return_value = [
        {
            "id": 1,
            "project_id": "m-agahi/yadgar",
            "number": 42,
            "status": "completed",
            "modified_at": (datetime.now(timezone.utc) - timedelta(days=100)).isoformat(),
        },
    ]

    with patch(
        "yadgar.backend.consolidation.archive_sweep._get_storage",
        return_value=mock_storage,
    ):
        result = run_archive_sweep()

    assert result["archived"] == 1
    mock_storage.update_task_status.assert_called_once()


def test_k_archive_sweep_no_op_when_nothing_to_archive() -> None:
    """No completed tasks → sweep is a no-op."""
    from yadgar.backend.consolidation.archive_sweep import run_archive_sweep

    mock_storage = MagicMock()
    mock_storage.list_task_rows_all_projects.return_value = [
        {"id": 1, "status": "pending"},
        {"id": 2, "status": "in_progress"},
    ]

    with patch(
        "yadgar.backend.consolidation.archive_sweep._get_storage",
        return_value=mock_storage,
    ):
        result = run_archive_sweep()

    assert result["archived"] == 0
    mock_storage.update_task_status.assert_not_called()
