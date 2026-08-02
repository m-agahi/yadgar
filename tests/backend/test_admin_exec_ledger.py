# SPDX-License-Identifier: Apache-2.0
"""RED tests for Car B — ledger backend ops.

Spine task-table-refactor-2026-07-29, Car B: backend ops + cache for the
spine ledger tables (task, adr, agent_prompt, runtime_config).

Each op is an undecorated ``(payload: dict) -> dict`` function that
forwards from core via `_forward_admin`. Mirrors the pattern in
``admin_exec/blocks.py``.

These tests validate the op surface and payload handling. Storage-layer
correctness is validated separately in tests/_shared/test_ledger_d31_allocation.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_storage():
    """Mock storage that records calls and returns canned data."""
    s = MagicMock()
    return s


def test_task_create_op_calls_storage(mock_storage) -> None:
    """ledger_task_create forwards to storage with correct payload mapping."""
    from yadgar.backend.admin_exec.ledger import ledger_task_create

    mock_storage.create_task.return_value = {
        "id": 1,
        "project_id": "m-agahi/yadgar",
        "origin": "yadgar",
        "number": 42,
        "title": "test task",
        "status": "pending",
        "state": "open",
    }

    with patch(
        "yadgar.backend.admin_exec.ledger._get_storage",
        return_value=mock_storage,
    ):
        result = ledger_task_create(
            {
                "project_id": "m-agahi/yadgar",
                "origin": "yadgar",
                "title": "test task",
            }
        )

    assert result["id"] == 1
    assert result["number"] == 42
    mock_storage.create_task.assert_called_once()


def test_task_create_op_returns_error_on_exception(mock_storage) -> None:
    """ledger_task_create returns {ok: False, error: ...} on storage failure."""
    from yadgar.backend.admin_exec.ledger import ledger_task_create

    mock_storage.create_task.side_effect = RuntimeError("db down")

    with patch(
        "yadgar.backend.admin_exec.ledger._get_storage",
        return_value=mock_storage,
    ):
        result = ledger_task_create({"title": "x"})

    assert result["ok"] is False
    assert "db down" in result["error"]


def test_adr_add_op_calls_storage(mock_storage) -> None:
    """ledger_adr_add forwards to storage with correct payload mapping."""
    from yadgar.backend.admin_exec.ledger import ledger_adr_add

    mock_storage.add_adr.return_value = {
        "id": 5,
        "project_id": "m-agahi/yadgar",
        "number": 194,
        "title": "spine plan",
        "status": "accepted",
    }

    with patch(
        "yadgar.backend.admin_exec.ledger._get_storage",
        return_value=mock_storage,
    ):
        result = ledger_adr_add(
            {
                "project_id": "m-agahi/yadgar",
                "title": "spine plan",
                "context": "...",
                "decision": "...",
            }
        )

    assert result["id"] == 5
    assert result["number"] == 194
    mock_storage.add_adr.assert_called_once()


def test_agent_prompt_save_op_calls_storage(mock_storage) -> None:
    """ledger_agent_prompt_save forwards to storage."""
    from yadgar.backend.admin_exec.ledger import ledger_agent_prompt_save

    mock_storage.save_agent_prompt.return_value = {
        "id": 1,
        "pattern": "dispatch-fix-bug",
        "uses": 0,
    }

    with patch(
        "yadgar.backend.admin_exec.ledger._get_storage",
        return_value=mock_storage,
    ):
        result = ledger_agent_prompt_save(
            {
                "pattern": "dispatch-fix-bug",
                "content": "test prompt",
                "directory": "/home/max/git/yadgar",
            }
        )

    assert result["id"] == 1
    mock_storage.save_agent_prompt.assert_called_once()


def test_runtime_config_set_op_calls_storage(mock_storage) -> None:
    """ledger_runtime_config_set forwards to storage — replaces SurrealDB path."""
    from yadgar.backend.admin_exec.ledger import ledger_runtime_config_set

    mock_storage.set_config_row.return_value = {"ok": True, "key": "test.knob"}

    with patch(
        "yadgar.backend.admin_exec.ledger._get_storage",
        return_value=mock_storage,
    ):
        result = ledger_runtime_config_set(
            {
                "key": "test.knob",
                "value": "42",
                "directory": "/home/max/git/yadgar",
            }
        )

    assert result["ok"] is True
    mock_storage.set_config_row.assert_called_once()
