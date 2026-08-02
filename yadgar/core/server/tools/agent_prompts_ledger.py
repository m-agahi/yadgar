# SPDX-License-Identifier: Apache-2.0
"""Agent prompt ledger MCP tools — Car I.

Spine task-table-refactor-2026-07-29, Car I: NEW tools agent_prompt_list,
agent_prompt_get. The legacy TOC machinery (`_TOC_ROW_RE`, `_upsert_toc_row`,
`_set_toc_row_count`, the `agent-prompt-toc` page, the `%10` throttle) is
deleted — D40 makes the SQL `uses` column the reader. `SELECT ... ORDER BY
uses DESC` is the reader.

D3: reach always global. D40: uses is a plain SQL integer column.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar.core.server._app import _tool

logger = logging.getLogger(__name__)


@_tool()
def agent_prompt_list(
    status: str | None = None,
    directory: str | None = None,
) -> list[dict]:
    """List agent prompts, sorted by uses descending (D40).

    Args:
        status: Optional filter — 'active' or 'archived'.
        directory: Absolute project path (unused; agent_prompt is global).

    Returns:
        list of {title, kind, purpose, uses, ...}
    """
    storage = _get_storage()
    return storage.list_agent_prompt_rows(status=status)


@_tool()
def agent_prompt_get(pattern: str, directory: str | None = None) -> dict:
    """Fetch one agent prompt by pattern key (legacy name; stored as `title`).

    Args:
        pattern: The agent_prompt pattern key (== title in the ledger).
        directory: Absolute project path (unused).

    Returns:
        {title, kind, purpose, uses, ...} or {} if not found.
    """
    storage = _get_storage()
    return storage.get_agent_prompt_row(title=pattern)
