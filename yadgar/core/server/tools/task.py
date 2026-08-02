# SPDX-License-Identifier: Apache-2.0
"""Task ledger MCP tool registrations (Car D).

Three tools:
  task_list  — list tasks for a project_id (default: open-only per D37)
  task_get   — fetch one task by (project_id, number)
  task_write — create or update a task row; number allocated via D31

All tools delegate to StorageEngine via yadgar.server.lifecycle._get_storage().
D20: every row access goes through _LedgerMixin (chokepoint guard).
D31: semantic number allocated by SELECT MAX(number)+1 FOR UPDATE inside
the same transaction as the INSERT.
D37: task_list defaults to open-only (status IN pending, in_progress).

Error model: {ok: False, error: "..."} — never raise.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar.core.server._app import _tool

logger = logging.getLogger(__name__)

_OPEN_STATUSES = ("pending", "in_progress")
_TITLE_MAX = 200  # D12


def _format_task_id(number: int) -> str:
    """D11 — format a task number as the harness-readable [id] prefix.

    The harness renders tasks as "[status] [id] subject". D11 says the
    [id] must be the prefix-reconciled task number, not a fresh
    session handle. Foreign projects get the [owner/repo/id] form.
    """
    return f"[{number}]"


@observe(tier="hot", metric="tools.task._validate_title")
def _validate_title(title: object) -> dict | None:
    """Return an error dict if title is invalid, else None.

    Strict types: title MUST be a non-empty string. No implicit coercion
    (int/float/None all rejected with a clear error). Python's automatic
    type casting would let `42` become `"42"` via str() — we reject
    it explicitly so callers get a clear error instead of a silent
    type-confused row.
    """
    if not isinstance(title, str):
        return {
            "ok": False,
            "error": f"title must be a string, got {type(title).__name__}",
        }
    if not title or not title.strip():
        return {"ok": False, "error": "title is required"}
    if len(title) > _TITLE_MAX:
        return {
            "ok": False,
            "error": f"title exceeds {_TITLE_MAX} chars (D12)",
        }
    return None


def _validate_project_id(project_id: object) -> dict | None:
    """Strict-type check: project_id MUST be a non-empty string."""
    if not isinstance(project_id, str):
        return {
            "ok": False,
            "error": f"project_id must be a string, got {type(project_id).__name__}",
        }
    if not project_id or not project_id.strip():
        return {"ok": False, "error": "project_id is required"}
    return None


@_tool(power=True)
def task_write(
    project_id: str,
    title: str,
    active_form: str | None = None,
    state: str = "open",
    plan_path: str | None = None,
    body_slug: str | None = None,
    directory: str | None = None,
) -> dict:
    """Create a task row. Number is allocated by D31.

    Args:
        project_id: Git-derived identity key (D13/D14).
        title: Task title, <= 200 chars (D12).
        active_form: Present-continuous form for harness display.
        state: open | planned | spike | needs_decision | built_unverified (D36).
        plan_path: Path to the plan doc if one exists (D36).
        body_slug: Wiki page slug for the task body (D4).
        directory: Absolute project path for directory guard.
    """
    err = _validate_project_id(project_id)
    if err is not None:
        return err
    err = _validate_title(title)
    if err is not None:
        return err
    storage = _get_storage()
    try:
        # D31: allocate number inside the same transaction as the INSERT.
        number = storage.allocate_task_number(project_id=project_id, origin="yadgar")
        return storage.create_task_row(
            project_id=project_id,
            origin="yadgar",
            number=number,
            title=title,
            active_form=active_form,
            state=state,
            plan_path=plan_path,
            body_slug=body_slug,
            directory=directory,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("task_write error title=%s: %s", title, exc)
        return {"ok": False, "error": str(exc)}


@_tool()
def task_list(
    project_id: str,
    include_closed: bool = False,
    directory: str | None = None,
) -> list[dict]:
    """List tasks for a project. Default: open-only (D37).

    Args:
        project_id: Git-derived identity key.
        include_closed: When True, include completed/archived rows.
        directory: Absolute project path for directory guard.
    """
    storage = _get_storage()
    if include_closed:
        return storage.list_task_rows(project_id=project_id, status=None, directory=directory)
    return storage.list_task_rows(
        project_id=project_id, status=list(_OPEN_STATUSES), directory=directory
    )


@_tool()
def task_get(
    project_id: str,
    number: int,
    directory: str | None = None,
) -> dict:
    """Fetch one task by (project_id, number).

    Args:
        project_id: Git-derived identity key.
        number: Semantic task number (allocated by D31).
        directory: Absolute project path for directory guard.
    """
    storage = _get_storage()
    return storage.get_task_row(project_id=project_id, number=number, directory=directory)
