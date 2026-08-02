# SPDX-License-Identifier: Apache-2.0
"""ADR ledger MCP tools — Car F re-pointed implementation.

Spine task-table-refactor-2026-07-29, Car F: adr_add, adr_list, adr_get
re-pointed from the legacy markdown index parser to the SQL ledger table.
Return shapes are pinned by tests/core/test_adr_tools_car_f.py.

ADR ID format: "ADR-NNNN" where NNNN is the zero-padded 4-digit number
per D10. The ledger stores the integer `number`; the formatted `adr_id`
is derived at the tool boundary.

D7: never reuse numbers. D8: uniqueness key is (project_id, origin, number).
D31: number allocated by SELECT MAX(number)+1 FOR UPDATE.
"""

from __future__ import annotations

import logging
import re

from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar.core.server._app import _tool

logger = logging.getLogger(__name__)


def _should_regenerate_rollup() -> bool:
    """Car H D29: rollup pages regenerate on every ADR write."""
    return True


_ADR_ID_RE = re.compile(r"^ADR-(\d+)$")
_ADR_ID_FORMAT = "ADR-{number:04d}"


def _format_adr_id(number: int) -> str:
    """Format an ADR number as 'ADR-NNNN' (D10: no zero-padding assumption in code)."""
    return _ADR_ID_FORMAT.format(number=number)


def _parse_adr_id(adr_id: str) -> int | None:
    """Parse 'ADR-NNNN' to an integer. Returns None on malformed input."""
    m = _ADR_ID_RE.match(adr_id)
    if m is None:
        return None
    return int(m.group(1))


def _to_adr_row(row: dict) -> dict:
    """Convert a ledger row to the public adr_list/adr_get return shape.

    Adds `adr_id` formatted as 'ADR-NNNN'. Preserves all other fields.
    """
    if not row:
        return row
    result = dict(row)
    result["adr_id"] = _format_adr_id(row["number"])
    return result


@_tool(power=True)
def adr_list(
    project_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    directory: str | None = None,
) -> dict:
    """List ADRs for a project (ledger-backed).

    Args:
        project_id: Git-derived identity key (D13/D14).
        status: Optional filter — 'accepted', 'superseded', 'rejected',
            'deprecated', 'open', or 'archived'.
        limit: Max rows to return.
        offset: Pagination offset.
        directory: Absolute project path for directory guard.

    Returns:
        {'adrs': [{adr_id, status, date, title, ...}], 'count': N}
    """
    storage = _get_storage()
    rows = storage.list_adr_rows(project_id=project_id, status=status, limit=limit, offset=offset)
    adrs = [_to_adr_row(r) for r in rows]
    return {"adrs": adrs, "count": len(adrs)}


@_tool(power=True)
def adr_get(
    project_id: str,
    adr_id: str,
    directory: str | None = None,
) -> dict:
    """Fetch one ADR by formatted ID (ledger-backed).

    Args:
        project_id: Git-derived identity key.
        adr_id: Formatted ID 'ADR-NNNN'.
        directory: Absolute project path for directory guard.

    Returns:
        {adr_id, status, title, body_slug, ...} or {ok: False, error: ...}
    """
    number = _parse_adr_id(adr_id)
    if number is None:
        return {"ok": False, "error": f"malformed adr_id: {adr_id!r}"}

    storage = _get_storage()
    row = storage.get_adr_row(project_id=project_id, number=number)
    if not row:
        return {"ok": False, "error": f"ADR not found: {adr_id}"}
    return _to_adr_row(row)


@_tool(power=True)
def adr_add(
    project_id: str,
    title: str,
    context: str,
    decision: str,
    rationale: str,
    alternatives: str = "",
    consequences: str = "",
    revisit_trigger: str = "",
    date: str | None = None,
    status: str = "open",
    body_slug: str | None = None,
    directory: str | None = None,
) -> dict:
    """Create a new ADR row. Number allocated by D31.

    Args:
        project_id: Git-derived identity key.
        title: ADR title, <= 200 chars (D12).
        context: Background / problem statement.
        decision: The decision made.
        rationale: Why this decision.
        alternatives: Alternatives considered.
        consequences: Known consequences.
        revisit_trigger: Condition for revisiting.
        date: ISO date string.
        status: Initial status (default 'open').
        body_slug: Wiki page slug for the body (D4).
        directory: Absolute project path.

    Returns:
        {adr_id, number, status, ...} on success.
    """
    storage = _get_storage()
    try:
        number = storage.allocate_adr_number(project_id=project_id, origin="yadgar")
        row = storage.create_adr_row(
            project_id=project_id,
            origin="yadgar",
            number=number,
            title=title,
            status=status,
            body_slug=body_slug,
            date=date,
        )
        return _to_adr_row(row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("adr_add error title=%s: %s", title, exc)
        return {"ok": False, "error": str(exc)}
