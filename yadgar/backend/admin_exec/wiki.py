"""Backend execution bodies for the wiki-EDIT admin ops (R3 Car 3c / R5 group 3).

These are the storage-write halves of the core wiki-edit + agent_prompt MCP tools
whose ``@_tool`` shells (validation + I26 secret-gate + slug→page_id resolution)
stay in ``yadgar.core.server.tools.*`` and forward the DB write here over HTTP
(POST /admin) via ``_forward_admin``.

Group 3 (R5) ops: wiki_delete, wiki_restore, wiki_update, wiki_append_section,
wiki_replace_text, wiki_delete_text, wiki_insert_after, wiki_insert_before,
wiki_replace_at, wiki_delete_at, wiki_insert_at, wiki_replace_markdown_block,
wiki_set_metadata, wiki_autolink, agent_prompt_save.

DESIGN — slug→page_id resolution stays CORE.
``_resolve_page_id_by_slug`` resolves against the caller's ``os.getcwd()``. The
backend container has a different cwd, so backend-side resolution would land the
wrong directory row. Core resolves the slug to a ``page_id``
(reads are allowed core-side — "zero DB" is a write-side goal — and core+backend
share the same DB), then forwards the write keyed by ``page_id``. Impls that need
the page therefore take ``page_id`` in the payload, not ``slug``+context.

CACHE EPOCH — every ``_st._wiki.*`` DB write funnels through
``storage.wiki.insert/update/delete/set_metadata`` which calls
``_bump_wiki_epoch → bump_epoch(None)``. That bump is file-backed on the shared
queue volume (Car 2), so a backend-side bump busts the core process's cached
wiki_read / wiki_query / agent_dispatch_prelude namespaces. Running these impls
backend-side keeps the invalidation correct.

Each op is an undecorated ``(payload: dict) -> dict`` function; the ``@observe``
decorators satisfy the I33 tri-signal ratchet. Storage / wiki / replay are fetched
via ``_st`` — the /admin route builds the slim engine set (which includes
``_st._wiki`` and ``_st._replay``) first.
"""

from __future__ import annotations

import logging

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar._shared.wiki.contract import WikiAddOptions
from yadgar._shared.wiki.wiki_meta import (
    PAGE_TYPE_AGENT_PROMPT_LEGACY,
)

logger = logging.getLogger(__name__)

# Car I removed: the agent-prompt TOC machinery (``_TOC_TITLE`` /
# ``_TOC_SLUG`` / ``_TOC_ROW_RE`` / ``_LIBRARY_ANCHOR_*`` / ``_toc_row`` /
# ``_toc_with_row`` / ``_upsert_toc_row`` / ``_set_toc_row_count`` /
# ``_ensure_library_anchor``) and the memory-row ``increment_prompt_usage``
# op all retired. The agent-prompt library now lives in the
# ``agent_pattern`` ledger table (see backend/admin_exec/ledger_agent.py and
# core/server/tools/agent_prompts.py).


# ── Layer 0: slug-keyed page ops ──────────────────────────────────────────────


@observe(tier="boundary", metric="backend.admin.wiki_delete")
def wiki_delete(payload: dict) -> dict:
    """Delete a wiki page by slug (DB + epoch bump). Storage-write half.

    payload: {"slug": str}
    Returns {deleted: bool} — the core shell adds the SSE push + file-queue mirror
    cleanup (both core-side side-effects) after this returns.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    slug = payload["slug"]
    deleted = _st._wiki.delete(slug)
    return {"deleted": bool(deleted)}


@observe(tier="boundary", metric="backend.admin.wiki_autolink")
def wiki_autolink(payload: dict) -> dict:
    """Auto-insert [[slug]] cross-refs (writes when dry_run=False). Storage-write half.

    payload: {directory, dry_run, min_title_len, max_links_per_page,
              similarity_threshold, semantic_guard}
    Returns the autolink result dict. Forwarding the dry-run compute too keeps a
    single path (harmless — no write on dry_run).
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    return _st._wiki.autolink(
        directory=payload.get("directory"),
        dry_run=bool(payload.get("dry_run", True)),
        min_title_len=int(payload.get("min_title_len", 6)),
        max_links_per_page=int(payload.get("max_links_per_page", 20)),
        similarity_threshold=float(payload.get("similarity_threshold", 0.70)),
        semantic_guard=bool(payload.get("semantic_guard", True)),
    )


# ── page_id-keyed edit ops (slug resolved core-side) ──────────────────────────


@observe(tier="boundary", metric="backend.admin.wiki_update")
def wiki_update(payload: dict) -> dict:
    """Patch selected fields on a wiki page record. Storage-write half.

    payload: {"page_id": int, "fields": dict}
    ``fields`` allowed-keys already validated + secret-gated core-side.
    Returns the updated page dict (embedding bytes stripped).
    Raises ValueError if the page is not found.
    """
    page_id = int(payload["page_id"])
    fields = payload.get("fields") or {}
    storage = _get_storage()
    if not fields:
        result = storage.get_wiki_page(page_id)
        if result is None:
            raise ValueError(f"Wiki page {page_id} not found")
        result.pop("embedding", None)
        return result
    storage.update_wiki_page(page_id, fields)
    updated = storage.get_wiki_page(page_id)
    if updated is None:
        raise ValueError(f"Wiki page {page_id} not found after update")
    updated.pop("embedding", None)
    return updated


@observe(tier="boundary", metric="backend.admin.wiki_restore")
def wiki_restore(payload: dict) -> dict:
    """Restore a wiki page to a previous version (new version N+1). Storage-write half.

    payload: {"page_id": int, "version": int, "slug": str}
    Returns the restore result with slug echoed.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    result = _st._wiki.restore_version(int(payload["page_id"]), int(payload["version"]))
    result["slug"] = payload["slug"]
    return result


@observe(tier="boundary", metric="backend.admin.wiki_append_section")
def wiki_append_section(payload: dict) -> dict:
    """Section-atomic wiki write. Storage-write half.

    payload: {page_id, section_heading, content, position, heading_type, slug}
    Content already secret-gated core-side. Returns the append result + slug.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    result = _st._wiki.append_section(
        int(payload["page_id"]),
        payload["section_heading"],
        payload["content"],
        payload["position"],
        payload["heading_type"],
    )
    result["slug"] = payload["slug"]
    return result


@observe(tier="boundary", metric="backend.admin.wiki_set_metadata")
def wiki_set_metadata(payload: dict) -> dict:
    """Set directory_context on ALL rows sharing a slug. Storage-write half.

    payload: {"slug": str, "field": str, "value": str | None}
    Returns {ok, slug, rows_updated, page_ids} or {ok: False, error}.
    Slug-keyed (all-rows path needs no §25 resolution — see core shell docstring).
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    return _st._wiki.set_metadata_by_slug(payload["slug"], payload["field"], payload["value"])


@observe(tier="boundary", metric="backend.admin.wiki_set_mutability")
def wiki_set_mutability(payload: dict) -> dict:
    """Set ``mutability_override`` on ALL rows sharing a slug. Storage-write half.

    Car J (0047 §7 D25/D26). The privileged write path for the per-page
    mutability override — mirrors ``wiki_set_metadata``'s all-rows shape and
    delegates to ``WikiStore.set_mutability_by_slug`` (sole writer).

    payload: {"slug": str, "value": str | None, "reason": str}
    Returns {ok, slug, rows_updated, page_ids} or {ok: False, error}.

    Validation (reason required, value ∈ {free, locked, derived, None}) is
    enforced at both layers — core shell + backend handler — so a forged
    payload that bypassed the MCP gate still fails before the storage write.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    reason = payload.get("reason", "")
    if not reason or not reason.strip():
        return {
            "ok": False,
            "error": "reason is required for mutability_override audit log",
        }
    return _st._wiki.set_mutability_by_slug(
        payload["slug"],
        payload.get("value"),
        reason=reason,
    )


@observe(tier="boundary", metric="backend.admin.wiki_replace_text")
def wiki_replace_text(payload: dict) -> dict:
    """Replace old_text with new_text in a wiki page. Storage-write half.

    payload: {page_id, old_text, new_text, occurrences, slug}
    new_text already secret-gated core-side. Returns the result + slug.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    result = _st._wiki.replace_text(
        int(payload["page_id"]),
        payload["old_text"],
        payload["new_text"],
        payload["occurrences"],
    )
    result["slug"] = payload["slug"]
    return result


@observe(tier="boundary", metric="backend.admin.wiki_delete_text")
def wiki_delete_text(payload: dict) -> dict:
    """Delete text from a wiki page. Storage-write half.

    payload: {page_id, text, occurrences, slug}
    No secret gate (nothing new written). Returns the result + slug.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    result = _st._wiki.delete_text(
        int(payload["page_id"]),
        payload["text"],
        payload["occurrences"],
    )
    result["slug"] = payload["slug"]
    return result


@observe(tier="boundary", metric="backend.admin.wiki_insert_after")
def wiki_insert_after(payload: dict) -> dict:
    """Insert new_text immediately after anchor_text. Storage-write half.

    payload: {page_id, anchor_text, new_text, slug}
    new_text already secret-gated core-side. Returns the result + slug.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    result = _st._wiki.insert_after(
        int(payload["page_id"]),
        payload["anchor_text"],
        payload["new_text"],
    )
    result["slug"] = payload["slug"]
    return result


@observe(tier="boundary", metric="backend.admin.wiki_insert_before")
def wiki_insert_before(payload: dict) -> dict:
    """Insert new_text immediately before anchor_text. Storage-write half.

    payload: {page_id, anchor_text, new_text, slug}
    new_text already secret-gated core-side. Returns the result + slug.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    result = _st._wiki.insert_before(
        int(payload["page_id"]),
        payload["anchor_text"],
        payload["new_text"],
    )
    result["slug"] = payload["slug"]
    return result


@observe(tier="boundary", metric="backend.admin.wiki_replace_at")
def wiki_replace_at(payload: dict) -> dict:
    """Replace `length` chars at (line, col). Storage-write half.

    payload: {page_id, line, col, length, new_text, anchor_hint, slug}
    new_text already secret-gated core-side. Returns the result + slug.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    result = _st._wiki.replace_at(
        int(payload["page_id"]),
        int(payload["line"]),
        int(payload["col"]),
        int(payload["length"]),
        payload["new_text"],
        payload["anchor_hint"],
    )
    result["slug"] = payload["slug"]
    return result


@observe(tier="boundary", metric="backend.admin.wiki_delete_at")
def wiki_delete_at(payload: dict) -> dict:
    """Delete `length` chars at (line, col). Storage-write half.

    payload: {page_id, line, col, length, anchor_hint, slug}
    No secret gate (nothing new written). Returns the result + slug.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    result = _st._wiki.delete_at(
        int(payload["page_id"]),
        int(payload["line"]),
        int(payload["col"]),
        int(payload["length"]),
        payload["anchor_hint"],
    )
    result["slug"] = payload["slug"]
    return result


@observe(tier="boundary", metric="backend.admin.wiki_insert_at")
def wiki_insert_at(payload: dict) -> dict:
    """Insert new_text at (line, col). Storage-write half.

    payload: {page_id, line, col, new_text, anchor_hint, slug}
    new_text already secret-gated core-side. Returns the result + slug.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    result = _st._wiki.insert_at(
        int(payload["page_id"]),
        int(payload["line"]),
        int(payload["col"]),
        payload["new_text"],
        payload["anchor_hint"],
    )
    result["slug"] = payload["slug"]
    return result


@observe(tier="boundary", metric="backend.admin.wiki_replace_markdown_block")
def wiki_replace_markdown_block(payload: dict) -> dict:
    """Replace the Nth block of block_type in a wiki page. Storage-write half.

    payload: {page_id, block_type, block_index, new_content, slug}
    new_content already secret-gated core-side. Returns the result + slug.
    """
    assert _st._wiki is not None, "WikiStore not initialized"
    result = _st._wiki.replace_markdown_block(
        int(payload["page_id"]),
        payload["block_type"],
        int(payload["block_index"]),
        payload["new_content"],
    )
    result["slug"] = payload["slug"]
    return result


# ── agent_prompt_save (wiki.add only — TOC + library anchor retired in Car I) ─


@observe(tier="boundary", metric="backend.admin.agent_prompt_save")
def agent_prompt_save(payload: dict) -> dict:
    """Upsert an agent-prompt page (body-only; ledger row is the core wrapper's job).

    payload: {slug, title, full_content, tags, pattern, purpose, directory,
              project_id}
    Content already secret-gated + directory-validated + wrapped core-side.
    Returns {saved: True, version, slug, page_id}.

    C4b (0047 PR#40 §5) — ``project_id``. This is the ONE op in this module
    that MINTS a row; every other op here is ``page_id``-keyed and edits a row
    whose project_id was stamped by whoever inserted it. Before this car the
    value was absent on both write arms, so ``insert_wiki_page`` fell through
    to the container-side classifier — the derivation ADR-0227 deletes. The
    caller (the core ``agent_prompt_save`` / ``_save_discipline_page``
    wrappers) resolves it in the process that can see the session and puts it
    on the payload; this op only forwards it. Both arms are stamped: the
    ``WikiStore.add`` path and the ``_st._wiki is None`` fallback.

    Car I: this op writes ONLY the wiki body page. The companion
    ``agent_pattern`` ledger row is written by ``save_agent_pattern_row``
    (registered in admin_exec/__init__.py under ``ledger``), which the
    core wrapper calls AFTER this returns — order matters (page first,
    row second, per §9 of the spine plan): a crash leaves an orphan
    page, not an orphan row, and ``check_page_row_desync`` is the
    detection arm.

    Touches the ``agent_prompt_prelude`` cache namespace via the wiki.add →
    _bump_wiki_epoch hook (global bump, file-backed cross-process, Car 2) — the
    prelude cache busts through the same epoch as wiki_read/query.
    """
    storage = _get_storage()
    slug = payload["slug"]
    title = payload["title"]
    full_content = payload["full_content"]
    tags = payload["tags"]
    effective_dir = payload["directory"]
    # ADR-0209: the family is decided CORE-side and carried on the payload —
    # this op keys everything else off the slug, but re-deriving the page_type
    # from a slug prefix here would rebuild exactly the string-matching the
    # split removes. Default only covers a payload from a pre-split core.
    page_type = payload.get("page_type") or PAGE_TYPE_AGENT_PROMPT_LEGACY
    # C4b: forwarded, never recomputed — see the docstring.
    project_id = payload.get("project_id")

    wiki = _st._wiki
    if wiki is not None:
        result = wiki.add(
            title=title,
            content=full_content,
            category="reference",
            tags=tags,
            opts=WikiAddOptions(
                source_memory_ids=[],
                confidence="high",
                directory_context=effective_dir,
                page_type=page_type,
                project_id=project_id,
            ),
        )
        page_id = result.get("id")
        version = storage.get_max_version_for_page(int(page_id)) if page_id is not None else 1
    else:
        # Fallback: direct storage upsert when wiki not initialised (should not
        # happen backend-side, but preserves the core tool's contract).
        existing = storage.get_wiki_page_by_slug(slug)
        if existing is not None:
            page_id = storage._extract_id(existing.get("id"))
            storage.update_wiki_page(
                page_id,
                {
                    "title": title,
                    "content": full_content,
                    "tags": tags,
                    "category": "reference",
                    "confidence": "high",
                    "directory_context": effective_dir,
                    "page_type": page_type,
                },
            )
            version = storage.get_max_version_for_page(page_id)
        else:
            page_id = storage.insert_wiki_page(
                {
                    "slug": slug,
                    "title": title,
                    "content": full_content,
                    "tags": tags,
                    "links": [],
                    "category": "reference",
                    "confidence": "high",
                    "source_memory_ids": [],
                    "directory_context": effective_dir,
                    "page_type": page_type,
                    "wiki_schema_version": 1,
                    "project_id": project_id,
                }
            )
            version = 1

    return {
        "saved": True,
        "version": version,
        "slug": slug,
        "page_id": page_id,
    }
