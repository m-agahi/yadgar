"""Trace-replay routes — viz "Traces" tab (viz-trace-replay Car B).

Endpoints (bearer-auth via _PROTECTED_PREFIXES; NOT debug-gated):
  GET /api/traces/recent       — last N tool boundary traces: {tool, total_ms, status, trace_id}
  GET /api/traces/{id}/mesh    — fixed-lane replay mesh for one trace by id

Data path: read from Tempo's query API (YADGAR_TEMPO_QUERY_URL, e.g.
http://localhost:3200). by-id fetch is fresh (~100ms); /recent uses a TraceQL
search matching any tool.* boundary span. The full span tree is flattened
(port of docs/diagrams/capture_trace.py) and fed to yadgar/_shared/trace_mesh.py
build_mesh — the pure simplify_trace aggregation.

Graceful degradation (never 500 — Tempo is an OPTIONAL observability dependency):
  - empty TEMPO_QUERY_URL (disabled)         → 200 {tempo:false, traces/mesh empty}
  - httpx ConnectError/TimeoutException/etc. → 200 empty payload + reason
  - non-200 from Tempo                       → 200 empty payload + reason
  - empty / absent trace                     → 200 empty mesh

Caching (ADR-0074, --cpus 1: no daemon hot-path work): a module-level OrderedDict
keyed by trace-id holds (mesh, expiry). Size cap 20, TTL 10 min. The mesh compute
runs on-demand only, off the daemon hot loop.

Registered as a side-effect import in yadgar/core/server/__init__.py.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar._shared.trace_mesh import build_mesh
from yadgar.core.server._app import mcp_server

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RECENT_LIMIT_DEFAULT = 10
_RECENT_LIMIT_MAX = 50
_TEMPO_TIMEOUT_S = 5.0
_SEARCH_WINDOW_S = 3600  # /recent looks back one hour by default

_MESH_CACHE_MAX = 20
_MESH_CACHE_TTL_S = 600.0  # 10 minutes

# trace-id -> (mesh_dict, expiry_epoch). Module-level; evicted on size/TTL.
_mesh_cache: OrderedDict[str, tuple[dict, float]] = OrderedDict()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _tempo_base_url() -> str:
    """Return the configured Tempo query base URL (empty = disabled)."""
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob(
        "YADGAR_TEMPO_QUERY_URL",
        "TEMPO_QUERY_URL",
        lambda s: s.strip().rstrip("/"),
        "",
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@observe(tier="hot", span=False)
def _cache_get(trace_id: str) -> dict | None:
    """Return a cached mesh if fresh, else None (and evict if expired)."""
    hit = _mesh_cache.get(trace_id)
    if hit is None:
        return None
    mesh, expiry = hit
    if time.monotonic() >= expiry:
        _mesh_cache.pop(trace_id, None)
        return None
    _mesh_cache.move_to_end(trace_id)
    return mesh


@observe(tier="hot", span=False)
def _cache_put(trace_id: str, mesh: dict) -> None:
    """Store a mesh with TTL, evicting the oldest entry past the size cap."""
    _mesh_cache[trace_id] = (mesh, time.monotonic() + _MESH_CACHE_TTL_S)
    _mesh_cache.move_to_end(trace_id)
    while len(_mesh_cache) > _MESH_CACHE_MAX:
        _mesh_cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Tempo client (httpx port of docs/diagrams/capture_trace.py)
# ---------------------------------------------------------------------------


@observe(tier="hot", span=False)
def _extract_spans(trace_json: dict) -> list[dict]:
    """Flatten a Tempo trace into rel-timed, depth-tagged, start-ordered spans."""
    spans: list[dict] = []
    for b in trace_json.get("batches", []):
        attrs = b.get("resource", {}).get("attributes", [])
        svc = next(
            (a["value"].get("stringValue", "") for a in attrs if a["key"] == "service.name"),
            "",
        )
        for ss in b.get("scopeSpans", []):
            for s in ss.get("spans", []):
                spans.append(
                    {
                        "id": s["spanId"],
                        "parent": s.get("parentSpanId", ""),
                        "name": s["name"],
                        "svc": svc,
                        "start": int(s["startTimeUnixNano"]),
                        "end": int(s["endTimeUnixNano"]),
                    }
                )
    if not spans:
        return spans
    t0 = min(s["start"] for s in spans)
    byid = {s["id"]: s for s in spans}

    for s in spans:
        s["rel_ms"] = round((s["start"] - t0) / 1e6, 2)
        s["dur_ms"] = round((s["end"] - s["start"]) / 1e6, 2)
        s["depth"] = _span_depth(s, byid)
    spans.sort(key=lambda s: s["start"])
    return [{k: s[k] for k in ("rel_ms", "dur_ms", "depth", "svc", "name")} for s in spans]


@observe(tier="hot", span=False)
def _span_depth(s: dict, byid: dict[str, dict]) -> int:
    """Depth of a span = number of parent hops (capped at 30 to bound cycles)."""
    d_, p = 0, s["parent"]
    while p and p in byid and d_ < 30:
        d_, p = d_ + 1, byid[p]["parent"]
    return d_


@observe(tier="stage")
async def _tempo_search_recent(base: str, limit: int) -> list[dict]:
    """Search Tempo for the most-recent tool.* boundary traces.

    Broader than capture_trace's exact-name search: matches any tool.* span via
    TraceQL regex. Returns [{tool, total_ms, status, trace_id}] newest-first.
    Never raises — returns [] on any Tempo failure.
    """
    now = int(time.time())
    params = {
        "q": r'{ name =~ "tool\\..*" }',
        "start": now - _SEARCH_WINDOW_S,
        "end": now + 5,
        "limit": limit,
    }
    try:
        async with httpx.AsyncClient(timeout=_TEMPO_TIMEOUT_S) as client:
            resp = await client.get(
                f"{base}/api/search",
                params=params,
                headers={"Accept": "application/json"},
            )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as exc:
        logger.warning("Tempo search failed (network): %s", exc)
        return []
    if resp.status_code != 200:
        logger.warning("Tempo search returned HTTP %s", resp.status_code)
        return []
    try:
        traces = resp.json().get("traces", [])
    except ValueError:
        return []
    traces.sort(key=lambda t: -int(t.get("startTimeUnixNano", 0)))
    out: list[dict] = []
    for t in traces[:limit]:
        out.append(
            {
                "trace_id": t.get("traceID", ""),
                "tool": _search_tool_name(t),
                "total_ms": round(float(t.get("durationMs", 0.0)), 1),
                "status": "ok",  # Tempo search does not carry per-trace error status
            }
        )
    return out


@observe(tier="hot", span=False)
def _search_tool_name(trace_hit: dict) -> str:
    """Extract the tool.* boundary name from a Tempo search result.

    The MCP trace root span is `POST /mcp` (depth 0), NOT the tool span —
    `rootTraceName` therefore reads "POST /mcp", not "tool.<name>". The tool
    name lives in the span our `name =~ "tool\\..*"` query matched, surfaced in
    the hit's `spanSet`/`spanSets[].spans[].name`. Fall back to rootTraceName
    then name only if no matched tool span is present.
    """
    span_sets = trace_hit.get("spanSets") or []
    if not span_sets and trace_hit.get("spanSet"):
        span_sets = [trace_hit["spanSet"]]
    for ss in span_sets:
        for sp in ss.get("spans", []):
            nm = sp.get("name", "")
            if nm.startswith("tool."):
                return nm
    return trace_hit.get("rootTraceName") or trace_hit.get("name") or ""


@observe(tier="stage")
async def _tempo_fetch_trace(base: str, trace_id: str) -> tuple[dict | None, str]:
    """Fetch one trace by id and flatten to a capture_trace-shaped payload.

    Returns ``(data, reason)``. ``data`` is None on any Tempo failure or empty
    trace (caller degrades / falls back); ``reason`` carries the WHY — the
    upstream HTTP status + body snippet, the network error class, or "empty
    trace" — so the UI can show why replay is empty/partial (Bug 7). Never
    raises. ``reason`` is "" on success.
    """
    try:
        async with httpx.AsyncClient(timeout=_TEMPO_TIMEOUT_S) as client:
            resp = await client.get(
                f"{base}/api/traces/{trace_id}",
                headers={"Accept": "application/json"},
            )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as exc:
        logger.warning("Tempo fetch %s failed (network): %s", trace_id, exc)
        return None, f"Tempo unreachable ({type(exc).__name__})"
    if resp.status_code != 200:
        # Surface the upstream status + a body snippet (Tempo's 500 typically
        # carries "queue doesn't have room for ~N jobs" on blocklist exhaustion).
        snippet = _body_snippet(resp)
        logger.warning("Tempo fetch %s returned HTTP %s", trace_id, resp.status_code)
        return None, f"Tempo returned HTTP {resp.status_code}{snippet}"
    try:
        spans = _extract_spans(resp.json())
    except ValueError:
        return None, "Tempo returned a malformed trace"
    if not spans:
        return None, "empty trace (no spans)"

    total_ms = round(max(s["rel_ms"] + s["dur_ms"] for s in spans), 1)
    tool_span = next((s["name"] for s in spans if s["name"].startswith("tool.")), "")
    label = tool_span.removeprefix("tool.") if tool_span else trace_id[:8]
    return {
        "label": label,
        "tool_span": tool_span,
        "trace_id": trace_id,
        "total_ms": total_ms,
        "span_count": len(spans),
        "spans": spans,
    }, ""


@observe(tier="hot", span=False)
def _body_snippet(resp: object, limit: int = 160) -> str:
    """A short, safe ' — <body>' snippet from a Tempo error response, or ''.

    Best-effort: any access/decoding failure yields ''. Bounded to `limit` chars.
    """
    try:
        text = getattr(resp, "text", "") or ""
        if not text:
            payload = resp.json()  # type: ignore[attr-defined]
            text = payload if isinstance(payload, str) else str(payload)
    except Exception:  # noqa: BLE001 — reason surfacing is best-effort
        return ""
    text = " ".join(str(text).split())  # collapse whitespace
    if not text:
        return ""
    return f" — {text[:limit]}"


@observe(tier="stage")
async def _tempo_fallback_mesh(base: str, trace_id: str) -> dict | None:
    """Build a minimal capture from the /api/search spanSet when by-id fails.

    Reuses the SAME proven TraceQL as ``_tempo_search_recent`` — ``{ name =~
    "tool\\..*" }`` (the query that already populates the clickable sidebar) —
    then filters to ``trace_id`` client-side. A hand-written ``trace:id`` query
    would be unverifiable while Tempo is down; any trace clickable in the sidebar
    is a tool.* trace in the search window, so it is findable here.

    Tempo's search returns the matched tool.* span (and any sibling spans) with
    per-span ``startTimeUnixNano`` + ``durationNanos`` in the hit's
    ``spanSet``/``spanSets`` — thinner than the full by-id trace, but it lets
    replay degrade to a PARTIAL timeline instead of 0 stages (Bug 7). Returns a
    capture_trace-shaped dict or None (no usable spans / any failure). Never raises.
    """
    try:
        now = int(time.time())
        params = {
            "q": r'{ name =~ "tool\\..*" }',  # proven query (mirrors _tempo_search_recent)
            "start": now - _SEARCH_WINDOW_S,
            "end": now + 5,
            "limit": _RECENT_LIMIT_MAX,
        }
        async with httpx.AsyncClient(timeout=_TEMPO_TIMEOUT_S) as client:
            resp = await client.get(
                f"{base}/api/search",
                params=params,
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            return None
        traces = resp.json().get("traces", [])
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError):  # fmt: off
        return None  # fmt: on
    except ValueError:
        return None
    except Exception:  # noqa: BLE001 — fallback must never raise (graceful degrade)
        return None

    hit = next((t for t in traces if t.get("traceID") == trace_id), None)
    if not hit:
        return None
    return _spanset_to_capture(hit, trace_id)


@observe(tier="hot", span=False)
def _ns_int(v: object) -> int:
    """Coerce a Tempo nanosecond field to int; 0 on missing/non-numeric."""
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):  # fmt: skip
        return 0


@observe(tier="hot", span=False)
def _partial_span_entry(s: dict, t0: int) -> dict:
    """One flat spanSet span → a capture-shaped span dict (rel-timed against t0).

    The search spanSet is flat (no parent hierarchy): the tool.* boundary is made
    the depth-0 root and every other span its depth-1 child so build_tree reparents
    them under the tool span (else they become siblings and select_stages sees 0).
    """
    start = _ns_int(s.get("startTimeUnixNano"))
    dur = _ns_int(s.get("durationNanos"))
    rel_ms = round((start - t0) / 1e6, 2) if (t0 and start) else 0.0
    name = s.get("name", "")
    return {
        "rel_ms": max(0.0, rel_ms),
        "dur_ms": round(dur / 1e6, 2),
        "depth": 0 if name.startswith("tool.") else 1,
        "svc": "yadgar-core",
        "name": name,
    }


@observe(tier="stage")
def _spanset_to_capture(hit: dict, trace_id: str) -> dict | None:
    """Convert a Tempo search hit's spanSet into a capture_trace-shaped payload.

    Each spanSet span carries ``name`` and (optionally) ``startTimeUnixNano`` +
    ``durationNanos``. We rel-time against the earliest span start; spans with no
    timing collapse to rel_ms=0/dur_ms=0 so a name-only spanSet still yields a
    (flat) partial timeline. Returns None when no spans are present.
    """
    span_sets = hit.get("spanSets") or []
    if not span_sets and hit.get("spanSet"):
        span_sets = [hit["spanSet"]]
    raw: list[dict] = []
    for ss in span_sets:
        raw.extend(ss.get("spans", []))
    if not raw:
        return None

    t0 = min((v for v in (_ns_int(s.get("startTimeUnixNano")) for s in raw) if v > 0), default=0)
    spans: list[dict] = [_partial_span_entry(s, t0) for s in raw]
    # build_tree consumes spans in list order (start-ordered): the depth-0 tool
    # span must precede its depth-1 children. Sort by start so it does.
    spans.sort(key=lambda s: (s["rel_ms"], 0 if s["name"].startswith("tool.") else 1))
    total_ms = round(max((s["rel_ms"] + s["dur_ms"] for s in spans), default=0.0), 1)
    if not total_ms:
        # search reported a duration even when per-span timing is absent
        total_ms = round(float(hit.get("durationMs", 0.0)), 1)
    tool_span = next((s["name"] for s in spans if s["name"].startswith("tool.")), "")
    label = tool_span.removeprefix("tool.") if tool_span else trace_id[:8]
    return {
        "label": label,
        "tool_span": tool_span,
        "trace_id": trace_id,
        "total_ms": total_ms,
        "span_count": len(spans),
        "spans": spans,
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@observe(tier="boundary")
async def traces_recent_handler(request: Request) -> JSONResponse:
    """GET /api/traces/recent?limit=N — recent tool boundary traces."""
    try:
        limit = int(request.query_params.get("limit", str(_RECENT_LIMIT_DEFAULT)))
    except (ValueError, TypeError):  # fmt: skip
        limit = _RECENT_LIMIT_DEFAULT
    limit = max(1, min(_RECENT_LIMIT_MAX, limit))

    base = _tempo_base_url()
    if not base:
        return JSONResponse({"tempo": False, "traces": [], "reason": "TEMPO_QUERY_URL not set"})

    traces = await _tempo_search_recent(base, limit)
    return JSONResponse({"tempo": True, "traces": traces})


@observe(tier="boundary")
async def trace_mesh_handler(request: Request) -> JSONResponse:
    """GET /api/traces/{trace_id}/mesh — fixed-lane replay mesh for one trace."""
    trace_id = request.path_params.get("trace_id", "")
    if not trace_id:
        return JSONResponse({"error": "missing trace_id"}, status_code=400)

    base = _tempo_base_url()
    if not base:
        return JSONResponse(
            {"tempo": False, "mesh": _empty_mesh(trace_id), "reason": "TEMPO_QUERY_URL not set"}
        )

    cached = _cache_get(trace_id)
    if cached is not None:
        return JSONResponse({"tempo": True, "mesh": cached, "cached": True})

    data, reason = await _tempo_fetch_trace(base, trace_id)
    if data is not None:
        mesh = build_mesh(data)
        _cache_put(trace_id, mesh)
        return JSONResponse({"tempo": True, "mesh": mesh, "cached": False})

    # Bug 7: by-id fetch failed (e.g. Tempo querier 500 on blocklist exhaustion).
    # Degrade to a PARTIAL mesh from the /api/search spanSet instead of 0 stages,
    # and surface WHY the full replay is unavailable so the UI can show it.
    fallback = await _tempo_fallback_mesh(base, trace_id)
    if fallback is not None:
        mesh = build_mesh(fallback)
        # do NOT cache the partial fallback — a later by-id success should win.
        return JSONResponse(
            {
                "tempo": True,
                "mesh": mesh,
                "cached": False,
                "partial": True,
                "reason": reason or "trace unavailable",
            }
        )

    # graceful: Tempo down, non-200, or empty/absent trace → empty mesh, 200.
    return JSONResponse(
        {"tempo": True, "mesh": _empty_mesh(trace_id), "reason": reason or "trace unavailable"}
    )


@observe(tier="hot", span=False)
def _empty_mesh(trace_id: str) -> dict:
    """A typed-empty mesh payload (graceful-degrade shape)."""
    return {
        "nodes": [],
        "edges": [],
        "timeline_ms": 0.0,
        "tool": "",
        "dropped_boundary": False,
        "trace_id": trace_id,
        "label": "",
    }


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


@mcp_server.custom_route("/api/traces/recent", methods=["GET"])
@trace_span()
async def traces_recent(request: Request) -> JSONResponse:
    """Recent tool boundary traces for the viz Traces sidebar."""
    return await traces_recent_handler(request)


@mcp_server.custom_route("/api/traces/{trace_id}/mesh", methods=["GET"])
@trace_span()
async def trace_mesh_route(request: Request) -> JSONResponse:
    """Fixed-lane replay mesh for one trace by id."""
    return await trace_mesh_handler(request)
