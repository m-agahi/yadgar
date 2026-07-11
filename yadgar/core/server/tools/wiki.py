"""Wiki MCP tool registrations."""

from __future__ import annotations

import logging
import os

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar._shared.security.enforcement import _enforcement_on, _inc_relaxed
from yadgar._shared.security.secrets import gate_or_reject
from yadgar._shared.server_helpers import _has_unpaired_surrogate, _push_event
from yadgar._shared.storage.directory import is_directory_eligible

# R2a Car D2: _get_file_queue moved to yadgar.core.lifecycle (core → core).
from yadgar.core.lifecycle import _get_file_queue
from yadgar.core.server._app import _tool
from yadgar.core.server.tools._forward import _forward_admin

logger = logging.getLogger(__name__)


@observe(tier="hot", metric="tools.wiki._check_wiki_add_context")
def _check_wiki_add_context(branch: str | None, directory: str | None) -> dict | None:
    """Check branch + directory enforcement at MCP boundary.

    Returns an error dict if enforcement is ON and a required field is absent,
    else None (caller may proceed).  Logs WARN + increments metric when
    enforcement is OFF and a field is missing.  is_draining() callers are
    exempt — this helper should only be called when not is_draining().

    v5.42.6 (I-C1): extracted from wiki_add to keep cyclomatic complexity ≤ 15.
    """
    if not branch:
        if _enforcement_on("YADGAR_BRANCH_ENFORCEMENT"):
            return {
                "error": "missing_branch",
                "stored": False,
                "message": (
                    "Branch context required. Supply branch=<branch> or branch_hint=<branch-name>. "
                    "Agents should pass branch_hint=$(git branch --show-current) before wiki_add."
                ),
                "field": "branch_hint",
                "op_type": "wiki_add",
            }
        logger.warning(
            "wiki_add: branch enforcement OFF — proceeding without branch context "
            "(YADGAR_BRANCH_ENFORCEMENT=false)"
        )
        _inc_relaxed("branch")

    _effective_dir: str | None = (directory or "").strip() or None
    if not _effective_dir:
        if _enforcement_on("YADGAR_DIRECTORY_ENFORCEMENT"):
            return {
                "error": "missing_directory",
                "stored": False,
                "message": (
                    "directory required and must be non-empty. "
                    "Pass the absolute project path or 'global'."
                ),
                "field": "directory",
                "op_type": "wiki_add",
            }
        logger.warning(
            "wiki_add: directory enforcement OFF — proceeding without directory context "
            "(YADGAR_DIRECTORY_ENFORCEMENT=false)"
        )
        _inc_relaxed("directory")
    return None


@observe(tier="stage", metric="tools.wiki._wiki_add_wait_path")
def _wiki_add_wait_path(payload: dict, new_slug: str, title: str) -> dict:
    """Handle wiki_add(wait=True): enqueue then poll for the terminal file.

    R3 Car 1 (write-half): the sync write body lives in the backend drainer
    (yadgar.backend.write_exec.run_wiki_add_replay). This shell enqueues and polls
    the shared archive/dlq dirs for the job's terminal state (FileQueue.wait_for_job).
    The drainer runs the similarity gate; a rejection lands in the DLQ .error.json
    sidecar and is surfaced here synchronously.

    Returns:
      {"stored": True, "committed": True}          — archived (committed)
      {"stored": False, "reason": "duplicate_detected", "candidates": [...]} — gate rejected
      {"stored": False, "reason": "wait_timeout", "queued": True}  — drainer timeout
    """
    fq = _get_file_queue()
    job_id = fq.enqueue("wiki_add", payload)

    # Nudge the background drainer to flush promptly so the caller does not wait a
    # full drain interval. Runtime shared-state access — guarded, non-fatal.
    _drainer = _st._queue_drainer
    if _drainer is not None:
        try:
            _drainer.drain_now()
        except Exception as exc:  # noqa: BLE001
            logger.warning("wiki_add wait: drain_now() failed (non-fatal): %s", exc)

    try:
        from yadgar._shared.config import get_settings as _get_settings  # noqa: PLC0415

        timeout = getattr(_get_settings(), "WIKI_WRITE_WAIT_TIMEOUT_SECONDS", 5.0)
    except Exception:
        timeout = 5.0

    outcome = fq.wait_for_job(job_id, timeout=timeout)

    if outcome["status"] == "timeout":
        return {
            "stored": False,
            "reason": "wait_timeout",
            "queued": True,
            "slug": new_slug,
            "hint": "Write still queued — will commit on next drain or hit DLQ on repeated failure.",
        }

    if outcome["status"] == "rejected":
        rejection = outcome.get("result")
        if rejection is not None:
            # Gate fired in drainer — return rejection synchronously.
            return rejection
        return {
            "stored": False,
            "reason": "rejected",
            "queued": False,
            "slug": new_slug,
        }

    return {
        "stored": True,
        "queued": False,
        "committed": True,
        "slug": new_slug,
        "title": title,
    }


@_tool()
def wiki_add(
    title: str,
    content: str,
    category: str = "reference",
    tags: list[str] | None = None,
    source_memory_ids: list[int] | None = None,
    confidence: str = "medium",
    append: bool = False,
    branch: str | None = None,
    branch_hint: str | None = None,
    force: bool = False,
    replace_slug: str | None = None,
    wait: bool = False,
    directory: str | None = None,
    page_type: str | None = None,
) -> dict:
    """Create or update a wiki page. Content can include [[slug]] cross-references.

    append=False (default): create a new page or overwrite an existing one.
    append=True: merge content into an existing page (appends with timestamp,
      merges tags and source_memory_ids). Use for accumulating knowledge over time.

    v5.39.0 similarity gate: wiki_add checks for near-duplicate pages before writing.
    v5.41.5 BREAKING CHANGE: gate moved from request thread to drainer (I9 fix).

    wait=False (default): gate check is DEFERRED — handler returns immediately.
      Response: {"stored": True, "queued": True, "similarity_check": "deferred", ...}
      Duplicate detection happens asynchronously in the drainer. If the gate fires,
      the job is archived (not inserted) and a rejection metric is emitted. Caller
      will NOT receive a sync rejection — use wait=True if rejection feedback needed.

    wait=True: gate runs in drainer, rejection surfaces synchronously.
      Gate fires  → {"stored": False, "reason": "duplicate_detected", "candidates": [...]}
      Gate passes → {"committed": True, "queued": False, "slug": ..., "title": ...}

    Use force=True to bypass the gate. Use replace_slug=<existing-slug> to overwrite
    an existing page by a different slug (gate is skipped for both).

    Categories: architecture, decision, pattern, debugging, reference, convention, fact, analysis.
    Confidence: high, medium, low.
    page_type: optional — one of: function, module, service, architecture, decision, analysis.
      When provided, stored with wiki_schema_version=1. Omit to leave page untyped (backward-compat).
      Typed pages are format-checked by wiki_lint (missing required sections reported as warnings).
      wiki_add never rejects a write due to page_type/template mismatch — lint is advisory only.

    Branch resolution (evaluated in priority order):
    1. branch (non-empty string) — caller knows the branch explicitly; used as-is.
    2. branch_hint (non-empty string) — host-side hook passed the caller's branch;
       used when branch is None/empty (mirrors memorize branch_hint, v5.4 W1).
    3. Both omitted / None — page stored with branch IS NULL, the canonical slot
       resolved by wiki_read step 3. DO NOT fall back to _detect_branch(os.getcwd());
       the daemon CWD is not the caller's repo and would always resolve to "master".

    wait=False (default): async fast path — returns immediately with {"queued": True}.
    Default async. Only set wait=True when callers depend on next-call read-your-writes.
    wait=True: read-your-writes path — enqueues then blocks until the drainer
      commits the write, then returns {"committed": True, "queued": False}.
      Preserves FIFO ordering (earlier queued writes to the same slug still land
      before this one). On timeout returns {"stored": False, "reason":
      "wait_timeout", "queued": True} — the write is still in the queue and will
      eventually commit or hit DLQ. I9 latency budget does NOT apply to wait=True
      (opt-in slow path). If no drainer is running or replace_slug is set, falls
      back to the sync write path with the same committed=True response.
    """
    assert _st._wiki is not None, "WikiStore not initialized"

    if len(content) > 65_536:
        return {"stored": False, "reason": "content_too_large", "max_bytes": 65_536}

    # v5.15.0: secret gate — use gate_or_reject() so allowlist tags= kwarg is forwarded.
    # Replaces direct check_secrets() call so v5.13.0 allowlist fires on real wiki_add() calls.
    _gate = gate_or_reject(content, tags=list(tags) if tags else [])
    if _gate is not None:
        return _gate
    if _st._rules_engine is not None:
        wp_blocked, wp_reason, wp_modified = _st._rules_engine.check_write_policy(
            content, "", tags or []
        )
        if wp_blocked:
            return {"stored": False, "reason": f"blocked_by_policy: {wp_reason}"}
        if wp_modified is not None:
            content = wp_modified

    for _field in (content, title):
        if _has_unpaired_surrogate(_field):
            return {"stored": False, "reason": "invalid_unicode_surrogates"}

    # Branch resolution — O(1), no subprocess. See docstring for priority order.
    if not branch and branch_hint:
        branch = branch_hint
    # v5.42.3/v5.42.6: MCP boundary context check — branch + directory.
    # Enqueue-only shell: always runs on the request thread (never draining);
    # the drainer validates at its own boundary. _check_wiki_add_context gates
    # enforcement; YADGAR_*_ENFORCEMENT=false relaxes.
    _ctx_err = _check_wiki_add_context(branch, directory)
    if _ctx_err is not None:
        return _ctx_err

    _effective_dir: str | None = (directory or "").strip() or None
    # DP-3: strip trailing slash (preserve "global" sentinel as-is)
    if _effective_dir and _effective_dir != "global":
        _effective_dir = _effective_dir.rstrip("/") or _effective_dir

    # v5.39.0 slug generation (O(1), needed for enqueue payload and wait path).
    import re as _re_slug  # noqa: PLC0415

    _new_slug = (_re_slug.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "untitled")[:64]

    # v5.41.5: similarity gate REMOVED from request path (I9 fix).
    # Gate now runs in the drainer pre-apply stage (_sim_gate_for_drainer).
    # wait=False callers get {queued: True, similarity_check: "deferred"} — see below.
    # wait=True callers get sync rejection via wait_for_job + get_job_result.
    # I26: secret-gate (above) still runs on request thread (cheap regex, stays here).
    # I6 no-double-pay: gate runs once in the drainer (backend write-exec).

    _payload = {
        "wiki_schema_version": 2,
        "slug": _new_slug,
        "title": title,
        "content": content,
        "category": category or "reference",
        "tags": tags,
        "source_memory_ids": source_memory_ids,
        "confidence": confidence,
        "append": append,
        "branch": branch,
        # v5.41.5: pass bypass flags so drainer can skip gate for these paths
        "force": force,
        "replace_slug": replace_slug,
        "directory_context": _effective_dir,
        "page_type": page_type,
    }

    # wait=True: enqueue first (preserves FIFO), then poll until the drainer commits.
    # The drainer runs the similarity gate; rejection surfaces synchronously via the
    # DLQ terminal-file poll inside _wiki_add_wait_path.
    if wait:
        return _wiki_add_wait_path(_payload, _new_slug, title)

    # Async path (wait=False default): enqueue and return immediately.
    # v5.41.5: similarity gate is deferred to drainer — caller gets
    # {similarity_check: "deferred"} and must use wait=True for sync rejection.
    _get_file_queue().enqueue("wiki_add", _payload)
    return {
        "stored": True,
        "queued": True,
        "similarity_check": "deferred",
        "slug": _new_slug,
        "title": title,
    }


# ── Car 2 (v5.113): wiki_read / wiki_query result caches ──────────────────────
#
# Both are query/slug-scoped read caches. Invalidation folds the structural wiki
# epoch (_current_epoch) into the key: ANY wiki write bumps the global epoch (via
# storage._bump_wiki_epoch → bump_epoch(None)), so a stale key becomes unreachable
# on the next read — the wiki-write-busts-read correctness guarantee. A short TTL
# backstops any non-write drift. deep_copy=True: callers mutate returned row-dicts
# (wiki_query bumps r["_retrieval_score"]; read dicts are handed out mutable).
_WIKI_READ_CACHE_TTL = 120.0
_WIKI_QUERY_CACHE_TTL = 60.0  # fuzzy search → shorter TTL acceptable


def _current_wiki_epoch() -> int:
    """Global structural epoch — bumped on every wiki write. Folded into cache keys
    so a wiki mutation busts every cached wiki read/query regardless of dir/branch
    normalization (the bump is global; see storage.wiki._bump_wiki_epoch)."""
    try:
        from yadgar._shared.runtime.cache_epoch import _current_epoch  # noqa: PLC0415

        return _current_epoch(None)
    except Exception:
        return 0


@observe(tier="stage", metric="tools.wiki._make_wiki_read_cache")
def _make_wiki_read_cache():
    from yadgar.core.cache import (  # noqa: PLC0415
        TTL,
        Cache,
        _core_cache_ram_pct,
        _core_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    total = _core_cache_total_budget_bytes(_core_cache_ram_pct())
    budget = _namespace_budget_bytes("wiki_read", total)
    return Cache(
        name="wiki_read",
        max_bytes=budget,  # byte-bounded LRU (core RAM-% budget, #49)
        invalidation=TTL(_WIKI_READ_CACHE_TTL),  # epoch in key + TTL backstop
        deep_copy=True,  # returned page dict is mutable / caller-owned
        obs_tier="cold",  # low call rate → full tri-signal fine
    )


@observe(tier="stage", metric="tools.wiki._make_wiki_query_cache")
def _make_wiki_query_cache():
    from yadgar.core.cache import (  # noqa: PLC0415
        TTL,
        Cache,
        _core_cache_ram_pct,
        _core_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    total = _core_cache_total_budget_bytes(_core_cache_ram_pct())
    budget = _namespace_budget_bytes("wiki_query", total)
    return Cache(
        name="wiki_query",
        max_bytes=budget,  # byte-bounded LRU (core RAM-% budget, #49)
        invalidation=TTL(_WIKI_QUERY_CACHE_TTL),  # fuzzy search → short TTL
        deep_copy=True,  # results carry mutated _retrieval_score row-dicts
        obs_tier="cold",
    )


_wiki_read_cache = _make_wiki_read_cache()
_wiki_query_cache = _make_wiki_query_cache()


@_tool()
def wiki_query(
    query: str,
    tags: list[str] | None = None,
    category: str | None = None,
    max_results: int = 5,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> list[dict]:
    """Search wiki pages by keyword + semantic similarity.

    Returns matching pages with relevance scores. Use tags and category to filter.

    directory: Absolute project path for scoping results to caller directory + 'global'.
        Required (v5.65 Fix D): callers must supply the real host directory.
        Container-safe: daemon does NOT fall back to os.getcwd().
    branch_hint: Caller branch for §25 branch filter (v5.43.0).
        Uses branch_hint when daemon-side _detect_branch returns None (container scenario).
        Resolution order: _detect_branch(directory) → branch_hint → None (canonical slot).

    DEPRECATION (Phase 2a): unified recall is now the only path — prefer
    ``recall(query, directory=..., type="wiki")`` which routes through the
    unified fan-out path with CE fusion and per-type quotas. This function
    remains fully functional for one release cycle as a thin alias.
    """
    import time as _time  # noqa: PLC0415

    # v5.65 Fix D: hard-require directory — MUST be first check (before any store access).
    # Container-safe: do NOT fall back to os.getcwd().
    _dir_stripped = (directory or "").strip().rstrip("/")
    if not _dir_stripped:
        raise ValueError(
            "wiki_query: directory is required (caller must supply project dir; "
            "container cannot detect it via os.getcwd())"
        )

    # Phase 2a: unified recall is now the ONLY path; emit deprecation unconditionally.
    try:
        logger.info(
            "wiki_query is deprecated. Use recall(query, directory=..., type='wiki') instead."
        )
    except Exception:
        pass

    # Car 2: cache the (embedding-computing) query by its inputs + wiki epoch.
    # A hit skips _st._wiki.query (which embeds the query text). A wiki write
    # bumps the epoch → the key moves → a stale result can never be served.
    # P11 / Car 2: start the duration clock BEFORE the cache lookup so the
    # yadgar_wiki_query_duration_ms histogram observes on EVERY wiki_query call
    # (cache hit AND miss) — obs total-visibility. The cache-hit early return
    # lives inside the try below so the finally still fires for hits.
    _wiki_query_t0 = _time.monotonic()
    _q_key = (
        query,
        _dir_stripped,
        branch_hint or "",
        category,
        tuple(tags) if tags else None,
        max_results,
        _current_wiki_epoch(),
    )
    results: list[dict] = []

    try:
        _q_hit = _wiki_query_cache.get(_q_key)
        if _q_hit is not None:
            return _q_hit

        assert _st._wiki is not None, "WikiStore not initialized"
        # Fetch extra results before branch filter so we still return max_results after pruning.
        results = _st._wiki.query(query, tags, category, max_results * 3)

        # §25 Branch filter + current-branch 1.5x score boost.
        # v5.43.0: accept caller-supplied directory + branch_hint to avoid daemon-CWD bug.
        # Resolution: _detect_branch(directory or os.getcwd()) → branch_hint → None.
        # Look up via yadgar.server so monkeypatches on "yadgar.server._detect_branch" etc. apply.
        try:
            import sys as _sys  # noqa: PLC0415

            _cwd = (
                _dir_stripped  # v5.65 Fix D: directory is required; _dir_stripped always non-empty
            )
            _srv = _sys.modules.get("yadgar.core.server")
            _detect_branch = getattr(_srv, "_detect_branch", None) if _srv else None
            _get_default_branch = getattr(_srv, "_get_default_branch", None) if _srv else None
            if _detect_branch is None or _get_default_branch is None:
                from yadgar.core.server.tools.project import (
                    _detect_branch,  # noqa: PLC0415
                    _get_default_branch,  # noqa: PLC0415
                )
            _current_branch = _detect_branch(_cwd)
            _default_branch = _get_default_branch(_cwd)
        except Exception:
            _current_branch = None
            _default_branch = None  # v5.42.4: canonical slot

        # v5.43.0: use branch_hint when daemon-side detection returns None (container scenario).
        # Mirrors the pattern in wiki_read (v5.42.6 F1) and _resolve_page_id_by_slug (v5.42.5).
        _effective_branch = _current_branch or branch_hint

        _allowed_branches: set[str | None] = {_default_branch, None}
        if _effective_branch is not None:
            _allowed_branches.add(_effective_branch)

        results = [r for r in results if r.get("branch") in _allowed_branches]

        # v5.43.0 / v5.62.0: directory scoping — scope to caller directory.
        # v5.62.0: replaces hand-rolled predicate with is_directory_eligible() from
        # storage/directory.py — single source of truth for the eligible-set rule.
        # Applied as Python-side post-filter (mirrors recall directory filter from v5.42.5).
        # v5.65 Fix D: directory is now required (validated at function top), so
        # _dir_stripped is always a non-empty absolute path here.
        results = [
            r for r in results if is_directory_eligible(r.get("directory_context"), _dir_stripped)
        ]

        if _effective_branch is not None:
            for r in results:
                if r.get("branch") == _effective_branch:
                    base = r.get("_retrieval_score", 0.0)
                    r["_retrieval_score"] = base * 1.5
            results.sort(key=lambda r: r.get("_retrieval_score", 0.0), reverse=True)

        results = results[:max_results]

        for r in results:
            r.pop("embedding", None)

        # Car 2: store the freshly-computed result under the epoch-folded key.
        # deep_copy=True → the cache holds an isolated copy; a caller mutating a
        # returned row (e.g. re-scoring) cannot corrupt the cached value.
        _wiki_query_cache.put(_q_key, results)
        return results

    finally:
        # P11: observe wiki_query total duration in finally so it fires on all paths.
        try:
            from yadgar._shared.observability.metrics import (
                yadgar_wiki_query_duration_ms,  # noqa: PLC0415
            )

            yadgar_wiki_query_duration_ms.observe((_time.monotonic() - _wiki_query_t0) * 1000)
        except Exception:
            pass


@_tool(power=True)
def wiki_read(
    slug: str,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Read a specific wiki page by slug.

    §25 Resolution order (v5.42.5 — directory-aware):
    1. directory=$caller_dir AND branch=$effective_branch  (project-branch-scoped)
    2. directory=$caller_dir AND branch IS NULL            (project-canonical)
    3. directory='global'    AND branch IS NULL            (global fallback)
    4. Not found → error dict.

    branch_hint: caller-supplied branch (v5.42.6 F1 fix, symmetric with wiki_add).
    Uses branch_hint if _detect_branch returns None (container scenario).
    Without branch_hint, falls through to steps 2+3 (permissive default —
    reads are more permissive than writes per §25 design).

    When directory is not supplied, falls back to legacy branch-only resolution
    (backward-compat mode; WARNING logged).
    """
    assert _st._wiki is not None, "WikiStore not initialized"

    # Detect current branch for resolution order.
    # Look up via yadgar.server so monkeypatches on "yadgar.server._detect_branch" etc. apply.
    try:
        import sys as _sys  # noqa: PLC0415

        _cwd = os.getcwd()
        _srv = _sys.modules.get("yadgar.core.server")
        _detect_branch = getattr(_srv, "_detect_branch", None) if _srv else None
        _get_default_branch = getattr(_srv, "_get_default_branch", None) if _srv else None
        if _detect_branch is None or _get_default_branch is None:
            from yadgar.core.server.tools.project import (
                _detect_branch,  # noqa: PLC0415
                _get_default_branch,  # noqa: PLC0415
            )
        _current_branch = _detect_branch(_cwd)
        _default_branch = _get_default_branch(_cwd)
    except Exception:
        _current_branch = None
        _default_branch = None  # v5.42.4: canonical slot

    # v5.42.6 F1 fix: use branch_hint when daemon-side detection returns None.
    # This mirrors _resolve_page_id_by_slug (v5.42.5) and wiki_add (v5.42.3).
    # The effective branch for §25 step 1 uses branch_hint if _detect_branch failed.
    _effective_branch = branch_hint or _current_branch

    # Car 2: cache the resolved page by (slug, dir, effective-branch) + wiki epoch.
    # A hit skips the WikiStore read. A wiki write to ANY page bumps the global
    # epoch → this key moves → a stale page can never be served (the
    # wiki-write-busts-read guarantee). Only found pages are cached; a not-found
    # result is cheap to recompute and a later create bumps the epoch anyway.
    _caller_dir = directory.strip().rstrip("/") if directory is not None else None
    _r_key = (slug, _caller_dir, _effective_branch, _current_wiki_epoch())
    _r_hit = _wiki_read_cache.get(_r_key)
    if _r_hit is not None:
        return _r_hit

    if directory is not None:
        # v5.42.5: 4-step directory-aware resolution
        caller_dir = _caller_dir or None
        page = _st._wiki.read_by_directory_branch(slug, caller_dir, _effective_branch)
    else:
        # Legacy fallback — no directory supplied; backward-compat mode.
        logger.warning(
            "wiki_read('%s'): no directory supplied — using legacy branch-only resolution. "
            "Pass directory= for project-scoped results (v5.42.5).",
            slug,
        )
        page = _st._wiki.read_by_branch(slug, _effective_branch, _default_branch)

    if page is None:
        return {"error": f"Wiki page '{slug}' not found"}
    page.pop("embedding", None)
    # Car 2: store the resolved page. deep_copy=True → callers cannot corrupt the
    # cached value, and each hit returns its own isolated copy.
    _wiki_read_cache.put(_r_key, page)
    return page


@_tool(power=True)
def wiki_delete(slug: str) -> dict:
    """Delete a wiki page by slug."""
    # R3 Car 3c: the DB delete (+ epoch bump) forwards to the backend /admin op.
    # The SSE push_event and file-queue mirror cleanup are CORE-side side-effects
    # (core's SSE bus + the shared file-queue mirror) — they stay here, after the
    # forward reports the delete succeeded.
    deleted = _forward_admin("wiki_delete", {"slug": slug}).get("deleted", False)
    if deleted:
        _push_event({"event": "wiki_deleted", "slug": slug})
        try:
            _get_file_queue().delete_wiki(slug)
        except Exception as _fq_exc:
            logger.debug("File queue wiki mirror cleanup failed (non-fatal): %s", _fq_exc)
        return {"deleted": True, "slug": slug}
    return {"deleted": False, "error": f"Wiki page '{slug}' not found"}


@_tool(power=True)
def wiki_list(
    category: str | None = None,
    limit: int = 100,
    slug_prefix: str | None = None,
    directory: str | None = None,
) -> list[dict]:
    """List wiki pages by metadata only (no content). Use wiki_read(slug) for full content.

    Categories: architecture, decision, pattern, debugging, reference, convention, fact, analysis.

    v5.42.5: when directory is supplied, results are scoped to that directory + 'global'.
    When absent (legacy call pattern), all pages are returned with a WARNING.
    """
    assert _st._wiki is not None, "WikiStore not initialized"

    if directory is None:
        logger.warning(
            "wiki_list: no directory supplied — returning all pages (backward-compat mode). "
            "Pass directory= for project-scoped results (v5.42.5)."
        )

    # Push LIMIT, category, slug_prefix, and directory filters to the DB layer
    db_limit = limit if (limit is not None and limit > 0) else None
    pages = _st._wiki.list_pages(
        category=category,
        slug_prefix=slug_prefix,
        limit=db_limit,
        directory=directory,
    )
    out = []
    for p in pages:
        out.append(
            {
                "slug": p.get("slug"),
                "title": p.get("title"),
                "category": p.get("category"),
                "tags": p.get("tags", []),
                "confidence": p.get("confidence"),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at"),
                "source_count": len(p.get("source_memory_ids") or []),
            }
        )
    return out


@_tool(power=True)
def wiki_lint() -> dict:
    """Check wiki health: orphan pages, broken cross-refs, stale pages, low confidence.

    Returns issues list and summary stats.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    return _st._wiki.lint()


@_tool(power=True)
def wiki_autolink(
    directory: str | None = None,
    dry_run: bool = True,
    min_title_len: int = 6,
    max_links_per_page: int = 20,
    similarity_threshold: float = 0.70,
    semantic_guard: bool = True,
) -> dict:
    """Auto-insert [[slug]] cross-refs by matching other pages' titles in body text.

    SAFE BY DEFAULT — dry_run=True returns the proposed [[slug]] insertions
    WITHOUT mutating any page. Run dry-run first, review the proposals, then call
    again with dry_run=False to apply via the wiki upsert path (re-syncs
    crossrefs, bumps versions, tags changed pages 'auto-linked').

    Guards (all enforced, non-negotiable):
    - dry_run default (no accidental corpus mutation)
    - verbatim guard — never links inside code fences, inline code, existing
      [[...]], or URLs
    - length/specificity guard — min_title_len + word-boundary verbatim match
    - similarity guard — semantic_guard requires the target to clear
      similarity_threshold (kills coincidental title collisions)
    - idempotent — skips already-linked targets; a second run proposes nothing
    - no metadata clobber — each page keeps its own category/directory_context

    directory: absolute project path; scopes both the title map and the pages
        scanned to that dir + 'global'.

    Returns {applied, dry_run, proposals:[{page,target,title}], pages_changed,
             links_added}.
    """
    # R3 Car 3c: forward the whole tool — it writes when dry_run=False (upsert +
    # crossref re-sync + epoch bump). Forwarding the dry-run compute too keeps a
    # single path (harmless — no write on dry_run).
    _dir = (directory or "").strip().rstrip("/") or None
    return _forward_admin(
        "wiki_autolink",
        {
            "directory": _dir,
            "dry_run": dry_run,
            "min_title_len": min_title_len,
            "max_links_per_page": max_links_per_page,
            "similarity_threshold": similarity_threshold,
            "semantic_guard": semantic_guard,
        },
    )


@_tool(power=True)
def wiki_drafts() -> list[dict]:
    """List all pending wiki drafts awaiting review.

    Drafts are candidate wiki pages queued but not yet approved.
    Use wiki_approve to promote a draft to a full page, or wiki_discard to delete it.
    """
    storage = _get_storage()
    drafts = storage.list_wiki_drafts()
    for d in drafts:
        if d.get("content"):
            d["content"] = d["content"][:200]
    return drafts


@_tool(power=True)
def wiki_approve(slug: str) -> dict:
    """Promote a pending draft wiki page to a full wiki page.

    Moves the draft into the wiki knowledge base with all its metadata,
    then deletes the draft. Fails if no draft with that slug exists.

    v5.42.3: reads branch from draft row (migration 015). If the draft carries
    a branch value, it is propagated to the wiki page. Legacy NULL-branch drafts
    (pre-v5.42.3 or explicit canonical-slot drafts) write to the NULL-branch
    canonical slot — this is now explicit rather than accidental.
    """
    # R3 Car 3c: the draft→page promotion (wiki.add + draft delete + epoch bump)
    # forwards to the backend /admin op. The file-queue mirror (write_wiki) is a
    # CORE-side side-effect — it stays here, driven by the draft content the impl
    # surfaces back.
    result = _forward_admin("wiki_approve", {"slug": slug})
    if not result.get("approved"):
        return {"approved": False, "error": result.get("error", f"Draft '{slug}' not found")}
    page = result.get("page", {})
    content = result.get("content", "")
    try:
        _get_file_queue().write_wiki(page.get("slug", slug), content)
    except Exception as _fq_exc:
        logger.debug("File queue wiki mirror failed (non-fatal): %s", _fq_exc)
    return {"approved": True, "slug": slug, "page": page}


@_tool(power=True)
def wiki_discard(slug: str) -> dict:
    """Discard a pending wiki draft without promoting it to a full page.

    Permanently deletes the draft. Use for incorrect or low-value drafts.
    """
    # R3 Car 3c: draft delete forwards to the backend /admin op.
    deleted = _forward_admin("wiki_discard", {"slug": slug}).get("deleted", False)
    if deleted:
        return {"discarded": True, "slug": slug}
    return {"discarded": False, "error": f"Draft '{slug}' not found"}


@_tool()
def wiki_check_duplicate(  # secret-gate: skip — read-only dry-run, never writes to DB
    title: str,
    content: str,
    branch: str | None = None,
    threshold: float | None = None,
    top_k: int = 5,
    directory: str | None = None,
) -> dict:
    """Dry-run similarity check: returns candidate duplicate pages without writing anything.

    Use before wiki_add to detect near-duplicates and decide whether to proceed.
    Returns candidates sorted by descending similarity score.

    Args:
        title: Title of the proposed new page.
        content: Content of the proposed new page.
        branch: Branch context for scope filter (None = canonical slot).
        threshold: Minimum cosine similarity (0-1). Defaults to WIKI_SIM_CONTENT_THRESHOLD.
        top_k: Maximum candidates to return (default 5).

    Returns:
        {"candidates": [...], "threshold_used": float}
        Each candidate: {slug, title, similarity, branch}
    """
    assert _st._wiki is not None, "WikiStore not initialized"

    from yadgar._shared.config import get_settings  # noqa: PLC0415

    cfg = get_settings()
    effective_threshold = (
        threshold if threshold is not None else getattr(cfg, "WIKI_SIM_CONTENT_THRESHOLD", 0.80)
    )

    # v5.42.2: auto-detect default branch when caller does not supply one,
    # mirroring wiki_query (lines 462-483). Without this, find_similar_wiki_pages
    # builds scope = {None} which excludes all branch="master" legacy pages
    # written by the pre-v5.42.2 drainer, making the gate silent in production.
    #
    # We pass _default_branch (not _current_branch) so that find_similar_wiki_pages
    # builds scope = {None, default_branch} — covering both the post-v5.42.2
    # canonical-slot pages (branch=None) and the pre-v5.42.2 legacy pages
    # (branch="master"). On a feature branch the scope still covers all real data.
    if branch is None:
        try:
            import sys as _sys  # noqa: PLC0415

            _cwd = os.getcwd()
            _srv = _sys.modules.get("yadgar.core.server")
            _get_default_branch = getattr(_srv, "_get_default_branch", None) if _srv else None
            if _get_default_branch is None:
                from yadgar.core.server.tools.project import _get_default_branch  # noqa: PLC0415
            _default_branch = _get_default_branch(_cwd)
        except Exception:
            _default_branch = None  # v5.42.4: canonical slot

        branch = _default_branch

    candidates = _st._wiki.find_similar_wiki_pages(
        title=title,
        content=content,
        branch=branch,
        threshold=effective_threshold,
        top_k=top_k,
    )
    return {
        "candidates": candidates,
        "threshold_used": effective_threshold,
    }


# ── v5.41.0: Versioning + section-patching tools ──────────────────────────────


@observe(tier="stage", metric="tools.wiki._resolve_page_id_by_slug")
def _resolve_page_id_by_slug(
    slug: str,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> tuple[int | None, dict | None]:
    """Directory+branch-resolve slug → page dict. Returns (page_id, page) or (None, None).

    v5.42.5 (F1 fix): accepts directory + branch_hint from caller so resolution uses
    caller context instead of daemon os.getcwd(). When directory is None, falls back
    to legacy branch-only resolution (backward-compat).
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    try:
        import sys as _sys  # noqa: PLC0415

        # Use caller-supplied branch_hint if available; otherwise detect from daemon CWD.
        # With directory supplied (v5.42.5), daemon CWD is irrelevant for branch detection
        # but we still resolve it as a secondary signal.
        _cwd = os.getcwd()
        _srv = _sys.modules.get("yadgar.core.server")
        _detect_branch = getattr(_srv, "_detect_branch", None) if _srv else None
        _get_default_branch = getattr(_srv, "_get_default_branch", None) if _srv else None
        if _detect_branch is None or _get_default_branch is None:
            from yadgar.core.server.tools.project import (  # noqa: PLC0415
                _detect_branch,
                _get_default_branch,
            )
        current_branch = branch_hint or _detect_branch(_cwd)
        default_branch = _get_default_branch(_cwd)
    except Exception:
        current_branch = branch_hint
        default_branch = None  # v5.42.4: canonical slot

    if directory is not None:
        # v5.42.5: use directory-aware 4-step resolution
        page = _st._wiki.read_by_directory_branch(slug, directory, current_branch)
    else:
        page = _st._wiki.read_by_branch(slug, current_branch, default_branch)
    if page is None:
        return None, None
    return page.get("id"), page


@_tool()
def wiki_history(
    slug: str, limit: int = 20, directory: str | None = None, branch_hint: str | None = None
) -> dict:
    """List version history for a wiki page, newest first.

    Returns metadata for each version (no content — use wiki_read_version for that).
    Each entry includes: version, created_at, change_summary, size_bytes, provenance_agent.

    Note: wiki_add uses an async file queue by default. Calling wiki_history immediately
    after wiki_add(wait=False) may return a stale list until the queue drains (typically
    within 30s). Use wiki_add(wait=True) on the preceding write to guarantee
    read-your-writes consistency without sleep — wait=True writes synchronously so the
    version row is visible immediately.

    Args:
        slug: Wiki page slug.
        limit: Max versions to return (default 20).
        directory: Caller directory for §25 resolution (v5.42.5 F1 fix).
        branch_hint: Caller branch for §25 resolution (v5.42.5 F1 fix).
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, page = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    versions = _st._wiki.history(page_id, limit=limit)
    total = _get_storage().get_max_version_for_page(page_id)
    return {"slug": slug, "page_id": page_id, "versions": versions, "total_versions": total}


@_tool()
def wiki_read_version(
    slug: str, version: int, directory: str | None = None, branch_hint: str | None = None
) -> dict:
    """Read a specific historical version of a wiki page (full content + snapshot fields).

    Args:
        slug: Wiki page slug.
        version: Version number (1-based; use wiki_history to find version numbers).
        directory: Caller directory for §25 resolution (v5.42.5 F1 fix).
        branch_hint: Caller branch for §25 resolution (v5.42.5 F1 fix).

    Returns the full snapshot including: version, title, content, category, tags,
    confidence, source_memory_ids, branch, change_summary, created_at.

    Error: {"error": "...", "max_version": N} if version not found.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    result = _st._wiki.read_version(page_id, version)
    result["slug"] = slug
    return result


@_tool()
def wiki_diff(
    slug: str,
    v1: int,
    v2: int,
    fmt: str = "unified",
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Diff two versions of a wiki page.

    Args:
        slug: Wiki page slug.
        v1: First (older) version number.
        v2: Second (newer) version number.
        fmt: "unified" (default, human-readable text diff) or "json" (structured).
        directory: Caller directory for §25 resolution (v5.42.5 F1 fix).
        branch_hint: Caller branch for §25 resolution (v5.42.5 F1 fix).

    unified format returns: {"diff": "<unified diff text>", "v1": N, "v2": M, ...}
    json format returns: {"hunks": [...], "added_lines": N, "removed_lines": M,
                          "sections_changed": [...], ...}
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    result = _st._wiki.diff(page_id, v1, v2, fmt=fmt)
    result["slug"] = slug
    return result


@_tool(power=True)
def wiki_restore(
    slug: str,
    version: int,
    wait: bool = False,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Restore a wiki page to a previous version by creating a new version.

    Creates a NEW version (N+1) whose content matches the specified historical version.
    Intervening versions are preserved — restore does not delete history.
    Rebuilds embedding, crossrefs, and all snapshot fields (title, tags, category,
    confidence) from the restored version.

    I26 secret-gate: NOT applied on restore. The content being restored was
    already secret-gated when first stored — re-gating would incorrectly reject
    your own previously approved content. This is intentional, not an oversight.

    Bypasses the v5.39 similarity gate: restore is explicit user intent (recovery
    from corruption), not a new duplicate page.

    Use wiki_history to see version numbers; use wiki_diff to confirm the content
    before restoring.

    Args:
        slug: Wiki page slug.
        version: Version number to restore from (use wiki_history to list).
        wait: Accepted for API symmetry with wiki_add. This tool writes
            synchronously (no queue) — wait=True is a no-op.
        directory: Caller directory for §25 resolution (v5.42.5 F1 fix).
        branch_hint: Caller branch for §25 resolution (v5.42.5 F1 fix).

    Returns: {"page_id": N, "restored_from_version": V, "new_version": N+1, "note": "..."}
    """
    # R3 Car 3c: slug→page_id resolution stays CORE (backend has no git/cwd, so
    # backend-side _detect_branch would resolve the wrong row); the restore write
    # forwards keyed by page_id.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    return _forward_admin("wiki_restore", {"page_id": page_id, "version": version, "slug": slug})


@_tool(power=True)
def wiki_append_section(
    slug: str,
    section_heading: str,
    content: str,
    position: str = "end_of_section",
    wait: bool = False,
    directory: str | None = None,
    branch_hint: str | None = None,
    heading_type: str = "h2",
) -> dict:
    """Section-atomic wiki write: patch a specific section without replacing entire content.

    Prevents the 2026-05-31 corruption pattern where agents replaced full wiki content
    with only their section patch (destroying everything else). Use this instead of
    wiki_update(fields={"content": <short patch>}) for targeted edits.

    Heading detection (controlled by heading_type, default 'h2'):
      h2 (default) — matches ## or ### at column 0. Case-insensitive. Ignores
        ## inside fenced code blocks. Use "Pipeline#2" syntax for 2nd occurrence.
      h3 — same as h2 (## and ### both matched by default).
      bold — matches **Bold Header** first-line patterns outside code fences.
      blockquote — matches "> text" first-line patterns.

    Positions:
      end_of_section    (default) — append content before next heading
      start_of_section  — insert immediately after heading line
      replace_section   — replace section body (heading preserved)
      new_section_top   — create new section at top (error if heading exists)
      new_section_bottom — create new section at bottom (error if heading exists)

    Error responses:
      {"error": "section_not_found", "available_sections": [...]}
      {"error": "section_exists"} — heading already present + new_section_* position
      {"error": "ambiguous_section"} — multiple headings + non-replace position
        (use "Heading#2" syntax to address 2nd occurrence)
      {"error": "invalid_heading_type"} — heading_type not in {h2, h3, bold, blockquote}

    wait: Accepted for API symmetry with wiki_add. This tool writes
        synchronously (no queue) — wait=True is a no-op.

    Returns: {"page_id": N, "new_version": M, "section_heading": "...",
              "action": "appended", "size_before": X, "size_after": Y}
    """
    # I26: secret-gate on written content (STAYS core)
    _gate = gate_or_reject(content, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: slug→page_id resolution stays core (backend has no git/cwd); the
    # section write forwards keyed by page_id.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_append_section",
        {
            "page_id": page_id,
            "section_heading": section_heading,
            "content": content,
            "position": position,
            "heading_type": heading_type,
            "slug": slug,
        },
    )


# ── v5.61.0: Layer 4 — Metadata primitives ───────────────────────────────────


@_tool(power=True)
def wiki_set_metadata(
    slug: str,
    field: str,
    value: str | None,
    directory: str | None = None,  # noqa: ARG001 — kept for API back-compat
    branch_hint: str | None = None,  # noqa: ARG001 — kept for API back-compat
) -> dict:
    """Set directory_context or branch on ALL rows sharing a slug (BC-G10 fix).

    Reaches every row for the slug — per-branch rows + 'global' stragglers —
    not just the single row returned by §25 resolution. This fixes the bug
    where wiki_set_metadata reported changed=False even though straggler rows
    were never touched (only one row was resolved via LIMIT 1 resolution).

    field must be 'directory_context' or 'branch'. Other fields are rejected.

    Validation per field:
      directory_context: 'global' or an absolute path (starts with '/').
      branch: null (sets canonical slot, resolves via IS NONE) or non-empty string.

    Idempotent per row: no version row created when the value already matches.
    On real change per row: creates a wiki_page_version row (v5.41 versioning).
    Logs old + new value per row for audit trail.

    Bypasses v5.39 similarity gate (metadata revision, not a new page).

    Args:
        slug: Wiki page slug.
        field: Metadata field to set. Must be 'directory_context' or 'branch'.
        value: New value. For branch, null clears it (sets canonical slot).
        directory: Kept for API back-compat (unused — all-rows path needs no §25 resolution).
        branch_hint: Kept for API back-compat (unused — same reason).

    Returns: {ok, slug, rows_updated, page_ids} or {ok: False, error}.
    Preserved keys for back-compat callers that inspect {ok, slug}.
    """
    # R3 Car 3c: slug-keyed all-rows metadata write forwards to backend /admin.
    # No §25 page_id resolution needed (impl reaches every row for the slug).
    return _forward_admin("wiki_set_metadata", {"slug": slug, "field": field, "value": value})


# ── v5.61.0: Layer 1 — Anchor-text primitives ────────────────────────────────


@_tool(power=True)
def wiki_replace_text(
    slug: str,
    old_text: str,
    new_text: str,
    occurrences: int | str = 1,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Replace old_text with new_text in a wiki page (surgical anchor-text edit).

    Caller never computes line/col. Server finds the text and applies the replacement.

    occurrences controls matching:
      1 (default) — require exactly one match (unique text). Reject if 0 or >1.
      N (int)     — require exactly N matches. Reject if count != N.
      'all'       — replace every occurrence (≥1 required, else reject).

    No-op (ok:True, replaced_count=0) when old_text == new_text.
    Reject (ok:False) when found-count != occurrences.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        old_text: Text to find (exact match, case-sensitive).
        new_text: Replacement text.
        occurrences: Expected match count, or 'all'.
        directory: Caller directory for §25 resolution.
        branch_hint: Caller branch for §25 resolution.

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_replace_text",
        {
            "page_id": page_id,
            "old_text": old_text,
            "new_text": new_text,
            "occurrences": occurrences,
            "slug": slug,
        },
    )


@_tool(power=True)
def wiki_delete_text(
    slug: str,
    text: str,
    occurrences: int | str = 1,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Delete text from a wiki page (surgical anchor-text edit).

    Absent text is a no-op (ok:True, replaced_count=0) — not an error.
    Reject (ok:False) when text IS present but found-count != occurrences.
    occurrences='all' deletes every match (≥1 required for present text).

    No secret gate: nothing new is written.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        text: Text to remove (exact match, case-sensitive).
        occurrences: Expected match count when text present, or 'all'.
        directory: Caller directory for §25 resolution.
        branch_hint: Caller branch for §25 resolution.

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    # No secret gate (nothing new is written).
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_delete_text",
        {"page_id": page_id, "text": text, "occurrences": occurrences, "slug": slug},
    )


@_tool(power=True)
def wiki_insert_after(
    slug: str,
    anchor_text: str,
    new_text: str,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Insert new_text immediately after anchor_text in a wiki page.

    anchor_text must be unique (exactly one occurrence). Reject if absent or non-unique.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        anchor_text: Unique text to locate (exact, case-sensitive).
        new_text: Content to insert immediately after anchor_text.
        directory: Caller directory for §25 resolution.
        branch_hint: Caller branch for §25 resolution.

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_insert_after",
        {"page_id": page_id, "anchor_text": anchor_text, "new_text": new_text, "slug": slug},
    )


@_tool(power=True)
def wiki_insert_before(
    slug: str,
    anchor_text: str,
    new_text: str,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Insert new_text immediately before anchor_text in a wiki page.

    anchor_text must be unique (exactly one occurrence). Reject if absent or non-unique.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        anchor_text: Unique text to locate (exact, case-sensitive).
        new_text: Content to insert immediately before anchor_text.
        directory: Caller directory for §25 resolution.
        branch_hint: Caller branch for §25 resolution.

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_insert_before",
        {"page_id": page_id, "anchor_text": anchor_text, "new_text": new_text, "slug": slug},
    )


# ── v5.61.0: Layer 2 — Positional primitives ─────────────────────────────────


@_tool(power=True)
def wiki_replace_at(
    slug: str,
    line: int,
    col: int,
    length: int,
    new_text: str,
    anchor_hint: str,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Replace `length` chars at (line, col) in a wiki page (positional escape hatch).

    anchor_hint MUST be ≥20 chars. The actual text at (line, col) must start with
    anchor_hint — guards against caller off-by-one arithmetic bugs.

    line/col are 1-indexed. length is in chars (not bytes).

    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        line: 1-indexed line number.
        col: 1-indexed column number.
        length: Number of chars to replace.
        new_text: Replacement text.
        anchor_hint: Expected text at (line, col). Must be ≥20 chars.
        directory: Caller directory for §25 resolution.
        branch_hint: Caller branch for §25 resolution.

    Returns: {ok, page_id, version_id, applied, length_delta}
      Mismatch: {ok: false, reason: "anchor_hint mismatch", actual_text_preview: "..."}
    """
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_replace_at",
        {
            "page_id": page_id,
            "line": line,
            "col": col,
            "length": length,
            "new_text": new_text,
            "anchor_hint": anchor_hint,
            "slug": slug,
        },
    )


@_tool(power=True)
def wiki_delete_at(
    slug: str,
    line: int,
    col: int,
    length: int,
    anchor_hint: str,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Delete `length` chars at (line, col) in a wiki page (positional escape hatch).

    anchor_hint MUST be ≥20 chars. The actual text at (line, col) must start with
    anchor_hint — guards against caller off-by-one arithmetic bugs.

    line/col are 1-indexed. length is in chars (not bytes).

    No secret gate: nothing new is written.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        line: 1-indexed line number.
        col: 1-indexed column number.
        length: Number of chars to delete.
        anchor_hint: Expected text at (line, col). Must be ≥20 chars.
        directory: Caller directory for §25 resolution.
        branch_hint: Caller branch for §25 resolution.

    Returns: {ok, page_id, version_id, applied, length_delta}
      Mismatch: {ok: false, reason: "anchor_hint mismatch", actual_text_preview: "..."}
    """
    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    # No secret gate (nothing new is written).
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_delete_at",
        {
            "page_id": page_id,
            "line": line,
            "col": col,
            "length": length,
            "anchor_hint": anchor_hint,
            "slug": slug,
        },
    )


@_tool(power=True)
def wiki_insert_at(
    slug: str,
    line: int,
    col: int,
    new_text: str,
    anchor_hint: str,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Insert new_text at (line, col) in a wiki page (positional escape hatch).

    anchor_hint MUST be ≥20 chars. The text immediately BEFORE the insertion
    point must end with anchor_hint — guards against off-by-one bugs.

    line/col are 1-indexed.

    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        line: 1-indexed line number.
        col: 1-indexed column (1 = start of line, len+1 = after end of line).
        new_text: Text to insert at position.
        anchor_hint: Expected text immediately before insertion point. Must be ≥20 chars.
        directory: Caller directory for §25 resolution.
        branch_hint: Caller branch for §25 resolution.

    Returns: {ok, page_id, version_id, applied, length_delta}
      Mismatch: {ok: false, reason: "anchor_hint mismatch", actual_text_preview: "..."}
    """
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_text, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_insert_at",
        {
            "page_id": page_id,
            "line": line,
            "col": col,
            "new_text": new_text,
            "anchor_hint": anchor_hint,
            "slug": slug,
        },
    )


# ── v5.61.0: Layer 3 — Structural primitives ─────────────────────────────────


@_tool(power=True)
def wiki_replace_markdown_block(
    slug: str,
    block_type: str,
    block_index: int,
    new_content: str,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Replace the Nth block of block_type in a wiki page (structural edit).

    Parses the markdown structure, locates the Nth block of the given type,
    and replaces the entire block span (including fence markers, >, #, etc.)
    with new_content.

    block_type must be one of: paragraph, heading, code_fence, blockquote, list, table.
    block_index is 0-based within the given block_type.

    Useful for: replace the 3rd code fence, swap a heading, rewrite a blockquote.
    Bypasses v5.39 similarity gate (revision, not new page).

    Args:
        slug: Wiki page slug.
        block_type: Type of markdown block to target.
        block_index: 0-based index within that block_type.
        new_content: Replacement content for the block (whole span including markers).
        directory: Caller directory for §25 resolution.
        branch_hint: Caller branch for §25 resolution.

    Returns: {ok, page_id, version_id, replaced_count, length_delta}
    """
    # I26: secret gate on new written content (STAYS core)
    _gate = gate_or_reject(new_content, tags=[])
    if _gate is not None:
        return _gate

    # R3 Car 3c: page_id resolved core (backend has no git/cwd); write forwards.
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"ok": False, "error": f"Wiki page '{slug}' not found"}

    return _forward_admin(
        "wiki_replace_markdown_block",
        {
            "page_id": page_id,
            "block_type": block_type,
            "block_index": block_index,
            "new_content": new_content,
            "slug": slug,
        },
    )
