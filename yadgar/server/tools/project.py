"""Project context tools: git helpers, branch detection, project_brief, wiki_refresh_stale.

# Module size justified: single cohesive domain — project/git/wiki-staleness context tools.
# All functions share _resolve_project_root, _detect_branch, and _git_safe_env helpers.
# Seven+ callers (server/__init__.py, tools/__init__.py, tools/recall.py, tools/wiki.py,
# tools/memorize.py, restoration.py, file_queue/apply.py) import helpers directly from
# this module; splitting would require updating all those import sites for no architectural
# benefit. The module is read-only (no DB writes), tight, and has a single public surface.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import math
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import yadgar.paths as _paths
from yadgar.config import get_settings
from yadgar.secrets import gate_or_reject
from yadgar.server._app import _tool
from yadgar.server.lifecycle import _get_storage

logger = logging.getLogger(__name__)

settings = get_settings()


# ── Git helpers ────────────────────────────────────────────────────────


def _git_safe_env() -> dict:
    """Return an env dict that prevents .git/config code-execution attacks (H-10).

    A malicious .git/config in a user-supplied directory can set
    core.fsmonitor or core.sshCommand to execute arbitrary commands on
    the next git invocation.  Passing GIT_CONFIG_NOSYSTEM + pointing
    GIT_CONFIG_GLOBAL at /dev/null plus disabling network protocols
    eliminates all git-config-driven execution paths.
    """
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


# Extra argv to pass to every git invocation — disable network protocols
# so a crafted .git/config cannot trigger remote fetch/clone helpers.
_GIT_SAFE_ARGS = [
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.file.allow=never",
    "-c",
    "uploadpack.allowFilter=false",
]


def _resolve_project_root(directory: str) -> str:
    """Resolve the git project root for a directory (walk-up via git rev-parse).

    Falls back to the given directory if not inside a git repo or git is unavailable.
    """
    try:
        out = (
            subprocess.check_output(
                ["git", *_GIT_SAFE_ARGS, "-C", directory, "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL,
                timeout=2,
                env=_git_safe_env(),
            )
            .decode()
            .strip()
        )
        if out:
            return out
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as _e:
        pass
    return directory


def _get_current_branch(directory: str) -> str | None:
    """Return current git branch for the given directory, or None if not in a repo."""
    try:
        out = (
            subprocess.check_output(
                ["git", *_GIT_SAFE_ARGS, "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
                stderr=subprocess.DEVNULL,
                timeout=2,
                env=_git_safe_env(),
            )
            .decode()
            .strip()
        )
        if out and out != "HEAD":
            return out
        return out or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as _e:
        return None


@functools.lru_cache(maxsize=128)
def _detect_branch_cached(directory: str, _ts_bucket: int) -> str | None:
    """Cached per 30s bucket. Returns None for detached HEAD or non-git.

    Do not call directly — use _detect_branch(directory) which injects the
    correct time bucket.
    """
    try:
        out = (
            subprocess.check_output(
                ["git", *_GIT_SAFE_ARGS, "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
                stderr=subprocess.DEVNULL,
                timeout=2,
                env=_git_safe_env(),
            )
            .decode()
            .strip()
        )
        return out if out and out != "HEAD" else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as _e:
        return None


def _detect_branch(directory: str) -> str | None:
    """Return the current git branch for directory, or None on non-git/error.

    LRU-cached with a ~30-second TTL via time-bucket trick.
    Each directory's bucket boundary is phase-shifted by hash(directory) % 30
    so invalidations don't align across all directories simultaneously
    (thundering-herd prevention — v5.1 C3).
    Never raises — all errors return None.
    """
    try:
        _before = _detect_branch_cached.cache_info().hits
        result = _detect_branch_cached(directory, int((time.time() + (hash(directory) % 30)) // 30))
        try:
            from yadgar.metrics import yadgar_cache_hit_total, yadgar_cache_miss_total

            if _detect_branch_cached.cache_info().hits > _before:
                yadgar_cache_hit_total.labels(cache="branch_detect").inc()
            else:
                yadgar_cache_miss_total.labels(cache="branch_detect").inc()
        except Exception:
            pass
        return result
    except Exception:
        return None


@functools.lru_cache(maxsize=128)
def _get_default_branch_cached(directory: str, _ts_bucket: int) -> str:
    """Cached per 5-minute bucket. Falls back to 'master'."""
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


def _get_default_branch(directory: str) -> str:
    """Return the repo default branch name (e.g. 'master' or 'main').

    Uses git symbolic-ref refs/remotes/origin/HEAD to detect the configured
    default. Falls back to 'master' if the repo has no remote or the command
    fails. LRU-cached with a 5-minute TTL.
    """
    _before = _get_default_branch_cached.cache_info().hits
    result = _get_default_branch_cached(directory, int(time.time() // 300))
    try:
        from yadgar.metrics import yadgar_cache_hit_total, yadgar_cache_miss_total

        if _get_default_branch_cached.cache_info().hits > _before:
            yadgar_cache_hit_total.labels(cache="default_branch").inc()
        else:
            yadgar_cache_miss_total.labels(cache="default_branch").inc()
    except Exception:
        pass
    return result


# ── Project tools ──────────────────────────────────────────────────────


def _render_project_brief(brief: dict) -> str:
    """Render a project_brief dict as markdown for hook injection (§28)."""
    project = brief.get("project", "unknown")
    branch = brief.get("branch") or "unknown"
    mode = brief.get("_mode", "catalog")

    lines: list[str] = [f"# {project} — {mode}\n"]
    lines.append(f"**Branch:** {branch}\n")

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
            "*Suggestion: call `bootstrap_project(directory, ...)` to seed project context.*"
        )
    if not active_present:
        lines.append(
            "*Suggestion: call `update_active_work(directory, ...)` once you start a session.*"
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

# Tag set that qualifies an anchor for promote-to-wiki detection.
# Triple AND: word_count > ANCHOR_PROMOTE_WORDS AND header_count >= ANCHOR_PROMOTE_HEADERS
# AND tags ∩ _ANCHOR_PROMOTE_TAGS ≠ ∅.
_ANCHOR_PROMOTE_TAGS: frozenset[str] = frozenset(
    {"rule", "pattern", "convention", "playbook", "workflow", "recipe"}
)

# Compiled regex to strip fenced code blocks before counting markdown headers.
# Handles ``` and ~~~ delimiters; DOTALL so . matches newlines inside blocks.
_FENCED_CODE_BLOCK_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)

# Compiled regex for markdown headers (after code-block stripping).
_MD_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)

# Max candidates per list (redundancy pairs + promote IDs) before hard truncation.
# K=3 chosen to keep signals mode payload ≤100 tokens even under pathological load.
# Not an env knob — too many already; this is an internal budget constant.
_SIGNALS_CANDIDATES_K = 3

# v5.53.0: Wiki catalog constants — max items shown per category group before
# truncation hint. Not an env knob (internal budget constant, same rationale as
# _SIGNALS_CANDIDATES_K). Keep low to bound catalog render length.
_WIKI_CATALOG_MAX_PER_GROUP = 5


# ── project_brief helpers (v5.7.12) ───────────────────────────────────────


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


def _build_hot_memories(storage, resolved: str, limit: int, snippet: int) -> list[dict]:
    """Fetch hot memories excluding anchored entries.

    Filters: heat > 0 AND 'anchor' NOTINSIDE tags AND '_anchor' NOTINSIDE tags.
    """
    rows = storage._q(
        "SELECT id, content, heat, tags FROM memory "
        "WHERE directory_context = $dir AND heat > 0 "
        "AND 'anchor' NOTINSIDE tags AND '_anchor' NOTINSIDE tags "
        f"ORDER BY heat DESC LIMIT {limit}",
        {"dir": resolved},
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


def _build_anchor_rows_catalog(storage, resolved: str) -> tuple:
    """Fetch global + project anchor rows for catalog/full modes.

    Returns (top_anchors_global, top_anchors_project, top_anchors_union).
    """
    _now = storage._now_iso()
    global_rows = storage._q(
        "SELECT id, content, tags, heat, access_count FROM memory "
        "WHERE '_anchor' INSIDE tags "
        "AND (directory_context = '' OR directory_context = 'global') "
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
        {"dir": resolved, "now": _now},
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


def _build_anchor_rows_restore(storage, resolved: str) -> list[dict]:
    """Fetch anchors for restore mode: merged list with scope field, truncated."""
    max_anchors = _get_max_anchors()
    _now = storage._now_iso()

    global_rows = storage._q(
        "SELECT id, content, tags, heat, access_count FROM memory "
        "WHERE '_anchor' INSIDE tags "
        "AND (directory_context = '' OR directory_context = 'global') "
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
        {"dir": resolved, "now": _now},
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


def _count_markdown_headers(content: str) -> int:
    """Count level-1..6 markdown headers in content, excluding fenced code blocks.

    Strips ``` and ~~~ fenced blocks first to avoid counting # comments inside
    code blocks (e.g. Python # comment, shell ## heading).
    """
    stripped = _FENCED_CODE_BLOCK_RE.sub("", content)
    return len(_MD_HEADER_RE.findall(stripped))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two float vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


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


def _fetch_expired_anchor_count(storage, _now: str) -> int:
    """Count expired anchors (valid_until < now) that are not in migration grace period."""
    try:
        exp_rows = storage._q(
            "SELECT count() AS cnt FROM memory "
            "WHERE '_anchor' INSIDE tags "
            "AND valid_until IS NOT NONE "
            "AND valid_until < $now "
            "AND (migration_grace IS NONE OR migration_grace = false) "
            "GROUP ALL",
            {"now": _now},
        )
        return int(exp_rows[0]["cnt"]) if exp_rows else 0
    except Exception:
        return 0


def _fetch_cross_project_candidates_for_signals(storage, _now: str, cfg) -> list[dict]:
    """Fetch cross-project redundancy candidates for signals mode payload.

    Delegates to audit module's detection logic.
    Capped to _SIGNALS_CANDIDATES_K for token budget.
    Returns empty list on any error (graceful degradation).
    """
    try:
        from yadgar.server.tools.audit import _fetch_cross_project_candidates  # noqa: PLC0415

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


def _get_master_head_info(resolved: str) -> dict | None:
    """Return HEAD info for the default (master) branch of resolved repo.

    Returns dict with keys: commit_ts (float), commit_msg (str), pyproject_version (str | None).
    Returns None on any error (git not available, not a repo, etc.).

    Uses committer date (%ct) to match v5.41.4 plan spec (robust to rebases).
    All git invocations use _GIT_SAFE_ARGS + _git_safe_env() (H-10 hardening).
    """
    try:
        default_branch = _get_default_branch(resolved)
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


def _get_pyproject_version_at_ts(resolved: str, ts: float) -> str | None:
    """Return pyproject.toml version at the most recent master commit on or before ts.

    Finds the commit hash via `git log --until=<iso>` then reads pyproject.toml at that
    revision.  Returns None on any error or if pyproject.toml absent at that revision.
    """
    try:
        default_branch = _get_default_branch(resolved)
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
    expired_no_grace_count = _fetch_expired_anchor_count(storage, _now)
    cross_project_candidates = _fetch_cross_project_candidates_for_signals(storage, _now, cfg)

    return {
        "anchor_count_project": anchor_count_project,
        "anchor_redundancy_candidates": redundancy_pairs,
        "anchor_promote_candidates": promote_ids,
        "expired_no_grace_count": expired_no_grace_count,
        "cross_project_redundancy_candidates": cross_project_candidates,
        "_truncated": trunc_r or trunc_p,
    }


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

    # v5.8.0 anchor hygiene actions
    audit_threshold = int(cfg.ANCHOR_AUDIT_THRESHOLD)
    if anchor_count_project > audit_threshold:
        actions.append(
            {
                "action": "audit_anchors",
                "reason": f"count={anchor_count_project} > threshold={audit_threshold}",
            }
        )

    if redundancy_count >= 1:
        actions.append(
            {
                "action": "merge_redundant_anchors",
                "reason": f"redundancy_pairs={redundancy_count}",
            }
        )

    if promote_count >= 1:
        actions.append(
            {
                "action": "promote_anchor_to_wiki",
                "reason": f"oversized={promote_count}",
            }
        )

    if expired_no_grace_count >= 1:
        actions.append(
            {
                "action": "forget_expired_anchors",
                "reason": f"expired={expired_no_grace_count}",
            }
        )

    return actions


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
        from yadgar.server.lifecycle import _get_file_queue  # noqa: PLC0415

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


def _omit_sentinel(d: dict, key: str, value: object, sentinel: object) -> None:
    """Set d[key]=value only when value != sentinel (for budget-trimming optional fields)."""
    if value != sentinel:
        d[key] = value


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
    _aw_call = f"update_active_work(directory={resolved!r}, content='...')"
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
            from yadgar.metrics import yadgar_signals_payload_oversized_total

            yadgar_signals_payload_oversized_total.inc()
        except Exception:
            pass  # non-fatal; metrics not available in all environments
    return result


def _project_brief_restore(
    resolved: str,
    mode: str,
    storage,
    checkpoint_rows: list,
) -> dict:
    """Build restore mode payload (<800 tokens)."""
    return {
        "_resolved_directory": resolved,
        "_mode": mode,
        "top_anchors": _build_anchor_rows_restore(storage, resolved),
        "hot_memories": _build_hot_memories(storage, resolved, limit=5, snippet=150),
        "checkpoint": _build_checkpoint_dict(checkpoint_rows),
        "key_wiki_pages": _build_wiki_pages(storage, limit=3, directory=resolved),
        # v5.53.0: grouped wiki catalog (metadata-only, length-capped).
        "wiki_catalog": _build_wiki_catalog(storage, resolved),
    }


def _project_brief_catalog_full(ctx: dict) -> dict:
    """Build catalog/full mode payload (back-compat).

    catalog mode is DEPRECATED as of v5.7.12. Kept for back-compat until v5.8.
    ctx keys: resolved, mode, project, branch, storage, init_rows, active_rows,
              init_memory_present, active_work_present, checkpoint_rows.
    """
    from datetime import timedelta

    resolved = ctx["resolved"]
    mode = ctx["mode"]
    storage = ctx["storage"]
    init_rows = ctx["init_rows"]
    active_rows = ctx["active_rows"]
    checkpoint_rows = ctx["checkpoint_rows"]

    top_anchors_global, top_anchors_project, top_anchors = _build_anchor_rows_catalog(
        storage, resolved
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
        "branch": ctx["branch"],
        "init_memory_present": ctx["init_memory_present"],
        "active_work_present": ctx["active_work_present"],
        "top_anchors": top_anchors,
        "top_anchors_global": top_anchors_global,
        "top_anchors_project": top_anchors_project,
        "recent_episode_count": len(ep_rows),
        # v5.53.1: real stale count (TTL-cached).
        "stale_wiki_count": _compute_stale_wiki_count(resolved),
        "hot_memories": _build_hot_memories(storage, resolved, limit=3, snippet=100),
        "key_wiki_pages": _build_wiki_pages(storage, limit=3, directory=resolved),
        "checkpoint": _build_checkpoint_dict(checkpoint_rows),
        # v5.53.0: grouped wiki catalog (metadata-only, length-capped).
        "wiki_catalog": _build_wiki_catalog(storage, resolved),
    }
    if mode == "full":
        result["init_memory"] = init_rows[0].get("content") if init_rows else None
        result["active_work"] = active_rows[0].get("content") if active_rows else None
        result["hot_memories"] = _build_hot_memories(storage, resolved, limit=10, snippet=200)
        result["key_wiki_pages"] = _build_wiki_pages(storage, limit=5, directory=resolved)
        result["wiki_catalog"] = _build_wiki_catalog(storage, resolved)
    # §28 — add _render for catalog+full (back-compat); signals+restore omit it
    result["_render"] = _render_project_brief(result)
    return result


@_tool()
def project_brief(directory: str, mode: str = "catalog", branch_hint: str | None = None) -> dict:
    """Return a layered project context snapshot.

    mode="signals" (<100 tokens): pure binary signals + age numerics + recommended_actions.
      Audience: stop-hook — needs minimal flags to decide which write actions to fire.
      No anchors, no hot_memories, no wiki keys, no _render.
    mode="restore" (<800 tokens): anchors + hot_memories + checkpoint + wiki keys.
      Audience: post-/clear, post-/compact context restoration.
      Single top_anchors list with scope field per entry.  No signal flags, no _render.
    mode="catalog" (~500 tokens): DEPRECATED (v5.7.12). Kept for back-compat.
      Returns current full shape: signals, anchors, presence flags, hot_memories,
      key_wiki_pages, checkpoint, _render.  Will be removed in v5.8.
    mode="full" (~1050 tokens): superset of catalog + inlined init_memory, active_work,
      expanded hot_memories, and key_wiki_pages.
    branch_hint: optional branch name supplied by the host-side hook (v5.1.9).
      When present, used directly — host has git visibility; container does not.
      When absent, falls back to _get_current_branch(resolved).
    """
    resolved = _resolve_project_root(directory)
    storage = _get_storage()

    # v5.1.9 F3: prefer host-supplied branch_hint.
    if branch_hint:
        branch: str | None = branch_hint
    else:
        branch = _get_current_branch(resolved)

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
        return _project_brief_restore(
            resolved=resolved,
            mode=mode,
            storage=storage,
            checkpoint_rows=checkpoint_rows,
        )

    # catalog / full modes (back-compat)
    return _project_brief_catalog_full(
        {
            "resolved": resolved,
            "mode": mode,
            "project": project,
            "branch": branch,
            "storage": storage,
            "init_rows": init_rows,
            "active_rows": active_rows,
            "init_memory_present": init_memory_present,
            "active_work_present": active_work_present,
            "checkpoint_rows": checkpoint_rows,
        }
    )


@_tool(power=True)
def bootstrap_project(directory: str, content: str) -> dict:
    """Replace this directory's _project_init memory atomically.

    Content must be concise markdown: wiki slugs, key memory IDs, conventions,
    lookup tips. Hard cap: 2000 chars. Raises ValueError on overflow.

    Idempotent: deletes any existing _project_init memory for this directory
    before inserting the new one.

    v5.33.0: also seeds default memory blocks (current_task + gotchas) if they
    don't already exist for this directory. Idempotent — existing blocks are
    not overwritten.
    """
    # v5.10.2: secret gate — scan content before any state mutation
    _gate = gate_or_reject(content)
    if _gate is not None:
        return _gate

    cfg = get_settings()
    cap = cfg.PROJECT_INIT_CAP_CHARS
    if len(content) > cap:
        raise ValueError(f"project_init content exceeds {cap} char cap (got {len(content)} chars)")
    resolved = _resolve_project_root(directory)
    storage = _get_storage()
    result = storage.upsert_project_init(resolved, content)

    # v5.33.0: Seed default memory blocks (idempotent — skip if already exist)
    _seed_default_blocks(storage, resolved)

    return result


def _seed_default_blocks(storage, directory: str) -> None:
    """Seed default memory blocks for a project directory (v5.33.0).

    Creates current_task and gotchas blocks if they don't already exist.
    Idempotent — existing blocks are not modified.
    """
    _DEFAULT_BLOCKS = [
        ("current_task", ""),
        ("gotchas", ""),
    ]
    for name, content in _DEFAULT_BLOCKS:
        try:
            existing = storage.get_block(name, scope="project", directory=directory)
            if existing is None:
                storage.create_block(
                    name=name,
                    content=content,
                    scope="project",
                    directory=directory,
                    char_limit=2000,
                )
        except Exception:
            logger.debug("_seed_default_blocks: failed to seed block %r for %r", name, directory)


def _get_active_work_tracked_dir() -> Path:
    """Return the base path for the active-work directory registry.

    Default: ~/.local/state/yadgar/active-work-tracked/
    Override via YADGAR_ACTIVE_WORK_TRACKED_DIR env var (used in tests for isolation).
    """
    override = os.environ.get("YADGAR_ACTIVE_WORK_TRACKED_DIR")
    if override:
        return Path(override)
    return _paths.ACTIVE_WORK_TRACKED_DIR


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
def update_active_work(directory: str, content: str, branch_hint: str | None = None) -> dict:
    """Replace this directory's _active_work memory atomically.

    Deletes any existing _active_work memory(ies) for the directory,
    then inserts the new one in a single transaction.

    v5.10.1: also writes a marker to ~/.local/state/yadgar/active-work-tracked/ so the
    watchdog timer knows which directories to poll.

    v5.42.3: branch_hint added for parity with memorize/anchor/checkpoint.
    Hard-rejects when branch context cannot be determined and no branch_hint supplied.
    Resolution order: _detect_branch(directory) → branch_hint → hard-reject.

    Returns: {previous_content: str | None, new_memory: dict}
    """
    # v5.10.2: secret gate — scan content before any state mutation
    _gate = gate_or_reject(content)
    if _gate is not None:
        return _gate

    # v5.46.7: branch context validation (MCP boundary).
    # Resolution order: _detect_branch(directory) → branch_hint → YADGAR_CI_BRANCH env → reject.
    _branch = None
    try:
        _branch = _detect_branch(directory)
    except Exception:
        pass

    if not _branch and branch_hint:
        _branch = branch_hint

    # v5.46.7: YADGAR_CI_BRANCH env fallback — CI runner sets this when git is unavailable.
    if not _branch:
        _branch = os.environ.get("YADGAR_CI_BRANCH") or None

    if not _branch:
        return {
            "error": "missing_branch",
            "stored": False,
            "message": (
                "Branch context required. Supply branch_hint=<current-branch-name> or ensure "
                "the working directory is a git repo accessible to the yadgar daemon."
            ),
            "field": "branch_hint",
            "op_type": "update_active_work",
        }

    resolved = _resolve_project_root(directory)
    storage = _get_storage()
    result = storage.upsert_active_work(resolved, content)
    _register_active_work_directory(resolved)
    return result


# ── Wiki stale-count helpers (v5.53.1) ─────────────────────────────────

# Module-level TTL cache: (resolved_dir -> (count, computed_at_epoch))
_stale_count_cache: dict[str, tuple[int, float]] = {}


def _is_wiki_page_stale(md_path: Path, yaml_mod) -> bool:
    """Return True if a single .md wiki page has hash drift vs its source files.

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
    stored_hash = fm.get("hash") or fm.get("sha256") or ""
    source_files = fm.get("source_files") or fm.get("sources") or []
    if isinstance(source_files, str):
        source_files = [source_files]
    if not source_files:
        return False
    current_hash = _compute_source_hash(source_files, _hl)
    return bool((current_hash or stored_hash) and current_hash != stored_hash)


def _scan_stale_wiki_slugs(directory: str) -> list[str]:
    """Pure side-effect-free scan of .local-review/wiki/*.md for hash-drift.

    Returns list of stale slugs.  Does NOT write a queue file, does NOT detect
    branch — callers that need those concerns use _wiki_refresh_stale_impl.
    Called from _compute_stale_wiki_count (signals path, TTL-cached) and
    re-used by _wiki_refresh_stale_impl.
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


def _compute_stale_wiki_count(resolved: str) -> int:
    """Return stale wiki page count for resolved directory, TTL-cached.

    Cheap enough for the signals hot path (I8/I9): disk scan runs at most
    once per STALE_COUNT_CACHE_TTL_S seconds per project directory.
    Returns 0 on any error (graceful degradation).
    """
    try:
        cfg = get_settings()
        ttl = getattr(cfg, "STALE_COUNT_CACHE_TTL_S", 300)
        now = time.monotonic()
        if ttl > 0 and resolved in _stale_count_cache:
            cached_count, cached_at = _stale_count_cache[resolved]
            if (now - cached_at) < ttl:
                return cached_count
        count = len(_scan_stale_wiki_slugs(resolved))
        _stale_count_cache[resolved] = (count, now)
        return count
    except Exception:
        return 0


# ── Wiki refresh helpers ────────────────────────────────────────────────


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
            logger.debug("wiki_refresh_stale: YAML parse error: %s", _e)
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


def _compute_source_hash(source_files: list[str], hashlib_mod) -> str:
    """Compute SHA256 over all source file contents, concatenated in order.

    Missing files contribute an empty byte string (the page is stale).
    """
    h = hashlib_mod.sha256()
    any_content = False
    for path_str in source_files:
        try:
            data = Path(path_str).read_bytes()
            h.update(data)
            any_content = True
        except (OSError, FileNotFoundError) as _e:
            # Missing source file → hash will differ from stored hash
            return ""
    if not any_content:
        return ""
    return h.hexdigest()


def _wiki_refresh_stale_impl(
    directory: str,
    slugs: list[str] | None,
    force_branch: bool,
) -> dict:
    """Inner implementation — may raise; caller wraps in try/except."""
    import hashlib as _hashlib

    try:
        import yaml as _yaml  # type: ignore[import]
    except ImportError:
        _yaml = None

    # Use directory directly for file I/O so tests can control it easily.
    # Branch detection still runs git from the same directory.
    # Look up via yadgar.server so monkeypatches on
    # "yadgar.server._get_current_branch" / "yadgar.server._get_default_branch"
    # take effect (v4.x patching contract preserved after server split).
    import sys as _sys  # noqa: PLC0415

    dir_path = Path(directory)
    _srv = _sys.modules.get("yadgar.server")
    _gcb = (
        getattr(_srv, "_get_current_branch", _get_current_branch) if _srv else _get_current_branch
    )
    _gdb = (
        getattr(_srv, "_get_default_branch", _get_default_branch) if _srv else _get_default_branch
    )
    branch = _gcb(directory)
    default = _gdb(directory)

    # Master-only enforcement
    if not force_branch and branch not in (default, "master", "main"):
        return {
            "stale": [],
            "dispatched_agent_id": None,
            "branch": branch,
            "skipped_reason": "not_default_branch",
        }

    wiki_dir = dir_path / ".local-review" / "wiki"
    if not wiki_dir.exists():
        return {
            "stale": [],
            "stale_count": 0,
            "dispatched_agent_id": None,
            "branch": branch,
            "skipped_reason": None,
            "suggested_calls": [],
        }

    # v5.53.1: full-scan path reuses _scan_stale_wiki_slugs (side-effect-free helper,
    # also used by the TTL-cached signals path).  Slug-filtered path keeps its own loop
    # so callers can check specific pages without a full directory scan.
    if not slugs:
        stale = _scan_stale_wiki_slugs(directory)
    else:
        # Slug-filtered scan (explicit subset)
        stale = []
        for slug in slugs:
            md_path = wiki_dir / f"{slug}.md"
            if not md_path.exists():
                continue
            try:
                raw = md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = _parse_frontmatter(raw, _yaml)
            if fm is None:
                continue
            stored_hash = fm.get("hash") or fm.get("sha256") or ""
            source_files = fm.get("source_files") or fm.get("sources") or []
            if isinstance(source_files, str):
                source_files = [source_files]
            if not source_files:
                continue
            current_hash = _compute_source_hash(source_files, _hashlib)
            if (current_hash or stored_hash) and current_hash != stored_hash:
                stale.append(slug)

    if stale:
        import json as _json
        import time as _time

        queue_dir = wiki_dir / "refresh-queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        ts = _time.strftime("%Y%m%dT%H%M%S")
        queue_file = queue_dir / f"{ts}.json"
        queue_file.write_text(_json.dumps({"stale": stale, "branch": branch, "requested_at": ts}))
        # Invalidate the TTL cache so next signals call reflects the freshly detected list.
        _stale_count_cache.pop(directory, None)

    # v5.53.1: surface stale slugs prominently so the stop-hook can dispatch regen.
    suggested_calls = (
        [
            f"Agent(subagent_type='general-purpose', prompt='/repo-wiki:repo-wiki update {s}')"
            for s in stale
        ]
        if stale
        else []
    )

    return {
        "stale": stale,
        "stale_count": len(stale),
        "dispatched_agent_id": None,
        "branch": branch,
        "skipped_reason": None,
        "suggested_calls": suggested_calls,
    }


@_tool(power=True)
def wiki_refresh_stale(
    directory: str,
    slugs: list[str] | None = None,
    force_branch: bool = False,
) -> dict:
    """Detect stale repo-wiki pages and signal for regeneration (§26).

    Stale = .local-review/wiki/*.md frontmatter `hash` ≠ SHA256(source_files).
    Writes a JSON file under .local-review/wiki/refresh-queue/<timestamp>.json
    listing the stale slugs; actual regen is done by the operator or a
    background Agent.

    Master-only: refuses on non-default branch unless force_branch=True.

    Returns:
        {
            "stale": [<slug>, ...],
            "dispatched_agent_id": None,
            "branch": <current branch>,
            "skipped_reason": null | "not_default_branch",
        }

    NEVER raises — all errors are caught and reported in the return dict.
    """
    try:
        return _wiki_refresh_stale_impl(directory, slugs=slugs, force_branch=force_branch)
    except Exception as _e:
        logger.warning("wiki_refresh_stale internal error (best-effort): %s", _e)
        return {
            "stale": [],
            "dispatched_agent_id": None,
            "branch": None,
            "skipped_reason": None,
            "error": str(_e),
        }


@_tool(power=True)
def wiki_cleanup_merged_branches(directory: str, dry_run: bool = True) -> dict:
    """List wiki_page rows whose branch is no longer in git branch -a (§26).

    Run from within a git repo. dry_run=True (default) returns the candidate
    list without deleting. dry_run=False deletes the listed pages.

    Pages with branch in (master, main, None) are never candidates.

    Returns:
        {
            "candidates": [{"id": int, "slug": str, "branch": str}, ...],
            "deleted_count": int,
            "dry_run": bool,
        }
    """
    storage = _get_storage()
    resolved = _resolve_project_root(directory)

    # Get live branch set
    try:
        raw = subprocess.check_output(
            ["git", *_GIT_SAFE_ARGS, "-C", resolved, "branch", "-a", "--format=%(refname:short)"],
            stderr=subprocess.DEVNULL,
            timeout=5,
            env=_git_safe_env(),
        ).decode()
        live_branches: set[str] = set()
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip remote prefix: "remotes/origin/feat/x" → "feat/x",
            # "origin/feat/x" → "feat/x"
            for prefix in ("remotes/origin/", "origin/"):
                if line.startswith(prefix):
                    line = line[len(prefix) :]
                    break
            live_branches.add(line)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as _e:
        logger.warning("wiki_cleanup_merged_branches: git branch failed: %s", _e)
        return {
            "error": "git branch enumeration failed; cleanup aborted",
            "deleted": [],
            "deleted_count": 0,
            "candidates": [],
            "dry_run": dry_run,
        }

    # Query wiki_page rows with a branch set, excluding canonical branches
    try:
        rows = storage._q(
            "SELECT id, slug, branch FROM wiki_page "
            "WHERE branch IS NOT NONE "
            "AND branch != 'master' AND branch != 'main'"
        )
    except Exception as _e:
        logger.warning("wiki_cleanup_merged_branches: DB query failed: %s", _e)
        rows = []

    candidates = []
    for row in rows:
        row_branch = row.get("branch") or ""
        if row_branch in live_branches:
            continue
        # Store both integer id (for delete_wiki_page) and raw id string (fallback)
        try:
            int_id = storage._extract_id(row.get("id"))
        except (ValueError, TypeError) as _e:
            int_id = None
        raw_id = row.get("id")
        candidates.append(
            {
                "id": int_id,
                "_raw_id": raw_id,
                "slug": row.get("slug", ""),
                "branch": row_branch,
            }
        )

    deleted_count = 0
    if not dry_run and candidates:
        for candidate in candidates:
            try:
                slug = candidate.get("slug", "")
                if candidate["id"] is not None:
                    storage.delete_wiki_page(candidate["id"])
                    deleted_count += 1
                elif slug:
                    # Fallback for auto-generated (non-integer) IDs: delete by slug
                    storage._q(
                        "DELETE wiki_page WHERE slug = $slug",
                        {"slug": slug},
                    )
                    deleted_count += 1
            except Exception as _e:
                logger.warning("wiki_cleanup_merged_branches: delete of page failed: %s", _e)

    # Strip internal fields from returned candidates
    return_candidates = [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates]

    return {
        "candidates": return_candidates,
        "deleted_count": deleted_count,
        "dry_run": dry_run,
    }
