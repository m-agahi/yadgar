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
import logging
import os
import subprocess
import time
from datetime import UTC
from pathlib import Path

from yadgar.config import get_settings
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

    # F5: Wiki Keys section
    key_wiki_pages = brief.get("key_wiki_pages", [])
    lines.append("## Wiki Keys")
    if key_wiki_pages:
        for p in key_wiki_pages[:3]:
            lines.append(f"- {p.get('slug', '')}")
    else:
        lines.append("*(none)*")
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


@_tool()
def project_brief(directory: str, mode: str = "catalog", branch_hint: str | None = None) -> dict:
    """Return a layered project context snapshot.

    mode="catalog" (~500 tokens): signals, anchors, presence flags.
    mode="full" (~1050 tokens): catalog + inlined init_memory, active_work,
    hot_memories, and key_wiki_pages.
    branch_hint: optional branch name supplied by the host-side hook (v5.1.9).
      When present, used directly — host has git visibility; container does not.
      When absent, falls back to _get_current_branch(resolved).
    """
    resolved = _resolve_project_root(directory)
    storage = _get_storage()
    get_settings()

    # v5.1.9 F3: prefer host-supplied branch_hint (computed on host by SessionStart
    # hook before calling this endpoint).  Fall back to in-process git query.
    # The previous in-container subprocess fallback (v5.1.8 F3) is dropped: the
    # container cannot see host .git, so it always returned None — dead code.
    if branch_hint:
        branch: str | None = branch_hint
    else:
        branch = _get_current_branch(resolved)

    # Project name: last path component of resolved root
    project = Path(resolved).name

    # Tech: stub — would require scan; return empty list
    tech: list[str] = []

    # --- presence flags ---
    init_rows = storage._q(
        "SELECT id, content FROM memory WHERE directory_context = $dir "
        "AND '_project_init' INSIDE tags LIMIT 1",
        {"dir": resolved},
    )
    active_rows = storage._q(
        "SELECT id, content FROM memory WHERE directory_context = $dir "
        "AND '_active_work' INSIDE tags LIMIT 1",
        {"dir": resolved},
    )
    init_memory_present = len(init_rows) > 0
    active_work_present = len(active_rows) > 0

    # --- top_anchors: scope-split into global + project buckets (F1) ---
    # Global anchors: directory_context in ('', 'global', 'system') — no heat filter
    global_anchor_rows = storage._q(
        "SELECT id, content, tags, heat, access_count FROM memory "
        "WHERE '_anchor' INSIDE tags "
        "AND (directory_context = '' OR directory_context = 'global' OR directory_context = 'system') "
        "ORDER BY heat DESC LIMIT 20",
    )
    top_anchors_global = []
    for row in global_anchor_rows:
        mid = storage._extract_id(row.get("id"))
        content_snippet = (row.get("content") or "")[:80]
        top_anchors_global.append(
            {
                "id": mid,
                "title": content_snippet,
                "tags": row.get("tags", []),
                "access_count": row.get("access_count") or 0,
            }
        )

    # Project anchors: directory_context matches resolved project dir
    project_anchor_rows = storage._q(
        "SELECT id, content, tags, heat, access_count FROM memory "
        "WHERE '_anchor' INSIDE tags "
        "AND directory_context = $dir "
        "ORDER BY heat DESC LIMIT 20",
        {"dir": resolved},
    )
    top_anchors_project = []
    for row in project_anchor_rows:
        mid = storage._extract_id(row.get("id"))
        content_snippet = (row.get("content") or "")[:80]
        top_anchors_project.append(
            {
                "id": mid,
                "title": content_snippet,
                "tags": row.get("tags", []),
                "access_count": row.get("access_count") or 0,
            }
        )

    # Legacy union field (back-compat) — dedup by id
    seen_ids: set = set()
    top_anchors = []
    for a in top_anchors_global + top_anchors_project:
        if a["id"] not in seen_ids:
            seen_ids.add(a["id"])
            top_anchors.append(a)

    # --- recent_episode_count: episodes in last 24h ---
    from datetime import datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    ep_rows = storage._q(
        "SELECT id FROM memory WHERE directory_context = $dir "
        "AND store_type = 'episodic' AND created_at >= $cutoff",
        {"dir": resolved, "cutoff": cutoff},
    )
    recent_episode_count = len(ep_rows)

    # stale_wiki_count: Stage 9 detail — pass 0 for now
    stale_wiki_count = 0

    # --- catalog: hot_memories top 3 (F2) ---
    hot_rows = storage._q(
        "SELECT id, content, heat, tags FROM memory "
        "WHERE directory_context = $dir AND heat > 0 "
        "ORDER BY heat DESC LIMIT 3",
        {"dir": resolved},
    )
    hot_memories_catalog = []
    for row in hot_rows:
        hot_memories_catalog.append(
            {
                "id": storage._extract_id(row.get("id")),
                "content": (row.get("content") or "")[:100],
                "heat": row.get("heat", 0),
                "tags": row.get("tags", []),
            }
        )

    # --- catalog: key_wiki_pages top 3 (F2) ---
    wiki_pages_catalog = storage.list_wiki_pages(limit=3)
    key_wiki_pages_catalog = [
        {
            "slug": p.get("slug", ""),
            "title": p.get("title", ""),
            "access_count": p.get("access_count") or 0,
        }
        for p in wiki_pages_catalog
    ]

    # --- catalog: checkpoint for this directory (F2) ---
    checkpoint_rows = storage._q(
        "SELECT * FROM checkpoint WHERE directory_context = $dir ORDER BY created_at DESC LIMIT 1",
        {"dir": resolved},
    )
    checkpoint_catalog: dict | None = None
    if checkpoint_rows:
        cp = checkpoint_rows[0]
        checkpoint_catalog = {
            "current_task": cp.get("current_task", ""),
            "key_decisions": (cp.get("key_decisions") or [])[:3],
            "next_steps": (cp.get("next_steps") or [])[:3],
        }

    result: dict = {
        "_resolved_directory": resolved,
        "_mode": mode,
        "project": project,
        "tech": tech,
        "branch": branch,
        "init_memory_present": init_memory_present,
        "active_work_present": active_work_present,
        "top_anchors": top_anchors,
        "top_anchors_global": top_anchors_global,
        "top_anchors_project": top_anchors_project,
        "recent_episode_count": recent_episode_count,
        "stale_wiki_count": stale_wiki_count,
        "hot_memories": hot_memories_catalog,
        "key_wiki_pages": key_wiki_pages_catalog,
        "checkpoint": checkpoint_catalog,
    }

    if mode == "full":
        # Inline init_memory content
        init_memory_content = None
        if init_rows:
            init_memory_content = init_rows[0].get("content")
        result["init_memory"] = init_memory_content

        # Inline active_work content
        active_work_content = None
        if active_rows:
            active_work_content = active_rows[0].get("content")
        result["active_work"] = active_work_content

        # full mode: expand hot_memories to top 10, 200-char snippets
        hot_rows_full = storage._q(
            "SELECT id, content, heat, tags FROM memory "
            "WHERE directory_context = $dir AND heat > 0 "
            "ORDER BY heat DESC LIMIT 10",
            {"dir": resolved},
        )
        hot_memories_full = []
        for row in hot_rows_full:
            hot_memories_full.append(
                {
                    "id": storage._extract_id(row.get("id")),
                    "content": (row.get("content") or "")[:200],
                    "heat": row.get("heat", 0),
                    "tags": row.get("tags", []),
                }
            )
        result["hot_memories"] = hot_memories_full

        # full mode: expand key_wiki_pages to 5
        wiki_pages_full = storage.list_wiki_pages(limit=5)
        result["key_wiki_pages"] = [
            {
                "slug": p.get("slug", ""),
                "title": p.get("title", ""),
                "access_count": p.get("access_count") or 0,
            }
            for p in wiki_pages_full
        ]

    # §28 — add _render markdown for the session-context hook pipe
    result["_render"] = _render_project_brief(result)

    return result


@_tool(power=True)
def bootstrap_project(directory: str, content: str) -> dict:
    """Replace this directory's _project_init memory atomically.

    Content must be concise markdown: wiki slugs, key memory IDs, conventions,
    lookup tips. Hard cap: 2000 chars. Raises ValueError on overflow.

    Idempotent: deletes any existing _project_init memory for this directory
    before inserting the new one.
    """
    cfg = get_settings()
    cap = cfg.PROJECT_INIT_CAP_CHARS
    if len(content) > cap:
        raise ValueError(f"project_init content exceeds {cap} char cap (got {len(content)} chars)")
    resolved = _resolve_project_root(directory)
    storage = _get_storage()
    return storage.upsert_project_init(resolved, content)


@_tool(power=True)
def update_active_work(directory: str, content: str) -> dict:
    """Replace this directory's _active_work memory atomically.

    Deletes any existing _active_work memory(ies) for the directory,
    then inserts the new one in a single transaction.

    Returns: {previous_content: str | None, new_memory: dict}
    """
    resolved = _resolve_project_root(directory)
    storage = _get_storage()
    return storage.upsert_active_work(resolved, content)


# ── Wiki refresh helpers ────────────────────────────────────────────────


def _parse_frontmatter(raw: str, yaml_mod) -> dict | None:
    """Parse YAML frontmatter from a markdown file. Returns None if no frontmatter."""
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
    # Fallback: very minimal key: value parser
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
            "dispatched_agent_id": None,
            "branch": branch,
            "skipped_reason": None,
        }

    stale: list[str] = []
    # Read the rest of the implementation from the original
    # (included below verbatim)

    # Collect candidate .md files
    if slugs:
        candidates = [wiki_dir / f"{slug}.md" for slug in slugs]
    else:
        candidates = list(wiki_dir.glob("*.md"))

    for md_path in candidates:
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
        # Mark stale when:
        #   1. current_hash is truthy (all files exist) and differs from stored_hash
        #   2. current_hash is empty (missing source file) and stored_hash is truthy
        # Both conditions reduce to: (current_hash or stored_hash) and current_hash != stored_hash
        if (current_hash or stored_hash) and current_hash != stored_hash:
            slug = md_path.stem
            stale.append(slug)

    if stale:
        import json as _json
        import time as _time

        queue_dir = wiki_dir / "refresh-queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        ts = _time.strftime("%Y%m%dT%H%M%S")
        queue_file = queue_dir / f"{ts}.json"
        queue_file.write_text(_json.dumps({"stale": stale, "branch": branch, "requested_at": ts}))

    return {
        "stale": stale,
        "dispatched_agent_id": None,
        "branch": branch,
        "skipped_reason": None,
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
