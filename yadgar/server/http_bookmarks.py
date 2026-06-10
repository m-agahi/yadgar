"""HTTP routes for wiki bookmarks (v5.23.0 + v5.24.0 frontend view).

All @mcp_server.custom_route decorators live here — they fire at import time.
This module is imported in server/__init__.py alongside http.py.

Routes:
  GET    /api/bookmarks                  → api_bookmarks_list
  POST   /api/bookmarks                  → api_bookmarks_add
  DELETE /api/bookmarks/{slug}           → api_bookmarks_remove
  PUT    /api/bookmarks/{slug}/position  → api_bookmarks_reorder
  GET    /api/wiki/search                → api_wiki_search  (bookmark UI)
  GET    /api/wiki/list                  → api_wiki_list    (bookmark UI)
  GET    /static/bookmarks.html          → bookmarks_view   (v5.24.0 frontend page)
"""

from __future__ import annotations

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

import yadgar.server._state as _st
from yadgar.server._app import mcp_server
from yadgar.tracing import trace_span

logger = logging.getLogger(__name__)

# Cache-Control: no-store prevents browser caching of wiki content (freshness per plan §4)
_CORS = {"Cache-Control": "no-cache"}
_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate"}


# ---------------------------------------------------------------------------
# Bookmark CRUD routes
# ---------------------------------------------------------------------------


@mcp_server.custom_route("/api/bookmarks", methods=["GET"])
@trace_span("api.bookmarks.list")
async def api_bookmarks_list(request: Request) -> JSONResponse:
    """List all wiki bookmarks ordered by position.

    GET /api/bookmarks

    Response: JSON array of {slug, label_override, position, added_at}
    """
    from yadgar.server.tools.bookmarks import bookmark_list  # noqa: PLC0415

    rows = await asyncio.to_thread(bookmark_list)
    return JSONResponse(rows, headers=_CORS)


@mcp_server.custom_route("/api/bookmarks", methods=["POST"])
@trace_span("api.bookmarks.add")
async def api_bookmarks_add(request: Request) -> JSONResponse:
    """Add or update a wiki bookmark.

    POST /api/bookmarks
    Body: {slug: str, label_override?: str}

    Response 200: {added: true, slug, position}
    Response 400: {added: false, reason}
    """
    from yadgar.server.tools.bookmarks import bookmark_add  # noqa: PLC0415

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"added": False, "reason": "invalid_json"}, status_code=400, headers=_CORS
        )

    slug = (body.get("slug") or "").strip()
    if not slug:
        return JSONResponse(
            {"added": False, "reason": "slug_required"}, status_code=400, headers=_CORS
        )

    label_override = body.get("label_override") or ""
    result = await asyncio.to_thread(bookmark_add, slug, label_override)
    status = 200 if result.get("added") else 400
    return JSONResponse(result, status_code=status, headers=_CORS)


@mcp_server.custom_route("/api/bookmarks/{slug}", methods=["DELETE"])
@trace_span("api.bookmarks.remove")
async def api_bookmarks_remove(request: Request) -> JSONResponse:
    """Remove a wiki bookmark.

    DELETE /api/bookmarks/{slug}

    Response 200: {removed: bool, slug}
    """
    from yadgar.server.tools.bookmarks import bookmark_remove  # noqa: PLC0415

    slug = request.path_params.get("slug", "")
    result = await asyncio.to_thread(bookmark_remove, slug)
    return JSONResponse(result, headers=_CORS)


@mcp_server.custom_route("/api/bookmarks/{slug}/position", methods=["PUT"])
@trace_span("api.bookmarks.reorder")
async def api_bookmarks_reorder(request: Request) -> JSONResponse:
    """Move a bookmark to a new position.

    PUT /api/bookmarks/{slug}/position
    Body: {position: int}

    Response 200: {reordered: bool, slug, new_position}
    Response 400: {reordered: false, reason}
    """
    from yadgar.server.tools.bookmarks import bookmark_reorder  # noqa: PLC0415

    slug = request.path_params.get("slug", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"reordered": False, "reason": "invalid_json"}, status_code=400, headers=_CORS
        )

    try:
        pos = int(body.get("position", 0))
    except (TypeError, ValueError):  # fmt: skip
        return JSONResponse(
            {"reordered": False, "reason": "position_must_be_int"},
            status_code=400,
            headers=_CORS,
        )

    result = await asyncio.to_thread(bookmark_reorder, slug, pos)
    return JSONResponse(result, headers=_CORS)


# ---------------------------------------------------------------------------
# Wiki passthrough routes (slug autocomplete + semantic search for bookmarks UI)
# ---------------------------------------------------------------------------


@mcp_server.custom_route("/api/wiki/search", methods=["GET"])
@trace_span("api.wiki.search")
async def api_wiki_search(request: Request) -> JSONResponse:
    """Semantic wiki search for bookmarks UI.

    GET /api/wiki/search?q=<query>[&tags=tag1,tag2][&limit=10]

    Response: array of {slug, title, score, ...}
    """
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return JSONResponse([], headers=_NO_CACHE)

    tags_raw = request.query_params.get("tags", "")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else None
    try:
        limit = int(request.query_params.get("limit", 10))
    except (ValueError, TypeError):  # fmt: skip
        limit = 10

    wiki = _st._wiki
    if wiki is None:
        return JSONResponse([], status_code=503, headers=_NO_CACHE)

    try:
        results = await asyncio.to_thread(wiki.query, q, tags, None, limit)
    except Exception as exc:
        logger.debug("api_wiki_search error q=%s: %s", q, exc)
        return JSONResponse([], status_code=500, headers=_NO_CACHE)

    cleaned = [dict(r, **{"embedding": None}) for r in results or []]
    for r in cleaned:
        r.pop("embedding", None)
    return JSONResponse(cleaned, headers=_NO_CACHE)


@mcp_server.custom_route("/api/wiki/list", methods=["GET"])
@trace_span("api.wiki.list")
async def api_wiki_list(request: Request) -> JSONResponse:
    """List wiki pages for slug autocomplete in bookmarks UI.

    GET /api/wiki/list[?slug_prefix=<prefix>]

    Response: array of {slug, title, category}
    """
    slug_prefix = (request.query_params.get("slug_prefix") or "").strip() or None
    if _st._wiki is None or _st._storage is None:
        return JSONResponse([], status_code=503, headers=_NO_CACHE)

    try:
        rows = await asyncio.to_thread(_st._storage.list_wiki_pages, None, slug_prefix, 100)
    except Exception as exc:
        logger.debug("api_wiki_list error: %s", exc)
        return JSONResponse([], status_code=500, headers=_NO_CACHE)

    result = [
        {
            "slug": r.get("slug", ""),
            "title": r.get("title", ""),
            "category": r.get("category", ""),
        }
        for r in rows or []
    ]
    return JSONResponse(result, headers=_NO_CACHE)


# ---------------------------------------------------------------------------
# Bookmarks frontend page (v5.24.0)
# ---------------------------------------------------------------------------


@mcp_server.custom_route("/static/bookmarks.html", methods=["GET"])
@trace_span("api.bookmarks_view")
async def bookmarks_view(request: Request) -> RedirectResponse:
    """Redirect to the #bookmarks tab in the main SPA (v5.50.0 migration).

    GET /static/bookmarks.html → 302 /#bookmarks

    bookmarks.html is deprecated as a standalone page (v5.50.0).
    The bookmarks tab now lives inside the main SPA at /#bookmarks.
    This redirect will be kept for one minor cycle; removed in v5.52.0 or later.
    """
    return RedirectResponse("/#bookmarks", status_code=302)
