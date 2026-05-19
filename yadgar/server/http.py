"""Bare HTTP routes: health, metrics, hooks, graph/viz API, SSE stream.

All @mcp_server.custom_route decorators live here — they fire at import time,
so this module must be imported in server/__init__.py.

# Module size justified: single-responsibility HTTP route registry. Every
# function is a @mcp_server.custom_route handler — routes register as
# side-effects at import time. Splitting into sub-modules (hooks, graph, SSE)
# would require server/__init__.py to import each sub-module explicitly, and
# any missed import would silently drop routes. Keeping all routes in one file
# provides a complete, auditable list of HTTP endpoints and prevents that class
# of registration bugs. The file has no domain logic — all work is delegated to
# _state singletons and domain modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

import yadgar.server._state as _st
from yadgar import __version__
from yadgar.graph_api import GraphAPI
from yadgar.sanitize import sanitize_log_field
from yadgar.server._app import mcp_server
from yadgar.server._helpers import _bounded_set, _build_dlq_alert_text  # noqa: F401

logger = logging.getLogger(__name__)

_CORS = {"Cache-Control": "no-cache"}


# ── Custom HTTP Endpoints ─────────────────────────────────────────────


@mcp_server.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint."""
    import httpx

    session_count = 0
    if mcp_server._session_manager is not None:
        session_count = len(mcp_server._session_manager._server_instances)

    db_url = os.environ.get("YADGAR_DB_URL")
    embed_url = os.environ.get("YADGAR_EMBED_URL")

    db_ok = None
    embed_ok = None

    # §9 Q5: Use async httpx client to avoid blocking the event loop.
    async with httpx.AsyncClient(timeout=2.0) as _aclient:
        if db_url:
            try:
                r = await _aclient.get(f"{db_url}/health")
                db_ok = r.status_code == 200
            except Exception:
                db_ok = False

        if embed_url:
            try:
                r = await _aclient.get(f"{embed_url}/health")
                embed_ok = r.status_code == 200
            except Exception:
                embed_ok = False

    payload: dict = {
        "status": "ok",
        "version": __version__,
        "transport": _st._active_transport,
        "uptime_seconds": round(time.time() - _st._start_time, 1) if _st._start_time else 0,
        "active_sessions": session_count,
    }
    if db_ok is not None:
        payload["db"] = db_ok
    if embed_ok is not None:
        payload["embed"] = embed_ok
    if db_ok is False or embed_ok is False:
        payload["status"] = "degraded"

    return JSONResponse(payload)


@mcp_server.custom_route("/metrics", methods=["GET"])
async def metrics_endpoint(request: Request):
    """Prometheus metrics endpoint (§15).

    Exempt from bearer-token auth (loopback Prometheus scrapers don't carry tokens).
    Returns 404 when YADGAR_METRICS_ENABLED=False / 0.
    """
    from yadgar.metrics import metrics_handler

    return await metrics_handler(request)


@mcp_server.custom_route("/hooks/pre-compact", methods=["POST"])
async def hook_pre_compact(request: Request) -> JSONResponse:
    """Called by PreCompact hook before context compaction."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    directory = body.get("cwd", os.getcwd())
    replay = _st._replay
    if replay is None:
        return JSONResponse(
            {"status": "error", "message": "Replay engine not initialized"}, status_code=503
        )

    result = replay.pre_compact_drain(directory)

    # Also trigger consolidation
    if _st._consolidation is not None:
        try:
            _st._consolidation.force_consolidate()
        except Exception:
            logger.debug("Emergency consolidation failed during pre-compact")

    return JSONResponse(result)


@mcp_server.custom_route("/hooks/post-compact", methods=["GET"])
async def hook_post_compact(request: Request) -> JSONResponse:
    """Called by SessionStart hook after compaction. Returns restoration context."""
    directory = request.query_params.get("directory", os.getcwd())
    replay = _st._replay
    if replay is None:
        return JSONResponse(
            {"status": "error", "message": "Replay engine not initialized"}, status_code=503
        )

    result = replay.restore(directory)
    return JSONResponse(result)


@mcp_server.custom_route("/hooks/auto-capture", methods=["POST"])
async def hook_auto_capture(request: Request) -> JSONResponse:
    """Capture a tool action from PostToolUse hook (HTTP transport).

    Accepts JSON: {tool_name, summary, directory, session_id}
    Writes directly to action_log table — no write gate, no embeddings.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)

    storage = _st._storage
    if storage is None:
        return JSONResponse(
            {"status": "error", "message": "Storage not initialized"}, status_code=503
        )

    from datetime import datetime

    tool_name = sanitize_log_field(body.get("tool_name", "unknown"), max_len=200)

    # §7: per-directory rate limit before any further processing
    _raw_dir = body.get("directory", "")
    _dir_key = sanitize_log_field(_raw_dir, max_len=500) if _raw_dir else ""
    if not _st._auto_capture_limiter.allow(_dir_key or "_default"):
        return JSONResponse({"status": "rate_limited"}, status_code=429)

    # Skip self-referential Yadgar tools
    for prefix in _st._SKIP_TOOL_PREFIXES:
        if tool_name.startswith(prefix):
            return JSONResponse({"status": "skipped", "reason": "yadgar_tool"})

    # Only capture state-modifying tools
    if tool_name not in _st._CAPTURE_TOOLS:
        return JSONResponse({"status": "skipped", "reason": "read_only_tool"})

    session_id = sanitize_log_field(body.get("session_id", "default"), max_len=100)
    action = {
        "tool_name": tool_name,
        "summary": sanitize_log_field(body.get("summary", ""), max_len=500),
        "directory": _dir_key,
        "session_id": session_id,
    }

    # §9 Q2: Protect _action_batch under asyncio.Lock to prevent data races.
    # §9 Q1: Wrap blocking storage call in asyncio.to_thread.
    async with _st._action_batch_lock:
        if session_id not in _st._action_batch:
            _bounded_set(_st._action_batch, session_id, [])
        batch = _st._action_batch[session_id]
        batch.append(action)
        if len(batch) < 5:
            return JSONResponse({"status": "batched", "pending": len(batch)})

        # Flush batch → one combined action_log entry.
        # Swap under the lock so concurrent appends go to the new list.
        to_flush = list(batch)
        _st._action_batch[session_id] = []

    combined_tools = ",".join(a["tool_name"] for a in to_flush)
    combined_summary = " | ".join(a["summary"] for a in to_flush if a["summary"])
    directory = to_flush[-1]["directory"]
    from datetime import UTC

    ts = datetime.now(UTC).isoformat()

    await asyncio.to_thread(
        storage.insert_action_log,
        tool_name=f"batch[{combined_tools}]",
        tool_input_summary=combined_summary[:500],
        directory=directory,
        session_id=session_id,
        timestamp=ts,
    )

    if _st._consolidation is not None:
        _st._consolidation.record_activity()

    return JSONResponse({"status": "captured", "batch_size": 5})


@mcp_server.custom_route("/hooks/session-context", methods=["GET"])
async def hook_session_context(request: Request) -> JSONResponse:
    """Return project_brief markdown for session-start hook (§28 pipe).

    Calls project_brief(directory, mode="catalog") and pipes the _render
    markdown field to the hook's stdin. All curation lives server-side.

    Query params:
        directory: project directory (optional, defaults to cwd)
        mode: brief mode (optional, defaults to "catalog")
        branch: host-side git branch hint (optional, v5.1.9 F2); passed to
            project_brief as branch_hint= so the container doesn't need git
            access.
    Returns: {"text": "...markdown..."}
    """
    directory = request.query_params.get("directory", os.getcwd())
    mode = request.query_params.get("mode", "catalog")
    branch_hint = request.query_params.get("branch", "") or None

    # Record timestamp for prompt-recall throttling (bounded dict)
    _bounded_set(_st._last_session_context, directory, time.monotonic())

    try:
        # Look up via yadgar.server so patch.object(srv, "project_brief", ...) takes effect
        import sys as _sys  # noqa: PLC0415

        _srv = _sys.modules.get("yadgar.server")
        _pb = getattr(_srv, "project_brief", None) if _srv else None
        if _pb is None:
            from yadgar.server.tools.project import project_brief as _pb  # noqa: PLC0415
        brief = _pb(directory, mode=mode, branch_hint=branch_hint)
        render = brief.get("_render", "")
        return JSONResponse({"text": render})
    except Exception as _e:
        logger.debug("session-context hook error: %s", _e)
        return JSONResponse({"text": ""})


@mcp_server.custom_route("/hooks/prompt-recall", methods=["GET"])
async def hook_prompt_recall(request: Request) -> JSONResponse:
    """Return auto-recall markdown for UserPromptSubmit hook (daemon mode).

    Query params: query, directory (optional)
    Returns: {"text": "...markdown..."}
    """
    query = request.query_params.get("query", "")
    directory = request.query_params.get("directory", os.getcwd())

    if not query or len(query) < 2:
        return JSONResponse({"text": ""})

    # Throttle: skip if session-context ran < 3 min ago (already loaded context)
    now = time.monotonic()
    if now - _st._last_session_context.get(directory, 0) < 180:
        return JSONResponse({"text": "", "skipped": "session_context_recent"})
    # Throttle: max 1 recall per 2 minutes per directory
    if now - _st._last_prompt_recall.get(directory, 0) < 120:
        return JSONResponse({"text": "", "skipped": "rate_limited"})

    retriever = _st._retriever
    if retriever is None:
        return JSONResponse({"text": ""})

    try:
        import asyncio

        results = await asyncio.to_thread(retriever.recall, query, max_results=5, min_heat=0.0)
    except Exception as e:
        logger.debug("prompt-recall hook error: %s", e)
        return JSONResponse({"text": ""})

    if not results:
        return JSONResponse({"text": ""})

    max_chars = 3000
    lines = ["# Yadgar — Auto-Recall\n"]
    total_chars = 0
    for m in results:
        content = m.get("content", "")
        if total_chars + len(content) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 50:
                content = content[:remaining] + "..."
            else:
                break
        mem_dir = m.get("directory_context", "")
        proj = f" [{Path(mem_dir).name}]" if mem_dir and mem_dir != directory else ""
        lines.append(f"- {content}{proj}")
        total_chars += len(content)
    lines.append(f"\n*{len(results)} memories surfaced for: {directory}*")

    # Prepend DLQ alerts if any items are stuck
    dlq_text = _build_dlq_alert_text()
    if dlq_text:
        lines = [dlq_text, ""] + lines

    _bounded_set(_st._last_prompt_recall, directory, time.monotonic())
    return JSONResponse({"text": "\n".join(lines)})


@mcp_server.custom_route("/hooks/subagent-stop", methods=["POST"])
async def hook_subagent_stop(request: Request) -> JSONResponse:
    """SubagentStop hook endpoint — memorize Yadgar findings from subagent reports.

    Called by yadgar/hooks/subagent-stop.py when a Claude Code subagent completes.

    Accepts JSON body:
        {
            "agent_type": "general-purpose",
            "cwd": "/path/to/project",
            "findings": ["bullet text 1", "bullet text 2", ...]
        }

    Each finding is stored as a memory with:
        - provenance_agent = agent_type
        - tags = ["from-subagent", "agent-type:<agent_type>"]
        - context = cwd
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)

    agent_type = sanitize_log_field(str(body.get("agent_type", "general-purpose")), max_len=64)
    cwd = sanitize_log_field(str(body.get("cwd", os.getcwd())), max_len=500)
    findings = body.get("findings", [])

    if not isinstance(findings, list):
        return JSONResponse(
            {"status": "error", "message": "findings must be a list"}, status_code=400
        )

    # Validate agent_type before use as provenance_agent
    import re as _re

    _AGENT_TYPE_RE = _re.compile(r"^[A-Za-z0-9_-]{1,64}$")
    if not agent_type or not _AGENT_TYPE_RE.match(agent_type):
        agent_type = "general-purpose"

    if not findings:
        return JSONResponse({"status": "ok", "stored": 0})

    # Import memorize at call time to avoid circular import at module load
    import sys as _sys

    _srv = _sys.modules.get("yadgar.server")
    _memorize = getattr(_srv, "memorize", None) if _srv else None
    if _memorize is None:
        from yadgar.server.tools.memorize import memorize as _memorize  # noqa: PLC0415

    tags = ["from-subagent", f"agent-type:{agent_type}"]
    stored = 0
    errors = []

    for finding in findings:
        if not isinstance(finding, str) or not finding.strip():
            continue
        finding_clean = sanitize_log_field(finding.strip(), max_len=32_768)
        if not finding_clean:
            continue
        try:
            result = await asyncio.to_thread(
                _memorize,
                content=finding_clean,
                context=cwd,
                tags=tags,
                is_protected=False,
                provenance_agent=agent_type,
            )
            if result.get("stored", True):  # queued=True counts as stored
                stored += 1
        except Exception as _e:
            logger.debug("subagent-stop memorize failed: %s", _e)
            errors.append(str(_e)[:100])

    response: dict = {"status": "ok", "stored": stored, "agent_type": agent_type}
    if errors:
        response["errors"] = errors
    return JSONResponse(response)


@mcp_server.custom_route("/hooks/instructions-loaded", methods=["GET"])
async def hook_instructions_loaded(request: Request) -> JSONResponse:
    """InstructionsLoaded hook endpoint — inject recalled context on CLAUDE.md load.

    Called by yadgar/hooks/instructions-loaded.py when Claude Code loads a
    CLAUDE.md file at session_start or compact. Returns a lightweight recall
    (~3 results) derived from the filename and load_reason.

    Query params:
        file_path:   path of the loaded instructions file
        load_reason: "session_start" | "compact"
    Returns: {"text": "<markdown to inject>"}
    """
    file_path = request.query_params.get("file_path", "")
    load_reason = request.query_params.get("load_reason", "")

    retriever = _st._retriever
    if retriever is None:
        return JSONResponse({"text": ""})

    # Build a query from the filename + load_reason for relevant memories
    import pathlib as _pathlib

    filename = _pathlib.Path(file_path).name if file_path else "CLAUDE.md"
    query = f"{filename} {load_reason} instructions context".strip()

    try:
        results = await asyncio.to_thread(retriever.recall, query, max_results=3, min_heat=0.0)
    except Exception as _e:
        logger.debug("instructions-loaded hook recall error: %s", _e)
        return JSONResponse({"text": ""})

    if not results:
        return JSONResponse({"text": ""})

    max_chars = 2000
    lines = ["# Yadgar — Instructions Context\n"]
    total_chars = 0
    for m in results:
        content = m.get("content", "")
        if total_chars + len(content) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 50:
                content = content[:remaining] + "..."
            else:
                break
        lines.append(f"- {content}")
        total_chars += len(content)

    return JSONResponse({"text": "\n".join(lines)})


@mcp_server.custom_route("/hooks/subagent-start", methods=["POST"])
async def hook_subagent_start(request: Request) -> JSONResponse:
    """SubagentStart hook endpoint — inject recalled context into subagent.

    Called by yadgar/hooks/subagent-start.py when Claude Code starts a subagent.
    Reads agent_type + cwd from query params and task description from body.
    Calls recall(task_description) and returns relevant memories + anchors to
    inject into the subagent's context at dispatch time.

    This reduces orchestrator burden: the main thread need not prepend context
    manually; the hook injects it automatically.

    Query params:
        agent_type: "general-purpose" | "Explore" | ...
        cwd:        project directory
    Body (JSON):
        {
            "description": "task description",
            "cwd": "/path/to/project"   (fallback if query param absent)
        }
    Returns: {"text": "<markdown to inject>"}
    """
    agent_type = sanitize_log_field(
        request.query_params.get("agent_type", "general-purpose"), max_len=64
    )
    cwd = sanitize_log_field(request.query_params.get("cwd", os.getcwd()), max_len=500)

    try:
        body = await request.json()
    except Exception:
        body = {}

    description = sanitize_log_field(str(body.get("description", "")), max_len=2000)
    if not cwd:
        cwd = sanitize_log_field(str(body.get("cwd", os.getcwd())), max_len=500)

    retriever = _st._retriever
    if retriever is None:
        return JSONResponse({"text": ""})

    # Use description as primary query; fall back to agent_type if empty
    query = description.strip() or f"agent {agent_type}"

    try:
        results = await asyncio.to_thread(retriever.recall, query, max_results=5, min_heat=0.0)
    except Exception as _e:
        logger.debug("subagent-start hook recall error: %s", _e)
        return JSONResponse({"text": ""})

    if not results:
        return JSONResponse({"text": ""})

    max_chars = 3000
    lines = [f"# Yadgar — Subagent Context [{agent_type}]\n"]
    total_chars = 0
    for m in results:
        content = m.get("content", "")
        if total_chars + len(content) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 50:
                content = content[:remaining] + "..."
            else:
                break
        mem_dir = m.get("directory_context", "")
        import pathlib as _pl

        proj = f" [{_pl.Path(mem_dir).name}]" if mem_dir and mem_dir != cwd else ""
        lines.append(f"- {content}{proj}")
        total_chars += len(content)

    return JSONResponse({"text": "\n".join(lines)})


# ── Graph / Visualization API ──────────────────────────────────────────────


@mcp_server.custom_route("/api/graph", methods=["GET"])
async def api_graph(request: Request) -> JSONResponse:
    """Return full knowledge graph (nodes + edges) for visualization."""
    if _st._storage is None:
        return JSONResponse({"nodes": [], "edges": []}, status_code=503)
    try:
        max_mem = int(request.query_params.get("max_memories", 500))
    except (ValueError, TypeError) as _e:
        max_mem = 500
    try:
        top_k = int(request.query_params.get("top_k", 8))
    except (ValueError, TypeError) as _e:
        top_k = 8
    data = await asyncio.to_thread(GraphAPI(_st._storage).get_full_graph, max_mem, top_k)
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request) -> JSONResponse:
    """Return memory statistics as JSON (used by `yadgar stats` CLI when daemon is running)."""
    if _st._storage is None:
        return JSONResponse({}, status_code=503)
    project = request.query_params.get("project")
    data = await asyncio.to_thread(_st._storage.get_memory_stats)
    if project:
        data["project_filter"] = project
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/graph/stats", methods=["GET"])
async def api_graph_stats(request: Request) -> JSONResponse:
    """Return graph statistics: counts + top entities by heat."""
    if _st._storage is None:
        return JSONResponse({}, status_code=503)
    data = await asyncio.to_thread(GraphAPI(_st._storage).get_graph_stats)
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/graph/neighborhood/{node_id}", methods=["GET"])
async def api_graph_neighborhood(request: Request) -> JSONResponse:
    """Return 1–2 hop subgraph around a node."""
    if _st._storage is None:
        return JSONResponse({"nodes": [], "edges": []}, status_code=503)
    node_id = request.path_params.get("node_id", "")
    try:
        hops = int(request.query_params.get("hops", 2))
    except (ValueError, TypeError) as _e:
        hops = 2
    data = await asyncio.to_thread(GraphAPI(_st._storage).get_neighborhood, node_id, hops)
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/system", methods=["GET"])
async def api_system(request: Request) -> JSONResponse:
    """Return current system and process metrics."""
    # §9 Q6: snapshot under lock before serialising to avoid torn reads.
    with _st._metrics_lock:
        snapshot = dict(_st._system_metrics_cache)
    return JSONResponse(snapshot, headers=_CORS)


@mcp_server.custom_route("/api/metrics/heat-histogram", methods=["GET"])
async def api_heat_histogram(request: Request) -> JSONResponse:
    """Return heat distribution bucketed into N bins."""
    if _st._storage is None:
        return JSONResponse({"buckets": [], "total": 0}, status_code=503)
    try:
        n_bins = max(1, min(50, int(request.query_params.get("bins", 10))))
    except (ValueError, TypeError) as _e:
        n_bins = 10

    def _compute() -> dict:
        rows = _st._storage._q("SELECT heat FROM memory") or []
        heats = [float(r.get("heat") or 0) for r in rows]
        step = 1.0 / n_bins
        counts = [0] * n_bins
        for h in heats:
            counts[min(int(h / step), n_bins - 1)] += 1
        return {
            "buckets": [
                {"min": round(i * step, 3), "max": round((i + 1) * step, 3), "count": counts[i]}
                for i in range(n_bins)
            ],
            "total": len(heats),
        }

    data = await asyncio.to_thread(_compute)
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/metrics/consolidation-log", methods=["GET"])
async def api_consolidation_log(request: Request) -> JSONResponse:
    """Return last N consolidation cycle records (oldest first)."""
    if _st._storage is None:
        return JSONResponse([], status_code=503)
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", 30))))
    except (ValueError, TypeError) as _e:
        limit = 30

    def _fetch() -> list:
        rows = (
            _st._storage._q(
                "SELECT timestamp, memories_added, memories_updated, "
                "memories_archived, memories_deleted, duration_ms "
                "FROM consolidation_log ORDER BY timestamp ASC LIMIT $lim",
                {"lim": limit},
            )
            or []
        )
        return [
            {
                "timestamp": str(r.get("timestamp") or ""),
                "added": int(r.get("memories_added") or 0),
                "updated": int(r.get("memories_updated") or 0),
                "archived": int(r.get("memories_archived") or 0),
                "deleted": int(r.get("memories_deleted") or 0),
                "duration_ms": int(r.get("duration_ms") or 0),
            }
            for r in rows
        ]

    data = await asyncio.to_thread(_fetch)
    return JSONResponse(data, headers=_CORS)


async def _make_event_stream(request: Request):
    """Async generator for one SSE client connection.

    Checks client disconnect at the top of every loop iteration and exits
    cleanly — no data is sent to an already-disconnected socket, so the
    asyncio transport never reaches ``socket.send()`` on a closed fd.

    Any transport-level write error that does slip through is caught here
    (``ConnectionResetError``, ``BrokenPipeError``, ``OSError``) and logged
    at DEBUG with the client id.  We do *not* re-raise: the generator simply
    returns, letting ``StreamingResponse`` close the connection quietly.
    This prevents the cascade of 74 ``socket.send() raised exception``
    entries observed in the journal at 2026-05-13 23:18 when many viz-UI
    tabs disconnected simultaneously.
    """
    try:
        last_seq = int(request.query_params.get("since", 0))
    except (ValueError, TypeError) as _e:
        last_seq = 0

    last_sys_push = 0.0
    client_id = id(request)

    while True:
        # Exit cleanly if the client disconnected before we yield anything.
        if await request.is_disconnected():
            logger.debug("SSE client %s disconnected; closing stream", client_id)
            return

        now = time.time()
        try:
            # Drain new graph events
            new_events = [e for e in _st._event_queue if e["seq"] > last_seq]
            for e in new_events:
                last_seq = e["seq"]
                yield f"data: {json.dumps(e)}\n\n"
            # Push system metrics every 5 s — snapshot under lock.
            if now - last_sys_push >= 5.0 and _st._system_metrics_cache:
                last_sys_push = now
                with _st._metrics_lock:
                    _metrics_snap = dict(_st._system_metrics_cache)
                payload = json.dumps({"event": "system_metrics", "data": _metrics_snap})
                yield f"data: {payload}\n\n"
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            # Transport write failed — client dropped between the disconnect
            # check and the actual socket write.  Log once at DEBUG and stop.
            logger.debug(
                "SSE client %s send error (%s: %s); dropping connection",
                client_id,
                type(exc).__name__,
                exc,
            )
            return

        await asyncio.sleep(0.5)


@mcp_server.custom_route("/api/graph/events", methods=["GET"])
async def api_graph_events(request: Request) -> StreamingResponse:
    """SSE stream of incremental graph update events + system metrics every 5s."""
    headers = {**_CORS, "Content-Type": "text/event-stream", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        _make_event_stream(request), media_type="text/event-stream", headers=headers
    )


@mcp_server.custom_route("/graph", methods=["GET"])
async def graph_view(request: Request) -> FileResponse:
    """3D memory force graph visualization."""
    static_dir = Path(__file__).parent.parent / "static"
    return FileResponse(static_dir / "graph.html")
