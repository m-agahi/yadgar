# SPDX-License-Identifier: Apache-2.0
"""Tests for Car D — task tools.

Spine task-table-refactor-2026-07-29, Car D: NEW MCP tools task_list,
task_get, task_write. id is the AUTO_INCREMENT PK and also the semantic
number — no separate allocation step.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_storage():
    s = MagicMock()
    return s


def test_task_write_creates_task(mock_storage) -> None:
    """task_write creates the row. id is the AUTO_INCREMENT number."""
    from yadgar.core.server.tools.task import task_write

    mock_storage.create_task_row.return_value = {
        "id": 42,
        "project_id": "m-agahi/yadgar",
        "title": "spine plan",
        "status": "pending",
        "state": "open",
    }

    with patch(
        "yadgar.core.server.tools.task._get_storage",
        return_value=mock_storage,
    ):
        result = task_write(
            project_id="m-agahi/yadgar",
            title="spine plan",
            active_form="writing spine plan",
        )

    assert result["id"] == 42
    mock_storage.create_task_row.assert_called_once()
    call_kwargs = mock_storage.create_task_row.call_args.kwargs
    assert call_kwargs["title"] == "spine plan"
    assert call_kwargs["project_id"] == "m-agahi/yadgar"


def test_task_list_defaults_to_open_only(mock_storage) -> None:
    """task_list defaults to status IN (pending, in_progress) per D37."""
    from yadgar.core.server.tools.task import task_list

    mock_storage.list_task_rows.return_value = [
        {"id": 1, "status": "pending", "title": "open task"},
    ]

    with patch(
        "yadgar.core.server.tools.task._get_storage",
        return_value=mock_storage,
    ):
        result = task_list(project_id="m-agahi/yadgar")

    call_kwargs = mock_storage.list_task_rows.call_args.kwargs
    assert call_kwargs.get("status") == ["pending", "in_progress"]
    assert result == mock_storage.list_task_rows.return_value


def test_task_list_can_include_closed(mock_storage) -> None:
    """task_list with include_closed=True returns all statuses."""
    from yadgar.core.server.tools.task import task_list

    mock_storage.list_task_rows.return_value = []

    with patch(
        "yadgar.core.server.tools.task._get_storage",
        return_value=mock_storage,
    ):
        task_list(project_id="m-agahi/yadgar", include_closed=True)

    call_kwargs = mock_storage.list_task_rows.call_args.kwargs
    assert call_kwargs.get("status") is None


def test_task_get_returns_single_row(mock_storage) -> None:
    """task_get fetches one row by (project_id, number)."""
    from yadgar.core.server.tools.task import task_get

    mock_storage.get_task_row.return_value = {
        "id": 42,
        "project_id": "m-agahi/yadgar",
        "title": "spine plan",
    }

    with patch(
        "yadgar.core.server.tools.task._get_storage",
        return_value=mock_storage,
    ):
        result = task_get(project_id="m-agahi/yadgar", number=42)

    mock_storage.get_task_row.assert_called_once_with(
        project_id="m-agahi/yadgar", number=42, directory=None
    )
    assert result["id"] == 42


def test_task_write_rejects_empty_title(mock_storage) -> None:
    """task_write rejects an empty title with a clear error."""
    from yadgar.core.server.tools.task import task_write

    with patch(
        "yadgar.core.server.tools.task._get_storage",
        return_value=mock_storage,
    ):
        result = task_write(project_id="m-agahi/yadgar", title="")

    assert result["ok"] is False
    assert "title" in result.get("error", "").lower()
    mock_storage.create_task_row.assert_not_called()


def test_task_write_rejects_oversized_title(mock_storage) -> None:
    """task_write rejects a title > 200 chars (D12)."""
    from yadgar.core.server.tools.task import task_write

    long_title = "x" * 201
    with patch(
        "yadgar.core.server.tools.task._get_storage",
        return_value=mock_storage,
    ):
        result = task_write(project_id="m-agahi/yadgar", title=long_title)

    assert result["ok"] is False
    mock_storage.create_task_row.assert_not_called()
