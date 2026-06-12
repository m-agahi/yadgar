"""Wiki MCP tool registrations."""

from __future__ import annotations

import logging
import os

import yadgar.server._state as _st
from yadgar.file_queue import is_draining
from yadgar.file_queue.dlq import _enforcement_on, _inc_relaxed
from yadgar.secrets import gate_or_reject
from yadgar.server._app import _tool
from yadgar.server._helpers import _has_unpaired_surrogate, _push_event
from yadgar.server.lifecycle import _get_file_queue, _get_storage

logger = logging.getLogger(__name__)


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


def _wiki_add_sync_write(
    title: str,
    content: str,
    category: str,
    tags: list[str] | None,
    source_memory_ids: list[int] | None,
    confidence: str,
    branch: str | None,
    append: bool,
    replace_slug: str | None,
    directory_context: str | None = None,
) -> dict:
    """Execute the sync wiki_add write path (QueueDrainer or fallback).

    Handles replace_slug overwrite, append merge, and normal upsert.
    v5.42.5: directory_context threaded through to WikiStore.add().
    """
    # replace_slug: overwrite a named existing page (gate already bypassed)
    if replace_slug is not None:
        existing = _st._wiki._storage.get_wiki_page_by_slug(replace_slug)
        if existing is not None:
            result = _st._wiki.add(
                title,
                content,
                category,
                tags or [],
                source_memory_ids,
                confidence,
                branch=branch,
                directory_context=directory_context,
            )
            result.pop("embedding", None)
            _push_event(
                {
                    "event": "wiki_updated",
                    "node": {
                        "id": f"wiki:{result.get('id', '')}",
                        "type": "wiki",
                        "slug": result.get("slug", ""),
                        "title": result.get("title", ""),
                    },
                }
            )
            try:
                _get_file_queue().write_wiki(result.get("slug", title), content)
            except Exception as exc:
                logger.debug("File queue wiki mirror failed (non-fatal): %s", exc)
            return result

    if append:
        result = _st._wiki.ingest(content, title, tags, source_memory_ids)
    else:
        result = _st._wiki.add(
            title,
            content,
            category,
            tags or [],
            source_memory_ids,
            confidence,
            branch=branch,
            directory_context=directory_context,
        )
    result.pop("embedding", None)
    event_type = "wiki_updated" if result.get("_merged") else "wiki_added"
    _push_event(
        {
            "event": event_type,
            "node": {
                "id": f"wiki:{result.get('id', '')}",
                "type": "wiki",
                "slug": result.get("slug", ""),
                "title": result.get("title", ""),
            },
        }
    )
    try:
        _get_file_queue().write_wiki(result.get("slug", title), content)
    except Exception as exc:
        logger.debug("File queue wiki mirror failed (non-fatal): %s", exc)
    return result


def _wiki_add_wait_path(
    title: str,
    content: str,
    category: str,
    tags: list[str] | None,
    source_memory_ids: list[int] | None,
    confidence: str,
    branch: str | None,
    append: bool,
    replace_slug: str | None,
    new_slug: str,
    force: bool = False,
    directory_context: str | None = None,
) -> dict:
    """Handle wiki_add(wait=True): enqueue + wait_for_job to preserve FIFO ordering.

    v5.41.5: similarity gate runs in the drainer pre-apply stage. On rejection,
    the drainer signals wait_for_job with the rejection payload; this function
    retrieves it via get_job_result() and returns the rejection dict to the caller
    (synchronous rejection, same observable contract as v5.39 for wait=True callers).

    Falls back to sync write when no drainer is running or replace_slug is set
    (queue path doesn't carry replace_slug semantics; named overwrite has no
    FIFO hazard anyway).

    Returns:
      {"committed": True}                          — success
      {"stored": False, "reason": "duplicate_detected", "candidates": [...]} — gate rejected
      {"stored": False, "reason": "wait_timeout"}  — drainer timeout
      sync-write result + committed=True           — fallback (no drainer)
    """
    drainer = _st._queue_drainer

    # Sync fallback: no drainer running, or replace_slug requires sync path.
    if drainer is None or replace_slug is not None:
        result = _wiki_add_sync_write(
            title,
            content,
            category,
            tags,
            source_memory_ids,
            confidence,
            branch,
            append,
            replace_slug,
            directory_context=directory_context,
        )
        result["stored"] = True
        result["queued"] = False
        result["committed"] = True
        return result

    # Enqueue like the async path so FIFO ordering is preserved.
    # v5.41.5: include force + replace_slug so drainer knows when to bypass gate.
    try:
        job_id = _get_file_queue().enqueue(
            "wiki_add",
            {
                "wiki_schema_version": 2,
                "slug": new_slug,
                "title": title,
                "content": content,
                "category": category or "reference",
                "tags": tags,
                "source_memory_ids": source_memory_ids,
                "confidence": confidence,
                "append": append,
                "branch": branch,
                "force": force,
                "replace_slug": replace_slug,
                "directory_context": directory_context,
            },
        )
    except Exception as fq_exc:
        logger.warning("wait=True enqueue failed, falling back to sync write: %s", fq_exc)
        result = _wiki_add_sync_write(
            title,
            content,
            category,
            tags,
            source_memory_ids,
            confidence,
            branch,
            append,
            replace_slug,
            directory_context=directory_context,
        )
        result["stored"] = True
        result["queued"] = False
        result["committed"] = True
        return result

    try:
        from yadgar.config import get_settings as _get_settings  # noqa: PLC0415

        timeout = getattr(_get_settings(), "WIKI_WRITE_WAIT_TIMEOUT_SECONDS", 5.0)
    except Exception:
        timeout = 5.0

    completed = drainer.wait_for_job(job_id, timeout=timeout)
    # v5.41.5: always retrieve result before cleanup (may carry rejection payload).
    rejection = _get_file_queue().get_job_result(job_id)
    _get_file_queue()._cleanup_job(job_id)

    if not completed:
        return {
            "stored": False,
            "reason": "wait_timeout",
            "queued": True,
            "slug": new_slug,
            "hint": "Write still queued — will commit on next drain or hit DLQ on repeated failure.",
        }

    if rejection is not None:
        # Gate fired in drainer — return rejection synchronously (DP-B).
        return rejection

    return {
        "stored": True,
        "queued": False,
        "committed": True,
        "slug": new_slug,
        "title": title,
    }


def _check_similarity_gate(
    title: str,
    content: str,
    branch: str | None,
    force: bool,
    replace_slug: str | None,
    append: bool,
    new_slug: str,
) -> dict | None:
    """Run the v5.39.0 similarity gate.

    Returns a rejection dict if gate fires in hard mode, None otherwise.
    Gate is skipped when: force=True, replace_slug set, append=True, or disabled via config.
    """
    if force:
        logger.info("wiki_add similarity gate bypassed via force=True for '%s'", title)
        return None
    if replace_slug is not None or append:
        return None  # update ops skip gate

    try:
        from yadgar.config import get_settings  # noqa: PLC0415

        cfg = get_settings()
        if not getattr(cfg, "WIKI_SIM_GATE_ENABLED", True):
            return None

        sim_mode = getattr(cfg, "WIKI_SIM_MODE", "hard")
        sim_threshold = getattr(cfg, "WIKI_SIM_CONTENT_THRESHOLD", 0.80)
        sim_top_k = getattr(cfg, "WIKI_SIM_TOP_K", 5)

        candidates = _st._wiki.find_similar_wiki_pages(
            title=title,
            content=content,
            branch=branch,
            threshold=sim_threshold,
            top_k=sim_top_k,
            exclude_slug=new_slug,  # skip self (upsert path)
        )
        if not candidates:
            return None

        if sim_mode == "soft":
            logger.warning(
                "wiki_add similarity gate (soft): near-duplicate for '%s', candidates=%s — allowing",
                title,
                [c["slug"] for c in candidates],
            )
            return None

        # hard mode: reject
        return {
            "stored": False,
            "reason": "duplicate_detected",
            "candidates": candidates,
            "hint": (
                "Use force=True to bypass, or replace_slug=<existing-slug> "
                "to overwrite the existing page."
            ),
        }
    except Exception as exc:
        logger.debug("wiki_add similarity gate error (non-fatal): %s", exc)
        return None


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
    # is_draining() path is exempt (drainer validates at its own boundary).
    # _check_wiki_add_context gates enforcement; YADGAR_*_ENFORCEMENT=false relaxes.
    if not is_draining():
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
    # I6 no-double-pay: gate runs once in drainer; is_draining()=True path below skips it.

    # QueueDrainer replay path: is_draining() means we're inside _apply().
    # Must not re-enqueue — write directly and return.
    if is_draining():
        return _wiki_add_sync_write(
            title,
            content,
            category,
            tags,
            source_memory_ids,
            confidence,
            branch,
            append,
            replace_slug,
            directory_context=_effective_dir,
        )

    # wait=True: enqueue first (preserves FIFO), then block until drainer commits.
    # v5.41.5: drainer runs similarity gate; rejection surfaces synchronously via
    # get_job_result() inside _wiki_add_wait_path. See DP-B in plan.
    if wait:
        return _wiki_add_wait_path(
            title,
            content,
            category,
            tags,
            source_memory_ids,
            confidence,
            branch,
            append,
            replace_slug,
            _new_slug,
            force=force,
            directory_context=_effective_dir,
        )

    # Async path (wait=False default): enqueue and return immediately.
    # v5.41.5: similarity gate is deferred to drainer — caller gets
    # {similarity_check: "deferred"} and must use wait=True for sync rejection.
    try:
        _get_file_queue().enqueue(
            "wiki_add",
            {
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
            },
        )
        return {
            "stored": True,
            "queued": True,
            "similarity_check": "deferred",
            "slug": _new_slug,
            "title": title,
        }
    except Exception as _fq_exc:
        logger.warning("File queue enqueue failed, falling back to sync write: %s", _fq_exc)

    # Queue fallback sync path
    return _wiki_add_sync_write(
        title,
        content,
        category,
        tags,
        source_memory_ids,
        confidence,
        branch,
        append,
        replace_slug,
        directory_context=_effective_dir,
    )


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
        When absent, all directories are returned (legacy mode; WARNING logged).
    branch_hint: Caller branch for §25 branch filter (v5.43.0).
        Uses branch_hint when daemon-side _detect_branch returns None (container scenario).
        Resolution order: _detect_branch(directory) → branch_hint → None (canonical slot).
    """
    import time as _time  # noqa: PLC0415

    _wiki_query_t0 = _time.monotonic()
    results: list[dict] = []

    try:
        assert _st._wiki is not None, "WikiStore not initialized"
        # Fetch extra results before branch filter so we still return max_results after pruning.
        results = _st._wiki.query(query, tags, category, max_results * 3)

        # §25 Branch filter + current-branch 1.5x score boost.
        # v5.43.0: accept caller-supplied directory + branch_hint to avoid daemon-CWD bug.
        # Resolution: _detect_branch(directory or os.getcwd()) → branch_hint → None.
        # Look up via yadgar.server so monkeypatches on "yadgar.server._detect_branch" etc. apply.
        try:
            import sys as _sys  # noqa: PLC0415

            _cwd = directory if directory else os.getcwd()
            _srv = _sys.modules.get("yadgar.server")
            _detect_branch = getattr(_srv, "_detect_branch", None) if _srv else None
            _get_default_branch = getattr(_srv, "_get_default_branch", None) if _srv else None
            if _detect_branch is None or _get_default_branch is None:
                from yadgar.server.tools.project import (
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

        # v5.43.0: directory scoping — scope to caller directory + 'global'.
        # Applied as Python-side post-filter (mirrors recall directory filter from v5.42.5).
        if directory is not None:
            caller_dir = directory.strip().rstrip("/") or None
            if caller_dir:
                results = [
                    r
                    for r in results
                    if r.get("directory_context") in (caller_dir, "global", "", None)
                ]
            else:
                logger.warning(
                    "wiki_query: directory supplied but empty after strip — skipping directory filter"
                )
        else:
            logger.warning(
                "wiki_query: no directory supplied — returning results from all directories "
                "(backward-compat mode). Pass directory= for project-scoped results (v5.43.0)."
            )

        if _effective_branch is not None:
            for r in results:
                if r.get("branch") == _effective_branch:
                    base = r.get("_retrieval_score", 0.0)
                    r["_retrieval_score"] = base * 1.5
            results.sort(key=lambda r: r.get("_retrieval_score", 0.0), reverse=True)

        results = results[:max_results]

        for r in results:
            r.pop("embedding", None)

        return results

    finally:
        # P11: observe wiki_query total duration in finally so it fires on all paths.
        try:
            from yadgar.metrics import yadgar_wiki_query_duration_ms  # noqa: PLC0415

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
        _srv = _sys.modules.get("yadgar.server")
        _detect_branch = getattr(_srv, "_detect_branch", None) if _srv else None
        _get_default_branch = getattr(_srv, "_get_default_branch", None) if _srv else None
        if _detect_branch is None or _get_default_branch is None:
            from yadgar.server.tools.project import (
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

    if directory is not None:
        # v5.42.5: 4-step directory-aware resolution
        caller_dir = directory.strip().rstrip("/") or None
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
    return page


@_tool(power=True)
def wiki_delete(slug: str) -> dict:
    """Delete a wiki page by slug."""
    assert _st._wiki is not None, "WikiStore not initialized"
    deleted = _st._wiki.delete(slug)
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
    assert _st._wiki is not None, "WikiStore not initialized"
    storage = _get_storage()
    draft = storage.get_wiki_draft_by_slug(slug)
    if draft is None:
        return {"approved": False, "error": f"Draft '{slug}' not found"}

    # v5.42.3: propagate branch from draft to wiki page.
    # branch=None → canonical slot (backward-compat for legacy drafts).
    draft_branch: str | None = draft.get("branch")

    result = _st._wiki.add(
        title=draft["title"],
        content=draft["content"],
        category=draft.get("category", "reference"),
        tags=draft.get("tags", []),
        source_memory_ids=draft.get("source_memory_ids", []),
        confidence=draft.get("confidence", "medium"),
        branch=draft_branch,
    )
    result.pop("embedding", None)
    storage.delete_wiki_draft(slug)
    try:
        _get_file_queue().write_wiki(result.get("slug", slug), draft["content"])
    except Exception as _fq_exc:
        logger.debug("File queue wiki mirror failed (non-fatal): %s", _fq_exc)
    return {"approved": True, "slug": slug, "page": result}


@_tool(power=True)
def wiki_discard(slug: str) -> dict:
    """Discard a pending wiki draft without promoting it to a full page.

    Permanently deletes the draft. Use for incorrect or low-value drafts.
    """
    storage = _get_storage()
    deleted = storage.delete_wiki_draft(slug)
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

    from yadgar.config import get_settings  # noqa: PLC0415

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
            _srv = _sys.modules.get("yadgar.server")
            _get_default_branch = getattr(_srv, "_get_default_branch", None) if _srv else None
            if _get_default_branch is None:
                from yadgar.server.tools.project import _get_default_branch  # noqa: PLC0415
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
        _srv = _sys.modules.get("yadgar.server")
        _detect_branch = getattr(_srv, "_detect_branch", None) if _srv else None
        _get_default_branch = getattr(_srv, "_get_default_branch", None) if _srv else None
        if _detect_branch is None or _get_default_branch is None:
            from yadgar.server.tools.project import (  # noqa: PLC0415
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
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    result = _st._wiki.restore_version(page_id, version)
    result["slug"] = slug
    return result


@_tool(power=True)
def wiki_append_section(
    slug: str,
    section_heading: str,
    content: str,
    position: str = "end_of_section",
    wait: bool = False,
    directory: str | None = None,
    branch_hint: str | None = None,
) -> dict:
    """Section-atomic wiki write: patch a specific section without replacing entire content.

    Prevents the 2026-05-31 corruption pattern where agents replaced full wiki content
    with only their section patch (destroying everything else). Use this instead of
    wiki_update(fields={"content": <short patch>}) for targeted edits.

    Heading detection: matches ## or ### at column 0. Case-insensitive. Ignores
    ## inside fenced code blocks. Use "Pipeline#2" syntax for 2nd occurrence.

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

    wait: Accepted for API symmetry with wiki_add. This tool writes
        synchronously (no queue) — wait=True is a no-op.

    Returns: {"page_id": N, "new_version": M, "section_heading": "...",
              "action": "appended", "size_before": X, "size_after": Y}
    """
    assert _st._wiki is not None, "WikiStore not initialized"

    # I26: secret-gate on written content
    _gate = gate_or_reject(content, tags=[])
    if _gate is not None:
        return _gate

    page_id, _ = _resolve_page_id_by_slug(slug, directory=directory, branch_hint=branch_hint)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}

    result = _st._wiki.append_section(page_id, section_heading, content, position)
    result["slug"] = slug
    return result
