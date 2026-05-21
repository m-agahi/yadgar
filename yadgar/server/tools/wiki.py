"""Wiki MCP tool registrations."""

from __future__ import annotations

import logging
import os

import yadgar.server._state as _st
from yadgar.file_queue import is_draining
from yadgar.secrets import check_secrets
from yadgar.server._app import _tool
from yadgar.server._helpers import _has_unpaired_surrogate, _push_event
from yadgar.server.lifecycle import _get_file_queue, _get_storage

logger = logging.getLogger(__name__)


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
) -> dict:
    """Create or update a wiki page. Content can include [[slug]] cross-references.

    append=False (default): create a new page or overwrite an existing one.
    append=True: merge content into an existing page (appends with timestamp,
      merges tags and source_memory_ids). Use for accumulating knowledge over time.

    Categories: architecture, decision, pattern, debugging, reference, convention, fact, analysis.
    Confidence: high, medium, low.

    Branch resolution (evaluated in priority order):
    1. branch (non-empty string) — caller knows the branch explicitly; used as-is.
    2. branch_hint (non-empty string) — host-side hook passed the caller's branch;
       used when branch is None/empty (mirrors memorize branch_hint, v5.4 W1).
    3. Both omitted / None — page stored with branch IS NULL, the canonical slot
       resolved by wiki_read step 3. DO NOT fall back to _detect_branch(os.getcwd());
       the daemon CWD is not the caller's repo and would always resolve to "master".
    """
    assert _st._wiki is not None, "WikiStore not initialized"

    if len(content) > 65_536:
        return {"stored": False, "reason": "content_too_large", "max_bytes": 65_536}

    # Secret detection and write-path rules
    sec_blocked, sec_reason, sec_pattern = check_secrets(content)
    if sec_blocked:
        return {"stored": False, "reason": sec_reason, "pattern_matched": sec_pattern}
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

    # Async path: enqueue and return immediately (skip during drain replay)
    if not is_draining():
        try:
            import re as _re

            slug = (_re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "untitled")[:64]
            _get_file_queue().enqueue(
                "wiki_add",
                {
                    "wiki_schema_version": 2,
                    "slug": slug,
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
            return {"stored": True, "queued": True, "slug": slug, "title": title}
        except Exception as _fq_exc:
            logger.warning("File queue enqueue failed, falling back to sync write: %s", _fq_exc)

    # Sync path: called by QueueDrainer (is_draining=True) or queue fallback
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
    except Exception as _fq_exc:
        logger.debug("File queue wiki mirror failed (non-fatal): %s", _fq_exc)

    return result


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
