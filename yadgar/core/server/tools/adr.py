# ruff: noqa: PLR0913  — adr_add / _build_adr_body intentionally have 11 params (ADR schema
#   has 10 mandatory content fields; FastMCP derives JSON Schema from flat keyword args).
#   Collapsing into **kwargs loses schema enforcement. PERMANENT — see .complexity-allowlist.json.
"""ADR (Architecture Decision Record) MCP tool registrations.

Spine Car F: adr_add, adr_list, adr_get re-pointed from the legacy markdown
index parser to the SQL ledger table. id is the AUTO_INCREMENT PK and also
the semantic number — no separate number column.

Three tools:
  adr_add  — create ledger row + wiki page body (D4), return id as number
  adr_get  — fetch one ADR by (project_id, number)
  adr_list — list ADRs for a project, optional status filter
"""

from __future__ import annotations

import logging
import re
import threading

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar.core.server._app import _tool
from yadgar.core.server.tools.adr_index import (
    _ADR_HEADER_RE,
    _ADR_PAGE_SLUG_RE,
    adr_index_slug,
    adr_log_slug,
    adr_page_slug,
    parse_adr_ids,
)
from yadgar.core.server.tools.adr_render import (
    _REQUIRED_FIELDS,
    _VALID_STATUSES,
    _adr_tags,
    _build_adr_body,
    _canonical_adr_payload,
    _flip_superseded_target,
    _parse_supersedes,
)

logger = logging.getLogger(__name__)


@observe(tier="stage")
def _validate_subsystem(subsystem: object) -> str:
    """Car H D28: subsystem is free-form. Returns the string or empty."""
    if not isinstance(subsystem, str):
        return ""
    return subsystem.strip()[:128]


def _should_regenerate_rollup() -> bool:
    """Car H D29: rollup pages regenerate on every ADR write."""
    return True


# ── Per-project ADR write lock ─────────────────────────────────────────────────
_ADR_LOG_LOCKS: dict[str, threading.Lock] = {}
_ADR_LOG_LOCKS_GUARD = threading.Lock()


@observe(exempt="trivial dict-lookup; no I/O, no external call, no error branch worth spanning")
def _adr_log_lock(resolved: str) -> threading.Lock:
    """Return the per-project threading.Lock for the ADR write sequence."""
    with _ADR_LOG_LOCKS_GUARD:
        if resolved not in _ADR_LOG_LOCKS:
            _ADR_LOG_LOCKS[resolved] = threading.Lock()
        return _ADR_LOG_LOCKS[resolved]


_FATAL_WRITE_REASONS: frozenset[str] = frozenset(
    {"duplicate_detected", "rejected", "content_too_large", "invalid_unicode_surrogates"}
)


@observe(exempt="trivial dict-field predicate; no I/O, no error branch worth spanning")
def _write_ok(result: dict) -> bool:
    """True when a canonical write committed OR is safely queued (converges)."""
    if result.get("stored") is not False:
        return True
    if result.get("queued"):
        return True
    reason = str(result.get("reason", ""))
    if reason.startswith("blocked_by_policy"):
        return False
    return reason not in _FATAL_WRITE_REASONS if reason else False


# ── Tools ──────────────────────────────────────────────────────────────────────


@_tool(power=True)
def adr_add(
    project_id: str,
    title: str,
    status: str,
    date: str,
    context: str,
    decision: str,
    rationale: str,
    alternatives: str,
    consequences: str,
    revisit_trigger: str,
    supersedes: str,
    directory: str | None = None,
) -> dict:
    """Create a new ADR — ledger row + wiki page body (D4).

    Spine Car F: id is the AUTO_INCREMENT PK and also the semantic number.
    The wiki page body is written to SurrealDB with slug
    {project_id}_adr-{id} per D32.

    Args:
        project_id: Git-derived identity key (D13/D14).
        title: Short human-readable title.
        status: open|accepted|superseded|rejected|deprecated.
        date: ISO date string.
        context/decision/rationale/alternatives/consequences/
        revisit_trigger: ADR body fields.
        supersedes: "none" or comma-separated ADR IDs.
        directory: Absolute project path (back-compat).
    """
    for field, val in {
        "title": title,
        "status": status,
        "date": date,
        "context": context,
        "decision": decision,
        "rationale": rationale,
    }.items():
        if not isinstance(val, str):
            return {
                "ok": False,
                "error": f"{field} must be a string, got {type(val).__name__}",
            }
        if not val.strip():
            return {"ok": False, "error": f"missing required field: {field!r}"}
    if not isinstance(status, str) or status not in {
        "open",
        "accepted",
        "superseded",
        "rejected",
        "deprecated",
        "archived",
    }:
        return {"ok": False, "error": f"invalid status {status!r}"}

    storage = _get_storage()
    try:
        row = storage.create_adr_row(
            project_id=project_id,
            origin="yadgar",
            title=title,
            status=status,
            date=date,
        )
        number = row["id"]
        adr_id = f"ADR-{number:04d}"
        body_slug = f"{project_id.replace('/', '_')}_adr-{number}"

        body = _build_adr_body(
            adr_id=adr_id,
            title=title,
            status=status,
            date=date,
            context=context,
            decision=decision,
            rationale=rationale,
            alternatives=alternatives,
            consequences=consequences,
            revisit_trigger=revisit_trigger,
            supersedes=supersedes,
        )
        payload = _canonical_adr_payload(
            slug=body_slug,
            content=body,
            category="decision",
<<<<<<< HEAD
            tags=_adr_tags(adr_id, status),
            directory=directory or "",
        )
        from yadgar.core.server.tools.wiki import _wiki_write_canonical

        write_result = _wiki_write_canonical(payload, wait=True)
        if not _write_ok(write_result):
            return {"ok": False, "error": f"wiki body write failed: {write_result.get('reason')}"}

        storage.set_adr_body_slug(project_id=project_id, number=number, body_slug=body_slug)
        return {
            "adr_id": adr_id,
            "number": number,
            "status": row["status"],
            "title": row["title"],
            "body_slug": body_slug,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("adr_add error title=%s: %s", title, exc)
        return {"ok": False, "error": str(exc)}


@_tool(power=True)
def adr_get(
    project_id: str,
    adr_id: str,
    directory: str | None = None,
) -> dict:
    """Read a single ADR by formatted ID — Car F ledger-backed.

    Args:
        project_id: Git-derived identity key (D13/D14).
        adr_id: "ADR-NNNN" (case-insensitive; "adr-1" / "1" also accepted).
        directory: Absolute project path (kept for back-compat).

    Returns:
        {adr_id, status, title, body_slug, ...} or {error: "..."} if absent.
    """
    if not isinstance(adr_id, str):
        return {"error": f"adr_id must be a string, got {type(adr_id).__name__}"}
    if not isinstance(project_id, str):
        return {"error": f"project_id must be a string, got {type(project_id).__name__}"}

    m = re.search(r"(\d+)", adr_id or "")
    if not m:
        return {"error": f"invalid adr_id {adr_id!r}; expected 'ADR-NNNN'"}
    number_str = m.group(1)
    if not number_str.isdigit():
        return {"error": f"invalid adr_id number {adr_id!r}; must be decimal integer"}
    number = int(number_str)

    storage = _get_storage()
    row = storage.get_adr_row(project_id=project_id, number=number)
    if not row:
        return {"error": f"ADR not found: ADR-{number:04d}"}

    row["adr_id"] = f"ADR-{number:04d}"
    return row


@_tool(power=True)
def adr_list(
    project_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    directory: str | None = None,
) -> list[dict]:
    """List ADRs from the ledger — Car F re-pointed.

    Args:
        project_id: Git-derived identity key (D13/D14).
        status: Optional filter (open/accepted/superseded/rejected/deprecated).
        limit: Max ADRs returned per page (default 50). <= 0 means no limit.
        offset: 0-based index of the first ADR returned (default 0).
        directory: Absolute project path (kept for back-compat).

    Returns:
        list of {adr_id, status, date, title, ...}
    """
    storage = _get_storage()
    rows = storage.list_adr_rows(project_id=project_id, status=status, limit=limit, offset=offset)
    adrs = []
    for r in rows:
        r["adr_id"] = f"ADR-{r['id']:04d}"
        adrs.append(r)
    return adrs


# ── Backward-compatible re-exports ────────────────────────────────────────────
__all__ = [
    "adr_add",
    "adr_get",
    "adr_list",
    "_ADR_HEADER_RE",
    "_ADR_PAGE_SLUG_RE",
    "adr_index_slug",
    "adr_log_slug",
    "adr_page_slug",
    "parse_adr_ids",
    "_REQUIRED_FIELDS",
    "_VALID_STATUSES",
    "_adr_tags",
    "_build_adr_body",
    "_canonical_adr_payload",
    "_flip_superseded_target",
    "_parse_supersedes",
    "_ADR_LOG_LOCKS",
    "_ADR_LOG_LOCKS_GUARD",
    "_FATAL_WRITE_REASONS",
    "_adr_log_lock",
    "_write_ok",
    "_validate_subsystem",
    "_should_regenerate_rollup",
]
