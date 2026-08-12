"""Project context tools: git helpers, project_brief, wiki-staleness context.

# Module size justified: single cohesive domain — project/git/wiki-staleness context tools.
# All functions share _resolve_project_root, _origin_head_short, and _git_safe_env helpers.
# Seven+ callers (server/__init__.py, tools/__init__.py, tools/recall.py, tools/wiki.py,
# tools/memorize.py, restoration.py, file_queue/apply.py) import helpers directly from
# this module; splitting would require updating all those import sites for no architectural
# benefit. The module is read-only (no DB writes), tight, and has a single public surface.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import yadgar._shared.paths as _paths
from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar._shared.security.secrets import gate_or_reject
from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool
from yadgar.core.server.tools._project_param import (
    accept_project_param,
    resolve_effective_project,
)

logger = logging.getLogger(__name__)

settings = get_settings()


# ── Git helpers ────────────────────────────────────────────────────────
#
# R3 Car 1 (write-half): _git_safe_env, _GIT_SAFE_ARGS, _resolve_project_root,
# and _bump_epoch_for_context moved to yadgar._shared.server_helpers so the
# backend write path (memorize post-write epoch bump) imports only _shared.
# Re-imported here — project.py's other git invocations use the same helpers.
from yadgar._shared.server_helpers import (  # noqa: E402
    _ANCHOR_PROMOTE_TAGS,
    _GIT_SAFE_ARGS,
    _cosine_similarity,
    _count_markdown_headers,
    _git_safe_env,
    _resolve_project_root,
    normalize_write_context,
)


@observe(tier="stage", metric="tools.project._origin_head_short_cached")
@functools.lru_cache(maxsize=128)
def _origin_head_short_cached(directory: str, _ts_bucket: int) -> str:
    """Cached per 5-minute bucket. Falls back to 'master'.

    Do not call directly — use _origin_head_short(directory), which injects the
    correct time bucket.
    """
    try:
        out = (
            subprocess.check_output(
                [
                    "git",
                    *_GIT_SAFE_ARGS,
                    "-C",
                    directory,
                    "symbolic-ref",
                    "--short",
                    "refs/remotes/origin/HEAD",
                ],
                stderr=subprocess.DEVNULL,
                timeout=2,
                env=_git_safe_env(),
            )
            .decode()
            .strip()
        )
        # Returns e.g. "origin/master" — strip the remote prefix
        if out:
            return out.split("/", 1)[-1] if "/" in out else out
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as _e:
        pass
    return "master"


@observe(tier="stage", metric="tools.project._origin_head_short")
def _origin_head_short(directory: str) -> str:
    """Short name of ``origin/HEAD`` for *directory* (e.g. ``master`` / ``main``).

    ADR-0215 removed branch scoping, and ADR-0217 removed the trusted per-directory
    trusted per-directory git-facts blob this used to be sourced from. The roadmap-
    update-lag signal still needs a mainline ref to name in ``git log`` /
    ``git show`` — so it reads it back from local git, at exactly the same daemon-
    side visibility its sibling ``git`` calls in ``_get_master_head_info`` already
    require. Never a write-path trust gate (that was the ADR-0126 objection to a
    daemon-side ``symbolic-ref``); purely a best-effort local read.

    Always returns a usable ref name — falls back to ``"master"`` — so callers can
    interpolate it into a git invocation without a ``None`` guard.
    """
    return _origin_head_short_cached(directory, int(time.time() // 300))


# ── Project tools ──────────────────────────────────────────────────────


@observe(tier="stage", metric="tools.project._render_project_brief")
def _render_project_brief(brief: dict) -> str:
    """Render a project_brief dict as markdown for hook injection (§28)."""
    project = brief.get("project", "unknown")
    mode = brief.get("_mode", "catalog")

    lines: list[str] = [f"# {project} — {mode}\n"]

    stale = brief.get("stale_wiki_count", 0)
    init_present = brief.get("init_memory_present", False)
    active_present = brief.get("active_work_present", False)

    signals = []
    if stale > 0:
        signals.append(f"stale_wiki_count={stale}")
    if not init_present:
        signals.append("init_memory=absent")
    if not active_present:
        signals.append("active_work=absent")
    if signals:
        lines.append(f"**Signals:** {', '.join(signals)}\n")

    # F4: empty-state nudges
    if not init_present:
        lines.append(
            "*Suggestion: call `bootstrap_project(directory, content, project=...)`"
            " to seed project context.*"
        )
    if not active_present:
        lines.append(
            "*Suggestion: call `update_active_work(directory, content, project=...)`"
            " once you start a session"
            " to store working-state and checkpoint context.*"
        )
        lines.append(
            "*To track TODOs/tasks use the harness task list (TaskCreate)"
            " and the yadgar `task_write` tool — the SQL ledger is the source"
            " of truth, the wiki `{project}-task-list` page is a derived mirror.*"
        )
    if not init_present or not active_present:
        lines.append("")

    # F5: Global Anchors section (all, up to 20)
    global_anchors = brief.get("top_anchors_global", [])
    lines.append("## Global Anchors")
    if global_anchors:
        for a in global_anchors:
            lines.append(f"- [{a.get('id')}] {(a.get('title') or '')[:80]}")
    else:
        lines.append("*(none)*")
    lines.append("")

    # F5: Project Anchors section (all, up to 20)
    project_anchors = brief.get("top_anchors_project", [])
    lines.append("## Project Anchors")
    if project_anchors:
        for a in project_anchors:
            lines.append(f"- [{a.get('id')}] {(a.get('title') or '')[:80]}")
    else:
        lines.append("*(none)*")
    lines.append("")

    # F5: Checkpoint section
    checkpoint = brief.get("checkpoint")
    if checkpoint:
        lines.append("## Checkpoint")
        current_task = checkpoint.get("current_task", "")
        if current_task:
            lines.append(f"**Task:** {current_task}")
        key_decisions = checkpoint.get("key_decisions") or []
        if key_decisions:
            lines.append("**Decisions:**")
            for d in key_decisions[:3]:
                lines.append(f"- {d}")
        next_steps = checkpoint.get("next_steps") or []
        if next_steps:
            lines.append("**Next:**")
            for s in next_steps[:3]:
                lines.append(f"- {s}")
        lines.append("")

    # F5: Hot Memories section
    hot_memories = brief.get("hot_memories", [])
    lines.append("## Hot Memories")
    if hot_memories:
        for m in hot_memories[:3]:
            lines.append(f"- {(m.get('content') or '')[:100]}")
    else:
        lines.append("*(none)*")
    lines.append("")

    # Car 2 (ADR-consultable): Recent ADRs — temporal newest-N from the canonical
    # index. Complements semantic recall (default profile fans out to the wiki arm).
    # Rendered in catalog/full (signals mode omits _render entirely).
    recent_adrs = brief.get("recent_adrs")
    if recent_adrs is not None:
        latest_ids = recent_adrs.get("latest_ids") or []
        lines.append("## Recent ADRs")
        if latest_ids:
            lines.append(", ".join(latest_ids))
            lines.append(
                f"*(consult: `adr_list(status='open')` or "
                f"`recall(type='wiki', tags=['adr'])` — index `{recent_adrs.get('slug', '')}`)*"
            )
        else:
            lines.append("*(none captured yet — use `adr_add` to record decisions)*")
        lines.append("")

    # v5.53.0: Wiki Catalog section — grouped titles + counts, length-capped.
    # Replaces the bare-slug "Wiki Keys" block so Claude sees a real index.
    wiki_catalog = brief.get("wiki_catalog")
    lines.append("## Wiki Index")
    if wiki_catalog:
        catalog_lines = _render_wiki_catalog(wiki_catalog, brief.get("_resolved_directory", ""))
        lines.extend(catalog_lines)
    else:
        # Fallback: show legacy key_wiki_pages (back-compat) or nothing.
        key_wiki_pages = brief.get("key_wiki_pages", [])
        if key_wiki_pages:
            for p in key_wiki_pages[:3]:
                title = (p.get("title") or "").strip() or p.get("slug", "")
                lines.append(f"- {title}")
        else:
            lines.append("*(none — call `wiki_list()` to enumerate pages)*")
    lines.append("")

    if mode == "full":
        init_content = brief.get("init_memory")
        if init_content:
            lines.append("## Init Memory")
            lines.append(str(init_content)[:600])
            lines.append("")
        active_content = brief.get("active_work")
        if active_content:
            lines.append("## Active Work")
            lines.append(str(active_content)[:400])
            lines.append("")

    lines.append(f"*Directory: {brief.get('_resolved_directory', '')}*")
    return "\n".join(lines)


# ── Anchor hygiene constants (v5.8.0 PR-B) ────────────────────────────────
# R3 Car 3d: _ANCHOR_PROMOTE_TAGS + the markdown-header/cosine helpers moved to
# yadgar._shared.server_helpers so the backend anchor-audit exec imports only
# _shared. Re-imported at the top of this module for project.py's own use.

# Max candidates per list (redundancy pairs + promote IDs) before hard truncation.
# K=3 chosen to keep signals mode payload ≤100 tokens even under pathological load.
# Not an env knob — too many already; this is an internal budget constant.
_SIGNALS_CANDIDATES_K = 3

# v5.53.0: Wiki catalog constants — max items shown per category group before
# truncation hint. Not an env knob (internal budget constant, same rationale as
# _SIGNALS_CANDIDATES_K). Keep low to bound catalog render length.
_WIKI_CATALOG_MAX_PER_GROUP = 5


# ── project_brief helpers (v5.7.12) ───────────────────────────────────────


@observe(tier="hot", metric="tools.project._compute_row_age_hours")
def _compute_row_age_hours(rows: list) -> float | None:
    """Return age in hours of the first row's created_at, or None if absent.

    created_at is stored as an ISO-8601 string.  Parses it with datetime.fromisoformat()
    and computes (now - created_at).total_seconds() / 3600.
    """
    if not rows:
        return None
    row = rows[0]
    created_at = row.get("created_at")
    if not created_at:
        return None
    try:
        if isinstance(created_at, str):
            # SurrealDB returns ISO strings; strip trailing 'Z' for Python compat
            ts = created_at.rstrip("Z").replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            # If no tzinfo, assume UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        else:
            # datetime object
            dt = created_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        return (now - dt).total_seconds() / 3600.0
    except Exception:
        logger.debug("_compute_row_age_hours: failed to parse created_at=%r", created_at)
        return None


def _get_max_anchors() -> int:
    """Return PROJECT_BRIEF_MAX_ANCHORS from settings.  Separate function for monkeypatching in tests."""
    return get_settings().PROJECT_BRIEF_MAX_ANCHORS


@observe(tier="stage", metric="tools.project._fetch_presence_rows")
def _fetch_presence_rows(storage, resolved: str) -> tuple:
    """Fetch presence + age rows for init_memory, active_work, and checkpoint.

    Returns (init_rows, active_rows, checkpoint_rows).
    """
    init_rows = storage._q(
        "SELECT id, content, created_at FROM memory WHERE directory_context = $dir "
        "AND '_project_init' INSIDE tags LIMIT 1",
        {"dir": resolved},
    )
    active_rows = storage._q(
        "SELECT id, content, created_at FROM memory WHERE directory_context = $dir "
        "AND '_active_work' INSIDE tags LIMIT 1",
        {"dir": resolved},
    )
    checkpoint_rows = storage._q(
        "SELECT * FROM checkpoint WHERE directory_context = $dir ORDER BY created_at DESC LIMIT 1",
        {"dir": resolved},
    )
    return init_rows, active_rows, checkpoint_rows


@observe(tier="hot", metric="tools.project._build_checkpoint_dict")
def _build_checkpoint_dict(checkpoint_rows: list) -> dict | None:
    """Build a compact checkpoint dict from raw checkpoint rows.  None if absent."""
    if not checkpoint_rows:
        return None
    cp = checkpoint_rows[0]
    return {
        "current_task": cp.get("current_task", ""),
        "key_decisions": (cp.get("key_decisions") or [])[:3],
        "next_steps": (cp.get("next_steps") or [])[:3],
    }


@observe(tier="stage", metric="tools.project._build_wiki_pages")
def _build_wiki_pages(storage, limit: int, directory: str | None = None) -> list[dict]:
    """Fetch and shape wiki pages list, scoped to directory + 'global' when supplied.

    v5.65: added directory param to prevent cross-project wiki page leakage in
    project_brief key_wiki_pages (callers now pass resolved directory).
    list_wiki_pages already accepts directory= and scopes to dir + 'global'.
    """
    pages = storage.list_wiki_pages(limit=limit, directory=directory)
    return [
        {
            "slug": p.get("slug", ""),
            "title": p.get("title", ""),
            "access_count": p.get("access_count") or 0,
        }
        for p in pages
    ]


@observe(tier="hot", metric="tools.project._slug_prefix")
def _slug_prefix(slug: str) -> str:
    """Extract the first segment of a slug (split on '-', take element [0] + '-').

    Examples:
      "fn-foo-bar"  → "fn-"
      "mod-core"    → "mod-"
      "services"    → "services"   (no '-': whole slug, no trailing dash)
    """
    if not slug:
        return "(other)"
    idx = slug.find("-")
    if idx == -1:
        return slug
    return slug[: idx + 1]


# v5.53.0: max distinct prefixes shown in the prefix-breakdown line for big categories.
_WIKI_CATALOG_MAX_PREFIXES = 8


@observe(tier="stage", metric="tools.project._build_wiki_catalog")
def _build_wiki_catalog(storage, resolved: str) -> dict:
    """Build a grouped wiki catalog for project_brief catalog/restore renders (v5.53.0).

    Fetches metadata-only rows (slug, title, category, updated_at) scoped to the
    resolved directory + 'global'. Groups pages by category, caps each group at
    _WIKI_CATALOG_MAX_PER_GROUP items, and returns a structured dict with:

      {
          "total": N,
          "groups": {
              "architecture": {"pages": [{"slug": ..., "title": ...}, ...], "more": M,
                               "prefix_counts": {"arch-": 3, ...}},
              "decision": {"pages": [...], "more": 0, "prefix_counts": {...}},
              ...
          },
      }

    Each page entry: {"slug": str, "title": str} — title falls back to slug when blank.
    `more` = number of additional pages in that category beyond the cap (0 if none).
    `prefix_counts` = Counter of first-segment prefixes over ALL pages in the category
      (not capped). Used by _render_wiki_catalog to show a prefix breakdown for large
      categories instead of an uninformative truncated title list.
    Uses list_wiki_catalog (metadata-only) to avoid content/embedding fetch latency.
    """
    try:
        rows = storage.list_wiki_catalog(directory=resolved)
    except Exception:
        rows = []

    total = len(rows)
    groups: dict[str, dict] = {}
    for row in rows:
        # v5.53.2: group by page_type when present, fall back to category.
        group_key = row.get("page_type") or row.get("category") or "uncategorized"
        title = (row.get("title") or "").strip() or row.get("slug", "")
        slug = row.get("slug", "")
        if group_key not in groups:
            groups[group_key] = {"pages": [], "more": 0, "prefix_counts": {}}
        entry = groups[group_key]
        if len(entry["pages"]) < _WIKI_CATALOG_MAX_PER_GROUP:
            entry["pages"].append({"slug": slug, "title": title})
        else:
            entry["more"] += 1
        # Accumulate prefix counts over ALL rows (not capped)
        prefix = _slug_prefix(slug)
        entry["prefix_counts"][prefix] = entry["prefix_counts"].get(prefix, 0) + 1

    return {"total": total, "groups": groups}


@observe(tier="hot", metric="tools.project._render_wiki_catalog")
def _render_wiki_catalog(catalog: dict, resolved: str) -> list[str]:
    """Render the wiki catalog dict as markdown lines for _render_project_brief.

    Returns a list of markdown lines (no trailing newline per line).
    When the catalog is empty, returns a nudge line.

    For categories whose total page count exceeds _WIKI_CATALOG_MAX_PER_GROUP (i.e. the
    title list would be a useless truncated sample), and where prefix_counts data is
    available, renders a slug-prefix breakdown instead:

      **reference** (237)
        by prefix: fn- (140) · mod- (45) · services- (30) · … 4 more prefixes

    Small categories (count ≤ cap, or no prefix_counts) keep the title list as-is.
    """
    total = catalog.get("total", 0)
    groups = catalog.get("groups", {})
    lines: list[str] = []

    if total == 0:
        lines.append("*(no wiki pages yet — call `wiki_list()` to confirm)*")
        return lines

    lines.append(f"yadgar knows **{total}** pages on this repo.")
    lines.append("")
    for cat, entry in sorted(groups.items()):
        pages = entry.get("pages", [])
        more = entry.get("more", 0)
        count = len(pages) + more
        prefix_counts: dict = entry.get("prefix_counts", {})

        lines.append(f"**{cat}** ({count})")

        # Big-category branch: count exceeds cap AND we have prefix data
        if count > _WIKI_CATALOG_MAX_PER_GROUP and prefix_counts:
            sorted_prefixes = sorted(prefix_counts.items(), key=lambda kv: (-kv[1], kv[0]))
            shown = sorted_prefixes[:_WIKI_CATALOG_MAX_PREFIXES]
            hidden = len(sorted_prefixes) - len(shown)
            parts = [f"{pfx} ({cnt})" for pfx, cnt in shown]
            prefix_line = "  by prefix: " + " · ".join(parts)
            if hidden > 0:
                prefix_line += f" · …{hidden} more prefixes"
            lines.append(prefix_line)
        else:
            # Small category: render individual titles + affordance
            for p in pages:
                lines.append(f"  - {p['title']}")
            if more > 0:
                lines.append(f"  - …{more} more — call `wiki_list(category={cat!r})` to see all")
    return lines


@observe(tier="stage", metric="tools.project._build_hot_memories")
def _build_hot_memories(storage, project_scope_key: str, limit: int, snippet: int) -> list[dict]:
    """Fetch hot memories excluding anchored entries.

    Filters: heat > 0 AND 'anchor' NOTINSIDE tags AND '_anchor' NOTINSIDE tags.

    Car F7: ``project_scope_key`` is the resolved project_id when the caller
    named one, else the resolved directory path — see ``project_brief``'s
    ``_project_scope_key`` comment. ``memorize``/``anchor`` (C10f) always
    stamp ``directory_context`` from the resolved project_id now, so binding
    on the raw directory path here silently returned zero rows for anything
    written after that car landed.
    """
    rows = storage._q(
        "SELECT id, content, heat, tags FROM memory "
        "WHERE directory_context = $dir AND heat > 0 "
        "AND 'anchor' NOTINSIDE tags AND '_anchor' NOTINSIDE tags "
        f"ORDER BY heat DESC LIMIT {limit}",
        {"dir": project_scope_key},
    )
    return [
        {
            "id": storage._extract_id(row.get("id")),
            "content": (row.get("content") or "")[:snippet],
            "heat": row.get("heat", 0),
            "tags": row.get("tags", []),
        }
        for row in rows
    ]


@observe(tier="stage", metric="tools.project._build_anchor_rows_catalog")
def _build_anchor_rows_catalog(storage, project_scope_key: str) -> tuple:
    """Fetch global + project anchor rows for catalog/full modes.

    Returns (top_anchors_global, top_anchors_project, top_anchors_union).

    Car F7: the project bucket's ``$dir`` is ``project_scope_key`` (resolved
    project_id when supplied, else the resolved directory path) — matching
    what C10g already did for ``get_anchored_memories_scoped``, the sibling
    reader of this same ``_anchor``-tagged bucket used by ``restore()``.
    """
    _now = storage._now_iso()
    global_rows = storage._q(
        "SELECT id, content, tags, heat, access_count FROM memory "
        "WHERE '_anchor' INSIDE tags "
        # C13 (0047 PR#40 §5): re-keyed off directory_context and onto the
        # ``global`` reach TAG, matching what C5 already did to the OTHER reader
        # of this same bucket (``get_anchored_memories_scoped``). C5 re-keyed
        # one and missed these two, leaving a reader whose predicate the write
        # path can no longer satisfy: every site that minted
        # ``directory_context = 'global'`` is deleted, so this bucket could only
        # ever shrink. Same C6 dependency as its sibling — narrow until the
        # backfill re-keys the legacy rows to a real owner plus the reach tag.
        "AND 'global' INSIDE tags "
        "AND (valid_until IS NONE OR valid_until > $now) "
        "ORDER BY heat DESC LIMIT 20",
        {"now": _now},
    )
    top_anchors_global = []
    for row in global_rows:
        mid = storage._extract_id(row.get("id"))
        top_anchors_global.append(
            {
                "id": mid,
                "title": (row.get("content") or "")[:80],
                "tags": row.get("tags", []),
                "access_count": row.get("access_count") or 0,
            }
        )

    project_rows = storage._q(
        "SELECT id, content, tags, heat, access_count FROM memory "
        "WHERE '_anchor' INSIDE tags "
        "AND directory_context = $dir "
        "AND (valid_until IS NONE OR valid_until > $now) "
        "ORDER BY heat DESC LIMIT 20",
        {"dir": project_scope_key, "now": _now},
    )
    top_anchors_project = []
    for row in project_rows:
        mid = storage._extract_id(row.get("id"))
        top_anchors_project.append(
            {
                "id": mid,
                "title": (row.get("content") or "")[:80],
                "tags": row.get("tags", []),
                "access_count": row.get("access_count") or 0,
            }
        )

    seen: set = set()
    top_anchors_union: list = []
    for a in top_anchors_global + top_anchors_project:
        if a["id"] not in seen:
            seen.add(a["id"])
            top_anchors_union.append(a)

    return top_anchors_global, top_anchors_project, top_anchors_union


@observe(tier="stage", metric="tools.project._build_anchor_rows_restore")
def _build_anchor_rows_restore(storage, project_scope_key: str) -> list[dict]:
    """Fetch anchors for restore mode: merged list with scope field, truncated.

    Car F7: the project bucket's ``$dir`` is ``project_scope_key`` (resolved
    project_id when supplied, else the resolved directory path) — matching
    what C10g already did for ``get_anchored_memories_scoped``, the sibling
    reader of this same ``_anchor``-tagged bucket used by ``restore()``.
    """
    max_anchors = _get_max_anchors()
    _now = storage._now_iso()

    global_rows = storage._q(
        "SELECT id, content, tags, heat, access_count FROM memory "
        "WHERE '_anchor' INSIDE tags "
        # C13 (0047 PR#40 §5): re-keyed off directory_context and onto the
        # ``global`` reach TAG, matching what C5 already did to the OTHER reader
        # of this same bucket (``get_anchored_memories_scoped``). C5 re-keyed
        # one and missed these two, leaving a reader whose predicate the write
        # path can no longer satisfy: every site that minted
        # ``directory_context = 'global'`` is deleted, so this bucket could only
        # ever shrink. Same C6 dependency as its sibling — narrow until the
        # backfill re-keys the legacy rows to a real owner plus the reach tag.
        "AND 'global' INSIDE tags "
        "AND (valid_until IS NONE OR valid_until > $now) "
        "ORDER BY heat DESC LIMIT 20",
        {"now": _now},
    )
    project_rows = storage._q(
        "SELECT id, content, tags, heat, access_count FROM memory "
        "WHERE '_anchor' INSIDE tags "
        "AND directory_context = $dir "
        "AND (valid_until IS NONE OR valid_until > $now) "
        "ORDER BY heat DESC LIMIT 20",
        {"dir": project_scope_key, "now": _now},
    )

    # Schema note: directory_context is binary (global OR project) today.
    # scope="both" reserved for future cross-scope migrations.
    global_ids: set = {storage._extract_id(r.get("id")) for r in global_rows}
    project_ids: set = {storage._extract_id(r.get("id")) for r in project_rows}
    seen: set = set()
    merged: list[dict] = []

    for row in global_rows + project_rows:
        mid = storage._extract_id(row.get("id"))
        if mid in seen:
            continue
        seen.add(mid)
        is_global = mid in global_ids
        is_project = mid in project_ids
        if is_global and is_project:
            scope = "both"
        elif is_project:
            scope = "project"
        else:
            scope = "global"
        merged.append(
            {
                "id": mid,
                "title": (row.get("content") or "")[:80],
                "tags": row.get("tags", []),
                "access_count": row.get("access_count") or 0,
                "scope": scope,
            }
        )

    return merged[:max_anchors]


@observe(tier="stage", metric="tools.project._fetch_anchor_redundancy_pairs")
def _fetch_anchor_redundancy_pairs(
    storage, resolved: str, _now: str, threshold: float
) -> tuple[list[list], bool]:
    """Fetch pairwise cosine-similarity candidates for same-dir project anchors.

    Returns (pairs_capped, truncated).  pairs_capped is sorted by similarity DESC
    and capped at _SIGNALS_CANDIDATES_K.  truncated=True when more pairs qualify.
    All errors return ([], False).

    Compact tuple encoding: each pair is [id_a, id_b, similarity] (list of 3 elements)
    rather than {"id_a": ..., "id_b": ..., "similarity": ...} to stay within the
    ≤100-token signals budget.
    """
    try:
        emb_rows = storage._q(
            "SELECT id, embedding FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND directory_context = $dir "
            "AND (valid_until IS NONE OR valid_until > $now) "
            "AND embedding IS NOT NONE",
            {"dir": resolved, "now": _now},
        )
        id_vec: list[tuple[int, list[float]]] = []
        for row in emb_rows:
            mid = storage._extract_id(row.get("id"))
            raw_emb = row.get("embedding")
            if raw_emb is None:
                continue
            if isinstance(raw_emb, (bytes, bytearray)):
                floats = storage._bytes_to_floats(raw_emb)
            elif isinstance(raw_emb, list):
                floats = [float(x) for x in raw_emb]
            else:
                continue
            id_vec.append((mid, floats))
        all_pairs: list[list] = []
        for i in range(len(id_vec)):
            for j in range(i + 1, len(id_vec)):
                mid_a, vec_a = id_vec[i]
                mid_b, vec_b = id_vec[j]
                sim = _cosine_similarity(vec_a, vec_b)
                if sim >= threshold:
                    all_pairs.append([mid_a, mid_b, round(sim, 4)])
        all_pairs.sort(key=lambda p: p[2], reverse=True)
        truncated = len(all_pairs) > _SIGNALS_CANDIDATES_K
        return all_pairs[:_SIGNALS_CANDIDATES_K], truncated
    except Exception:
        return [], False


@observe(tier="stage", metric="tools.project._fetch_anchor_promote_ids")
def _fetch_anchor_promote_ids(storage, resolved: str, _now: str, cfg) -> tuple[list[int], bool]:
    """Fetch IDs of anchors qualifying for promote-to-wiki detection.

    Triple AND: word_count > ANCHOR_PROMOTE_WORDS, header_count >=
    ANCHOR_PROMOTE_HEADERS, tags ∩ _ANCHOR_PROMOTE_TAGS ≠ ∅.
    Returns (ids_capped, truncated).  All errors return ([], False).
    """
    try:
        promote_rows = storage._q(
            "SELECT id, content, tags FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND directory_context = $dir "
            "AND (valid_until IS NONE OR valid_until > $now)",
            {"dir": resolved, "now": _now},
        )
        word_threshold = int(cfg.ANCHOR_PROMOTE_WORDS)
        header_threshold = int(cfg.ANCHOR_PROMOTE_HEADERS)
        all_promote: list[int] = []
        for row in promote_rows:
            content = row.get("content") or ""
            tags = row.get("tags") or []
            if len(content.split()) <= word_threshold:
                continue
            if _count_markdown_headers(content) < header_threshold:
                continue
            if not (_ANCHOR_PROMOTE_TAGS & set(tags)):
                continue
            all_promote.append(storage._extract_id(row.get("id")))
        truncated = len(all_promote) > _SIGNALS_CANDIDATES_K
        return all_promote[:_SIGNALS_CANDIDATES_K], truncated
    except Exception:
        return [], False


@observe(tier="stage", metric="tools.project._fetch_expired_anchor_count")
def _fetch_expired_anchor_count(storage, _now: str, directory: str) -> int:
    """Count expired anchors (valid_until < now) that are not in migration grace period.

    Scoped to *directory* so signal parity matches audit_anchors(directory=...) which
    also runs per-directory.  Using a global count here (no dir filter) caused phantom
    audit_anchors recommendations when expired anchors existed in other projects
    (2026-07-09 live-daemon regression, fix/anchor-signal-predicate-parity).
    """
    try:
        exp_rows = storage._q(
            "SELECT count() AS cnt FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND directory_context = $dir "
            "AND valid_until IS NOT NONE "
            "AND valid_until < $now "
            "AND (migration_grace IS NONE OR migration_grace = false) "
            "GROUP ALL",
            {"dir": directory, "now": _now},
        )
        return int(exp_rows[0]["cnt"]) if exp_rows else 0
    except Exception:
        return 0


@observe(tier="stage", metric="tools.project._fetch_cross_project_candidates_for_signals")
def _fetch_cross_project_candidates_for_signals(storage, _now: str, cfg) -> list[dict]:
    """Fetch cross-project redundancy candidates for signals mode payload.

    Delegates to audit module's detection logic.
    Capped to _SIGNALS_CANDIDATES_K for token budget.
    Returns empty list on any error (graceful degradation).
    """
    try:
        from yadgar.core.server.tools.audit import _fetch_cross_project_candidates  # noqa: PLC0415

        cross_threshold = float(cfg.ANCHOR_CROSS_PROJECT_COSINE)
        candidates = _fetch_cross_project_candidates(storage, _now, cross_threshold)
        return candidates[:_SIGNALS_CANDIDATES_K]
    except Exception:
        return []


# ── Roadmap update signal (v5.41.4) ───────────────────────────────────────────

#: Slug of the canonical roadmap wiki page.  Change here if wiki slug changes.
_ROADMAP_WIKI_SLUG = "yadgar-roadmap-future-improvements"

#: Regex patterns that indicate a ship commit (fallback when pyproject diff unavailable).
_SHIP_COMMIT_RE = re.compile(
    r"^merge:\s+v\d+\.\d+\.\d+|chore:\s+bump\s+version",
    re.IGNORECASE | re.MULTILINE,
)


@observe(tier="stage", metric="tools.project._get_master_head_info")
def _get_master_head_info(resolved: str) -> dict | None:
    """Return HEAD info for the default (master) branch of resolved repo.

    Returns dict with keys: commit_ts (float), commit_msg (str), pyproject_version (str | None).
    Returns None on any error (git not available, not a repo, etc.).

    Uses committer date (%ct) to match v5.41.4 plan spec (robust to rebases).
    All git invocations use _GIT_SAFE_ARGS + _git_safe_env() (H-10 hardening).
    """
    try:
        default_branch = _origin_head_short(resolved)
        # Committer timestamp
        ts_out = (
            subprocess.check_output(
                [
                    "git",
                    *_GIT_SAFE_ARGS,
                    "-C",
                    resolved,
                    "log",
                    default_branch,
                    "-1",
                    "--format=%ct",
                ],
                stderr=subprocess.DEVNULL,
                timeout=3,
                env=_git_safe_env(),
            )
            .decode()
            .strip()
        )
        if not ts_out:
            return None
        commit_ts = float(ts_out)

        # Commit message (subject + body)
        msg_out = (
            subprocess.check_output(
                [
                    "git",
                    *_GIT_SAFE_ARGS,
                    "-C",
                    resolved,
                    "log",
                    default_branch,
                    "-1",
                    "--format=%B",
                ],
                stderr=subprocess.DEVNULL,
                timeout=3,
                env=_git_safe_env(),
            )
            .decode()
            .strip()
        )

        # pyproject.toml version at master HEAD
        pyproject_version: str | None = None
        try:
            pyp_out = subprocess.check_output(
                [
                    "git",
                    *_GIT_SAFE_ARGS,
                    "-C",
                    resolved,
                    "show",
                    f"{default_branch}:pyproject.toml",
                ],
                stderr=subprocess.DEVNULL,
                timeout=3,
                env=_git_safe_env(),
            ).decode()
            m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', pyp_out, re.MULTILINE)
            if m:
                pyproject_version = m.group(1)
        except Exception:
            pass

        return {
            "commit_ts": commit_ts,
            "commit_msg": msg_out,
            "pyproject_version": pyproject_version,
        }
    except Exception:
        return None


@observe(tier="stage", metric="tools.project._get_pyproject_version_at_ts")
def _get_pyproject_version_at_ts(resolved: str, ts: float) -> str | None:
    """Return pyproject.toml version at the most recent master commit on or before ts.

    Finds the commit hash via `git log --until=<iso>` then reads pyproject.toml at that
    revision.  Returns None on any error or if pyproject.toml absent at that revision.
    """
    try:
        default_branch = _origin_head_short(resolved)
        dt = datetime.fromtimestamp(ts, UTC)
        until_iso = dt.strftime("%Y-%m-%dT%H:%M:%S")

        # Find the commit hash at or before ts on default_branch
        hash_out = (
            subprocess.check_output(
                [
                    "git",
                    *_GIT_SAFE_ARGS,
                    "-C",
                    resolved,
                    "log",
                    default_branch,
                    f"--until={until_iso}",
                    "-1",
                    "--format=%H",
                ],
                stderr=subprocess.DEVNULL,
                timeout=3,
                env=_git_safe_env(),
            )
            .decode()
            .strip()
        )
        if not hash_out:
            return None

        # Read pyproject.toml at that commit
        pyp_out = subprocess.check_output(
            ["git", *_GIT_SAFE_ARGS, "-C", resolved, "show", f"{hash_out}:pyproject.toml"],
            stderr=subprocess.DEVNULL,
            timeout=3,
            env=_git_safe_env(),
        ).decode()
        m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', pyp_out, re.MULTILINE)
        return m.group(1) if m else None
    except Exception:
        return None


@observe(tier="stage", metric="tools.project._get_roadmap_wiki_updated_at")
def _get_roadmap_wiki_updated_at(storage) -> float | None:
    """Return roadmap wiki page updated_at as a unix timestamp float.

    Queries wiki_page table directly for the canonical roadmap slug.
    Returns None when page not found or timestamp unparseable.
    Sentinel distinction: returns None (missing) vs 0.0 (parseable but epoch).
    """
    try:
        rows = storage._q(
            "SELECT updated_at FROM wiki_page WHERE slug = $slug LIMIT 1",
            {"slug": _ROADMAP_WIKI_SLUG},
        )
        if not rows:
            return None
        ts_raw = rows[0].get("updated_at")
        if ts_raw is None:
            return None
        if isinstance(ts_raw, (int, float)):
            return float(ts_raw)
        # ISO string
        ts_str = str(ts_raw).rstrip("Z").replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except Exception:
        return None


@observe(tier="hot", metric="tools.project._detect_ship")
def _detect_ship(head_info: dict, resolved: str, roadmap_ts: float) -> bool:
    """Return True when a ship is detected since roadmap_ts.

    PRIMARY:  pyproject version at master HEAD ≠ version at roadmap-updated-at commit.
    FALLBACK: HEAD commit message matches ^merge: vX.Y.Z or contains 'chore: bump version'.
    """
    head_version = head_info.get("pyproject_version")
    roadmap_version = _get_pyproject_version_at_ts(resolved, roadmap_ts)
    if head_version and roadmap_version and head_version != roadmap_version:
        return True
    msg = head_info.get("commit_msg") or ""
    return bool(_SHIP_COMMIT_RE.search(msg))


@observe(tier="stage", metric="tools.project._compute_roadmap_signal")
def _compute_roadmap_signal(resolved: str, storage) -> tuple[float, dict | None]:
    """Compute roadmap_update_lag_hours and optional update_roadmap action.

    Returns (lag_hours, action_dict_or_None).

    lag_hours semantics:
      -1.0  → roadmap wiki slug not found (I3 sentinel)
       0.0  → roadmap is up to date (updated_at >= master HEAD commit_ts)
      > 0   → master has moved X hours since roadmap was last updated
    """
    roadmap_ts = _get_roadmap_wiki_updated_at(storage)
    if roadmap_ts is None:
        return -1.0, None

    head_info = _get_master_head_info(resolved)
    if head_info is None:
        return 0.0, None

    lag_hours = max(0.0, (head_info["commit_ts"] - roadmap_ts) / 3600.0)
    if lag_hours <= 0.0:
        return 0.0, None

    if not _detect_ship(head_info, resolved, roadmap_ts):
        return lag_hours, None

    action = {
        "action": "update_roadmap",
        "reason": f"master moved {lag_hours:.1f}h ago; roadmap not updated since",
        "suggested_call": (
            "wiki_append_section("
            f"slug='{_ROADMAP_WIKI_SLUG}', "
            "section_heading='Recently shipped', "
            "content='- vX.Y.Z (date): description', "
            "position='start_of_section')"
        ),
    }
    return lag_hours, action


@observe(tier="stage", metric="tools.project._compute_anchor_signals")
def _compute_anchor_signals(storage, resolved: str, cfg) -> dict:
    """Compute anchor hygiene signals for project_brief(mode='signals').

    Delegates to focused sub-helpers to keep cyclomatic complexity bounded.
    All DB errors are swallowed; callers receive safe zero/empty defaults.
    """
    _now = storage._now_iso()

    try:
        count_rows = storage._q(
            "SELECT count() AS cnt FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND directory_context = $dir "
            "AND (valid_until IS NONE OR valid_until > $now) "
            "GROUP ALL",
            {"dir": resolved, "now": _now},
        )
        anchor_count_project: int = int(count_rows[0]["cnt"]) if count_rows else 0
    except Exception:
        anchor_count_project = 0

    redundancy_pairs, trunc_r = _fetch_anchor_redundancy_pairs(
        storage, resolved, _now, float(cfg.ANCHOR_REDUNDANCY_COSINE)
    )
    promote_ids, trunc_p = _fetch_anchor_promote_ids(storage, resolved, _now, cfg)
    expired_no_grace_count = _fetch_expired_anchor_count(storage, _now, resolved)
    cross_project_candidates = _fetch_cross_project_candidates_for_signals(storage, _now, cfg)

    return {
        "anchor_count_project": anchor_count_project,
        "anchor_redundancy_candidates": redundancy_pairs,
        "anchor_promote_candidates": promote_ids,
        "expired_no_grace_count": expired_no_grace_count,
        "cross_project_redundancy_candidates": cross_project_candidates,
        "_truncated": trunc_r or trunc_p,
    }


@observe(tier="stage", metric="tools.project._check_session_end_sentinel")
def _check_session_end_sentinel(storage, resolved: str) -> dict | None:
    """Check for an unprocessed session_end_sentinel memory row for this directory.

    Returns an extract_last_session_findings recommended_action dict, or None.
    Handles missing transcript (tombstone note) gracefully.
    v5.10.6.
    """
    import json as _json  # noqa: PLC0415 — local to avoid circular if json not top-level

    try:
        sentinel_rows = storage._q(
            "SELECT id, content, created_at FROM memory "
            "WHERE '_session_end_sentinel' INSIDE tags "
            "AND directory_context = $dir "
            "ORDER BY created_at DESC LIMIT 1",
            {"dir": resolved},
        )
    except Exception:
        return None

    if not sentinel_rows:
        return None

    row = sentinel_rows[0]
    try:
        sentinel_data = _json.loads(row.get("content", "{}"))
    except Exception:
        return None

    transcript_path = sentinel_data.get("transcript_path", "")
    ended_at = sentinel_data.get("ended_at", "")
    msg_count = sentinel_data.get("message_count", 0)
    last_human_turns = sentinel_data.get("last_human_turns", [])
    last_touched_files = sentinel_data.get("last_touched_files", [])
    sentinel_id = storage._extract_id(row.get("id"))

    transcript_exists = bool(transcript_path) and Path(transcript_path).exists()

    if transcript_exists:
        suggested_call = (
            f"# Read transcript at {transcript_path!r}, extract key decisions/findings,\n"
            f"# then call: memorize(content='...', context={resolved!r}, tags=['session-finding'])\n"
            f"# and: forget(memory_id={sentinel_id})"
        )
        reason = f"sentinel found: ended_at={ended_at}, msg_count={msg_count}"
    else:
        suggested_call = (
            f"# Transcript at {transcript_path!r} no longer exists.\n"
            f"# last_human_turns embedded in sentinel may still be useful.\n"
            f"# Call: forget(memory_id={sentinel_id})  # clean up stale sentinel"
        )
        reason = (
            f"sentinel found: ended_at={ended_at}, msg_count={msg_count}"
            f" [transcript_not_found — extract from memory only]"
        )

    return {
        "action": "extract_last_session_findings",
        "reason": reason,
        "suggested_call": suggested_call,
        "transcript_path": transcript_path,
        "sentinel_id": sentinel_id,
        "last_human_turns": last_human_turns,
        "last_touched_files": last_touched_files,
    }


@observe(tier="stage", metric="tools.project._build_recommended_actions")
def _build_recommended_actions(
    init_memory_present: bool,
    active_work_present: bool,
    active_work_age_hours: float | None,
    stale_checkpoint_hours: float | None,
    anchor_count_project: int = 0,
    redundancy_count: int = 0,
    promote_count: int = 0,
    expired_no_grace_count: int = 0,
) -> list[dict]:
    """Build deterministic recommended_actions list from signals + thresholds.

    Order: bootstrap_project → refresh_active_work/consider_refresh_active_work →
           refresh_checkpoint/consider_refresh_checkpoint →
           audit_anchors → merge_redundant_anchors → promote_anchor_to_wiki →
           forget_expired_anchors.

    Soft/hard mutual exclusion: for each row, exactly one fires — hard takes
    precedence when age > STALE_HOURS; soft fires when WARN_HOURS < age ≤ STALE_HOURS.

    Note: suggested_call fields are NOT populated here — the caller
    (_project_brief_signals) enriches them post-build to avoid the resolved-dir
    parameter propagating into this function (keeps param count ≤8).
    """
    cfg = get_settings()
    actions: list[dict] = []

    if not init_memory_present:
        actions.append(
            {
                "action": "bootstrap_project",
                "reason": "init_memory absent",
            }
        )

    # v5.10.1: soft/hard mutual exclusion for active_work
    if active_work_age_hours is not None:
        if active_work_age_hours > cfg.ACTIVE_WORK_STALE_HOURS:
            # Hard action — age exceeds stale threshold
            actions.append(
                {
                    "action": "refresh_active_work",
                    "reason": f"age_hours={active_work_age_hours:.1f} > threshold={cfg.ACTIVE_WORK_STALE_HOURS}",
                }
            )
        elif active_work_age_hours > cfg.ACTIVE_WORK_WARN_HOURS:
            # Soft action — age in warn window (WARN_HOURS < age ≤ STALE_HOURS)
            actions.append(
                {
                    "action": "consider_refresh_active_work",
                    "reason": f"age_hours={active_work_age_hours:.1f} > warn={cfg.ACTIVE_WORK_WARN_HOURS}; not yet stale ({cfg.ACTIVE_WORK_STALE_HOURS}h)",
                }
            )

    # v5.10.1: soft/hard mutual exclusion for checkpoint
    if stale_checkpoint_hours is not None:
        if stale_checkpoint_hours > cfg.CHECKPOINT_STALE_HOURS:
            # Hard action
            actions.append(
                {
                    "action": "refresh_checkpoint",
                    "reason": f"age_hours={stale_checkpoint_hours:.1f} > threshold={cfg.CHECKPOINT_STALE_HOURS}",
                }
            )
        elif stale_checkpoint_hours > cfg.CHECKPOINT_WARN_HOURS:
            # Soft action
            actions.append(
                {
                    "action": "consider_refresh_checkpoint",
                    "reason": f"age_hours={stale_checkpoint_hours:.1f} > warn={cfg.CHECKPOINT_WARN_HOURS}; not yet stale ({cfg.CHECKPOINT_STALE_HOURS}h)",
                }
            )

    # v5.8.0 anchor hygiene actions — gated on actual actionability, not raw count.
    # Only emit audit_anchors when there is work to do: expired anchors without grace,
    # redundant pairs above cosine threshold, or oversized anchors promotable to wiki.
    # Phantom action names (forget_expired_anchors, merge_redundant_anchors,
    # promote_anchor_to_wiki) are internal audit-action strings, not MCP tools —
    # all cases collapse to a single "audit_anchors" recommendation with a reason
    # that names what is actionable.
    actionable_parts: list[str] = []
    if expired_no_grace_count >= 1:
        actionable_parts.append(f"{expired_no_grace_count} expired")
    if redundancy_count >= 1:
        actionable_parts.append(f"{redundancy_count} redundant pairs")
    if promote_count >= 1:
        actionable_parts.append(f"{promote_count} promotable")
    if actionable_parts:
        actions.append(
            {
                "action": "audit_anchors",
                "reason": "; ".join(actionable_parts),
            }
        )

    return actions


@observe(tier="stage", metric="tools.project._apply_roadmap_signal")
def _apply_roadmap_signal(resolved: str, storage, actions: list) -> float:
    """Compute roadmap lag and append update_roadmap action if warranted.

    Returns roadmap_update_lag_hours (-1 = wiki missing, 0 = up-to-date, >0 = lag).
    Appends to actions in-place when a ship is detected.  Never raises.
    """
    if storage is None:
        return -1.0
    try:
        lag, action = _compute_roadmap_signal(resolved, storage)
        if action is not None:
            actions.append(action)
        return lag
    except Exception:
        return -1.0


# ── v5.42.0: DLQ rejection signal ───────────────────────────────────────────

#: failure_reason values treated as "rejections" (must match admin_dlq._REJECTION_TAXONOMY).
_REJECTION_REASONS: frozenset[str] = frozenset({"duplicate_detected", "policy_rejected"})


@observe(tier="stage", metric="tools.project._compute_pending_rejections")
def _compute_pending_rejections(resolved: str) -> int:
    """Count DLQ rejection entries whose caller_context.directory matches resolved.

    Reads DLQ sidecar files directly (single-pass, O(n) files, no DB query).
    Returns 0 on any error (graceful degradation).

    Filters by caller_context.directory to enable cross-directory isolation: Stop hook
    only surfaces rejections relevant to the current project. dlq_inspect(filter='rejections')
    still lists all rejections regardless of directory.

    v5.42.0 plan §3.3 spec.
    """
    import json as _json  # noqa: PLC0415

    try:
        from yadgar.core.lifecycle import _get_file_queue  # noqa: PLC0415

        fq = _get_file_queue()
        dlq_dir = fq.dlq_dir
    except Exception:
        return 0
    if not dlq_dir.exists():
        return 0
    count = 0
    try:
        for sidecar in dlq_dir.glob("*.json.error.json"):
            try:
                meta = _json.loads(sidecar.read_text())
                failure_reason = meta.get("failure_reason") or "permanent_error"
                if failure_reason not in _REJECTION_REASONS:
                    continue
                caller_dir = (
                    (meta.get("failure_metadata") or {})
                    .get("caller_context", {})
                    .get("directory", "")
                )
                if caller_dir and caller_dir == resolved:
                    count += 1
            except Exception:
                continue
    except Exception:
        pass
    return count


@observe(tier="stage", metric="tools.project._apply_rejection_signal")
def _apply_rejection_signal(resolved: str, actions: list) -> int:
    """Compute pending_rejections_count and append review_rejections action if warranted.

    Returns count (0 if none or on error). Appends to actions in-place. Never raises.
    Same structural pattern as _apply_roadmap_signal.
    """
    try:
        count = _compute_pending_rejections(resolved)
        if count > 0:
            actions.append(
                {
                    "action": "review_rejections",
                    "reason": f"{count} write rejection(s) pending review",
                    "suggested_call": "dlq_inspect(filter='rejections')",
                }
            )
        return count
    except Exception:
        return 0


# ── v5.84.0 car #12: ADR nudge signal ─────────────────────────────────────────


@observe(tier="stage", metric="tools.project._get_adr_log_updated_at")
def _get_adr_log_updated_at(storage, resolved: str) -> float | None:
    """Return the ADR ledger's most-recent ``updated_at`` for this project.

    Car G (0047 §7): re-pointed off the deleted ``<project>-adr-index`` wiki
    page onto the ``adr`` SQL ledger (D35a — the seed lifted the per-ADR
    PAGES into rows). The storage engine exposes ``max_adr_updated_at`` for
    this purpose; ``storage`` is passed in so the call does not import the
    engine directly. Returns ``None`` when the table has no rows for the
    project, or the timestamp is unparseable.
    """
    import os as _os  # noqa: PLC0415

    project_name = _os.path.basename(resolved)
    try:
        # C5 (0047 PR#40 §5): the ``derive_project_id`` call and its
        # basename fallback are DELETED. This is the ADR-due NUDGE — a
        # best-effort staleness hint on the session-start brief, not a write —
        # so it keys on the directory basename it already computed and says so.
        # It must not raise into ``project_brief`` (an unresolvable identity is
        # not a reason to fail the whole brief) and it must not invent a
        # project_id either; the basename is a LOOKUP KEY here, never stamped
        # onto a row. C6 re-points the nudge at the registry.
        max_dt = storage.max_adr_updated_at(project_id=project_name)
    except AttributeError:
        # The storage engine surface is partial — fall back to the live
        # forward path (preserves the legacy test stubs).
        max_dt = _get_adr_log_updated_at_via_forward(resolved)
    except Exception:  # noqa: BLE001
        return None
    if max_dt is None:
        return None
    if isinstance(max_dt, (int, float)):
        return float(max_dt)
    try:
        dt = (
            max_dt
            if isinstance(max_dt, datetime)
            else datetime.fromisoformat(str(max_dt).rstrip("Z").replace("Z", "+00:00"))
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return None


@observe(tier="stage", metric="tools.project._get_adr_log_updated_at_fwd")
def _get_adr_log_updated_at_via_forward(resolved: str) -> datetime | None:
    """Forward path for ``_get_adr_log_updated_at`` — covers the test seam.

    Production callers reach ``storage.max_adr_updated_at`` directly. When
    the storage surface is a stub (unit-test path) the ``AttributeError``
    fallback above routes through here, which talks to the backend over
    the canonical PTC chain.
    """
    import os as _os  # noqa: PLC0415

    # C5 (0047 PR#40 §5): ``derive_project_id`` + basename fallback deleted;
    # same reasoning as the storage-surface arm above — a nudge lookup key, not
    # a stamped identity.
    project_name = _os.path.basename(resolved)

    try:
        result = _forward_admin("max_adr_updated_at", {"project_id": project_name})
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(result, dict):
        return None
    raw = result.get("updated_at") or result.get("timestamp")
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(str(raw).rstrip("Z").replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


@observe(tier="stage", metric="tools.project._get_active_work_updated_at")
def _get_active_work_updated_at(storage, resolved: str) -> float | None:
    """Return the most recent _active_work memory created_at as a unix timestamp float.

    Returns None when no _active_work memory is found for this directory.
    """
    try:
        rows = storage._q(
            "SELECT created_at FROM memory WHERE directory_context = $dir "
            "AND '_active_work' INSIDE tags LIMIT 1",
            {"dir": resolved},
        )
        if not rows:
            return None
        ts_raw = rows[0].get("created_at")
        if ts_raw is None:
            return None
        if isinstance(ts_raw, (int, float)):
            return float(ts_raw)
        ts_str = str(ts_raw).rstrip("Z").replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except Exception:
        return None


@observe(tier="stage", metric="tools.project._apply_adr_signal")
def _apply_adr_signal(resolved: str, storage, actions: list) -> None:
    """Append capture_adr action when decisions are likely uncaptured.

    Heuristic: active_work was updated more recently than the ADR log by
    > ADR_DUE_WARN_HOURS.  Fires only when there is evidence of session activity
    (active_work present).  Silent when active_work absent or ADR log is fresh.
    Never raises.

    Same structural pattern as _apply_roadmap_signal / _apply_rejection_signal.
    """
    if storage is None:
        return
    try:
        cfg = get_settings()
        warn_hours = cfg.ADR_DUE_WARN_HOURS

        active_work_ts = _get_active_work_updated_at(storage, resolved)
        if active_work_ts is None:
            # No session activity detected — silent.
            return

        adr_ts = _get_adr_log_updated_at(storage, resolved)
        now = time.time()

        if adr_ts is None:
            # ADR log absent but active_work present — always fire (0 ADRs captured yet).
            active_work_age_h = (now - active_work_ts) / 3600.0
            actions.append(
                {
                    "action": "capture_adr",
                    "reason": (
                        f"active_work updated {active_work_age_h:.1f}h ago; "
                        "ADR log absent — no decisions captured yet"
                    ),
                    "suggested_call": (
                        f"adr_add(directory={resolved!r}, title='...', status='open', "
                        "date='YYYY-MM-DD', context='...', decision='...', "
                        "rationale='...', alternatives='...', consequences='...', "
                        "revisit_trigger='...', supersedes='none')"
                    ),
                }
            )
            return

        # Both timestamps available — fire when ADR log is stale relative to active_work.
        delta_hours = (active_work_ts - adr_ts) / 3600.0
        if delta_hours > warn_hours:
            active_work_age_h = (now - active_work_ts) / 3600.0
            adr_age_h = (now - adr_ts) / 3600.0
            actions.append(
                {
                    "action": "capture_adr",
                    "reason": (
                        f"active_work updated {active_work_age_h:.1f}h ago; "
                        f"last ADR {adr_age_h:.1f}h ago "
                        f"(delta {delta_hours:.1f}h > threshold {warn_hours}h)"
                    ),
                    "suggested_call": (
                        f"adr_add(directory={resolved!r}, title='...', status='open', "
                        "date='YYYY-MM-DD', context='...', decision='...', "
                        "rationale='...', alternatives='...', consequences='...', "
                        "revisit_trigger='...', supersedes='none')"
                    ),
                }
            )
    except Exception:
        return


@observe(tier="stage", metric="tools.project._get_agent_prompt_toc_updated_at")
def _get_agent_prompt_toc_updated_at(storage, resolved: str) -> float | None:
    """Return the global agent-prompt library's last-grow timestamp as a unix float.

    0047 Car I: the S6 restore-surface signal reads ``MAX(agent_pattern.updated_at)``
    instead of the old wiki-TOC page's ``updated_at`` (the TOC page is retired
    per D35a — kept as an ignored pointer slug). The new op reaches
    ``get_agent_prompt_toc_updated_at`` via the backend /admin dispatcher; the
    engine-#2 absence path returns ``None`` so the S6 caller treats the
    restore surface as missing rather than crashing the project_brief build.
    Returns None when the table is empty or unreachable.
    Same pattern as _get_adr_log_updated_at.
    """
    try:
        result = _forward_admin("get_agent_prompt_toc_updated_at", {})
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    if result.get("ok") is False:
        return None
    ts = result.get("timestamp")
    if ts is None:
        return None
    try:
        return float(ts)
    except (TypeError, ValueError):  # fmt: off
        return None  # fmt: on


@observe(tier="stage", metric="tools.project._get_dispatch_prelude_updated_at")
def _get_dispatch_prelude_updated_at(storage, resolved: str) -> float | None:
    """Return the most recent _dispatch_prelude marker created_at as a unix timestamp.

    Returns None when no marker is found for this directory.
    Mirrors _get_active_work_updated_at but queries tag '_dispatch_prelude'.
    """
    try:
        rows = storage._q(
            "SELECT created_at FROM memory WHERE directory_context = $dir "
            "AND '_dispatch_prelude' INSIDE tags LIMIT 1",
            {"dir": resolved},
        )
        if not rows:
            return None
        ts_raw = rows[0].get("created_at")
        if ts_raw is None:
            return None
        if isinstance(ts_raw, (int, float)):
            return float(ts_raw)
        ts_str = str(ts_raw).rstrip("Z").replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except Exception:
        return None


@observe(tier="stage", metric="tools.project._apply_dispatch_prelude_signal")
def _apply_dispatch_prelude_signal(resolved: str, storage, actions: list) -> None:
    """Append use_agent_prompt_library action when the library hasn't been used recently.

    READ-side mirror of _apply_agent_prompt_signal (capture/write-side, ADR-0007).
    Instead of checking whether prompts were SAVED, checks whether
    agent_dispatch_prelude was CALLED (the _dispatch_prelude marker).

    Gates (same pattern as _apply_agent_prompt_signal):
    - Kill-gate: silent when AGENT_PROMPT_LIBRARY_ENABLED is False.
    - Activity gate: active_work present (session did real work).
    - Freshness gate: active_work updated > DISPATCH_PRELUDE_DUE_WARN_HOURS more
      recently than the prelude marker.  Absent marker + active_work → fire.
    Never raises.
    """
    if storage is None:
        return
    try:
        cfg = get_settings()
        if not cfg.AGENT_PROMPT_LIBRARY_ENABLED:
            return
        warn_hours = cfg.DISPATCH_PRELUDE_DUE_WARN_HOURS

        active_work_ts = _get_active_work_updated_at(storage, resolved)
        if active_work_ts is None:
            return

        prelude_ts = _get_dispatch_prelude_updated_at(storage, resolved)
        now = time.time()
        suggested_call = (
            "agent_dispatch_prelude(pattern='<kebab-task-shape>', task_topic='<short topic>')"
        )

        if prelude_ts is None:
            active_work_age_h = (now - active_work_ts) / 3600.0
            actions.append(
                {
                    "action": "use_agent_prompt_library",
                    "reason": (
                        f"active_work updated {active_work_age_h:.1f}h ago; "
                        "agent_dispatch_prelude has never been called — "
                        "check library before dispatching subagents"
                    ),
                    "suggested_call": suggested_call,
                }
            )
            return

        delta_hours = (active_work_ts - prelude_ts) / 3600.0
        if delta_hours > warn_hours:
            active_work_age_h = (now - active_work_ts) / 3600.0
            prelude_age_h = (now - prelude_ts) / 3600.0
            actions.append(
                {
                    "action": "use_agent_prompt_library",
                    "reason": (
                        f"active_work updated {active_work_age_h:.1f}h ago; "
                        f"agent-prompt library last USED {prelude_age_h:.1f}h ago "
                        f"(delta {delta_hours:.1f}h > threshold {warn_hours}h)"
                    ),
                    "suggested_call": suggested_call,
                }
            )
    except Exception:
        return


@observe(tier="stage", metric="tools.project._apply_agent_prompt_signal")
def _apply_agent_prompt_signal(resolved: str, storage, actions: list) -> None:
    """Append capture_agent_prompt action when the prompt library looks stale.

    ADR-0007 capture loop. Mirrors _apply_adr_signal structurally but keyed on the
    GLOBAL agent-prompt TOC instead of the ADR log:

    - HARD kill-gate FIRST: silent when AGENT_PROMPT_LIBRARY_ENABLED is False
      (reuses the existing flag — no new knob).
    - Activity gate: active_work present (evidence the session did real work).
    - Freshness gate: active_work updated > ADR_DUE_WARN_HOURS more recently than
      the TOC last grew (reuses the ADR cadence threshold — no new knob).
      Absent TOC + active_work present → fire (library never seeded).

    Cadence note: the TOC is GLOBAL, so a save in any project refreshes it and
    suppresses the nudge everywhere — fine for an ambient cadence nudge. The
    precise per-session "is this prompt reusable?" scan lives in the stop-hook
    step; this nudge only reminds at the same age basis the ADR nudge uses.
    Never raises.
    """
    if storage is None:
        return
    try:
        cfg = get_settings()
        # Kill-gate: library disabled → fully silent.
        if not cfg.AGENT_PROMPT_LIBRARY_ENABLED:
            return
        warn_hours = cfg.ADR_DUE_WARN_HOURS

        active_work_ts = _get_active_work_updated_at(storage, resolved)
        if active_work_ts is None:
            # No session activity detected — silent.
            return

        toc_ts = _get_agent_prompt_toc_updated_at(storage, resolved)
        now = time.time()
        suggested_call = (
            f"agent_prompt_save(directory={resolved!r}, pattern='<kebab-task-shape>', "
            "content='<the dispatch prompt>', purpose='<one line>')"
        )

        if toc_ts is None:
            # TOC absent but active_work present — library never seeded; fire.
            active_work_age_h = (now - active_work_ts) / 3600.0
            actions.append(
                {
                    "action": "capture_agent_prompt",
                    "reason": (
                        f"active_work updated {active_work_age_h:.1f}h ago; "
                        "agent-prompt library empty — no reusable prompts captured yet"
                    ),
                    "suggested_call": suggested_call,
                }
            )
            return

        # Both timestamps available — fire when the library is stale vs active_work.
        delta_hours = (active_work_ts - toc_ts) / 3600.0
        if delta_hours > warn_hours:
            active_work_age_h = (now - active_work_ts) / 3600.0
            toc_age_h = (now - toc_ts) / 3600.0
            actions.append(
                {
                    "action": "capture_agent_prompt",
                    "reason": (
                        f"active_work updated {active_work_age_h:.1f}h ago; "
                        f"agent-prompt library last grew {toc_age_h:.1f}h ago "
                        f"(delta {delta_hours:.1f}h > threshold {warn_hours}h)"
                    ),
                    "suggested_call": suggested_call,
                }
            )
    except Exception:
        return


@observe(tier="hot", metric="tools.project._omit_sentinel")
def _omit_sentinel(d: dict, key: str, value: object, sentinel: object) -> None:
    """Set d[key]=value only when value != sentinel (for budget-trimming optional fields)."""
    if value != sentinel:
        d[key] = value


@observe(tier="stage", metric="tools.project._project_brief_signals")
def _project_brief_signals(
    resolved: str,
    mode: str,
    init_memory_present: bool,
    active_work_present: bool,
    init_memory_age_hours: float | None,
    active_work_age_hours: float | None,
    stale_checkpoint_hours: float | None,
    storage=None,
) -> dict:
    """Build signals mode payload (<100 tokens).

    v5.8.0: includes 3 new anchor hygiene signals (anchor_count_project,
    anchor_redundancy_candidates, anchor_promote_candidates) and 4 new
    recommended_actions types.  storage arg required for anchor queries;
    signals degrade gracefully (empty lists, count=0) when storage=None.
    """
    cfg = get_settings()
    if storage is not None:
        anchor_signals = _compute_anchor_signals(storage, resolved, cfg)
    else:
        anchor_signals = {
            "anchor_count_project": 0,
            "anchor_redundancy_candidates": [],
            "anchor_promote_candidates": [],
            "expired_no_grace_count": 0,
            "cross_project_redundancy_candidates": [],
            "_truncated": False,
        }

    recommended_actions = _build_recommended_actions(
        init_memory_present=init_memory_present,
        active_work_present=active_work_present,
        active_work_age_hours=active_work_age_hours,
        stale_checkpoint_hours=stale_checkpoint_hours,
        anchor_count_project=anchor_signals["anchor_count_project"],
        redundancy_count=len(anchor_signals["anchor_redundancy_candidates"]),
        promote_count=len(anchor_signals["anchor_promote_candidates"]),
        expired_no_grace_count=anchor_signals["expired_no_grace_count"],
    )
    # Enrich actions with suggested_call (copy-paste-able MCP call) — v5.9+v5.10.1 pattern.
    # Enrichment is done post-build to avoid passing resolved dir into _build_recommended_actions.
    # C5b: the suggested call must be copy-paste-able, and ``update_active_work``
    # now RAISES without ``project=`` (its write path stamps project_id). A
    # suggestion that fails on paste is worse than none. The literal placeholder
    # is deliberate — this helper has no ``project`` in scope, and inventing one
    # from ``resolved`` is the derivation ADR-0227 deletes.
    _aw_call = f"update_active_work(directory={resolved!r}, content='...', project='<owner/repo>')"
    _cp_call = f"checkpoint(directory={resolved!r}, current_task='...', key_decisions=[...], next_steps=[...])"
    _audit_call = f"audit_anchors(directory={resolved!r}, dry_run=True)"
    for action_entry in recommended_actions:
        act = action_entry.get("action")
        if act in ("refresh_active_work", "consider_refresh_active_work"):
            action_entry["suggested_call"] = _aw_call
        elif act in ("refresh_checkpoint", "consider_refresh_checkpoint"):
            action_entry["suggested_call"] = _cp_call
        elif act == "audit_anchors":
            action_entry["suggested_call"] = _audit_call

    # v5.10.6: session-end sentinel check — surface extract_last_session_findings action.
    if storage is not None:
        _sentinel_action = _check_session_end_sentinel(storage, resolved)
        if _sentinel_action is not None:
            recommended_actions.append(_sentinel_action)

    # v5.41.4: roadmap update lag signal.
    roadmap_update_lag_hours = _apply_roadmap_signal(resolved, storage, recommended_actions)

    # v5.42.0: DLQ rejection signal — pending_rejections_count + review_rejections action.
    pending_rejections_count = _apply_rejection_signal(resolved, recommended_actions)

    # v5.84.0 car #12: ADR nudge signal — capture_adr action when decisions uncaptured.
    _apply_adr_signal(resolved, storage, recommended_actions)

    # ADR-0007 capture loop: agent-prompt nudge — capture_agent_prompt action when the
    # prompt library is stale (gated on AGENT_PROMPT_LIBRARY_ENABLED; silent when off).
    _apply_agent_prompt_signal(resolved, storage, recommended_actions)

    # v5.89 #69: read-side mirror — use_agent_prompt_library when prelude not called recently.
    _apply_dispatch_prelude_signal(resolved, storage, recommended_actions)

    # v5.53.1: compute real stale wiki count (TTL-cached, cheap for hot path).
    stale_wiki_count = _compute_stale_wiki_count(resolved)

    result: dict = {
        "_resolved_directory": resolved,
        "_mode": mode,
        "init_memory_present": init_memory_present,
        "active_work_present": active_work_present,
        "stale_wiki_count": stale_wiki_count,
        "stale_checkpoint_hours": stale_checkpoint_hours,
        "active_work_age_hours": active_work_age_hours,
        "init_memory_age_hours": init_memory_age_hours,
        "anchor_count_project": anchor_signals["anchor_count_project"],
        "recommended_actions": recommended_actions,
    }
    # v5.46.4: omit roadmap_update_lag_hours when -1.0 (roadmap page not found) to
    # stay within 100-token budget. Callers treat absent key as -1 (no roadmap).
    _omit_sentinel(result, "roadmap_update_lag_hours", roadmap_update_lag_hours, -1.0)
    # v5.42.0: omit pending_rejections_count when 0 to stay within 100-token budget.
    # Non-zero count is always included; callers treat absent key as 0.
    if pending_rejections_count > 0:
        result["pending_rejections_count"] = pending_rejections_count
    # Omit empty candidate lists to stay within 100-token budget.
    # Non-empty lists are always included; callers must handle key absence for
    # empty case (equivalent to empty list).
    if anchor_signals["anchor_redundancy_candidates"]:
        result["anchor_redundancy_candidates"] = anchor_signals["anchor_redundancy_candidates"]
    if anchor_signals["anchor_promote_candidates"]:
        result["anchor_promote_candidates"] = anchor_signals["anchor_promote_candidates"]
    if anchor_signals.get("cross_project_redundancy_candidates"):
        result["cross_project_redundancy_candidates"] = anchor_signals[
            "cross_project_redundancy_candidates"
        ]
    if anchor_signals["_truncated"]:
        result["_truncated"] = True
    # Observability: fire counter if payload exceeds operator-tunable token budget.
    _payload_tokens = len(__import__("json").dumps(result)) // 4
    if _payload_tokens > cfg.SIGNALS_TOKEN_BUDGET_SOFT:
        try:
            from yadgar._shared.observability.metrics import yadgar_signals_payload_oversized_total

            yadgar_signals_payload_oversized_total.inc()
        except Exception:
            pass  # non-fatal; metrics not available in all environments
    return result


@observe(tier="stage", metric="tools.project._build_recent_writes")
def _build_recent_writes(storage, resolved: str, limit: int = 10) -> list[dict]:
    """Fetch memories written in the last 24h for this project, newest first.

    Returns a compact list: id, created_at, content (≤150 chars), tags.
    Used by _project_brief_restore to surface recent work after compaction.
    """
    from datetime import timedelta

    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    try:
        rows = storage.get_recent_memories_since(
            since=cutoff,
            limit=limit,
            directory=resolved if resolved else None,
        )
    except Exception:
        return []
    result = []
    for row in rows:
        content = row.get("content") or ""
        result.append(
            {
                "id": row.get("id"),
                "created_at": row.get("created_at"),
                "content": content[:150],
                "tags": row.get("tags") or [],
            }
        )
    return result


@observe(tier="stage", metric="tools.project._build_adr_log")
def _build_adr_log(resolved: str) -> dict:
    """Build the adr_log field for restore mode.

    Car G (0047 §7): re-pointed off the deleted ``<project>-adr-index`` wiki
    page onto the SQL ledger (``list_adr_rows`` ordered by id DESC, take 3).
    The pre-G shape — ``wiki_read + parse_index_rows`` — would resolve a
    dead slug now that the index page carries the ``superseded-by-ledger``
    tag (D35d, rollback path for one release cycle) and its content has
    been replaced by a one-line pointer.

    Returns a cheap metadata-only dict: ``slug`` (the
    ``superseded-by-ledger``-tagged index page — preserved for the
    one-cycle rollback per D35d) + up to 3 most-recent ADR ids drawn from
    the ledger. ``slug`` is intentionally kept stable so external callers
    that pin the string continue to work.
    """
    import os as _os  # noqa: PLC0415

    project_name = _os.path.basename(resolved)
    # D35d: the index page slug is preserved for one release cycle. After
    # the rollback window the seed op marks the page
    # ``superseded-by-ledger`` (D35d — kept-and-ignored, NOT deleted).
    slug = f"{project_name}-adr-index"
    latest_ids: list[str] = []
    try:
        # C5 (0047 PR#40 §5): ``derive_project_id`` + basename fallback deleted.
        # This builds the ADR *log* section of the session brief — a read-only
        # convenience listing — so it keys on the directory basename it already
        # holds and never stamps one. C6 re-points it at the registry.
        result = _forward_admin("list_adr_rows", {"project_id": project_name})
        rows_raw = result.get("rows") if isinstance(result, dict) else []
        rows = rows_raw if isinstance(rows_raw, list) else []
        ordered = sorted(
            (r for r in rows if isinstance(r, dict)),
            key=lambda r: int(r.get("id", 0) or 0),
            reverse=True,
        )
        latest_ids = [f"ADR-{int(r['id']):04d}" for r in ordered[:3]]
    except Exception:  # noqa: BLE001 — degraded mode returns empty latest_ids
        latest_ids = []
    return {"slug": slug, "latest_ids": latest_ids}


@observe(tier="stage", metric="tools.project._build_agent_prompt_toc")
def _build_agent_prompt_toc(storage) -> dict:
    """Build the agent_prompt_toc field for restore mode (S6 discovery surface).

    0047 Car I: the TOC is now the ``agent_pattern`` ledger table (replaces the
    wiki-TOC page scan). The page slug ``agent-prompt-toc`` is KEPT as a
    pointer-only entry (D35d) for one cycle — the restore surface reaches the
    table instead. Slug-shaped response is preserved so callers that pin the
    slug still see a stable shape; ``patterns`` carries the pattern NAMES
    (top 20 by ``uses`` DESC) instead of wiki-row regex matches.
    Returns a cheap metadata-only dict: slug + capped pattern list (no body)
    to keep the restore token budget safe. Graceful on any error → empty
    patterns.
    """
    from yadgar.core.server.tools.agent_prompts import _TOC_POINTER_SLUG  # noqa: PLC0415

    patterns: list[str] = []
    try:
        result = _forward_admin("list_agent_pattern_rows_uses_desc", {"limit": 20})
        if isinstance(result, dict) and result.get("ok") is not False:
            rows = result.get("rows") or []
            patterns = [str(r.get("name", "")) for r in rows if r.get("name")][:20]
    except Exception:
        patterns = []
    return {"slug": _TOC_POINTER_SLUG, "patterns": patterns}


@observe(tier="stage", metric="tools.project._project_brief_restore")
def _project_brief_restore(
    resolved: str,
    mode: str,
    storage,
    checkpoint_rows: list,
    project_scope_key: str | None = None,
) -> dict:
    """Build restore mode payload (<800 tokens).

    Car F7: ``top_anchors``/``hot_memories`` key off ``project_scope_key``
    (resolved project_id when the caller named one, else ``resolved`` — see
    ``project_brief``'s docstring comment). Every OTHER field here
    (``key_wiki_pages``, ``wiki_catalog``, ``adr_log``, ``recent_writes``,
    ``checkpoint``) stays on ``resolved`` — those buckets are directory-keyed
    by design (matching ``TestProjectBriefWikiScoping``'s documented reasons).

    ``project_scope_key`` defaults to ``None`` (falling back to ``resolved``
    below) so the direct-call test fixtures in ``test_fresh_memory_restore.py``
    — which construct this payload from a mocked storage without threading
    the new parameter — keep working unchanged; ``project_brief()`` itself
    always passes it explicitly.
    """
    if not project_scope_key:
        project_scope_key = resolved
    out = {
        "_resolved_directory": resolved,
        "_mode": mode,
        "top_anchors": _build_anchor_rows_restore(storage, project_scope_key),
        "hot_memories": _build_hot_memories(storage, project_scope_key, limit=5, snippet=150),
        "checkpoint": _build_checkpoint_dict(checkpoint_rows),
        "key_wiki_pages": _build_wiki_pages(storage, limit=3, directory=resolved),
        # v5.53.0: grouped wiki catalog (metadata-only, length-capped).
        "wiki_catalog": _build_wiki_catalog(storage, resolved),
        # #35: recent writes in last 24h — helps agent recall what was stored before compaction.
        "recent_writes": _build_recent_writes(storage, resolved),
        # car #13: ADR log first-class in restore context (slug + up to 3 latest IDs).
        "adr_log": _build_adr_log(resolved),
    }
    # S6: surface the global agent-prompt TOC — gated by AGENT_PROMPT_LIBRARY_ENABLED.
    # Flag-False suppresses the surface entirely (library is inert).
    if get_settings().AGENT_PROMPT_LIBRARY_ENABLED:
        out["agent_prompt_toc"] = _build_agent_prompt_toc(storage)
    return out


@observe(tier="stage", metric="tools.project._project_brief_catalog_full")
def _project_brief_catalog_full(ctx: dict) -> dict:
    """Build catalog/full mode payload (back-compat).

    catalog mode is DEPRECATED as of v5.7.12. Kept for back-compat until v5.8.
    ctx keys: resolved, mode, project, storage, init_rows, active_rows,
              init_memory_present, active_work_present, checkpoint_rows,
              project_scope_key.

    Car F7: ``top_anchors_project``/``hot_memories`` key off
    ``project_scope_key`` (resolved project_id when the caller named one,
    else ``resolved`` — see ``project_brief``'s docstring comment).
    ``top_anchors_global``, ``key_wiki_pages``, ``wiki_catalog``,
    ``recent_adrs`` and ``recent_episode_count`` stay on ``resolved`` /
    the tag-based predicate — unrelated to the bucket this car fixes.
    """
    from datetime import timedelta

    resolved = ctx["resolved"]
    mode = ctx["mode"]
    storage = ctx["storage"]
    init_rows = ctx["init_rows"]
    active_rows = ctx["active_rows"]
    checkpoint_rows = ctx["checkpoint_rows"]
    project_scope_key = ctx["project_scope_key"]

    top_anchors_global, top_anchors_project, top_anchors = _build_anchor_rows_catalog(
        storage, project_scope_key
    )
    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    ep_rows = storage._q(
        "SELECT id FROM memory WHERE directory_context = $dir "
        "AND store_type = 'episodic' AND created_at >= $cutoff",
        {"dir": resolved, "cutoff": cutoff},
    )
    result: dict = {
        "_resolved_directory": resolved,
        "_mode": mode,
        "project": ctx["project"],
        "tech": [],
        "init_memory_present": ctx["init_memory_present"],
        "active_work_present": ctx["active_work_present"],
        "top_anchors": top_anchors,
        "top_anchors_global": top_anchors_global,
        "top_anchors_project": top_anchors_project,
        "recent_episode_count": len(ep_rows),
        # v5.53.1: real stale count (TTL-cached).
        "stale_wiki_count": _compute_stale_wiki_count(resolved),
        "hot_memories": _build_hot_memories(storage, project_scope_key, limit=3, snippet=100),
        "key_wiki_pages": _build_wiki_pages(storage, limit=3, directory=resolved),
        "checkpoint": _build_checkpoint_dict(checkpoint_rows),
        # v5.53.0: grouped wiki catalog (metadata-only, length-capped).
        "wiki_catalog": _build_wiki_catalog(storage, resolved),
        # Car 2: Recent ADRs (temporal) — reads the canonical index (reuses _build_adr_log).
        "recent_adrs": _build_adr_log(resolved),
    }
    if mode == "full":
        result["init_memory"] = init_rows[0].get("content") if init_rows else None
        result["active_work"] = active_rows[0].get("content") if active_rows else None
        result["hot_memories"] = _build_hot_memories(
            storage, project_scope_key, limit=10, snippet=200
        )
        result["key_wiki_pages"] = _build_wiki_pages(storage, limit=5, directory=resolved)
        result["wiki_catalog"] = _build_wiki_catalog(storage, resolved)
    # §28 — add _render for catalog+full (back-compat); signals+restore omit it
    result["_render"] = _render_project_brief(result)
    return result


# ── Car 1 (v5.111): project_brief whole-payload cache ─────────────────────────
#
# Query-AGNOSTIC — every agent hitting the same (dir, mode) computes an
# identical brief, so the key has no query term and cross-agent calls collapse to
# one compute. Invalidation = Epoch(dir) folded into the key + a TTL(300) backstop
# for heat/anchor drift (which does NOT bump the epoch). deep_copy=True because the
# brief dict (and its row-dicts) is mutated by callers / _render.
#
# The epoch bus already busts on the two STRUCTURAL writes (memorize via
# _bump_epoch_for_context; consolidation's global bump). ⚠️ Epoch-key
# normalization: the read below and every bump caller MUST feed the SAME
# git-root-resolved key — see _bump_epoch_for_context.
_PROJECT_BRIEF_CACHE_TTL = 300.0


def _project_brief_key(resolved: str, mode: str, project_scope_key: str) -> tuple:
    """Effective cache key: (git-root, mode, structural epoch, project scope).

    Reads the epoch under the SAME resolved git-root the bump callers normalize
    to, so a structural write busts the entry (not a decorative no-op).

    Car F7: ``project_scope_key`` joins the tuple so two callers sharing the
    same ``resolved`` directory but naming DIFFERENT ``project=`` overrides
    never collide on the same cache entry — the anchor/hot_memories builders
    below now read ``directory_context`` keyed on the resolved project_id
    (when supplied), so a stale hit would leak one project's rows into
    another's brief. When no ``project=`` is supplied this equals ``resolved``,
    so the key is byte-identical to the pre-F7 shape for that (still the
    common) call pattern.
    """
    from yadgar._shared.runtime.cache_epoch import _current_epoch  # noqa: PLC0415

    return (resolved, mode, _current_epoch(resolved), project_scope_key)


@observe(tier="stage", metric="tools.project._make_project_brief_cache")
def _make_project_brief_cache():
    from yadgar.core.cache import (  # noqa: PLC0415
        TTL,
        Cache,
        _core_cache_ram_pct,
        _core_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    total = _core_cache_total_budget_bytes(_core_cache_ram_pct())
    budget = _namespace_budget_bytes("project_brief", total)
    return Cache(
        name="project_brief",
        max_bytes=budget,  # byte-bounded LRU (core RAM-% budget, #49)
        invalidation=TTL(_PROJECT_BRIEF_CACHE_TTL),  # heat/anchor-drift backstop
        key_fn=lambda k: k,  # caller passes the already-built effective (epoch) key tuple
        deep_copy=True,  # brief dict mutated by callers / _render
        obs_tier="cold",  # few calls/session → full tri-signal fine
    )


_project_brief_cache = _make_project_brief_cache()


@_tool(always_load=True)
def project_brief(directory: str, mode: str = "catalog", *, project: str | None = None) -> dict:
    """Return a layered project context snapshot for the given directory.

    Choose mode based on your use-case — new callers should use "signals" or "restore":

    mode="signals" (<100 tokens): stop-hook mode. Binary flags + age numerics +
        recommended_actions only. No anchors, no hot_memories, no wiki keys, no _render.
        Use in stop-hooks that need minimal signals to decide which write actions to fire.
    mode="restore" (~800 tokens): post-compaction context restoration. Returns
        anchors + hot_memories + checkpoint + wiki keys. Single top_anchors list with
        scope field per entry. No signal flags, no _render.
        Use after /clear or /compact to reconstruct working context.
    mode="catalog" (~500 tokens): DEPRECATED (v5.7.12) — avoid for new calls.
        Kept for back-compat. Returns signals, anchors, presence flags, hot_memories,
        key_wiki_pages, checkpoint, _render. Will be removed in v5.8.
    mode="full" (~1050 tokens): debug superset of catalog + inlined init_memory,
        active_work, expanded hot_memories, and key_wiki_pages. Use for deep
        debugging; too verbose for routine session start.

    Args:
        directory: Project root directory path (required).
        mode: One of "signals", "restore", "catalog" (deprecated), "full".
            Default "catalog" is kept only for back-compat — prefer "signals" or
            "restore" for all new callers.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; a full C7 re-key
    # of this tool's scope from ``directory`` onto the resolved project_id is
    # still future work for the anchor/hot_memories BUCKET-CHOICE queries
    # below (key_wiki_pages / wiki_catalog / adr_log / presence rows stay
    # directory-keyed on purpose — see TestProjectBriefWikiScoping).
    #
    # Car F7: the validated value used to be computed and discarded here —
    # ``get_anchored_memories_scoped`` (the sibling reader of this same
    # ``_anchor``-tagged bucket, used by the ``restore()`` tool) was re-keyed
    # onto the resolved project_id in lockstep with its writer (C10g), but
    # this tool's OWN duplicate anchor/hot_memories queries were not, because
    # ``memorize``/``anchor`` (C10f) now ALWAYS stamp ``directory_context``
    # from the resolved project_id — never the literal directory a caller
    # passed as ``context``. A caller who names the SAME ``project=`` on both
    # the write and this read got zero rows back, not "their current
    # project's rows" (accept_project_param's documented interim gap) — the
    # predicate simply could not match either identity convention.
    # ``_project_scope_key`` prefers the resolved project_id and falls back to
    # ``resolved`` (old behaviour) when no ``project=`` was supplied, matching
    # accept_project_param's contract of not resolving in that case.
    _scope_project_id = accept_project_param(project, directory)
    resolved = _resolve_project_root(directory)
    _project_scope_key = _scope_project_id if _scope_project_id else resolved

    # Car 1: whole-payload cache for the query-agnostic modes (catalog/restore/full).
    # Checked BEFORE any storage round-trip so a hit skips _fetch_presence_rows +
    # the catalog scan (the expensive work). signals mode is INTENTIONALLY not
    # cached (option A) — it drives the stop-hook's recommended_actions writes and
    # tolerates no staleness; its age numerics are recomputed every call.
    _cacheable = mode != "signals"
    if _cacheable:
        _key = _project_brief_key(resolved, mode, _project_scope_key)
        _hit = _project_brief_cache.get(_key)
        if _hit is not None:
            return _hit

    storage = _get_storage()

    # Project name: last path component of resolved root
    project = Path(resolved).name

    # Shared: presence rows + age numerics (all modes)
    init_rows, active_rows, checkpoint_rows = _fetch_presence_rows(storage, resolved)
    init_memory_present = len(init_rows) > 0
    active_work_present = len(active_rows) > 0
    init_memory_age_hours = _compute_row_age_hours(init_rows)
    active_work_age_hours = _compute_row_age_hours(active_rows)
    stale_checkpoint_hours = _compute_row_age_hours(checkpoint_rows)

    if mode == "signals":
        return _project_brief_signals(
            resolved=resolved,
            mode=mode,
            init_memory_present=init_memory_present,
            active_work_present=active_work_present,
            init_memory_age_hours=init_memory_age_hours,
            active_work_age_hours=active_work_age_hours,
            stale_checkpoint_hours=stale_checkpoint_hours,
            storage=storage,
        )

    if mode == "restore":
        result = _project_brief_restore(
            resolved=resolved,
            mode=mode,
            storage=storage,
            checkpoint_rows=checkpoint_rows,
            project_scope_key=_project_scope_key,
        )
    else:
        # catalog / full modes (back-compat)
        result = _project_brief_catalog_full(
            {
                "resolved": resolved,
                "mode": mode,
                "project": project,
                "storage": storage,
                "init_rows": init_rows,
                "active_rows": active_rows,
                "init_memory_present": init_memory_present,
                "active_work_present": active_work_present,
                "checkpoint_rows": checkpoint_rows,
                "project_scope_key": _project_scope_key,
            }
        )

    # Car 1: store the freshly-computed brief. deep_copy=True means the cache holds
    # an isolated copy — a caller mutating `result` cannot corrupt the cached value,
    # and a later hit returns its own deep copy (so callers can't corrupt each other).
    if _cacheable:
        _project_brief_cache.put(_key, result)
    return result


@_tool(power=True)
def bootstrap_project(directory: str, content: str, *, project: str | None = None) -> dict:
    """Replace this directory's _project_init memory with caller-supplied content.

    Content must be concise markdown: wiki slugs, key memory IDs, conventions,
    lookup tips. Hard cap: 2000 chars. Raises ValueError on overflow.

    Idempotent: deletes any existing _project_init memory for this directory
    before inserting the new one.

    v5.33.0: also seeds default memory blocks (current_task + gotchas) if they
    don't already exist for this directory. Idempotent — existing blocks are
    not overwritten.

    STALENESS NOTE: caller-supplied content will not refresh automatically.
    For auto-generated and always-current project context, prefer seed_project()
    which scans the directory and rewrites _project_init on each run.
    Use bootstrap_project only when you need a hand-curated init string that
    seed_project's auto-scan cannot capture.
    """
    # C5b (0047 PR#40 §2 amendment 2): PROMOTED from ``accept_project_param``
    # to a real resolution. That helper exists for tools whose scope key is
    # still ``directory`` and whose write path has no ``project_id`` sink —
    # the init upsert behind this tool now stamps the column, so the
    # boundary-validation-only path would keep minting unattributed rows.
    # Raising rather than returning an envelope matches this tool's existing
    # hard-failure style (the cap check below raises ValueError).
    _effective_project_id = resolve_effective_project(
        project=project,
        directory=directory,
        session_project=None,
        tool="bootstrap_project",
    )
    # v5.10.2: secret gate — scan content before any state mutation
    _gate = gate_or_reject(content)
    if _gate is not None:
        return _gate

    cfg = get_settings()
    cap = cfg.PROJECT_INIT_CAP_CHARS
    if len(content) > cap:
        raise ValueError(f"project_init content exceeds {cap} char cap (got {len(content)} chars)")
    resolved = _resolve_project_root(directory)
    # T2 Car F (ADR-0078): the store phase (init upsert + default-block seed)
    # forwards to the backend bootstrap_project_store /admin op — validation +
    # secret gate + host path resolution stay core-side.
    return _forward_admin(
        "bootstrap_project_store",
        {"resolved": resolved, "content": content, "project_id": _effective_project_id},
    )


@observe(tier="stage", metric="tools.project._get_active_work_tracked_dir")
def _get_active_work_tracked_dir() -> Path:
    """Return the base path for the active-work directory registry.

    Default: ~/.local/state/yadgar/active-work-tracked/
    Override via YADGAR_ACTIVE_WORK_TRACKED_DIR env var (used in tests for isolation).
    """
    override = os.environ.get("YADGAR_ACTIVE_WORK_TRACKED_DIR")
    if override:
        return Path(override)
    return _paths.ACTIVE_WORK_TRACKED_DIR


@observe(tier="stage", metric="tools.project._register_active_work_directory")
def _register_active_work_directory(resolved: str) -> None:
    """Write a marker file to the active-work directory registry.

    Path: <tracked_dir>/<sha256(resolved)[:12]>/directory.txt
    Content: the absolute resolved path.

    Registry is additive (never auto-pruned).  Errors are swallowed — registry
    is best-effort; failure must not break update_active_work().
    """
    try:
        tracked_dir = _get_active_work_tracked_dir()
        key = hashlib.sha256(resolved.encode()).hexdigest()[:12]
        entry_dir = tracked_dir / key
        entry_dir.mkdir(parents=True, exist_ok=True)
        (entry_dir / "directory.txt").write_text(resolved)
    except Exception:
        logger.debug("_register_active_work_directory: failed to write marker for %r", resolved)


@_tool(power=True)
def update_active_work(directory: str, content: str, *, project: str | None = None) -> dict:
    """Replace this directory's _active_work memory atomically.

    Deletes any existing _active_work memory(ies) for the directory,
    then inserts the new one in a single transaction.

    v5.10.1: also writes a marker to ~/.local/state/yadgar/active-work-tracked/ so the
    watchdog timer knows which directories to poll.


    Returns: {previous_content: str | None, new_memory: dict}
    """
    # C5b (0047 PR#40 §2 amendment 2): promoted to a real resolution — see
    # ``bootstrap_project`` for why ``accept_project_param`` is no longer the
    # right helper for a tool whose write path stamps ``project_id``.
    _effective_project_id = resolve_effective_project(
        project=project,
        directory=directory,
        session_project=None,
        tool="update_active_work",
    )
    # v5.10.2: secret gate — scan content before any state mutation
    _gate = gate_or_reject(content)
    if _gate is not None:
        return _gate

    # T2 fold-in (Q1 orphaned-memories fix): collapse worktree contexts to the
    # canonical repo root before resolution — _resolve_project_root alone returns
    # the WORKTREE toplevel for linked worktrees, which orphans the row.
    directory = normalize_write_context(directory)

    # R3 Car 3d: the secret gate stays core (above); the atomic
    # _active_work delete-then-insert (+ project_brief epoch bump) forwards to the
    # backend /admin op. The host-FS watchdog marker stays core (host lifecycle).
    resolved = _resolve_project_root(directory)
    result = _forward_admin(
        "update_active_work",
        {"resolved": resolved, "content": content, "project_id": _effective_project_id},
    )
    _register_active_work_directory(resolved)
    return result


# ── Wiki stale-count helpers (v5.53.1) ─────────────────────────────────

# Module-level TTL cache: (resolved_dir -> (count, computed_at_epoch))
_stale_count_cache: dict[str, tuple[int, float]] = {}
_stale_count_cache_lock = threading.Lock()


@observe(tier="stage", metric="tools.project._read_stale_count_cache")
def _read_stale_count_cache(resolved: str, now: float, ttl: float) -> int | None:
    """Return cached stale count if still fresh, else None. Thread-safe."""
    if ttl <= 0:
        return None
    with _stale_count_cache_lock:
        entry = _stale_count_cache.get(resolved)
    if entry is None:
        return None
    cached_count, cached_at = entry
    return cached_count if (now - cached_at) < ttl else None


@observe(tier="hot", metric="tools.project._is_wiki_page_stale")
def _is_wiki_page_stale(md_path: Path, yaml_mod) -> bool:
    """Return True if a single .md wiki page has hash drift vs its source files.

    External fn/index pages (entity_id: fn:* or api:*) are classified
    'external-sourced, not tracked' — the checker cannot reproduce their
    per-function or IR-based hash, so they must not be flagged stale.

    Extracted from _scan_stale_wiki_slugs to reduce cyclomatic complexity (I13).
    Never raises — returns False on any read/parse error.
    """
    import hashlib as _hl  # noqa: PLC0415

    try:
        raw = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    fm = _parse_frontmatter(raw, yaml_mod)
    if fm is None:
        return False

    # External fn/index pages: entity_id starts with "fn:" or "api:" — not tracked.
    entity_id = fm.get("entity_id", "")
    if entity_id.startswith("fn:") or entity_id.startswith("api:"):
        return False  # external-sourced, not tracked

    stored_hash = fm.get("hash") or fm.get("sha256") or ""
    # Real repo-wiki pages store `source_file` (SINGULAR, one path string that
    # may be a file OR a directory). Older fixtures use `source_files`/`sources`.
    source_files = fm.get("source_files") or fm.get("sources") or fm.get("source_file") or []
    if isinstance(source_files, str):
        source_files = [source_files]
    if not source_files:
        return False
    current_hash = _compute_source_hash(source_files, _hl)
    return bool((current_hash or stored_hash) and current_hash != stored_hash)


@observe(tier="stage", metric="tools.project._scan_stale_wiki_slugs")
def _scan_stale_wiki_slugs(directory: str) -> list[str]:
    """Pure side-effect-free scan of wiki pages for hash-drift.

    Scans disk .local-review/wiki/*.md files (externally-authored pages, e.g.
    the /repo-wiki:repo-wiki skill) for hash drift against their frontmatter-
    declared source file(s).

    External fn/index pages (entity_id: fn:* or api:*) are classified
    'external-sourced, not tracked' and excluded from stale results.

    Returns list of stale slugs.  Does NOT write a queue file, does NOT detect
    branch — see _compute_stale_wiki_count and the signals path for callers that
    need those concerns.
    Called from _compute_stale_wiki_count (signals path, TTL-cached).

    (Formerly also scanned DB-stored page_type='code' pages — the
    repo_wiki-generator store bridge, car #36 — removed with repo_wiki's
    decommission, #33/ADR-0162; that path always returned [] in practice since
    the generator stamped page_type='repo_wiki', not 'code'.)
    """
    try:
        import yaml as _yaml  # type: ignore[import]  # noqa: PLC0415
    except ImportError:
        _yaml = None

    wiki_dir = Path(directory) / ".local-review" / "wiki"
    if not wiki_dir.exists():
        return []

    return [
        md_path.stem
        for md_path in wiki_dir.glob("*.md")
        if md_path.exists() and _is_wiki_page_stale(md_path, _yaml)
    ]


@observe(tier="stage", metric="tools.project._compute_stale_wiki_count")
def _compute_stale_wiki_count(resolved: str) -> int:
    """Return stale wiki page count for resolved directory, TTL-cached.

    Cheap enough for the signals hot path (I8/I9): disk scan runs at most
    once per STALE_COUNT_CACHE_TTL_S seconds per project directory.
    Returns 0 on any error (graceful degradation).
    """
    from yadgar._shared.observability.metrics import record_cache_hit, record_cache_miss

    try:
        cfg = get_settings()
        ttl = getattr(cfg, "STALE_COUNT_CACHE_TTL_S", 300)
        now = time.monotonic()
        cached = _read_stale_count_cache(resolved, now, ttl)
        if cached is not None:
            record_cache_hit("stale_wiki_count")
            return cached
        record_cache_miss("stale_wiki_count")
        count = len(_scan_stale_wiki_slugs(resolved))
        with _stale_count_cache_lock:
            _stale_count_cache[resolved] = (count, now)
        return count
    except Exception:
        return 0


# ── Wiki refresh helpers ────────────────────────────────────────────────


@observe(tier="hot", metric="tools.project._parse_frontmatter")
def _parse_frontmatter(raw: str, yaml_mod) -> dict | None:
    """Parse YAML frontmatter from a markdown file. Returns None if no frontmatter.

    v5.53.1: falls back to ruamel.yaml when the explicit yaml_mod argument is None,
    allowing full YAML list/nested-value parsing even when PyYAML is not installed.
    The terminal fallback is a minimal single-line key:value parser (hash + flat scalars
    only — cannot parse multi-line lists).
    """
    if not raw.startswith("---"):
        return None
    end = raw.find("\n---", 3)
    if end == -1:
        return None
    fm_text = raw[3:end].strip()
    if yaml_mod is not None:
        try:
            data = yaml_mod.safe_load(fm_text)
            if isinstance(data, dict):
                return data
        except Exception as _e:
            logger.debug("_parse_frontmatter: YAML parse error: %s", _e)
            return None
    # v5.53.1: try ruamel.yaml as second YAML backend (available in yadgar deps).
    try:
        from ruamel.yaml import YAML as _RYAML  # noqa: PLC0415

        _ry = _RYAML()
        _ry.preserve_quotes = True
        import io as _io  # noqa: PLC0415

        data = _ry.load(_io.StringIO(fm_text))
        if isinstance(data, dict):
            return dict(data)
    except Exception:
        pass
    # Terminal fallback: very minimal key: value parser (scalars only).
    result: dict = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip()
    return result if result else None


@observe(tier="hot", metric="tools.project._compute_source_hash")
def _compute_source_hash(source_files: list[str], hashlib_mod) -> str:
    """Compute SHA256 over all source contents, concatenated in order.

    Each entry may be a FILE or a DIRECTORY:
      - FILE → hash its raw bytes (existing behaviour).
      - DIRECTORY → hash a stable recursive MANIFEST of the files under it
        (sorted relative path + size + per-file content digest). Real index
        pages store directory sources (`architecture.md` → ``yadgar/``,
        `overview.md` → ``.``); ``Path(dir).read_bytes()`` would raise
        IsADirectoryError and pin every such page to "always stale", so the
        manifest path keeps the hash content-stable: it changes iff a file
        under the directory is added, removed, or modified.

    mtime is deliberately NOT part of the manifest — it is not content-stable
    across checkouts/clones and would resurrect always-stale in CI.

    Missing sources contribute an empty hash (the page is treated as stale).
    """
    h = hashlib_mod.sha256()
    any_content = False
    for path_str in source_files:
        p = Path(path_str)
        try:
            if p.is_dir():
                if _hash_directory_manifest(p, h, hashlib_mod):
                    any_content = True
            else:
                data = p.read_bytes()
                h.update(data)
                any_content = True
        except OSError:
            # Missing/unreadable source (FileNotFoundError ⊆ OSError, includes
            # IsADirectoryError for non-manifest paths) → hash differs from stored.
            return ""
    if not any_content:
        return ""
    return h.hexdigest()


@observe(tier="hot", metric="tools.project._hash_directory_manifest")
def _hash_directory_manifest(directory: Path, h, hashlib_mod) -> bool:
    """Fold a stable recursive manifest of ``directory`` into accumulator ``h``.

    Returns True if at least one file was folded in. The manifest is built from
    files in SORTED relative-path order; for each file we mix in its relative
    path, byte size, and SHA256 of its contents. This makes the resulting hash
    invariant to traversal order and to mtime, while changing whenever any file
    under the directory is added, removed, or edited.
    """
    any_file = False
    # Exclude churn-y, non-source artifacts so the manifest stays stable on a
    # live tree (e.g. __pycache__/*.pyc are rewritten on every interpreter run
    # and would otherwise flip the hash every check → always-stale).
    files = sorted(
        p
        for p in directory.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")
    )
    for fp in files:
        rel = fp.relative_to(directory).as_posix()
        data = fp.read_bytes()
        file_digest = hashlib_mod.sha256(data).hexdigest()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(str(len(data)).encode("utf-8"))
        h.update(b"\0")
        h.update(file_digest.encode("utf-8"))
        h.update(b"\0")
        any_file = True
    return any_file
