# SPDX-License-Identifier: Apache-2.0
"""RED tests for Car I — agent_prompt ledger tools.

Spine task-table-refactor-2026-07-29, Car I: NEW MCP tools
agent_prompt_list, agent_prompt_get. The agent-prompt TOC machinery
(`_TOC_ROW_RE`, `_TOC_SLUG`, `_upsert_toc_row`, `_set_toc_row_count`,
the `agent-prompt-toc` page) is deleted — D40 makes the SQL `uses`
column the reader, no dedicated reader function needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_storage():
    s = MagicMock()
    return s


def test_agent_prompt_list_returns_rows_sorted_by_uses(mock_storage) -> None:
    """agent_prompt_list returns rows sorted by uses descending."""
    from yadgar.core.server.tools.agent_prompts_ledger import agent_prompt_list

    mock_storage.list_agent_prompt_rows.return_value = [
        {"id": 1, "pattern": "a", "uses": 10},
        {"id": 2, "pattern": "b", "uses": 5},
    ]

    with patch(
        "yadgar.core.server.tools.agent_prompts_ledger._get_storage",
        return_value=mock_storage,
    ):
        result = agent_prompt_list()

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["uses"] >= result[1]["uses"]


def test_agent_prompt_get_returns_single_row(mock_storage) -> None:
    """agent_prompt_get fetches one row by pattern."""
    from yadgar.core.server.tools.agent_prompts_ledger import agent_prompt_get

    mock_storage.get_agent_prompt_row.return_value = {
        "id": 1,
        "pattern": "dispatch-fix-bug",
        "uses": 42,
        "purpose": "fix a bug",
    }

    with patch(
        "yadgar.core.server.tools.agent_prompts_ledger._get_storage",
        return_value=mock_storage,
    ):
        result = agent_prompt_get(pattern="dispatch-fix-bug")

    assert result["pattern"] == "dispatch-fix-bug"
    assert result["uses"] == 42


def test_agent_prompt_get_returns_empty_for_missing(mock_storage) -> None:
    """agent_prompt_get returns {} when the pattern doesn't exist."""
    from yadgar.core.server.tools.agent_prompts_ledger import agent_prompt_get

    mock_storage.get_agent_prompt_row.return_value = {}

    with patch(
        "yadgar.core.server.tools.agent_prompts_ledger._get_storage",
        return_value=mock_storage,
    ):
        result = agent_prompt_get(pattern="nonexistent")

    assert result == {}


def test_toc_machinery_deleted() -> None:
    """The legacy TOC regex/upsert functions are deleted."""
    from yadgar.backend.admin_exec import wiki
    from yadgar.core.server.tools import agent_prompts

    assert not hasattr(wiki, "_TOC_ROW_RE"), "TOC regex should be deleted"
    assert not hasattr(wiki, "_upsert_toc_row"), "TOC upsert should be deleted"
    assert not hasattr(agent_prompts, "_TOC_SLUG"), "TOC slug should be deleted"
