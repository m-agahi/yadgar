"""Wiki MCP tool registrations."""

from __future__ import annotations

import logging
import os

import yadgar.server._state as _st
from yadgar.file_queue import is_draining
from yadgar.secrets import gate_or_reject
from yadgar.server._app import _tool
from yadgar.server._helpers import _has_unpaired_surrogate, _push_event
from yadgar.server.lifecycle import _get_file_queue, _get_storage

logger = logging.getLogger(__name__)


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
) -> dict:
    """Execute the sync wiki_add write path (QueueDrainer or fallback).

    Handles replace_slug overwrite, append merge, and normal upsert.
    """
    # replace_slug: overwrite a named existing page (gate already bypassed)
    if replace_slug is not None:
        existing = _st._wiki._storage.get_wiki_page_by_slug(replace_slug)
        if existing is not None:
            result = _st._wiki.add(
                title, content, category, tags or [], source_memory_ids, confidence, branch=branch
            )
            result.pop("embedding", None)
            _push_event(
                {
                    "event": "wiki_updated",
                    "node": {
                        "id": f"wiki:{result.get('id', '')}",
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
            title, content, category, tags or [], source_memory_ids, confidence, branch=branch
        )
    result.pop("embedding", None)
    event_type = "wiki_updated" if result.get("_merged") else "wiki_added"
    _push_event(
        {
            "event": event_type,
            "node": {
                "id": f"wiki:{result.get('id', '')}",
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
) -> dict:
    """Handle wiki_add(wait=True): enqueue + wait_for_job to preserve FIFO ordering.

    Falls back to sync write when no drainer is running or replace_slug is set
    (queue path doesn't carry replace_slug semantics; named overwrite has no
    FIFO hazard anyway).

    Returns {"committed": True} on success, {"reason": "wait_timeout"} on
    drainer timeout, or the sync-write result annotated with committed=True
    on fallback.
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
        )
        result["stored"] = True
        result["queued"] = False
        result["committed"] = True
        return result

    # Enqueue like the async path so FIFO ordering is preserved.
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

    if drainer.wait_for_job(job_id, timeout=timeout):
        return {
            "stored": True,
            "queued": False,
            "committed": True,
            "slug": new_slug,
            "title": title,
        }
    return {
        "stored": False,
        "reason": "wait_timeout",
        "queued": True,
        "slug": new_slug,
        "hint": "Write still queued — will commit on next drain or hit DLQ on repeated failure.",
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
) -> dict:
    """Create or update a wiki page. Content can include [[slug]] cross-references.

    append=False (default): create a new page or overwrite an existing one.
    append=True: merge content into an existing page (appends with timestamp,
      merges tags and source_memory_ids). Use for accumulating knowledge over time.

    v5.39.0 similarity gate: wiki_add now checks for near-duplicate pages before
    writing. If a similar page exists (combined cosine similarity >= threshold),
    the call is rejected with {"stored": False, "reason": "duplicate_detected", "candidates": [...]}.

    Use force=True to bypass the gate (logs warning). Use replace_slug=<existing-slug>
    to explicitly update an existing page by a different slug (treated as overwrite,
    gate is skipped).

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
    # If both are falsy (None / ""), branch stays None → canonical NULL slot.

    # v5.39.0: similarity gate — runs AFTER secret-gate, BEFORE enqueue (I26 ordering).
    import re as _re_slug  # noqa: PLC0415

    _new_slug = (_re_slug.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "untitled")[:64]

    _gate_result = _check_similarity_gate(
        title, content, branch, force, replace_slug, append, _new_slug
    )
    if _gate_result is not None:
        return _gate_result

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
        )

    # wait=True: enqueue first (preserves FIFO), then block until drainer commits.
    # See _wiki_add_wait_path for fallback semantics (no drainer, replace_slug).
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
        )

    # Async path (wait=False default): enqueue and return immediately.
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
            },
        )
        return {"stored": True, "queued": True, "slug": _new_slug, "title": title}
    except Exception as _fq_exc:
        logger.warning("File queue enqueue failed, falling back to sync write: %s", _fq_exc)

    # Queue fallback sync path
    return _wiki_add_sync_write(
        title, content, category, tags, source_memory_ids, confidence, branch, append, replace_slug
    )


@_tool()
def wiki_query(
    query: str,
    tags: list[str] | None = None,
    category: str | None = None,
    max_results: int = 5,
) -> list[dict]:
    """Search wiki pages by keyword + semantic similarity.

    Returns matching pages with relevance scores. Use tags and category to filter.
    """
    import time as _time  # noqa: PLC0415

    _wiki_query_t0 = _time.monotonic()
    results: list[dict] = []

    try:
        assert _st._wiki is not None, "WikiStore not initialized"
        # Fetch extra results before branch filter so we still return max_results after pruning.
        results = _st._wiki.query(query, tags, category, max_results * 3)

        # §25 Branch filter + current-branch 1.5x score boost.
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
            _default_branch = "master"

        _allowed_branches: set[str | None] = {_default_branch, None}
        if _current_branch is not None:
            _allowed_branches.add(_current_branch)

        results = [r for r in results if r.get("branch") in _allowed_branches]

        if _current_branch is not None:
            for r in results:
                if r.get("branch") == _current_branch:
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
def wiki_read(slug: str) -> dict:
    """Read a specific wiki page by slug.

    §25 Resolution order:
    1. Exact slug match on current branch.
    2. Exact slug match on default branch.
    3. Exact slug match with branch IS NONE (legacy/canonical).
    4. Not found → error dict.
    """
    assert _st._wiki is not None, "WikiStore not initialized"

    # Detect current and default branch for resolution order.
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
        _default_branch = "master"

    page = _st._wiki.read_by_branch(slug, _current_branch, _default_branch)
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
) -> list[dict]:
    """List wiki pages by metadata only (no content). Use wiki_read(slug) for full content.

    Categories: architecture, decision, pattern, debugging, reference, convention, fact, analysis.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    # Push LIMIT, category, and slug_prefix filters to the DB layer
    db_limit = limit if (limit is not None and limit > 0) else None
    pages = _st._wiki.list_pages(category=category, slug_prefix=slug_prefix, limit=db_limit)
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
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    storage = _get_storage()
    draft = storage.get_wiki_draft_by_slug(slug)
    if draft is None:
        return {"approved": False, "error": f"Draft '{slug}' not found"}
    result = _st._wiki.add(
        title=draft["title"],
        content=draft["content"],
        category=draft.get("category", "reference"),
        tags=draft.get("tags", []),
        source_memory_ids=draft.get("source_memory_ids", []),
        confidence=draft.get("confidence", "medium"),
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


def _resolve_page_id_by_slug(slug: str) -> tuple[int | None, dict | None]:
    """Branch-resolve slug → page dict. Returns (page_id, page) or (None, None)."""
    assert _st._wiki is not None, "WikiStore not initialized"
    try:
        import sys as _sys  # noqa: PLC0415

        _cwd = os.getcwd()
        _srv = _sys.modules.get("yadgar.server")
        _detect_branch = getattr(_srv, "_detect_branch", None) if _srv else None
        _get_default_branch = getattr(_srv, "_get_default_branch", None) if _srv else None
        if _detect_branch is None or _get_default_branch is None:
            from yadgar.server.tools.project import (  # noqa: PLC0415
                _detect_branch,
                _get_default_branch,
            )
        current_branch = _detect_branch(_cwd)
        default_branch = _get_default_branch(_cwd)
    except Exception:
        current_branch = None
        default_branch = "master"

    page = _st._wiki.read_by_branch(slug, current_branch, default_branch)
    if page is None:
        return None, None
    return page.get("id"), page


@_tool()
def wiki_history(slug: str, limit: int = 20) -> dict:
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
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, page = _resolve_page_id_by_slug(slug)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    versions = _st._wiki.history(page_id, limit=limit)
    total = _get_storage().get_max_version_for_page(page_id)
    return {"slug": slug, "page_id": page_id, "versions": versions, "total_versions": total}


@_tool()
def wiki_read_version(slug: str, version: int) -> dict:
    """Read a specific historical version of a wiki page (full content + snapshot fields).

    Args:
        slug: Wiki page slug.
        version: Version number (1-based; use wiki_history to find version numbers).

    Returns the full snapshot including: version, title, content, category, tags,
    confidence, source_memory_ids, branch, change_summary, created_at.

    Error: {"error": "...", "max_version": N} if version not found.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, _ = _resolve_page_id_by_slug(slug)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    result = _st._wiki.read_version(page_id, version)
    result["slug"] = slug
    return result


@_tool()
def wiki_diff(slug: str, v1: int, v2: int, fmt: str = "unified") -> dict:
    """Diff two versions of a wiki page.

    Args:
        slug: Wiki page slug.
        v1: First (older) version number.
        v2: Second (newer) version number.
        fmt: "unified" (default, human-readable text diff) or "json" (structured).

    unified format returns: {"diff": "<unified diff text>", "v1": N, "v2": M, ...}
    json format returns: {"hunks": [...], "added_lines": N, "removed_lines": M,
                          "sections_changed": [...], ...}
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, _ = _resolve_page_id_by_slug(slug)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}
    result = _st._wiki.diff(page_id, v1, v2, fmt=fmt)
    result["slug"] = slug
    return result


@_tool(power=True)
def wiki_restore(slug: str, version: int, wait: bool = False) -> dict:
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

    Returns: {"page_id": N, "restored_from_version": V, "new_version": N+1, "note": "..."}
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    page_id, _ = _resolve_page_id_by_slug(slug)
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

    page_id, _ = _resolve_page_id_by_slug(slug)
    if page_id is None:
        return {"error": f"Wiki page '{slug}' not found"}

    result = _st._wiki.append_section(page_id, section_heading, content, position)
    result["slug"] = slug
    return result
