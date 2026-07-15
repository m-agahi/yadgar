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

import os
import re
import threading

from yadgar._shared.observability.observe import observe
from yadgar.core.server._app import _tool

# Sub-module imports (split seams).
from yadgar.core.server.tools.adr_index import (
    _ADR_HEADER_RE,
    _ADR_PAGE_SLUG_RE,
    _INDEX_HEADER,
    _INDEX_ROW_RE,
    _build_index_content,
    _committed_page_max_id,
    _index_max_id,
    _next_adr_id,
    _next_adr_id_from_index,
    _render_index_row,
    adr_index_slug,
    adr_log_slug,
    adr_page_slug,
    parse_adr_ids,
    parse_index_rows,
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
from yadgar.core.server.tools.project import _resolve_project_root
from yadgar.core.server.tools.wiki import (
    _wiki_write_canonical,
    wiki_read,
)

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
    directory: str,
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
) -> dict:
    """Create a new Architecture Decision Record (ADR).

    Car 2: writes ONE canonical wiki page per ADR (`<project>-adr-NNNN`,
    branch IS NULL — readable from any branch AND in non-git dirs) plus a row in
    the canonical `<project>-adr-index`. IDs are assigned sequentially from the
    index (max+1). Supersede targets' status tag is flipped to `superseded`.

    Args:
        directory: Absolute path to the project root.
        title: Short human-readable title (e.g. "Use SurrealDB for storage").
        status: One of: open, accepted, superseded, rejected, deprecated.
        date: ISO date string (e.g. "2026-06-25").
        context: Background / problem statement.
        decision: The decision that was made.
        rationale: Why this decision was made.
        alternatives: Alternatives that were considered.
        consequences: Known / expected consequences.
        revisit_trigger: Condition that would trigger revisiting this decision.
        supersedes: "none" or a comma-separated list of superseded ADR IDs (e.g. "ADR-0002").

    Returns:
        {"adr_id": "ADR-NNNN", "slug": "<project>-adr-NNNN"} on success.
        {"error": "...", "ok": False} on validation failure or storage error.
    """
    # ── Validation (before any storage access) ─────────────────────────────────
    provided: dict[str, str] = {
        "title": title,
        "status": status,
        "date": date,
        "context": context,
        "decision": decision,
        "rationale": rationale,
        "alternatives": alternatives,
        "consequences": consequences,
        "revisit_trigger": revisit_trigger,
        "supersedes": supersedes,
    }
    for field in _REQUIRED_FIELDS:
        val = provided.get(field)
        if not val or not str(val).strip():
            return {"ok": False, "error": f"missing required field: {field!r}"}
    if status not in _VALID_STATUSES:
        return {
            "ok": False,
            "error": (f"invalid status {status!r}; must be one of {sorted(_VALID_STATUSES)}"),
        }

    try:
        resolved = _resolve_project_root(directory)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"cannot resolve project root: {exc}"}

    project_name = os.path.basename(resolved)
    index_slug = adr_index_slug(resolved)

    # ── Serialize the read-assign-write sequence under a per-project lock ───────
    with _adr_log_lock(resolved):
        # Read the canonical index (no branch_hint — canonical resolves via §25 step-2).
        index_page = wiki_read(index_slug, directory=resolved)
        index_exists = "error" not in index_page
        existing_index = index_page.get("content", "") if index_exists else ""

        # ID = max over the index rows AND the COMMITTED per-ADR page slugs. The
        # page-slug scan makes this resilient to a lagging index (an index write
        # still queued after wait_timeout): the committed page carries the ID.
        adr_id = _next_adr_id(resolved, existing_index)
        page_slug = adr_page_slug(resolved, adr_id)

        # ── Write the per-ADR canonical page — wait=True (ID-bearing artifact) ──
        # It is written FIRST + synchronously so it is committed WITHIN this lock,
        # making its slug visible to the next adr_add's _committed_page_max_id scan
        # even if the index write below only converges later.
        page_content = _build_adr_body(
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
        # Stored TITLE equals the slug string so _slugify(title) == page_slug.
        page_payload = _canonical_adr_payload(
            page_slug, page_content, "decision", _adr_tags(adr_id, status), resolved
        )
        page_result = _wiki_write_canonical(page_payload, wait=True)
        if not _write_ok(page_result):
            return {
                "ok": False,
                "error": f"per-ADR page write failed: {page_result.get('reason', 'unknown')}",
                "adr_id": adr_id,
            }

        # ── Append the index row + rebuild the canonical index ─────────────────
        # The index is a DERIVED convenience view (not the ID source of truth), so a
        # queued/wait_timeout index write is NOT a failure — it converges on the next
        # drain, and next-ID correctness is guaranteed by the committed page slug scan
        # above. Only a hard rejection (duplicate_detected / blocked) is fatal.
        target_ids = _parse_supersedes(supersedes)
        new_row = {
            "adr_id": adr_id,
            "status": status,
            "date": date,
            "title": title,
            "supersedes": supersedes if supersedes.strip().lower() != "none" else "none",
            "superseded_by": "-",
            "slug": page_slug,
        }
        rows = _assemble_index_rows(existing_index, new_row, adr_id, target_ids)
        index_content = _build_index_content(project_name, rows)
        index_payload = _canonical_adr_payload(
            index_slug,
            index_content,
            "reference",
            ["adr", "adr-index"],
            resolved,
            replace_slug=index_slug if index_exists else None,
        )
        index_result = _wiki_write_canonical(index_payload, wait=True)
        if not _write_ok(index_result):
            return {
                "ok": False,
                "error": f"index write failed: {index_result.get('reason', 'unknown')}",
                "adr_id": adr_id,
                "slug": page_slug,
            }

        # ── Flip superseded targets' page status tag (best-effort) ─────────────
        for tid in target_ids:
            _flip_superseded_target(resolved, tid, adr_id)

        return {"adr_id": adr_id, "slug": page_slug}


@_tool(power=True)
def adr_get(directory: str, adr_id: str) -> dict:
    """Read a single ADR's canonical page directly.

    Args:
        directory: Absolute path to the project root.
        adr_id: "ADR-NNNN" (case-insensitive; "adr-1" / "1" also accepted).

    Returns:
        The wiki page dict for `<project>-adr-NNNN`, or {"error": "..."} if absent.
    """
    try:
        resolved = _resolve_project_root(directory)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"cannot resolve project root: {exc}"}

    m = re.search(r"(\d+)", adr_id or "")
    if not m:
        return {"error": f"invalid adr_id {adr_id!r}; expected 'ADR-NNNN'"}
    normalized = f"ADR-{int(m.group(1)):04d}"
    slug = adr_page_slug(resolved, normalized)
    return wiki_read(slug, directory=resolved)


@_tool(power=True)
def adr_list(directory: str, status: str | None = None) -> dict:
    """List ADRs from the canonical index; optional status filter.

    Args:
        directory: Absolute path to the project root.
        status: Optional filter (open/accepted/superseded/rejected/deprecated).

    Returns:
        {"adrs": [{adr_id, status, date, title, supersedes, superseded_by, slug}, ...],
         "count": N}. Empty list when the index is absent.
    """
    try:
        resolved = _resolve_project_root(directory)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"cannot resolve project root: {exc}"}

    index_slug = adr_index_slug(resolved)
    page = wiki_read(index_slug, directory=resolved)
    if "error" in page or not page.get("content"):
        return {"adrs": [], "count": 0}
    rows = parse_index_rows(page["content"])
    if status is not None:
        rows = [r for r in rows if r["status"] == status]
    return {"adrs": rows, "count": len(rows)}


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
    "_INDEX_HEADER",
    "_INDEX_ROW_RE",
    "_build_index_content",
    "_committed_page_max_id",
    "_index_max_id",
    "_next_adr_id",
    "_next_adr_id_from_index",
    "_render_index_row",
    "adr_index_slug",
    "adr_log_slug",
    "adr_page_slug",
    "parse_adr_ids",
    "parse_index_rows",
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
]
