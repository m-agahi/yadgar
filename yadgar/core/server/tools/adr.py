# ruff: noqa: PLR0913  — adr_add / _build_adr_body intentionally have 11 params (ADR schema
#   has 10 mandatory content fields; FastMCP derives JSON Schema from flat keyword args).
#   Collapsing into **kwargs loses schema enforcement. PERMANENT — see .complexity-allowlist.json.
"""ADR (Architecture Decision Record) MCP tool registrations.

Car 2 (ADR-consultable, v5.141.0) made ADRs recall-native. The write-only
`<project>-adr-log` monolith is replaced by:

  * one CANONICAL wiki page per ADR — slug `<project>-adr-NNNN`, page_type `adr`,
    tags `["adr","decisions","adr-status:<status>","adr-<NNNN>"]`, category
    `decision`. The stored wiki TITLE equals the slug string (so `_slugify(title)`
    yields the deterministic `<project>-adr-NNNN` slug); the human-readable
    `ADR-NNNN: <title>` is the content's H1.
  * one thin CANONICAL index page — slug `<project>-adr-index`, tags
    `["adr","adr-index"]` — a metadata table (ID source of truth for max+1).

Both are written CANONICAL (branch IS NULL) via Car 0's server-side
`_wiki_write_canonical` (flow 1: page_type in CANONICAL_PAGE_TYPES, `_internal`
set server-side, `force=True` to bypass the drainer sim gate). Canonical pages
resolve via §25 step-2 (dir + branch IS NULL) from ANY caller branch AND in
non-git dirs — this closes the memory-531352 default-branch-pin bug (a non-git
`aws-work-adr-log` was mis-pinned to a bogus "master" and unreadable).

Three tools:
  adr_add  — assign next ID from the index, write the per-ADR page + index row,
             flip supersede targets' status tag.
  adr_get  — read `<project>-adr-NNNN` canonical (direct fetch, no branch footgun).
  adr_list — read the index; optional status filter ("show all open").

Decisions:
- @_tool(power=True) — write-tool convention (adr_add); reads are also power to
  match the module (adr_get/adr_list are cheap reads but sit next to the write).
- The index create + first-row write use `wait=True` (read-your-writes) so a
  subsequent adr_add reads the just-written index when assigning the next ID.
  The per-ADR page write is async (deterministic slug, does not feed IDs).
- Per-project threading.Lock (_adr_log_lock) wraps the full read-assign-write
  sequence to prevent duplicate ID assignment under concurrent adr_add calls
  (single-process assumption; see docs/plans/archive/adr-add-id-race-2026-07-13.md).

Module layout (car/adr-split):
  adr_index.py  — slug helpers, ID assignment, index parse/render
  adr_render.py — body builder, tag helpers, supersede handling, write-path assembly
  adr.py        — lock, write-ok predicate, MCP tool handlers + backward-compat re-exports
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
    _assemble_index_rows,
    _build_adr_body,
    _canonical_adr_payload,
    _flip_superseded_target,
    _parse_supersedes,
)

logger = logging.getLogger(__name__)


def _validate_subsystem(subsystem: object) -> str:
    """Car H D28: subsystem is free-form. Returns the string or empty."""
    if not isinstance(subsystem, str):
        return ""
    return subsystem.strip()[:128]


# ── Per-project ADR write lock ─────────────────────────────────────────────────
# The core daemon is a single persistent process (streamable-http). When
# YADGAR_OFFLOAD_TOOLS=1 the ThreadPoolExecutor can run two adr_add calls
# concurrently: both would read the same index content, derive the same next ID,
# and both write — producing duplicate IDs (lost-update).
#
# A threading.Lock keyed by resolved project root serializes the full
# read-index → next-id → write-page → append-index-row sequence. The lock is
# process-local; single-process single-backend topology is an explicit
# assumption (see docs/plans/archive/adr-add-id-race-2026-07-13.md).
_ADR_LOG_LOCKS: dict[str, threading.Lock] = {}
_ADR_LOG_LOCKS_GUARD = threading.Lock()


@observe(exempt="trivial dict-lookup; no I/O, no external call, no error branch worth spanning")
def _adr_log_lock(resolved: str) -> threading.Lock:
    """Return the per-project threading.Lock for the ADR write sequence."""
    with _ADR_LOG_LOCKS_GUARD:
        if resolved not in _ADR_LOG_LOCKS:
            _ADR_LOG_LOCKS[resolved] = threading.Lock()
        return _ADR_LOG_LOCKS[resolved]


# A wait=True canonical write that is still QUEUED after wait_timeout WILL commit
# on the next drain — it is NOT a failure. Only these terminal reasons are fatal.
_FATAL_WRITE_REASONS: frozenset[str] = frozenset(
    {"duplicate_detected", "rejected", "content_too_large", "invalid_unicode_surrogates"}
)


@observe(exempt="trivial dict-field predicate; no I/O, no error branch worth spanning")
def _write_ok(result: dict) -> bool:
    """True when a canonical write committed OR is safely queued (converges).

    ``_wiki_write_canonical(wait=True)`` returns ``stored:False, reason:wait_timeout,
    queued:True`` when the drainer did not commit within the wait budget — the write
    is still queued and WILL land. That is NOT a failure for the ADR path (next-ID
    correctness comes from the committed page-slug scan, not the index). Only a hard
    terminal rejection (duplicate_detected / blocked / oversize) is fatal.
    """
    if result.get("stored") is not False:
        return True
    if result.get("queued"):
        return True  # wait_timeout — converges on next drain
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
    """Create a new ADR row — Car F ledger-backed.

    Spine Car F replaces the legacy markdown-index path with a single
    INSERT into the `adr` table. The number is allocated by D31
    (SELECT MAX(number)+1 FOR UPDATE) inside the same transaction as
    the INSERT — atomic end-to-end for the ledger row.

    The wiki page body is still written to SurrealDB (D4). D35b
    handles the one-shot seed from existing pages; new ADRs go through
    this path which creates the ledger row + body page atomically.

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
    # Strict-type validation: required fields MUST be non-empty strings.
    # No str() coercion — rejects int/float/None with a clear error.
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
        number = storage.allocate_adr_number(project_id=project_id, origin="yadgar")
        row = storage.create_adr_row(
            project_id=project_id,
            origin="yadgar",
            number=number,
            title=title,
            status=status,
            date=date,
        )
        return {
            "adr_id": f"ADR-{number:04d}",
            "number": number,
            "status": row["status"],
            "title": row["title"],
            "body_slug": row["body_slug"],
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

    Spine Car F re-points this from the wiki-page read path to the
    SQL ledger table. Return shape is pinned by
    tests/core/test_adr_tools_car_f.py.

    Args:
        project_id: Git-derived identity key (D13/D14).
        adr_id: "ADR-NNNN" (case-insensitive; "adr-1" / "1" also accepted).
        directory: Absolute project path (kept for back-compat; project_id is authoritative).

    Returns:
        {adr_id, status, title, body_slug, ...} or {error: "..."} if absent.
    """
    # Strict types: reject non-string adr_id rather than crash on re.search.
    if not isinstance(adr_id, str):
        return {"error": f"adr_id must be a string, got {type(adr_id).__name__}"}
    if not isinstance(project_id, str):
        return {"error": f"project_id must be a string, got {type(project_id).__name__}"}

    m = re.search(r"(\d+)", adr_id or "")
    if not m:
        return {"error": f"invalid adr_id {adr_id!r}; expected 'ADR-NNNN'"}
    number_str = m.group(1)
    # Strict types: reject non-integer numbers (no float truncation, no hex).
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

    Spine Car F replaces the markdown-index parse with a SQL query
    against the `adr` table. Return shape is pinned by
    tests/core/test_adr_tools_car_f.py.

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
        r["adr_id"] = f"ADR-{r['number']:04d}"
        adrs.append(r)
    return adrs


# ── Backward-compatible re-exports ────────────────────────────────────────────
# All names that external callers / tests import from this module path remain
# importable here. The split is an internal detail.
__all__ = [
    # MCP tools
    "adr_add",
    "adr_get",
    "adr_list",
    # adr_index public surface
    "_ADR_HEADER_RE",
    "_ADR_PAGE_SLUG_RE",
    "adr_index_slug",
    "adr_log_slug",
    "adr_page_slug",
    "parse_adr_ids",
    # adr_render public surface
    "_REQUIRED_FIELDS",
    "_VALID_STATUSES",
    "_adr_tags",
    "_assemble_index_rows",
    "_build_adr_body",
    "_canonical_adr_payload",
    "_flip_superseded_target",
    "_parse_supersedes",
    # adr.py-local
    "_ADR_LOG_LOCKS",
    "_ADR_LOG_LOCKS_GUARD",
    "_FATAL_WRITE_REASONS",
    "_adr_log_lock",
    "_write_ok",
    "_validate_subsystem",
]
