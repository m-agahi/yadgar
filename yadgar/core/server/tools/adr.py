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
"""

from __future__ import annotations

import os
import re
import threading

from yadgar._shared.contracts.models import ADR
from yadgar._shared.observability.observe import observe
from yadgar.core.server._app import _tool

# Imported lazily at call-time to match the pattern in other tool modules,
# but we list them here for clarity and to enable patching in tests.
from yadgar.core.server.tools.admin_other import wiki_update
from yadgar.core.server.tools.project import _resolve_project_root
from yadgar.core.server.tools.wiki import (
    _resolve_page_id_by_slug,
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


# Valid ADR status values.
_VALID_STATUSES: frozenset[str] = frozenset(
    {"open", "accepted", "superseded", "rejected", "deprecated"}
)

# Regex that matches ## ADR-NNNN at column 0 (header-only scan — used by the
# migration to parse the legacy monolith, and by parse_adr_ids).
_ADR_HEADER_RE = re.compile(r"^## ADR-(\d{4})", re.MULTILINE)

# Index table row: | ADR-NNNN | status | date | title | supersedes | superseded-by | slug |
_INDEX_ROW_RE = re.compile(
    r"^\|\s*(ADR-\d{4})\s*\|\s*(?P<status>[^|]*?)\s*\|\s*(?P<date>[^|]*?)\s*\|"
    r"\s*(?P<title>[^|]*?)\s*\|\s*(?P<supersedes>[^|]*?)\s*\|"
    r"\s*(?P<superseded_by>[^|]*?)\s*\|\s*(?P<slug>[^|]*?)\s*\|\s*$",
    re.MULTILINE,
)

_INDEX_HEADER = (
    "| ADR | Status | Date | Title | Supersedes | Superseded-by | Slug |\n"
    "| --- | --- | --- | --- | --- | --- | --- |\n"
)

# Required caller-supplied content fields (directory is handled separately).
_REQUIRED_FIELDS: tuple[str, ...] = (
    "title",
    "status",
    "date",
    "context",
    "decision",
    "rationale",
    "alternatives",
    "consequences",
    "revisit_trigger",
    "supersedes",
)


# ── Slug helpers ───────────────────────────────────────────────────────────────


def adr_log_slug(resolved: str) -> str:
    """Legacy monolith slug `<project>-adr-log` (migration source + back-compat).

    Retained for the migration script (reads the old monolith) and
    _get_adr_log_updated_at back-compat. New writes never touch this slug.
    """
    return f"{os.path.basename(resolved)}-adr-log"


def adr_index_slug(resolved: str) -> str:
    """The canonical ADR index slug `<project>-adr-index`."""
    return f"{os.path.basename(resolved)}-adr-index"


def adr_page_slug(resolved: str, adr_id: str) -> str:
    """The canonical per-ADR page slug `<project>-adr-NNNN`.

    ``adr_id`` is an "ADR-NNNN" string; the slug lowercases it to `adr-NNNN`
    (slugify maps "ADR-0001" → "adr-0001"), so the stored wiki title must equal
    this slug string to make ``_slugify(title)`` deterministic.
    """
    return f"{os.path.basename(resolved)}-{adr_id.lower()}"


def parse_adr_ids(content: str) -> list[str]:
    """Extract ADR IDs from legacy monolith content, sorted descending.

    Retained for the migration (parses the old `## ADR-NNNN` monolith) and
    _build_adr_log back-compat. Returns ["ADR-NNNN", ...] descending, [] if none.
    """
    matches = _ADR_HEADER_RE.findall(content)
    return [f"ADR-{int(n):04d}" for n in sorted(matches, key=int, reverse=True)]


# ── Index parsing / rendering ──────────────────────────────────────────────────


@observe(tier="stage", metric="tools.adr.parse_index_rows")
def parse_index_rows(content: str) -> list[dict]:
    """Parse the ADR index table into a list of row dicts (in file order).

    Each row: {adr_id, status, date, title, supersedes, superseded_by, slug}.
    Empty list if the index is absent or has no data rows.
    """
    rows: list[dict] = []
    for m in _INDEX_ROW_RE.finditer(content):
        rows.append(
            {
                "adr_id": m.group(1),
                "status": m.group("status").strip(),
                "date": m.group("date").strip(),
                "title": m.group("title").strip(),
                "supersedes": m.group("supersedes").strip(),
                "superseded_by": m.group("superseded_by").strip(),
                "slug": m.group("slug").strip(),
            }
        )
    return rows


def _index_max_id(content: str) -> int:
    """Max ADR number from index table rows (0 when empty/absent)."""
    ids = [int(r["adr_id"].split("-")[1]) for r in parse_index_rows(content)]
    return max(ids) if ids else 0


# Committed per-ADR page slug pattern: <project>-adr-NNNN.
_ADR_PAGE_SLUG_RE = re.compile(r"-adr-(\d{4})$")


@observe(tier="stage", metric="tools.adr._committed_page_max_id")
def _committed_page_max_id(resolved: str) -> int:
    """Max ADR number from COMMITTED `<project>-adr-NNNN` page slugs (0 when none).

    The per-ADR page is the ID-BEARING artifact and is written wait=True (committed
    within the per-project lock). Scanning committed page slugs makes the next-ID
    assignment resilient to a LAGGING index: even if the index write is still queued
    (wait_timeout — the write converges but is not yet in the DB), the freshly
    committed per-ADR page slug is already visible here, so a subsequent adr_add
    never re-assigns a used ID. Fixes the RYW-on-timeout duplicate-ID race.
    """
    project = os.path.basename(resolved)
    prefix = f"{project}-adr-"
    try:
        from yadgar.core.server.tools.wiki import wiki_list  # noqa: PLC0415

        pages = wiki_list(slug_prefix=prefix, directory=resolved, limit=10000)
    except Exception:  # noqa: BLE001 — fall back to index-only on a list failure
        return 0
    max_n = 0
    for p in pages:
        m = _ADR_PAGE_SLUG_RE.search(p.get("slug") or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


@observe(tier="stage", metric="tools.adr._next_adr_id")
def _next_adr_id(resolved: str, index_content: str) -> str:
    """Next sequential ADR id from BOTH the index rows AND committed page slugs.

    Authoritative source = the committed per-ADR page slugs (ID-bearing, wait=True);
    the index is a derived convenience view that may lag when its write is queued.
    Taking the max over both guarantees uniqueness even on an index wait_timeout.
    """
    n = max(_index_max_id(index_content), _committed_page_max_id(resolved))
    return f"ADR-{n + 1:04d}"


@observe(tier="stage", metric="tools.adr._next_adr_id_from_index")
def _next_adr_id_from_index(content: str) -> str:
    """Index-only next-ID (retained for tests / callers that pass only index text).

    Prefer ``_next_adr_id(resolved, content)`` which also scans committed page
    slugs and is resilient to a lagging index.
    """
    n = _index_max_id(content)
    return f"ADR-{n + 1:04d}"


def _render_index_row(
    adr_id: str,
    status: str,
    date: str,
    title: str,
    supersedes: str,
    superseded_by: str,
    slug: str,
) -> str:
    """Render one index table row. Pipes/newlines in free text are sanitised."""

    def _clean(v: str) -> str:
        return str(v).replace("|", "/").replace("\n", " ").strip() or "-"

    return (
        f"| {adr_id} | {_clean(status)} | {_clean(date)} | {_clean(title)} | "
        f"{_clean(supersedes)} | {_clean(superseded_by)} | {slug} |"
    )


@observe(tier="stage", metric="tools.adr._build_index_content")
def _build_index_content(project_name: str, rows: list[dict]) -> str:
    """Build the full index page content from row dicts (ascending by ID)."""
    ordered = sorted(rows, key=lambda r: int(r["adr_id"].split("-")[1]))
    body = _INDEX_HEADER
    for r in ordered:
        body += (
            _render_index_row(
                r["adr_id"],
                r["status"],
                r["date"],
                r["title"],
                r.get("supersedes", "none"),
                r.get("superseded_by", "-"),
                r["slug"],
            )
            + "\n"
        )
    return (
        f"# {project_name} ADR Index\n\n"
        f"Architecture Decision Records for `{project_name}` — one canonical page per ADR "
        f"(`{project_name}-adr-NNNN`). This index is the ID source of truth.\n\n"
        f"{body}"
    )


# ── Body builder ───────────────────────────────────────────────────────────────


def _build_adr_body(
    adr_id: str,
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
) -> str:
    """Return the per-ADR page CONTENT (H1 + Context/Decision/Consequences + bullets).

    The page_type `adr` requires sections Context / Decision / Consequences (for
    wiki_lint). The 9 flat bullets carry the full structured record; the three
    required ## sections satisfy the page_type template.
    """
    record = ADR(
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
    bullets = record.to_markdown_body()
    return (
        f"# {adr_id}: {title}\n\n"
        f"{bullets}\n"
        f"## Context\n\n{context}\n\n"
        f"## Decision\n\n{decision}\n\n"
        f"## Consequences\n\n{consequences}\n"
    )


def _adr_tags(adr_id: str, status: str) -> list[str]:
    """Tags for a per-ADR page. adr_id is 'ADR-NNNN'."""
    nnnn = adr_id.split("-")[1]
    return ["adr", "decisions", f"adr-status:{status}", f"adr-{nnnn}"]


# ── Supersede handling ─────────────────────────────────────────────────────────


@observe(tier="stage", metric="tools.adr._parse_supersedes")
def _parse_supersedes(supersedes: str) -> list[str]:
    """Parse a 'supersedes' field into a list of 'ADR-NNNN' ids ([] for none)."""
    if not supersedes or supersedes.strip().lower() == "none":
        return []
    ids = re.findall(r"ADR-(\d{4})", supersedes)
    return [f"ADR-{n}" for n in ids]


@observe(tier="stage", metric="tools.adr._flip_superseded_target")
def _flip_superseded_target(resolved: str, target_id: str, new_adr_id: str) -> None:
    """Flip a superseded ADR page's status tag to 'superseded' (best-effort).

    Reads the target page canonically, replaces its adr-status:* tag with
    adr-status:superseded, and records the superseding id in an adr-superseded-by
    tag. Never raises — a missing target must not abort the new ADR write.
    """
    try:
        slug = adr_page_slug(resolved, target_id)
        page_id, page = _resolve_page_id_by_slug(slug, directory=resolved)
        if page_id is None or page is None:
            return
        tags = list(page.get("tags") or [])
        tags = [t for t in tags if not t.startswith("adr-status:")]
        tags.append("adr-status:superseded")
        nnnn = new_adr_id.split("-")[1]
        sb_tag = f"adr-superseded-by:{nnnn}"
        if sb_tag not in tags:
            tags.append(sb_tag)
        wiki_update(page_id, {"tags": tags})
    except Exception:  # noqa: BLE001 — supersede patch is best-effort
        return


# ── adr_add helpers (extracted to keep adr_add under the I30 cyclomatic cap) ───


def _canonical_adr_payload(
    slug: str,
    content: str,
    category: str,
    tags: list[str],
    directory: str,
    *,
    replace_slug: str | None = None,
) -> dict:
    """Build a canonical (branch-NULL) wiki_add payload for the ADR write path.

    ``title`` == ``slug`` so ``_slugify(title)`` yields the deterministic slug.
    ``force=True`` bypasses the drainer sim gate (canonical ADR/index pages are
    legitimately near-duplicate). ``page_type="adr"`` satisfies the
    ``_wiki_write_canonical`` CANONICAL_PAGE_TYPES allowlist assertion.
    """
    return {
        "wiki_schema_version": 2,
        "slug": slug,
        "title": slug,
        "content": content,
        "category": category,
        "tags": tags,
        "source_memory_ids": None,
        "confidence": "high",
        "append": False,
        "force": True,
        "replace_slug": replace_slug,
        "directory_context": directory,
        "page_type": "adr",
    }


@observe(tier="stage", metric="tools.adr._assemble_index_rows")
def _assemble_index_rows(
    existing_index: str,
    new_row: dict,
    new_adr_id: str,
    target_ids: list[str],
) -> list[dict]:
    """Return the full ADR index row-set after adding ``new_row`` + supersede back-links.

    Two-pass: append the new row, then flip each supersede target's status to
    'superseded' and record the new ADR's NNNN in its ``superseded_by`` column.
    """
    rows = parse_index_rows(existing_index)
    rows.append(new_row)
    if not target_ids:
        return rows
    by_id = {r["adr_id"]: r for r in rows}
    nnnn = new_adr_id.split("-")[1]
    for tid in target_ids:
        row = by_id.get(tid)
        if row is None:
            continue
        row["status"] = "superseded"
        prev = row.get("superseded_by", "-")
        row["superseded_by"] = nnnn if prev in ("-", "") else f"{prev},{nnnn}"
    return rows


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
