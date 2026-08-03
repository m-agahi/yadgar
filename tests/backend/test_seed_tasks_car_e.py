# SPDX-License-Identifier: Apache-2.0
"""RED tests for Car E — task seed + hook rewire.

Spine task-table-refactor-2026-07-29, Car E:
- One-shot admin op: seed the `task` table from existing
  `{project}-task-list` wiki pages (D35b).
- SessionStart/stop-hook rewire: task_list reads from the ledger
  table, not the markdown page.
- D11 prefix reconciliation: task IDs carry the origin prefix
  `[local-id]` so cross-session IDs survive the restore-nudge.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_storage():
    s = MagicMock()
    return s


def test_seed_tasks_from_task_list_page(mock_storage) -> None:
    """seed_tasks_from_page reads the markdown task-list and seeds rows."""
    from yadgar.backend.admin_exec.seed_ledger import seed_tasks_from_page

    # Per-task section count must equal seeded rows per project (D35c).
    mock_storage.get_wiki_page_by_slug.return_value = {
        "content": "## task:0001\n\nfirst task\n\n## task:0002\n\nsecond task\n",
        "slug": "m-agahi_yadgar_task-list",
    }
    mock_storage.list_task_rows.return_value = []

    with patch(
        "yadgar.backend.admin_exec.seed_ledger._get_storage",
        return_value=mock_storage,
    ):
        result = seed_tasks_from_page(
            directory="/home/max/git/yadgar",
            project_id="m-agahi/yadgar",
        )

    assert result["seeded"] == 2
    assert result["skipped"] == 0


def test_seed_tasks_idempotent(mock_storage) -> None:
    """Re-running the task seed converges."""
    from yadgar.backend.admin_exec.seed_ledger import seed_tasks_from_page

    mock_storage.get_wiki_page_by_slug.return_value = {
        "content": "## task:0001\n\nfirst task\n",
        "slug": "m-agahi_yadgar_task-list",
    }
    # id=999 (≠ section number 0001) PROVES dedup is by body_slug, not id.
    # Seed computes slug "m-agahi_yadgar_task-0001" for section 0001.
    mock_storage.list_task_rows.return_value = [
        {"id": 999, "project_id": "m-agahi/yadgar", "body_slug": "m-agahi_yadgar_task-0001"},
    ]

    with patch(
        "yadgar.backend.admin_exec.seed_ledger._get_storage",
        return_value=mock_storage,
    ):
        result = seed_tasks_from_page(
            directory="/home/max/git/yadgar",
            project_id="m-agahi/yadgar",
        )

    assert result["seeded"] == 0
    assert result["skipped"] == 1


def test_task_list_restore_nudge_uses_ledger() -> None:
    """The SessionStart restore-nudge reads open tasks from the ledger."""
    from yadgar.core.server.tools.task import task_list

    mock_storage = MagicMock()
    mock_storage.list_task_rows.return_value = [
        {"number": 1, "title": "spine plan", "status": "pending"},
        {"number": 2, "title": "another task", "status": "in_progress"},
    ]

    with patch(
        "yadgar.core.server.tools.task._get_storage",
        return_value=mock_storage,
    ):
        result = task_list(project_id="m-agahi/yadgar")

    # D37: open-only by default — the status filter is applied
    call_kwargs = mock_storage.list_task_rows.call_args.kwargs
    assert call_kwargs.get("status") == ["pending", "in_progress"]
    assert len(result) == 2


def test_d11_task_id_prefix_format() -> None:
    """D11: task IDs carry the [local-id] prefix for cross-session survival."""
    # The harness renders tasks as "[status] [id] subject". D11 says
    # the [id] must be the prefix-reconciled task number, not a fresh
    # session handle. The test pins the formatter.
    from yadgar.core.server.tools.task import _format_task_id

    assert _format_task_id(42) == "[42]"
    assert _format_task_id(1) == "[1]"
