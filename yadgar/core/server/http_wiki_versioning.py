"""HTTP routes for wiki versioning + search (v5.50.1 Bookmarks tab).

These routes expose the v5.41 wiki versioning MCP tools over HTTP so the
Bookmarks tab frontend can consume them without requiring MCP transport.

Routes (no debug gate — core routes):
  GET  /api/wiki_query?q=&mode=semantic|keyword|slug[&limit=N]
  GET  /api/wiki_history?slug=
  GET  /api/wiki_read_version?slug=&version=N
  GET  /api/wiki_diff?slug=&v1=A&v2=B[&fmt=unified|json]
  POST /api/wiki_restore   {slug: str, version: int}

Mode handling:
  semantic — delegates to WikiStore.query() (embedding path, no SurrealDB FULLTEXT)
  keyword  — Python substring filter over list_wiki_pages results
  slug     — prefix match via list_wiki_pages(slug_prefix=q)

Do NOT duplicate /api/wiki/search (http_bookmarks.py) or /api/wiki/read (http.py).
"""

from __future__ import annotations

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe
from yadgar._shared.tracing import trace_span
from yadgar.core.server._app import mcp_server

logger = logging.getLogger(__name__)

_CORS = {"Cache-Control": "no-store, no-cache, must-revalidate"}


# ---------------------------------------------------------------------------
# /api/wiki_query  — unified search (semantic / keyword / slug)
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _parse_limit(raw: str | None, default: int = 20) -> int:
    """Parse limit query param, clamped to [1, 100]."""
    try:
        return max(1, min(int(raw or default), 100))
    except (ValueError, TypeError):  # fmt: skip
        return default


@observe(tier="stage")
def _page_row_to_dict(r: dict) -> dict:
    """Convert a storage list_wiki_pages row to a lean response dict."""
    return {
        "slug": r.get("slug", ""),
        "title": r.get("title", ""),
        "category": r.get("category", ""),
        "tags": r.get("tags") or [],
    }


@observe(tier="stage")
async def _wiki_search_semantic(wiki: object, q: str, limit: int) -> list[dict]:
    """Embedding-based search — no SurrealDB syntax."""
    results = await asyncio.to_thread(wiki.query, q, None, None, limit)
    return [{k: v for k, v in r.items() if k != "embedding"} for r in (results or [])]


@observe(tier="stage")
async def _wiki_search_slug(storage: object, q: str, limit: int) -> list[dict]:
    """Prefix match on slug via list_wiki_pages(slug_prefix=q)."""
    rows = await asyncio.to_thread(storage.list_wiki_pages, None, q, limit)
    return [_page_row_to_dict(r) for r in (rows or [])]


@observe(tier="stage")
async def _wiki_search_keyword(storage: object, q: str, limit: int) -> list[dict]:
    """Python substring filter — no FULLTEXT index, no SurrealDB FTS."""
    rows = await asyncio.to_thread(storage.list_wiki_pages, None, None, 500)
    q_lower = q.lower()
    matched = []
    for r in rows or []:
        if q_lower in r.get("slug", "").lower() or q_lower in r.get("title", "").lower():
            matched.append(_page_row_to_dict(r))
            if len(matched) >= limit:
                break
    return matched


@mcp_server.custom_route("/api/wiki_query", methods=["GET"])
@trace_span()
async def api_wiki_query(request: Request) -> JSONResponse:
    """Unified wiki search for the Bookmarks tab.

    GET /api/wiki_query?q=<query>&mode=semantic|keyword|slug[&limit=N]

    Response: JSON array of {slug, title, score?, category, tags}
    """
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return JSONResponse([], headers=_CORS)

    mode = (request.query_params.get("mode") or "semantic").strip().lower()
    if mode not in ("semantic", "keyword", "slug"):
        mode = "semantic"

    limit = _parse_limit(request.query_params.get("limit"))

    wiki = _st._wiki
    storage = _st._storage
    if wiki is None or storage is None:
        return JSONResponse([], status_code=503, headers=_CORS)

    try:
        if mode == "semantic":
            results = await _wiki_search_semantic(wiki, q, limit)
        elif mode == "slug":
            results = await _wiki_search_slug(storage, q, limit)
        else:
            results = await _wiki_search_keyword(storage, q, limit)
        return JSONResponse(results, headers=_CORS)
    except Exception as exc:
        logger.debug("api_wiki_query error q=%s mode=%s: %s", q, mode, exc)
        return JSONResponse([], status_code=500, headers=_CORS)


# ---------------------------------------------------------------------------
# /api/wiki_history  — version list for a wiki page
# ---------------------------------------------------------------------------


@mcp_server.custom_route("/api/wiki_history", methods=["GET"])
@trace_span()
async def api_wiki_history(request: Request) -> JSONResponse:
    """List version history for a wiki page.

    GET /api/wiki_history?slug=<slug>[&limit=N]

    Response: {slug, page_id, versions: [{version, created_at, change_summary,
                                          size_bytes, provenance_agent}],
               total_versions: N}
    Error: {error: "..."} with status 404 or 500.
    """
    slug = (request.query_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "slug required"}, status_code=400, headers=_CORS)

    try:
        limit = int(request.query_params.get("limit", 20))
        limit = max(1, min(limit, 100))
    except (ValueError, TypeError):  # fmt: skip
        limit = 20

    wiki = _st._wiki
    if wiki is None:
        return JSONResponse({"error": "wiki not initialized"}, status_code=503, headers=_CORS)

    try:
        from yadgar.core.server.tools.wiki import wiki_history  # noqa: PLC0415

        result = await asyncio.to_thread(wiki_history, slug, limit)
    except Exception as exc:
        logger.debug("api_wiki_history error slug=%s: %s", slug, exc)
        return JSONResponse({"error": str(exc)}, status_code=500, headers=_CORS)

    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=404, headers=_CORS)

    return JSONResponse(result, headers=_CORS)


# ---------------------------------------------------------------------------
# /api/wiki_read_version  — read a specific historical version
# ---------------------------------------------------------------------------


@mcp_server.custom_route("/api/wiki_read_version", methods=["GET"])
@trace_span()
async def api_wiki_read_version(request: Request) -> JSONResponse:
    """Read a specific historical version of a wiki page.

    GET /api/wiki_read_version?slug=<slug>&version=N

    Response: full snapshot {version, title, content, category, tags,
                              confidence, change_summary, created_at, slug}
    Error: {error: "...", max_version?: N} with status 404 or 500.
    """
    slug = (request.query_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "slug required"}, status_code=400, headers=_CORS)

    version_raw = (request.query_params.get("version") or "").strip()
    try:
        version = int(version_raw)
    except (ValueError, TypeError):  # fmt: skip
        return JSONResponse({"error": "version must be an integer"}, status_code=400, headers=_CORS)

    wiki = _st._wiki
    if wiki is None:
        return JSONResponse({"error": "wiki not initialized"}, status_code=503, headers=_CORS)

    try:
        from yadgar.core.server.tools.wiki import wiki_read_version  # noqa: PLC0415

        result = await asyncio.to_thread(wiki_read_version, slug, version)
    except Exception as exc:
        logger.debug("api_wiki_read_version error slug=%s v=%s: %s", slug, version, exc)
        return JSONResponse({"error": str(exc)}, status_code=500, headers=_CORS)

    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=404, headers=_CORS)

    return JSONResponse(result, headers=_CORS)


# ---------------------------------------------------------------------------
# /api/wiki_diff  — diff two versions
# ---------------------------------------------------------------------------


@mcp_server.custom_route("/api/wiki_diff", methods=["GET"])
@trace_span()
async def api_wiki_diff(request: Request) -> JSONResponse:
    """Diff two versions of a wiki page.

    GET /api/wiki_diff?slug=<slug>&v1=A&v2=B[&fmt=unified|json]

    fmt=unified (default): {diff: "<text>", v1, v2, slug, page_id}
    fmt=json: {hunks: [...], added_lines: N, removed_lines: M, ...}

    Error: {error: "..."} with status 404 or 500.
    """
    slug = (request.query_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "slug required"}, status_code=400, headers=_CORS)

    try:
        v1 = int(request.query_params.get("v1", ""))
        v2 = int(request.query_params.get("v2", ""))
    except (ValueError, TypeError):  # fmt: skip
        return JSONResponse({"error": "v1 and v2 must be integers"}, status_code=400, headers=_CORS)

    fmt = (request.query_params.get("fmt") or "unified").strip().lower()
    if fmt not in ("unified", "json"):
        fmt = "unified"

    wiki = _st._wiki
    if wiki is None:
        return JSONResponse({"error": "wiki not initialized"}, status_code=503, headers=_CORS)

    try:
        from yadgar.core.server.tools.wiki import wiki_diff  # noqa: PLC0415

        result = await asyncio.to_thread(wiki_diff, slug, v1, v2, fmt)
    except Exception as exc:
        logger.debug("api_wiki_diff error slug=%s v1=%s v2=%s: %s", slug, v1, v2, exc)
        return JSONResponse({"error": str(exc)}, status_code=500, headers=_CORS)

    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=404, headers=_CORS)

    return JSONResponse(result, headers=_CORS)


# ---------------------------------------------------------------------------
# /api/wiki_restore  — restore a historical version (confirmation-gated)
# ---------------------------------------------------------------------------


@mcp_server.custom_route("/api/wiki_restore", methods=["POST"])
@trace_span()
async def api_wiki_restore(request: Request) -> JSONResponse:
    """Restore a wiki page to a previous version (creates a new version).

    POST /api/wiki_restore
    Body: {slug: str, version: int}

    Confirmation-gated: caller must supply both slug and version. This endpoint
    is called only after the user confirms in the ConfirmModal — no separate
    confirmation token required (modal is the gate).

    Response 200: {restored: true, slug, restored_from_version, new_version}
    Response 400: {restored: false, reason}
    Response 404: {error: "..."} — slug not found
    Response 500: {error: "..."} — internal error
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"restored": False, "reason": "invalid_json"}, status_code=400, headers=_CORS
        )

    slug = (body.get("slug") or "").strip()
    if not slug:
        return JSONResponse(
            {"restored": False, "reason": "slug_required"}, status_code=400, headers=_CORS
        )

    version_raw = body.get("version")
    try:
        version = int(version_raw)
    except (ValueError, TypeError):  # fmt: skip
        return JSONResponse(
            {"restored": False, "reason": "version_must_be_int"}, status_code=400, headers=_CORS
        )

    wiki = _st._wiki
    if wiki is None:
        return JSONResponse({"error": "wiki not initialized"}, status_code=503, headers=_CORS)

    try:
        from yadgar.core.server.tools.wiki import wiki_restore  # noqa: PLC0415

        result = await asyncio.to_thread(wiki_restore, slug, version)
    except Exception as exc:
        logger.debug("api_wiki_restore error slug=%s v=%s: %s", slug, version, exc)
        return JSONResponse({"error": str(exc)}, status_code=500, headers=_CORS)

    if isinstance(result, dict) and "error" in result:
        return JSONResponse(result, status_code=404, headers=_CORS)

    # Normalise response shape for frontend
    return JSONResponse(
        {
            "restored": True,
            "slug": slug,
            "restored_from_version": result.get("restored_from_version", version),
            "new_version": result.get("new_version"),
        },
        headers=_CORS,
    )
