"""Yadgar MCP server — supports SSE and Streamable HTTP transports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from collections import deque
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from yadgar import __version__
from yadgar.astrocyte_pool import AstrocytePool
from yadgar.causal_discovery import CausalDiscovery
from yadgar.cls_store import DualStoreCLS
from yadgar.cognitive_map import CognitiveMap
from yadgar.config import get_settings
from yadgar.consolidation import ConsolidationScheduler
from yadgar.curation import MemoryCurator
from yadgar.embeddings import EmbeddingEngine
from yadgar.engram import EngramAllocator
from yadgar.file_queue import is_draining
from yadgar.graph_api import GraphAPI
from yadgar.knowledge_graph import KnowledgeGraph
from yadgar.metacognition import MetaCognition
from yadgar.narrative import NarrativeEngine
from yadgar.predictive_coding import WriteGate
from yadgar.prospective import ProspectiveMemoryEngine

# SurrealDB is the sole storage backend (StorageEngine in storage.py)
from yadgar.restoration import CheckpointRestore
from yadgar.retrieval import Retriever
from yadgar.rules_engine import RulesEngine
from yadgar.secrets import check_secrets
from yadgar.sensory_buffer import ActionLogger
from yadgar.sleep_compute import SleepComputeEngine
from yadgar.staleness import StalenessDetector
from yadgar.storage import StorageEngine
from yadgar.thermodynamics import MemoryThermodynamics
from yadgar.wiki import WikiStore

if TYPE_CHECKING:
    from yadgar.file_queue import FileQueue, QueueDrainer

logger = logging.getLogger(__name__)

# Strong decision patterns for auto-protection
_DECISION_STRONG_RE = re.compile(
    r"\b(chose .+ over|decided to use|switched from .+ to|migrated from|"
    r"will use .+ instead|going with|opted for|selected .+ because|"
    r"choosing .+ approach|picking .+ strategy)\b",
    re.IGNORECASE,
)

# Global instances — initialized in main()
_storage: StorageEngine | None = None
_embeddings: EmbeddingEngine | None = None
_buffer: ActionLogger | None = None
_consolidation: ConsolidationScheduler | None = None
_staleness: StalenessDetector | None = None
_thermo: MemoryThermodynamics | None = None
_retriever: Retriever | None = None
_curator: MemoryCurator | None = None
_prospective: ProspectiveMemoryEngine | None = None
_narrative: NarrativeEngine | None = None
_sleep: SleepComputeEngine | None = None
_pool: AstrocytePool | None = None
_kg: KnowledgeGraph | None = None
_write_gate: WriteGate | None = None
_engram: EngramAllocator | None = None
_rules_engine: RulesEngine | None = None
_cls: DualStoreCLS | None = None
_cognitive_map: CognitiveMap | None = None
_causal: CausalDiscovery | None = None
_metacognition: MetaCognition | None = None
_replay: CheckpointRestore | None = None
_wiki: WikiStore | None = None
_file_queue: FileQueue | None = None
_queue_drainer: QueueDrainer | None = None
_queue_lock = threading.Lock()
_event_lock = threading.Lock()

# ── Hook state ─────────────────────────────────────────────────────────────
# Only capture state-modifying tool calls (skip Read, Glob, Grep, etc.)
_CAPTURE_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "Bash", "NotebookEdit", "Agent"})
# Tool name prefixes that are self-referential — never capture
_SKIP_TOOL_PREFIXES: tuple[str, ...] = (
    "mcp__yadgar__",
    "mcp__plugin_claude-code-home-manager_yadgar__",
    "mcp__plugin_oh-my-claudecode_t__",
)
# Per-table content fields used by check_invariants and memory_stats for size estimates.
# Maps table name → content field (or None for row-count-only tables).
_PER_TABLE_FIELDS: dict[str, str | None] = {
    "memory": "content",
    "wiki_page": "content",
    "episode": "raw_content",
    "action_log": None,
    "entity": None,
}
# Batch buffer: session_id → list of pending action dicts (flush at 5)
_action_batch: dict[str, list] = {}


def _q_with_timeout(
    storage, surql: str, params: dict | None = None, timeout_seconds: int = 60
) -> list:  # noqa: E501
    """Run a storage query with an optional per-request timeout.

    In server (httpx) mode the httpx Client timeout is temporarily widened to
    *timeout_seconds*.  In embedded mode _q handles its own retry.  Always routes
    through storage._q so test stubs patching _q remain effective.
    """
    http = getattr(storage, "_http", None)
    if http is not None:
        try:
            import httpx as _httpx
        except ImportError:
            return storage._q(surql, params)
        old_timeout = http.timeout
        try:
            http.timeout = _httpx.Timeout(float(timeout_seconds))
            return storage._q(surql, params)
        finally:
            http.timeout = old_timeout
    return storage._q(surql, params)


# Throttle timestamps: directory → monotonic time
_last_session_context: dict[str, float] = {}
_last_prompt_recall: dict[str, float] = {}

# ── Visualization event queue ──────────────────────────────────────────────
# Ring buffer of the last 500 events; SSE clients poll with a sequence cursor.
_event_queue: deque = deque(maxlen=500)
_event_seq: int = 0
_system_metrics_cache: dict = {}


def _has_unpaired_surrogate(s: str) -> bool:
    """Return True if the string contains unpaired UTF-16 surrogate code points,
    which cannot be encoded as UTF-8 and would crash the storage pipeline."""
    if not s:
        return False
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _push_event(event: dict) -> None:
    """Append an event to the ring buffer with a monotonic sequence number."""
    global _event_seq
    with _event_lock:
        _event_seq += 1
        _event_queue.append({"seq": _event_seq, **event})


# Session state for transition tracking
_last_recalled_ids: dict[str, int] = {}  # session_id → last recalled memory_id

# Transport type used by the running server
_active_transport: str = "sse"

# Server start timestamp for uptime tracking
_start_time: float = 0.0

settings = get_settings()

# DB-size warning throttle — stores the calendar hour (0–23) when the last
# WARN was emitted.  -1 means "never logged".  Reset to -1 at midnight by
# the consolidation cycle.  Guarded by the GIL (int write is atomic enough).
_db_size_warn_last_logged_hour: int = -1

mcp_server = FastMCP(
    name="yadgar",
    instructions="Persistent memory engine for Claude Code — heat decay, sleep consolidation, and surprise-gated storage.",
    host=settings.HOST,
    port=settings.PORT,
)


# ── CORS: allow the viz server (port 42069) to fetch the API (port 8765) ─────
def _cors_wrapped_http_app(self):
    from starlette.middleware.cors import CORSMiddleware

    return CORSMiddleware(
        app=_orig_streamable_http_app(self),
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )


_orig_streamable_http_app = mcp_server.streamable_http_app.__func__
mcp_server.streamable_http_app = _cors_wrapped_http_app.__get__(mcp_server, type(mcp_server))

# ── Tool profile (read at import time — decorators execute on module load) ────
# YADGAR_PROFILE=minimal  →  10 core tools only
# YADGAR_PROFILE=full     →  all tools including power tier (default)
_PROFILE = os.environ.get("YADGAR_PROFILE", "full")


def _tool(power: bool = False):
    """Register a function as an MCP tool.

    power=True tools are omitted when YADGAR_PROFILE=minimal.
    """

    def decorator(func):
        if power and _PROFILE == "minimal":
            return func  # skip registration; function still callable internally
        return mcp_server.tool()(func)

    return decorator


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

    if db_url:
        try:
            r = httpx.get(f"{db_url}/health", timeout=2.0)
            db_ok = r.status_code == 200
        except Exception:
            db_ok = False

    if embed_url:
        try:
            r = httpx.get(f"{embed_url}/health", timeout=2.0)
            embed_ok = r.status_code == 200
        except Exception:
            embed_ok = False

    payload: dict = {
        "status": "ok",
        "version": __version__,
        "transport": _active_transport,
        "uptime_seconds": round(time.time() - _start_time, 1) if _start_time else 0,
        "active_sessions": session_count,
    }
    if db_ok is not None:
        payload["db"] = db_ok
    if embed_ok is not None:
        payload["embed"] = embed_ok
    if db_ok is False or embed_ok is False:
        payload["status"] = "degraded"

    return JSONResponse(payload)


@mcp_server.custom_route("/hooks/pre-compact", methods=["POST"])
async def hook_pre_compact(request: Request) -> JSONResponse:
    """Called by PreCompact hook before context compaction."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    directory = body.get("cwd", os.getcwd())
    replay = _replay
    if replay is None:
        return JSONResponse(
            {"status": "error", "message": "Replay engine not initialized"}, status_code=503
        )

    result = replay.pre_compact_drain(directory)

    # Also trigger consolidation
    if _consolidation is not None:
        try:
            _consolidation.force_consolidate()
        except Exception:
            logger.debug("Emergency consolidation failed during pre-compact")

    return JSONResponse(result)


@mcp_server.custom_route("/hooks/post-compact", methods=["GET"])
async def hook_post_compact(request: Request) -> JSONResponse:
    """Called by SessionStart hook after compaction. Returns restoration context."""
    directory = request.query_params.get("directory", os.getcwd())
    replay = _replay
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

    storage = _storage
    if storage is None:
        return JSONResponse(
            {"status": "error", "message": "Storage not initialized"}, status_code=503
        )

    from datetime import datetime

    tool_name = body.get("tool_name", "unknown")

    # Skip self-referential Yadgar tools
    for prefix in _SKIP_TOOL_PREFIXES:
        if tool_name.startswith(prefix):
            return JSONResponse({"status": "skipped", "reason": "yadgar_tool"})

    # Only capture state-modifying tools
    if tool_name not in _CAPTURE_TOOLS:
        return JSONResponse({"status": "skipped", "reason": "read_only_tool"})

    session_id = body.get("session_id", "default")
    action = {
        "tool_name": tool_name,
        "summary": body.get("summary", "")[:200],
        "directory": body.get("directory", ""),
        "session_id": session_id,
    }

    # Batch: accumulate 5 actions, then flush as one combined entry
    batch = _action_batch.setdefault(session_id, [])
    batch.append(action)
    if len(batch) < 5:
        return JSONResponse({"status": "batched", "pending": len(batch)})

    # Flush batch → one combined action_log entry
    # Take local snapshot then clear so concurrent appends go to the new list
    to_flush = list(batch)
    _action_batch[session_id] = []
    combined_tools = ",".join(a["tool_name"] for a in to_flush)
    combined_summary = " | ".join(a["summary"] for a in to_flush if a["summary"])
    directory = to_flush[-1]["directory"]

    storage.insert_action_log(
        tool_name=f"batch[{combined_tools}]",
        tool_input_summary=combined_summary[:500],
        directory=directory,
        session_id=session_id,
        timestamp=datetime.now(UTC).isoformat(),
    )

    if _consolidation is not None:
        _consolidation.record_activity()

    return JSONResponse({"status": "captured", "batch_size": 5})


@mcp_server.custom_route("/hooks/session-context", methods=["GET"])
async def hook_session_context(request: Request) -> JSONResponse:
    """Return session context markdown for session-start hook (daemon mode).

    Query params: directory (optional, defaults to cwd)
    Returns: {"text": "...markdown..."}
    """
    directory = request.query_params.get("directory", os.getcwd())
    storage = _storage
    if storage is None:
        return JSONResponse({"text": ""})

    try:
        cp_res = storage._q(
            "SELECT current_task, key_decisions FROM checkpoint "
            "WHERE is_active = true ORDER BY created_at DESC LIMIT 1"
        )
        checkpoint = cp_res[0] if cp_res else None

        hot = storage._q(
            "SELECT content, heat FROM memory "
            "WHERE directory_context = $dir AND heat >= 0 "
            "ORDER BY heat DESC LIMIT 10",
            {"dir": directory},
        )

        anchored = storage._q(
            "SELECT content FROM memory "
            "WHERE is_protected = true AND heat > 0 "
            "AND tags CONTAINSANY ['_anchor'] "
            "ORDER BY created_at DESC LIMIT 4"
        )

        total_res = storage._q(
            "SELECT count() AS n FROM memory "
            "WHERE directory_context = $dir AND is_stale = false GROUP ALL",
            {"dir": directory},
        )
        total_count = total_res[0].get("n", 0) if total_res else 0

        wiki_pages = storage._q(
            "SELECT title, content FROM wiki_page ORDER BY updated_at DESC LIMIT 5"
        )
    except Exception as e:
        logger.debug("session-context hook error: %s", e)
        return JSONResponse({"text": ""})

    if not hot and not anchored:
        return JSONResponse({"text": ""})

    # Record timestamp for prompt-recall throttling
    _last_session_context[directory] = time.monotonic()

    lines = ["# Yadgar — Session Context\n"]
    if checkpoint and checkpoint.get("current_task"):
        task = checkpoint["current_task"]
        if not str(task).startswith("[auto-captured"):
            lines.append(f"**Last task:** {task}\n")
    if anchored:
        lines.append("## Critical Facts")
        for row in anchored:
            lines.append(f"- {row['content'][:200]}")
        lines.append("")
    if hot:
        shown = len(hot)
        lines.append(f"## Project Context (showing {shown} of {total_count} memories)")
        for row in hot:
            content = row["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            lines.append(f"- [{row['heat']:.1f}] {content}")
        lines.append("")
    if wiki_pages:
        lines.append("## Wiki")
        for page in wiki_pages:
            snippet = page.get("content", "")[:120].replace("\n", " ")
            lines.append(f"- **{page['title']}**: {snippet}...")
        lines.append("")
    lines.append(f"*Context for: {directory}*")

    return JSONResponse({"text": "\n".join(lines)})


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
    if now - _last_session_context.get(directory, 0) < 180:
        return JSONResponse({"text": "", "skipped": "session_context_recent"})
    # Throttle: max 1 recall per 2 minutes per directory
    if now - _last_prompt_recall.get(directory, 0) < 120:
        return JSONResponse({"text": "", "skipped": "rate_limited"})

    retriever = _retriever
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

    _last_prompt_recall[directory] = time.monotonic()
    return JSONResponse({"text": "\n".join(lines)})


def _build_dlq_alert_text() -> str:
    """Return a markdown warning string if any items are in the DLQ, else ''."""
    try:
        data_dir = Path(os.environ.get("YADGAR_DATA_DIR", settings.DATA_DIR))
        dlq_dir = data_dir / "dlq"
        if not dlq_dir.exists():
            return ""
        alerts = []
        for sidecar in sorted(dlq_dir.glob("*.json.error.json")):
            try:
                meta = json.loads(sidecar.read_text())
                meta["_file"] = sidecar.name[: -len(".error.json")]
                alerts.append(meta)
            except Exception:
                pass
        if not alerts:
            return ""
        lines = [f"# Yadgar DLQ Alert — {len(alerts)} item(s) stuck\n"]
        lines.append("These writes failed permanently and will not be retried automatically.")
        lines.append(
            "Run `dlq_inspect()` for details, `dlq_requeue(filename)` after fixing root cause.\n"
        )
        for a in alerts[:5]:
            lines.append(
                f"- {a.get('op_type', '?')}  attempts={a.get('attempts')}  "
                f"moved={a.get('moved_to_dlq_at', '')[:19]}  "
                f"error={str(a.get('last_error', ''))[:80]}"
            )
        if len(alerts) > 5:
            lines.append(f"  ... and {len(alerts) - 5} more")
        return "\n".join(lines)
    except Exception:
        return ""


# ── Graph / Visualization API ──────────────────────────────────────────────
_CORS = {"Cache-Control": "no-cache"}


@mcp_server.custom_route("/api/graph", methods=["GET"])
async def api_graph(request: Request) -> JSONResponse:
    """Return full knowledge graph (nodes + edges) for visualization."""
    if _storage is None:
        return JSONResponse({"nodes": [], "edges": []}, status_code=503)
    try:
        max_mem = int(request.query_params.get("max_memories", 500))
    except (ValueError, TypeError):
        max_mem = 500
    try:
        top_k = int(request.query_params.get("top_k", 8))
    except (ValueError, TypeError):
        top_k = 8
    data = await asyncio.to_thread(GraphAPI(_storage).get_full_graph, max_mem, top_k)
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request) -> JSONResponse:
    """Return memory statistics as JSON (used by `yadgar stats` CLI when daemon is running)."""
    if _storage is None:
        return JSONResponse({}, status_code=503)
    project = request.query_params.get("project")
    data = await asyncio.to_thread(_storage.get_memory_stats)
    if project:
        data["project_filter"] = project
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/graph/stats", methods=["GET"])
async def api_graph_stats(request: Request) -> JSONResponse:
    """Return graph statistics: counts + top entities by heat."""
    if _storage is None:
        return JSONResponse({}, status_code=503)
    data = await asyncio.to_thread(GraphAPI(_storage).get_graph_stats)
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/graph/neighborhood/{node_id}", methods=["GET"])
async def api_graph_neighborhood(request: Request) -> JSONResponse:
    """Return 1–2 hop subgraph around a node."""
    if _storage is None:
        return JSONResponse({"nodes": [], "edges": []}, status_code=503)
    node_id = request.path_params.get("node_id", "")
    try:
        hops = int(request.query_params.get("hops", 2))
    except (ValueError, TypeError):
        hops = 2
    data = await asyncio.to_thread(GraphAPI(_storage).get_neighborhood, node_id, hops)
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/system", methods=["GET"])
async def api_system(request: Request) -> JSONResponse:
    """Return current system and process metrics."""
    return JSONResponse(_system_metrics_cache, headers=_CORS)


@mcp_server.custom_route("/api/metrics/heat-histogram", methods=["GET"])
async def api_heat_histogram(request: Request) -> JSONResponse:
    """Return heat distribution bucketed into N bins."""
    if _storage is None:
        return JSONResponse({"buckets": [], "total": 0}, status_code=503)
    try:
        n_bins = max(1, min(50, int(request.query_params.get("bins", 10))))
    except (ValueError, TypeError):
        n_bins = 10

    def _compute() -> dict:
        rows = _storage._q("SELECT heat FROM memory") or []
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
    if _storage is None:
        return JSONResponse([], status_code=503)
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", 30))))
    except (ValueError, TypeError):
        limit = 30

    def _fetch() -> list:
        rows = (
            _storage._q(
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
    except (ValueError, TypeError):
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
            new_events = [e for e in _event_queue if e["seq"] > last_seq]
            for e in new_events:
                last_seq = e["seq"]
                yield f"data: {json.dumps(e)}\n\n"
            # Push system metrics every 5 s
            if now - last_sys_push >= 5.0 and _system_metrics_cache:
                last_sys_push = now
                payload = json.dumps({"event": "system_metrics", "data": _system_metrics_cache})
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
    static_dir = Path(__file__).parent / "static"
    return FileResponse(static_dir / "graph.html")


def _get_storage() -> StorageEngine:
    assert _storage is not None, "StorageEngine not initialized"
    return _storage


def _get_embeddings() -> EmbeddingEngine:
    assert _embeddings is not None, "EmbeddingEngine not initialized"
    return _embeddings


def _get_buffer() -> ActionLogger:
    assert _buffer is not None, "ActionLogger not initialized"
    return _buffer


def _is_episodic_query(query: str) -> bool:
    """Return True if the query is temporal/episodic — wiki blending is skipped."""
    q = query.lower()
    for kw in settings.TEMPORAL_KEYWORDS.split(","):
        kw = kw.strip()
        if kw and kw in q:
            return True
    return False


def _get_consolidation() -> ConsolidationScheduler:
    assert _consolidation is not None, "ConsolidationScheduler not initialized"
    return _consolidation


def _get_staleness() -> StalenessDetector:
    assert _staleness is not None, "StalenessDetector not initialized"
    return _staleness


def _get_thermo() -> MemoryThermodynamics:
    assert _thermo is not None, "MemoryThermodynamics not initialized"
    return _thermo


def _get_retriever() -> Retriever:
    assert _retriever is not None, "Retriever not initialized"
    return _retriever


def _get_write_gate() -> WriteGate:
    assert _write_gate is not None, "WriteGate not initialized"
    return _write_gate


def _get_engram() -> EngramAllocator:
    assert _engram is not None, "EngramAllocator not initialized"
    return _engram


def _get_replay() -> CheckpointRestore:
    assert _replay is not None, "CheckpointRestore not initialized"
    return _replay


def _get_file_queue():
    global _file_queue, _queue_drainer
    if _file_queue is None:
        with _queue_lock:
            if _file_queue is None:
                from yadgar.file_queue import FileQueue, QueueDrainer

                base = Path(os.environ.get("YADGAR_DATA_DIR", settings.DATA_DIR))
                _file_queue = FileQueue(base, wiki_prefix=settings.WIKI_SLUG_PREFIX)
                _queue_drainer = QueueDrainer(
                    _file_queue,
                    _get_storage,
                    drain_interval=float(settings.QUEUE_DRAIN_INTERVAL),
                    max_permanent_attempts=settings.QUEUE_MAX_PERMANENT_ATTEMPTS,
                    max_transient_attempts=settings.QUEUE_MAX_TRANSIENT_ATTEMPTS,
                    backoff_base_s=float(settings.QUEUE_BACKOFF_BASE_S),
                    backoff_max_s=float(settings.QUEUE_BACKOFF_MAX_S),
                    dlq_retention_days=settings.QUEUE_DLQ_RETENTION_DAYS,
                )
                _queue_drainer.start()
    return _file_queue


def _file_hash(filepath: str) -> str | None:
    """Compute SHA-256 hash of a file if it exists and is a regular file."""
    try:
        p = Path(filepath).expanduser().resolve()
    except Exception:
        return None
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ── MCP Tools ──────────────────────────────────────────────────────────


@_tool()
def memorize(
    content: str,
    context: str,
    tags: list[str],
    is_protected: bool = False,
) -> dict:
    """Store a new memory with embedding.

    context MUST be the actual working directory path (e.g., '/home/user/projects/myapp'),
    NOT a description. get_project_context() filters by directory path match —
    descriptive strings will make memories unfindable by project.

    Persistence options:
    - is_protected=True: memory is exempt from heat decay and will never be aged out.
      Use this for facts that must persist indefinitely (credentials locations, key
      decisions, permanent constraints). Equivalent to calling anchor() but inline.
    - Alternatively, include "_anchor" in tags for the same effect.
    - Without either flag, memories decay naturally based on heat and last-access time.
    """
    if len(content) > 32_768:
        return {"stored": False, "reason": "content_too_large", "max_bytes": 32_768}

    # Secret detection — always on, fires before anything else
    sec_blocked, sec_reason, sec_pattern = check_secrets(content)
    if sec_blocked:
        return {"stored": False, "reason": sec_reason, "pattern_matched": sec_pattern}

    # Write-path policy rules — may block or redact content
    if _rules_engine is not None:
        wp_blocked, wp_reason, wp_modified = _rules_engine.check_write_policy(
            content, context, tags
        )
        if wp_blocked:
            return {"stored": False, "reason": f"blocked_by_policy: {wp_reason}"}
        if wp_modified is not None:
            content = wp_modified

    if _has_unpaired_surrogate(content):
        return {"stored": False, "reason": "invalid_unicode_surrogates"}

    # Async path: enqueue and return immediately (skip during drain replay)
    if not is_draining():
        try:
            _fq_path = _get_file_queue().enqueue(
                "memorize",
                {
                    "content": content,
                    "context": context,
                    "tags": list(tags),
                    "is_protected": is_protected,
                },
            )
            from pathlib import Path as _Path

            return {"stored": True, "queued": True, "queue_id": _Path(_fq_path).name}
        except Exception as _fq_exc:
            logger.warning("File queue enqueue failed, falling back to sync: %s", _fq_exc)

    # Sync path — only runs during drain replay (is_draining=True) or queue fallback
    storage = _get_storage()
    embeddings = _get_embeddings()
    buffer = _get_buffer()

    # Predictive coding write gate — FIRST check before any storage
    gate_result = None
    if _write_gate is not None:
        should_store, surprisal, reason = _write_gate.should_store(content, context, tags)
        gate_result = {
            "surprisal": round(surprisal, 4),
            "gate_reason": reason,
        }
        if not should_store:
            return {
                "stored": False,
                "surprisal": round(surprisal, 4),
                "reason": reason,
                "message": "Memory below surprisal threshold, skipped",
            }

    # Archive path var not needed in sync path (drainer archives on its own)
    _fq_path = None

    # Generate contextual prefix for richer embedding semantics
    contextual_prefix = None
    retriever = _retriever
    if retriever is not None and settings.CONTEXTUAL_PREFIX_ENABLED:
        from datetime import datetime

        contextual_prefix = retriever.generate_contextual_prefix(
            content, context, tags, datetime.now(UTC)
        )

    # Embed with contextual prefix prepended if available
    embed_text = f"{contextual_prefix}{content}" if contextual_prefix else content
    embedding = embeddings.encode(embed_text)
    fhash = _file_hash(context)

    # Compute thermodynamic scores
    thermo = _thermo
    if thermo is not None:
        surprise = thermo.compute_surprise(content, context)
        importance = thermo.compute_importance(content, tags)
        valence = thermo.compute_valence(content)
        initial_heat = thermo.apply_surprise_boost(1.0, surprise)
    else:
        surprise = 0.0
        importance = 0.5
        valence = 0.0
        initial_heat = 1.0

    # Use curator for intelligent ingestion (merge/link/create)
    curator = _curator
    if curator is not None and embedding is not None:
        curation_result = curator.curate_on_remember(
            content,
            context,
            tags,
            embedding,
            initial_heat=initial_heat,
            surprise=surprise,
            importance=importance,
            valence=valence,
            file_hash=fhash,
            embedding_model=embeddings.get_model_name(),
            contextual_prefix=contextual_prefix,
        )
        memory_id = curation_result["memory_id"]
        curation_action = curation_result["action"]
        # Race: candidate was deleted between search and merge — fall back to direct insert
        if memory_id is None:
            memory_id = storage.insert_memory(
                {
                    "content": content,
                    "embedding": embedding,
                    "tags": tags,
                    "directory_context": context,
                    "heat": initial_heat,
                    "is_stale": False,
                    "file_hash": fhash,
                    "embedding_model": embeddings.get_model_name(),
                }
            )
            curation_action = "created"
    else:
        # Fallback: direct insert (no curator or no embedding)
        memory_id = storage.insert_memory(
            {
                "content": content,
                "embedding": embedding,
                "tags": tags,
                "directory_context": context,
                "heat": initial_heat,
                "is_stale": False,
                "file_hash": fhash,
                "embedding_model": embeddings.get_model_name(),
            }
        )

        if contextual_prefix:
            storage.update_memory_fields(memory_id, contextual_prefix=contextual_prefix)

        storage.update_memory_scores(
            memory_id,
            surprise_score=surprise,
            importance=importance,
            emotional_valence=valence,
        )
        curation_action = "created"

    # CLS dual-store: classify memory as episodic or semantic
    if _consolidation is not None and _consolidation.cls is not None:
        store_type = _consolidation.cls.classify_memory(content, tags, context)
        storage.update_memory_fields(memory_id, store_type=store_type)

    # Register file hash so staleness detector can find the filepath later
    if fhash is not None:
        storage.upsert_file_hash(context, fhash)

    # Capture in sensory buffer
    buffer.capture(content, context)

    # Record activity on consolidation engine
    if _consolidation is not None:
        _consolidation.record_activity()

    # Assign to astrocyte processes for domain-aware consolidation
    if _pool is not None:
        mem_data = storage.get_memory(memory_id)
        if mem_data:
            _pool.assign_memory(mem_data)

    # Synaptic boost for high-importance memories
    if thermo is not None and importance > 0.7:
        thermo.synaptic_boost(memory_id, initial_heat)

    # Prospective memory: auto-create triggers from content & check existing triggers
    triggered_memories = []
    if _prospective is not None:
        _prospective.auto_create_from_content(content, context)

        from datetime import datetime as _dt

        trigger_context = {
            "directory": context,
            "content": content,
            "entities": tags,
            "current_time": _dt.now(UTC),
        }
        triggered_memories = _prospective.check_triggers(trigger_context)

    # Engram allocation — competitive slot assignment with temporal linking
    engram_result = None
    if _engram is not None:
        try:
            engram_result = _engram.allocate(memory_id)
        except Exception:
            logger.debug("Engram allocation failed for memory %s", memory_id)

    # ── Zero-Gap Enhancements ────────────────────────────────────────────

    # 1. Record store in write gate for task continuity tracking
    if _write_gate is not None:
        _write_gate.record_stored(content, context, embedding)

    # 2. Explicit protection + decision auto-protection
    auto_protected = False
    explicit_anchor = is_protected or "_anchor" in tags
    if explicit_anchor:
        storage.update_memory_fields(memory_id, is_protected=1, importance=1.0)
        if "_anchor" not in tags:
            tags = list(tags) + ["_anchor"]
            storage.update_memory_fields(memory_id, tags=tags)
        auto_protected = True
        logger.debug("Explicitly protected: memory %s", memory_id)
    elif settings.DECISION_AUTO_PROTECT and _DECISION_STRONG_RE.search(content):
        storage.update_memory_fields(memory_id, is_protected=1, importance=1.0)
        auto_protected = True
        logger.debug("Decision auto-protected: memory %s", memory_id)

    # 3. Session coherence: boost heat for current-session memories
    if thermo is not None:
        mem_data = storage.get_memory(memory_id)
        if mem_data and mem_data.get("created_at"):
            coherent_heat = thermo.apply_session_coherence(mem_data["heat"], mem_data["created_at"])
            if coherent_heat != mem_data["heat"]:
                storage.update_memory_heat(memory_id, coherent_heat)

    # 4. Micro-checkpoint: auto-checkpoint on significant events
    if _replay is not None and settings.MICRO_CHECKPOINT_ENABLED:
        gate_surprisal = gate_result["surprisal"] if gate_result else 0.0
        should_micro, micro_reason = _replay.should_micro_checkpoint(content, tags, gate_surprisal)
        if should_micro:
            try:
                _replay.create_micro_checkpoint(context, content, micro_reason)
                logger.debug("Micro-checkpoint created: %s", micro_reason)
            except Exception:
                logger.debug("Micro-checkpoint failed")

    # 5. Action stream: log this remember operation
    if buffer is not None:
        summary = content[:150].replace("\n", " ")
        buffer.capture_action("memorize", context, summary, curation_action)

    # 6. Related context reinjection: surface what you already know
    related_context = []
    if settings.REINJECTION_ENABLED and _retriever is not None:
        try:
            related = _retriever.recall(
                content[:300],
                max_results=settings.REINJECTION_MAX_RESULTS + 1,
                min_heat=0.0,
            )
            for r in related:
                if r["id"] != memory_id:
                    r_content = r.get("content", "")
                    if len(r_content) > 300:
                        r_content = r_content[:300] + "..."
                    related_context.append(
                        {
                            "id": r["id"],
                            "content": r_content,
                            "heat": r.get("heat", 0),
                        }
                    )
                if len(related_context) >= settings.REINJECTION_MAX_RESULTS:
                    break
        except Exception:
            logger.debug("Reinjection recall failed")

    # Track tool call for auto-checkpoint interval
    if _replay is not None:
        _replay.record_tool_call()

    # ── Build Response ─────────────────────────────────────────────────

    memory = storage.get_memory(memory_id)
    if memory is None:
        # Archive queue entry even on readback failure — DB write succeeded
        if _fq_path is not None:
            try:
                _get_file_queue().archive(Path(_fq_path))
            except Exception as _fq_exc:
                logger.debug("File queue archive failed (non-fatal): %s", _fq_exc)
        return {
            "stored": True,
            "id": memory_id,
            "curation_action": curation_action,
            "warning": "memory written but not found on readback",
        }
    # Strip binary fields from response (not JSON-serializable)
    memory.pop("embedding", None)

    # Publish visualization event
    _push_event(
        {
            "event": "memory_added",
            "node": {
                "id": f"mem:{memory_id}",
                "heat": memory.get("heat", initial_heat),
                "content": content[:200],
                "tags": tags,
                "directory": context,
            },
        }
    )

    memory.setdefault("file_hash", None)
    # Stamp CRDT provenance on newly created memories
    if curation_action == "created":
        _agent = settings.CRDT_AGENT_ID
        _clock = json.dumps({_agent: 1})
        storage._q(
            "UPDATE type::record('memory', $id) SET provenance_agent = $a, vector_clock = $c",
            {"id": memory_id, "a": _agent, "c": _clock},
        )
        memory["provenance_agent"] = _agent
        memory["vector_clock"] = _clock
    memory["curation_action"] = curation_action
    if gate_result is not None:
        memory["surprisal"] = gate_result["surprisal"]
        memory["gate_reason"] = gate_result["gate_reason"]
    if triggered_memories:
        memory["triggered_prospective_memories"] = [
            {"id": pm["id"], "content": pm["content"]} for pm in triggered_memories
        ]
    if engram_result is not None:
        memory["engram_slot"] = engram_result["slot_index"]
        memory["temporal_links"] = engram_result["temporally_linked"]
        memory["temporal_link_count"] = engram_result["link_count"]
    if auto_protected:
        memory["auto_protected"] = True
        memory["protection_reason"] = "decision_detected"
    if related_context:
        memory["related_context"] = related_context

    # Archive the queue entry — DB write confirmed
    if _fq_path is not None:
        try:
            _get_file_queue().archive(Path(_fq_path))
        except Exception as _fq_exc:
            logger.debug("File queue archive failed (non-fatal): %s", _fq_exc)

    return memory


@_tool()
def remember(
    content: str,
    context: str,
    tags: list[str],
    is_protected: bool = False,
) -> dict:
    """Renamed to memorize. Update your MCP config or CLAUDE.md."""
    return {
        "stored": False,
        "reason": "Tool renamed to memorize — call memorize() instead",
    }


@_tool()
def recall(query: str, max_results: int = 5, min_heat: float = 0.0) -> list[dict]:
    """Semantic + keyword search filtered by heat. Boosts accessed memories."""
    storage = _get_storage()

    # Record activity on consolidation engine
    if _consolidation is not None:
        _consolidation.record_activity()

    # Use HippoRetriever for unified 4-signal recall
    retriever = _retriever
    if retriever is not None:
        merged = retriever.recall(query, max_results=max_results, min_heat=min_heat)
    else:
        # Fallback to basic FTS + vector if retriever not initialized
        embeddings = _get_embeddings()
        try:
            fts_results = storage.search_memories_fts(
                query, min_heat=min_heat, limit=max_results * 2
            )
        except Exception:
            fts_results = []

        semantic_results = []
        query_embedding = embeddings.encode(query)
        if query_embedding is not None:
            vec_hits = storage.search_vectors(
                query_embedding, top_k=max_results * 2, min_heat=min_heat
            )
            for mid, _distance in vec_hits:
                mem = storage.get_memory(mid)
                if mem:
                    semantic_results.append(mem)

        seen = set()
        merged = []
        for m in fts_results + semantic_results:
            if m["id"] not in seen:
                seen.add(m["id"])
                merged.append(m)

        merged.sort(
            key=lambda m: m["heat"] * m.get("confidence", 1.0),
            reverse=True,
        )
        merged = merged[:max_results]
        for m in merged:
            m.pop("embedding", None)

    # Boost heat, update last_accessed, and record metamemory access
    now = storage._now_iso()
    thermo = _thermo
    for m in merged:
        new_heat = min(m["heat"] + 0.1, 1.0)
        storage.update_memory_heat(m["id"], new_heat)
        storage.update_memory_last_accessed(m["id"], now)
        m["heat"] = new_heat
        m["last_accessed"] = now
        if thermo is not None:
            thermo.record_access(m["id"], was_useful=True)

    # Record SR transitions: link previous recall → current recall
    if _cognitive_map is not None and merged:
        session_key = "default"
        top_id = merged[0]["id"]
        prev_id = _last_recalled_ids.get(session_key)
        if prev_id is not None and prev_id != top_id:
            try:
                _cognitive_map.record_transition(prev_id, top_id, session_key)
                _cognitive_map.incremental_update(prev_id, top_id)
            except Exception:
                logger.debug("SR transition recording failed")
        _last_recalled_ids[session_key] = top_id

    # Reconsolidation disabled: memories are never rewritten on retrieval.
    # Content integrity must be preserved exactly as stored.

    # Action stream: log this recall operation
    buffer = _buffer
    if buffer is not None:
        result_count = len(merged)
        buffer.capture_action(
            "recall", "", f"query='{query[:80]}' results={result_count}", f"found_{result_count}"
        )

    # Track tool call for auto-checkpoint interval
    if _replay is not None:
        _replay.record_tool_call()

    # Wiki blending — relevance-gated, skip for episodic/temporal queries
    if _wiki is not None and not _is_episodic_query(query):
        try:
            wiki_results = _wiki.query(query, max_results=5)
            qualifying = [wr for wr in wiki_results if wr.get("_retrieval_score", 0.0) > 0.3]
            for wr in qualifying:
                wr["_source"] = "wiki"
                wr.pop("embedding", None)
            if qualifying:
                merged = sorted(
                    merged + qualifying,
                    key=lambda m: m.get("_retrieval_score", 0.0),
                    reverse=True,
                )[:max_results]
        except Exception:
            pass  # Wiki blending is best-effort

    # Strip binary fields from response (not JSON-serializable)
    for m in merged:
        m.pop("embedding", None)

    return merged


@_tool()
def forget(memory_id: int) -> dict:
    """Mark a memory for deletion by setting heat to 0, then delete it."""
    storage = _get_storage()
    memory = storage.get_memory(memory_id)
    if memory is None:
        return {"memory_id": memory_id, "status": "not_found"}
    storage.delete_memory(memory_id)
    return {"memory_id": memory_id, "status": "deleted"}


@_tool(power=True)
def validate_memory(memory_id: int) -> dict:
    """Check memory validity against current file state."""
    if _staleness is not None:
        result = _staleness.validate_memory(memory_id)
        # Normalize response format for the MCP tool
        return {
            "memory_id": memory_id,
            "is_valid": result["valid"],
            "reason": result["reason"],
        }

    # Fallback if staleness detector not initialized
    storage = _get_storage()
    memory = storage.get_memory(memory_id)
    if memory is None:
        return {"memory_id": memory_id, "is_valid": False, "reason": "memory not found"}

    if not memory.get("file_hash"):
        return {"memory_id": memory_id, "is_valid": True, "reason": "no file hash to validate"}

    current_hash = _file_hash(memory["directory_context"])
    if current_hash is None:
        storage.update_memory_staleness(memory_id, True)
        return {"memory_id": memory_id, "is_valid": False, "reason": "file no longer exists"}

    if current_hash != memory["file_hash"]:
        storage.update_memory_staleness(memory_id, True)
        return {"memory_id": memory_id, "is_valid": False, "reason": "file has changed"}

    return {"memory_id": memory_id, "is_valid": True, "reason": "file hash matches"}


@_tool(power=True)
def check_invariants() -> dict:
    """Run consistency checks on the memory store, auto-repairing fixable issues.

    Returns {"ok": bool, "violations": [...], "fixed": [...], "counts": {...}}.
    - violations: unfixable structural problems (ceiling breaches, slot anomalies, etc.)
    - fixed: descriptions of auto-repaired issues (dangling FK rows deleted)
    - ok: True only when violations is empty (fixed items don't affect ok)
    Logs INFO for each auto-repair, CRITICAL for each remaining violation.
    """
    storage = _get_storage()
    return _run_check_invariants(storage)


def _run_check_invariants(storage) -> dict:  # type: ignore[no-untyped-def]
    """Core logic for check_invariants — separated so tests can call it directly.

    Auto-repairs fixable violations (dangling foreign keys with no information loss)
    and returns them in the 'fixed' list. Non-fixable structural issues remain in
    'violations'. ok=True only when violations is empty.

    Each table check runs with a per-table timeout
    (CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS, default 60 s).  On timeout the table
    is logged at WARN and recorded in 'timeouts'; remaining tables still run.
    'ok' is False whenever violations or timeouts is non-empty.
    """
    import datetime as _dt

    global _db_size_warn_last_logged_hour

    violations: list[str] = []
    # warn_violations: non-repairable issues that are expected / low-severity.
    # Logged at WARN (not CRITICAL) but still count toward ok=False.
    warn_violations: list[str] = []
    fixed: list[str] = []
    counts: dict[str, int] = {}
    timed_out: list[str] = []

    query_timeout = settings.CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS

    # ── helpers ──────────────────────────────────────────────────────────────

    # Timeout sentinel — catch both Python's TimeoutError and httpx's variant.
    def _is_timeout(exc: BaseException) -> bool:
        try:
            import httpx as _httpx

            if isinstance(exc, _httpx.TimeoutException):
                return True
        except ImportError:
            pass
        return isinstance(exc, TimeoutError)

    def _q_t(surql: str, params: dict | None = None) -> list:
        """_q with a per-call timeout — delegates to module-level _q_with_timeout."""
        return _q_with_timeout(storage, surql, params, timeout_seconds=query_timeout)

    def _count_q(surql: str, params: dict | None = None) -> int:
        rows = _q_t(surql, params)
        if not rows:
            return 0
        row = rows[0]
        return int(row.get("c", row.get("count", 0)))

    # ── 1. Dangling links ────────────────────────────────────────────────────

    # memory table row count (used repeatedly — short query, no special timeout)
    mem_count = _count_q("SELECT count() AS c FROM memory GROUP ALL")
    counts["memory"] = mem_count

    # memory_similarity_link dangling endpoints — FIXABLE (safe DELETE).
    #
    # Previous implementation used a correlated NOT IN subquery that SurrealDB
    # v3 re-evaluates per row → O(N*M) → timeout on large tables.
    #
    # Rewritten as a Python-side set-difference:
    #   1. Fetch all live memory IDs into a Python set (one indexed lookup).
    #   2. Fetch all MSL rows (source_memory_id, target_memory_id, id).
    #   3. Compute dangling set in Python — no correlated subquery.
    #   4. Issue targeted DELETE by ID list if needed.
    try:
        live_ids_rows = _q_t("SELECT VALUE meta::id(id) FROM memory")
        live_ids: set[int] = {int(x) for x in live_ids_rows if x is not None}

        msl_rows = _q_t(
            "SELECT meta::id(id) AS rid, source_memory_id, target_memory_id "
            "FROM memory_similarity_link"
        )
        dangling_rids: list[int] = []
        for row in msl_rows:
            src = row.get("source_memory_id")
            tgt = row.get("target_memory_id")
            if src not in live_ids or tgt not in live_ids:
                rid = row.get("rid")
                if rid is not None:
                    dangling_rids.append(rid)

        dangling_msl = len(dangling_rids)
        counts["memory_similarity_link_dangling"] = dangling_msl
        if dangling_msl:
            # Batch-delete by ID to avoid re-running the full scan.
            for rid in dangling_rids:
                try:
                    storage._q("DELETE type::record('memory_similarity_link', $rid)", {"rid": rid})
                except Exception as del_exc:
                    logger.warning(
                        "check_invariants: failed to delete dangling MSL row %s: %s", rid, del_exc
                    )
            fixed.append(
                f"Deleted {dangling_msl} memory_similarity_link rows referencing non-existent memory IDs"
            )
            logger.info(
                "check_invariants: auto-fixed %d dangling memory_similarity_link rows", dangling_msl
            )
            counts["memory_similarity_link_dangling"] = 0
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: memory_similarity_link check timed out after %ds; "
                "skipping auto-repair this cycle",
                query_timeout,
            )
            timed_out.append("memory_similarity_link")
        else:
            logger.warning("check_invariants: memory_similarity_link check failed: %s", exc)

    # memory_transition dangling — safe to delete: orphan rows have no valid endpoints
    try:
        dangling_mt = _count_q(
            "SELECT count() AS c FROM memory_transition "
            "WHERE from_memory_id NOT IN (SELECT VALUE meta::id(id) FROM memory) "
            "OR to_memory_id NOT IN (SELECT VALUE meta::id(id) FROM memory) "
            "GROUP ALL"
        )
        counts["memory_transition_dangling"] = dangling_mt
        if dangling_mt:
            storage._q(
                "DELETE FROM memory_transition WHERE from_memory_id NOT IN "
                "(SELECT VALUE meta::id(id) FROM memory) OR to_memory_id NOT IN "
                "(SELECT VALUE meta::id(id) FROM memory)"
            )
            fixed.append(
                f"Deleted {dangling_mt} dangling memory_transition row(s) (both endpoints gone)"
            )
            logger.info(
                "check_invariants: auto-fixed %d dangling memory_transition row(s)", dangling_mt
            )
            counts["memory_transition_dangling"] = 0
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: memory_transition check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("memory_transition")
        else:
            logger.warning("check_invariants: memory_transition check failed: %s", exc)

    # memory_archive dangling — NOT fixable (archival records)
    try:
        dangling_ma = _count_q(
            "SELECT count() AS c FROM memory_archive "
            "WHERE original_memory_id NOT IN (SELECT VALUE meta::id(id) FROM memory) "
            "GROUP ALL"
        )
        counts["memory_archive_dangling"] = dangling_ma
        if dangling_ma:
            violations.append(
                f"{dangling_ma} memory_archive rows reference non-existent memory IDs"
            )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: memory_archive check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("memory_archive")
        else:
            logger.warning("check_invariants: memory_archive check failed: %s", exc)

    # caused_by dangling entity endpoints — FIXABLE (safe DELETE: no information loss)
    # Other relationship types — NOT fixable (structural data)
    try:
        ent_count = _count_q("SELECT count() AS c FROM entity GROUP ALL")
        counts["entity"] = ent_count

        # Fetch live entity IDs into a Python set for O(1) lookup.
        live_ent_ids_rows = _q_t("SELECT VALUE meta::id(id) FROM entity")
        live_ent_ids: set[int] = {int(x) for x in live_ent_ids_rows if x is not None}

        # Safety guard: if the ID fetch returned nothing but the count query said >0 rows
        # exist, this is a transient query glitch.  Proceeding would treat every caused_by
        # row as dangling and mass-delete all of them.  Skip and let the next cycle retry.
        if not live_ent_ids and ent_count > 0:
            logger.critical(
                "check_invariants: live_ent_ids is empty but ent_count=%d — "
                "possible transient query glitch; skipping dangling-relationship detection "
                "this cycle to avoid mass deletion",
                ent_count,
            )
        else:
            # Fetch all relationship rows that have dangling endpoints.
            dangling_rel_rows = _q_t(
                "SELECT meta::id(id) AS rid, relationship_type, source_entity_id, target_entity_id "
                "FROM relationship"
            )
            dangling_caused_by_rids: list[int] = []
            dangling_other_count = 0
            for row in dangling_rel_rows:
                src = row.get("source_entity_id")
                tgt = row.get("target_entity_id")
                is_dangling = src not in live_ent_ids or tgt not in live_ent_ids
                if not is_dangling:
                    continue
                if row.get("relationship_type") == "caused_by":
                    rid = row.get("rid")
                    if rid is not None:
                        dangling_caused_by_rids.append(rid)
                else:
                    dangling_other_count += 1

            dangling_caused_by = len(dangling_caused_by_rids)
            counts["caused_by_dangling"] = dangling_caused_by
            if dangling_caused_by:
                for rid in dangling_caused_by_rids:
                    try:
                        storage._q("DELETE type::record('relationship', $rid)", {"rid": rid})
                    except Exception as del_exc:
                        logger.warning(
                            "check_invariants: failed to delete dangling caused_by row %s: %s",
                            rid,
                            del_exc,
                        )
                fixed.append(
                    f"Deleted {dangling_caused_by} caused_by relationship rows referencing "
                    f"non-existent entity IDs"
                )
                logger.info(
                    "check_invariants: auto-fixed %d dangling caused_by rows", dangling_caused_by
                )
                counts["caused_by_dangling"] = 0

            # Renamed from "relationship_dangling" in v4.9: caused_by got its own count key
            # above; this key now represents non-caused_by dangling relationships only.
            counts["relationship_dangling_other"] = dangling_other_count
            if dangling_other_count:
                violations.append(
                    f"{dangling_other_count} relationship rows (non-caused_by) reference "
                    f"non-existent entity IDs"
                )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: relationship/caused_by check timed out after %ds; "
                "skipping this cycle",
                query_timeout,
            )
            timed_out.append("relationship")
        else:
            logger.warning("check_invariants: relationship/caused_by check failed: %s", exc)

    # caused_by row-count ceiling — prune oldest by created_at when exceeded.
    try:
        caused_by_ceiling = settings.MAX_CAUSED_BY_ROWS
        if caused_by_ceiling > 0:
            caused_by_count = _count_q(
                "SELECT count() AS c FROM relationship "
                "WHERE relationship_type = 'caused_by' GROUP ALL"
            )
            counts["caused_by"] = caused_by_count
            if caused_by_count > caused_by_ceiling:
                excess = caused_by_count - caused_by_ceiling
                # Fetch oldest rows to prune (by created_at ascending — oldest first).
                # Fetch all matching rows and slice in Python to avoid SurrealDB v3
                # LIMIT-with-parameter incompatibilities on the relationship table.
                oldest_rows_all = _q_t(
                    "SELECT meta::id(id) AS rid, created_at FROM relationship "
                    "WHERE relationship_type = 'caused_by' "
                    "ORDER BY created_at ASC"
                )
                oldest_rows = oldest_rows_all[:excess]
                pruned = 0
                for row in oldest_rows:
                    rid = row.get("rid")
                    if rid is not None:
                        try:
                            storage._q("DELETE type::record('relationship', $rid)", {"rid": rid})
                            pruned += 1
                        except Exception as del_exc:
                            logger.warning(
                                "check_invariants: failed to prune caused_by row %s: %s",
                                rid,
                                del_exc,
                            )
                if pruned:
                    fixed.append(
                        f"Pruned {pruned} oldest caused_by rows (ceiling={caused_by_ceiling})"
                    )
                    logger.info(
                        "check_invariants: pruned %d oldest caused_by rows (ceiling=%d)",
                        pruned,
                        caused_by_ceiling,
                    )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: caused_by ceiling check timed out after %ds; "
                "skipping this cycle",
                query_timeout,
            )
            timed_out.append("caused_by_ceiling")
        else:
            logger.warning("check_invariants: caused_by ceiling check failed: %s", exc)

    # wiki_crossref dangling slugs — FIXABLE (safe DELETE, slugs are just links)
    try:
        slug_rows = storage._q("SELECT VALUE slug FROM wiki_page")
        valid_slugs = set(slug_rows) if slug_rows else set()
        all_refs = storage.get_all_wiki_crossrefs()
        dangling_xrefs = [
            r
            for r in all_refs
            if r.get("from_slug") not in valid_slugs or r.get("to_slug") not in valid_slugs
        ]
        dangling_xref = len(dangling_xrefs)
        counts["wiki_crossref_dangling"] = dangling_xref
        if dangling_xref:
            # Delete each dangling crossref row
            for ref in dangling_xrefs:
                try:
                    storage._q(
                        "DELETE wiki_crossref WHERE from_slug = $fs AND to_slug = $ts",
                        {"fs": ref.get("from_slug"), "ts": ref.get("to_slug")},
                    )
                except Exception as del_exc:
                    logger.warning(
                        "check_invariants: failed to delete wiki_crossref row: %s", del_exc
                    )
            fixed.append(
                f"Deleted {dangling_xref} wiki_crossref rows referencing non-existent page slugs"
            )
            logger.info(
                "check_invariants: auto-fixed %d dangling wiki_crossref rows", dangling_xref
            )
            counts["wiki_crossref_dangling"] = 0
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: wiki_crossref check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("wiki_crossref")
        else:
            logger.warning("check_invariants: wiki_crossref check failed: %s", exc)

    # ── 2. memory:N orphan entities — FIXABLE (safe DELETE, purely derived data) ─
    try:
        mem_entity_rows = storage._q(
            "SELECT meta::id(id) AS eid, name FROM entity "
            "WHERE string::starts_with(name, 'memory:')"
        )
        orphan_eids: list[int] = []
        mem_ids_set: set[int] = set()
        if mem_count > 0:
            id_rows = storage._q("SELECT VALUE meta::id(id) FROM memory")
            mem_ids_set = {int(x) for x in id_rows if x is not None}
        for row in mem_entity_rows:
            name = row.get("name", "")
            suffix = name.split(":", 1)[1] if ":" in name else ""
            try:
                mid = int(suffix)
            except (ValueError, TypeError):
                continue
            if mid not in mem_ids_set:
                eid = row.get("eid")
                if eid is not None:
                    orphan_eids.append(eid)
        orphan_count = len(orphan_eids)
        counts["memory_entity_orphans"] = orphan_count
        if orphan_count:
            for eid in orphan_eids:
                try:
                    storage._q(
                        "DELETE type::record('entity', $eid)",
                        {"eid": eid},
                    )
                except Exception as del_exc:
                    logger.warning(
                        "check_invariants: failed to delete orphan entity %s: %s", eid, del_exc
                    )
            fixed.append(
                f"Deleted {orphan_count} entity rows named 'memory:<N>' where N is not a live memory ID"
            )
            logger.info("check_invariants: auto-fixed %d memory entity orphans", orphan_count)
            counts["memory_entity_orphans"] = 0
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: memory entity orphan check timed out after %ds; "
                "skipping this cycle",
                query_timeout,
            )
            timed_out.append("memory_entity_orphans")
        else:
            logger.warning("check_invariants: memory entity orphan check failed: %s", exc)

    # ── 3. Row-count ceilings (non-fixable — structural) ────────────────────
    _CEILINGS = {
        "action_log": 100_000,
        "episode": 10_000,
        "wiki_page": 5_000,
    }
    for table, ceiling in _CEILINGS.items():
        try:
            n = _count_q(f"SELECT count() AS c FROM {table} GROUP ALL")
            counts[table] = n
            if n > ceiling:
                violations.append(f"{table} has {n} rows (ceiling {ceiling}) — consider pruning")
        except Exception as exc:
            if _is_timeout(exc):
                logger.warning(
                    "check_invariants: %s ceiling check timed out after %ds; skipping this cycle",
                    table,
                    query_timeout,
                )
                timed_out.append(f"{table}_ceiling")
            else:
                logger.warning("check_invariants: %s ceiling check failed: %s", table, exc)

    # memory_similarity_link ceiling (dynamic, non-fixable)
    try:
        msl_count = _count_q("SELECT count() AS c FROM memory_similarity_link GROUP ALL")
        counts["memory_similarity_link"] = msl_count
        msl_ceiling = mem_count * settings.MAX_SIMILARITY_LINKS_PER_MEMORY * 2
        if msl_ceiling > 0 and msl_count > msl_ceiling:
            violations.append(
                f"memory_similarity_link has {msl_count} rows (ceiling {msl_ceiling})"
            )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: msl ceiling check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("memory_similarity_link_ceiling")
        else:
            logger.warning("check_invariants: msl ceiling check failed: %s", exc)

    # ── 4. Engram slot distribution ───────────────────────────────────────────
    try:
        if mem_count > 0:
            # Attempt rebalancing first if the allocator is available
            if _engram is not None:
                moved = _engram.rebalance_if_needed(threshold_pct=0.05)
                if moved:
                    fixed.append(
                        f"Rebalanced engram slots: moved {moved} memories from over-occupied slots"
                    )
                    logger.info(
                        "check_invariants: rebalanced %d memories from over-occupied engram slots",
                        moved,
                    )

            # Re-check occupancy after any rebalancing
            slot_rows = storage._q(
                "SELECT slot_index, count() AS n FROM memory "
                "WHERE slot_index IS NOT NONE GROUP BY slot_index"
            )
            threshold = max(1, int(mem_count * 0.05))
            for row in slot_rows:
                n = int(row.get("n", 0))
                slot = row.get("slot_index")
                if n > threshold:
                    violations.append(
                        f"Slot {slot} holds {n} memories (>{threshold}, >5% of {mem_count}) — engram collapse?"
                    )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: slot distribution check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("engram_slot_distribution")
        else:
            logger.warning("check_invariants: slot distribution check failed: %s", exc)

    # ── 5. Engram slot table integrity (non-fixable — structural) ───────────
    try:
        engram_count = _count_q("SELECT count() AS c FROM engram_slot GROUP ALL")
        counts["engram_slot"] = engram_count
        expected = settings.HOPFIELD_MAX_PATTERNS
        if engram_count != expected:
            violations.append(
                f"engram_slot has {engram_count} rows (expected {expected} = HOPFIELD_MAX_PATTERNS)"
            )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: engram_slot check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("engram_slot")
        else:
            logger.warning("check_invariants: engram_slot check failed: %s", exc)

    # ── 6. DB-size telemetry ─────────────────────────────────────────────────
    try:
        db_size = storage.get_db_size()
        if db_size["size_warning"]:
            # Throttle WARN to at most once per hour.
            current_hour = _dt.datetime.now(_dt.UTC).hour
            if current_hour != _db_size_warn_last_logged_hour:
                _db_size_warn_last_logged_hour = current_hour
                logger.warning(
                    "check_invariants: db_size %d bytes exceeds warning threshold %d bytes "
                    "(vlog=%d, sstables=%d, wal=%d)",
                    db_size["db_size_bytes"],
                    settings.DB_SIZE_WARNING_BYTES,
                    db_size["vlog_size_bytes"],
                    db_size["sstables_size_bytes"],
                    db_size["wal_size_bytes"],
                )
    except Exception as exc:
        logger.warning("check_invariants: db_size telemetry failed: %s", exc)
        db_size = {}

    # ── 7. Per-table size breakdown ──────────────────────────────────────────
    # Uses the module-level _PER_TABLE_FIELDS constant (shared with memory_stats).
    per_table: dict[str, dict] = {}
    for _tbl, _content_field in _PER_TABLE_FIELDS.items():
        try:
            if _content_field:
                _rows = _q_t(
                    f"SELECT count() AS c, "
                    f"math::sum(string::len({_content_field})) AS content_bytes "
                    f"FROM {_tbl} GROUP ALL"
                )
            else:
                _rows = _q_t(f"SELECT count() AS c FROM {_tbl} GROUP ALL")
            if _rows:
                _r = _rows[0]
                _row_count = int(_r.get("c", 0))
                _entry: dict = {"rows": _row_count}
                if _content_field:
                    _entry["estimated_bytes"] = int(_r.get("content_bytes") or 0)
                per_table[_tbl] = _entry
            else:
                per_table[_tbl] = {"rows": 0}
        except Exception as _tbl_exc:
            logger.warning(
                "check_invariants: per_table size query failed for %s: %s", _tbl, _tbl_exc
            )
            per_table[_tbl] = {"rows": 0, "error": str(_tbl_exc)}

    if db_size:
        db_size["per_table"] = per_table

    # ok=False when any violations, warn_violations, or timeouts exist
    ok = len(violations) == 0 and len(warn_violations) == 0 and len(timed_out) == 0
    for v in violations:
        logger.critical("check_invariants: %s", v)
    for v in warn_violations:
        logger.warning("check_invariants: %s", v)

    all_violations = violations + warn_violations
    result: dict = {
        "ok": ok,
        "violations": all_violations,
        "fixed": fixed,
        "counts": counts,
    }
    if timed_out:
        result["timeouts"] = timed_out
    if db_size:
        result["db_size"] = db_size

    return result


@_tool()
def get_project_context(directory: str) -> dict:
    """Return all hot memories for a directory, sorted by heat descending.

    Also checks if Hippocampal Replay hooks are installed for this project
    and includes a suggestion if they're missing.
    """
    storage = _get_storage()
    memories = storage.get_memories_for_directory(
        directory, min_heat=settings.PROJECT_CONTEXT_MIN_HEAT
    )
    for m in memories:
        m.pop("embedding", None)

    total_count = len(memories)
    limit = 15
    memories = memories[:limit]

    # Check if hooks are installed for this project
    hooks_installed = False
    project_dir = Path(directory)
    # Walk up to find .claude/settings.json
    for parent in [project_dir] + list(project_dir.parents):
        hooks_settings = parent / ".claude" / "settings.json"
        if hooks_settings.exists():
            try:
                data = json.loads(hooks_settings.read_text())
                hooks = data.get("hooks", {})
                has_pre = "PreCompact" in hooks
                has_post = any(h.get("matcher") == "compact" for h in hooks.get("SessionStart", []))
                hooks_installed = has_pre and has_post
            except Exception:
                pass
            break

    result = {"memories": memories, "total": total_count, "showing": len(memories)}
    if total_count > limit:
        result["_context_hint"] = (
            f"Showing {limit} of {total_count} memories. Use recall() for specific queries."
        )
    if not hooks_installed:
        result["_hint"] = (
            "Hippocampal Replay hooks are not installed for this project. "
            "Run `install_hooks` with this project directory to enable automatic "
            "context drain/restore on compaction. This is a one-time setup."
        )
    return result


@_tool(power=True)
def consolidate_now() -> dict:
    """Trigger an immediate consolidation cycle."""
    if _consolidation is not None:
        stats = _consolidation.force_consolidate()
        if _sleep is not None:
            try:
                sleep_stats = _sleep.run_sleep_cycle()
                stats["sleep_cycle"] = sleep_stats
            except Exception:
                logger.exception("Sleep cycle failed during consolidate_now")
        return {"status": "completed", **stats}
    return {"status": "error", "message": "Consolidation engine not initialized"}


@_tool(power=True)
def reembed_all() -> dict:
    """Generate embeddings for all memories that are missing them.

    Bulk-imported memories often lack embeddings. This tool generates them
    using the current embedding model, enabling similarity search and
    semantic relationship discovery during consolidation.
    """
    storage = _get_storage()
    embeddings = _get_embeddings()

    rows = storage.get_memories_without_embeddings()

    if not rows:
        return {"status": "ok", "message": "All memories already have embeddings", "reembedded": 0}

    batch_size = 64
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        texts = [r["content"] for r in batch]
        ids = [r["id"] for r in batch]
        encoded = embeddings.encode_batch(texts)
        for mid, emb in zip(ids, encoded, strict=False):
            if emb is not None:
                storage.update_memory_embedding(mid, emb, embeddings.model_name)
                total += 1

    return {
        "status": "ok",
        "reembedded": total,
        "total_missing": len(rows),
        "model": embeddings.model_name,
    }


@_tool()
def memory_stats() -> dict:
    """Return system memory statistics."""
    storage = _get_storage()
    stats = storage.get_memory_stats()

    if _write_gate is not None:
        # Track rejections via memories with surprisal below threshold
        stats["write_gate_rejections"] = getattr(_write_gate, "_rejection_count", 0)

    if _engram is not None:
        try:
            slot_stats = _engram.get_slot_statistics()
            total = slot_stats.get("total_slots", 1)
            occupied = slot_stats.get("occupied_slots", 0)
            stats["engram_slot_utilization"] = round(occupied / max(total, 1), 4)
        except Exception:
            stats["engram_slot_utilization"] = 0.0

    if _rules_engine is not None:
        active_rules = _rules_engine.get_all_rules()
        stats["active_rules"] = len(active_rules)

    if _cls is not None:
        stats["episodic_count"] = storage.count_memories_by_store_type("episodic")
        stats["semantic_count"] = storage.count_memories_by_store_type("semantic")

    if _cognitive_map is not None:
        stats["sr_dimensions"] = (
            "active" if _cognitive_map.has_sufficient_data() else "insufficient_data"
        )

    if _causal is not None:
        causal_edges = storage.get_all_causal_edges()
        stats["causal_edges"] = len(causal_edges)

    if _metacognition is not None:
        # Average coverage across recent queries isn't tracked globally,
        # but we can report the chunk limit setting
        stats["cognitive_load_limit"] = _metacognition._chunk_limit

    # DB-size telemetry — always include so callers can monitor disk usage.
    try:
        db_size_info = storage.get_db_size()
        # Append per-table breakdown so callers can identify which table drives bloat.
        # Uses the module-level _PER_TABLE_FIELDS constant (shared with check_invariants).
        _ms_timeout = settings.CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS
        _ms_per_table: dict[str, dict] = {}
        for _ms_tbl, _ms_field in _PER_TABLE_FIELDS.items():
            try:
                if _ms_field:
                    _ms_rows = _q_with_timeout(
                        storage,
                        f"SELECT count() AS c, "
                        f"math::sum(string::len({_ms_field})) AS content_bytes "
                        f"FROM {_ms_tbl} GROUP ALL",
                        timeout_seconds=_ms_timeout,
                    )
                else:
                    _ms_rows = _q_with_timeout(
                        storage,
                        f"SELECT count() AS c FROM {_ms_tbl} GROUP ALL",
                        timeout_seconds=_ms_timeout,
                    )
                if _ms_rows:
                    _ms_r = _ms_rows[0]
                    _ms_entry: dict = {"rows": int(_ms_r.get("c", 0))}
                    if _ms_field:
                        _ms_entry["estimated_bytes"] = int(_ms_r.get("content_bytes") or 0)
                    _ms_per_table[_ms_tbl] = _ms_entry
                else:
                    _ms_per_table[_ms_tbl] = {"rows": 0}
            except Exception as _ms_exc:
                logger.warning("memory_stats: per_table query failed for %s: %s", _ms_tbl, _ms_exc)
                _ms_per_table[_ms_tbl] = {"rows": 0, "error": str(_ms_exc)}
        db_size_info["per_table"] = _ms_per_table
        stats["db_size"] = db_size_info
    except Exception:
        pass  # non-fatal: stats are best-effort

    return stats


@_tool(power=True)
def add_rule(
    rule_type: str,
    scope: str,
    condition: str,
    action: str,
    priority: int = 0,
    scope_value: str = "",
) -> dict:
    """Add a neuro-symbolic rule for filtering/re-ranking memories.

    rule_type: "hard" (must satisfy) or "soft" (preference).
    scope: "global", "directory", or "file".
    condition: e.g. "importance > 0.7", "tag contains architecture".
    action: "filter" for hard rules, "boost:0.3" or "penalty:0.2" for soft rules.
    priority: Higher = applied first (default 0).
    scope_value: Directory path or file pattern for scoped rules.
    """
    if _rules_engine is None:
        return {"status": "error", "message": "RulesEngine not initialized"}
    try:
        rule_id = _rules_engine.add_rule(
            rule_type=rule_type,
            scope=scope,
            condition=condition,
            action=action,
            priority=priority,
            scope_value=scope_value or None,
        )
        return {"status": "created", "rule_id": rule_id}
    except ValueError as e:
        return {"status": "error", "message": str(e)}


@_tool(power=True)
def get_rules(directory: str = "") -> list[dict]:
    """Get active rules. If directory is provided, returns only applicable rules."""
    if _rules_engine is None:
        return []
    if directory:
        return _rules_engine.get_applicable_rules(directory)
    return _rules_engine.get_all_rules()


@_tool()
def checkpoint(
    directory: str,
    current_task: str = "",
    files_being_edited: list[str] = None,
    key_decisions: list[str] = None,
    open_questions: list[str] = None,
    next_steps: list[str] = None,
    active_errors: list[str] = None,
    custom_context: str = "",
) -> dict:
    """Snapshot your current working state for post-compaction recovery.

    Call this periodically during long sessions. After context compaction,
    the restore tool uses this checkpoint to reconstruct what you were doing.
    Checkpoints auto-supersede — only the latest one matters.
    """
    for _field in (current_task, custom_context):
        if _has_unpaired_surrogate(_field):
            return {"stored": False, "reason": "invalid_unicode_surrogates"}
    for _lst in (
        key_decisions or [],
        open_questions or [],
        next_steps or [],
        active_errors or [],
        files_being_edited or [],
    ):
        for _item in _lst:
            if isinstance(_item, str) and _has_unpaired_surrogate(_item):
                return {"stored": False, "reason": "invalid_unicode_surrogates"}

    # Async path: enqueue and return immediately (skip during drain replay)
    if not is_draining():
        try:
            _get_file_queue().enqueue(
                "checkpoint",
                {
                    "directory": directory,
                    "current_task": current_task,
                    "files_being_edited": files_being_edited,
                    "key_decisions": key_decisions,
                    "open_questions": open_questions,
                    "next_steps": next_steps,
                    "active_errors": active_errors,
                    "custom_context": custom_context,
                },
            )
            return {"queued": True, "directory": directory}
        except Exception as _fq_exc:
            logger.warning("File queue enqueue failed, falling back to sync: %s", _fq_exc)

    # Sync path — only runs during drain replay (is_draining=True) or queue fallback
    replay = _get_replay()

    # Enrich checkpoint with action stream summary if available
    enriched_context = custom_context
    buffer = _buffer
    if buffer is not None:
        action_summary = buffer.get_action_summary()
        if action_summary:
            enriched_context = (
                f"{custom_context}\n\n{action_summary}" if custom_context else action_summary
            )

    return replay.create_checkpoint(
        directory=directory,
        current_task=current_task,
        files_being_edited=files_being_edited,
        key_decisions=key_decisions,
        open_questions=open_questions,
        next_steps=next_steps,
        active_errors=active_errors,
        custom_context=enriched_context,
    )


@_tool()
def restore(directory: str = "") -> dict:
    """Restore context after compaction using Hippocampal Replay.

    Reconstructs your working context from:
    - Latest checkpoint (what you were doing)
    - Anchored memories (critical facts)
    - Hot project memories (thermodynamic ranking)
    - Predicted context (SR cognitive map navigation)
    - Detected knowledge gaps

    Call this after context compaction, or it will be called
    automatically via the post-compact hook.
    """
    replay = _get_replay()
    return replay.restore(directory=directory)


@_tool()
def anchor(content: str, context: str, reason: str = "") -> dict:
    """Mark critical context as compaction-resistant.

    Anchored memories get max heat, max importance, and is_protected=True.
    They are ALWAYS included in post-compaction restoration regardless
    of other scoring. Use for decisions, constraints, and critical facts
    that must survive compaction.
    """
    for _field in (content, context, reason):
        if _has_unpaired_surrogate(_field):
            return {"stored": False, "reason": "invalid_unicode_surrogates"}

    # Async path: enqueue and return immediately (skip during drain replay)
    if not is_draining():
        try:
            _get_file_queue().enqueue(
                "anchor",
                {"content": content, "context": context, "reason": reason},
            )
            return {"queued": True, "status": "anchored", "is_protected": True, "reason": reason}
        except Exception as _fq_exc:
            logger.warning("File queue enqueue failed, falling back to sync: %s", _fq_exc)

    # Sync path — only runs during drain replay (is_draining=True) or queue fallback
    replay = _get_replay()
    tags = ["_anchor"]
    if reason:
        tags.append(f"anchor:{reason}")
    memory_id = replay.anchor_memory(content, context, tags, reason)
    return {
        "memory_id": memory_id,
        "status": "anchored",
        "is_protected": True,
        "reason": reason,
    }


@_tool(power=True)
def install_hooks(project_directory: str = "", scope: str = "project") -> dict:
    """Install Claude Code hooks for automatic memory capture and replay.

    Installs five hook types:
      - PreCompact: drain context before compaction
      - SessionStart (compact): restore context after compaction
      - SessionStart (all): inject project context on every new session
      - PostToolUse: capture every tool action into action_log
      - UserPromptSubmit: auto-recall relevant memories on every user turn

    Works in both stdio and HTTP transport modes.

    project_directory: The project root. Defaults to cwd.
    scope: "project" (default) writes hooks to project .claude/settings.json;
           "global" writes SessionStart, PreCompact, PostToolUse, UserPromptSubmit,
           and PreToolUse hooks to ~/.claude/settings.json and scripts to ~/.claude/hooks/.
           Stop hook is always global regardless of scope.
    """
    import shutil

    if scope not in ("project", "global"):
        return {
            "status": "error",
            "reason": f"Invalid scope '{scope}': must be 'project' or 'global'",
        }

    project_dir = Path(project_directory) if project_directory else Path.cwd()

    # Global paths (Stop hook always here; all hooks go here when scope=global)
    global_claude_dir = Path.home() / ".claude"
    global_hooks_dir = global_claude_dir / "hooks"
    global_hooks_dir.mkdir(parents=True, exist_ok=True)

    # Determine where hook scripts and settings are written based on scope
    if scope == "global":
        hooks_dir = global_hooks_dir
        settings_target_dir = global_claude_dir
    else:
        claude_dir = project_dir / ".claude"
        hooks_dir = claude_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        settings_target_dir = claude_dir

    # Copy hook scripts from package
    package_hooks = Path(__file__).parent / "hooks"

    hook_files = {
        "pre-compact-drain.sh": 0o755,
        "post-compact-rehydrate.sh": 0o755,
        "post-tool-capture.py": 0o755,
        "session-start-context.py": 0o755,
        "prompt-recall.py": 0o755,
    }

    for filename, mode in hook_files.items():
        src = package_hooks / filename
        dst = hooks_dir / filename
        if src.exists():
            shutil.copy2(src, dst)
            dst.chmod(mode)

    # Stop hook — always installed globally so it fires in every session
    stop_hook_src = package_hooks / "stop-memory-checkpoint.py"
    stop_hook_dst = global_hooks_dir / "yadgar-stop-memory-checkpoint.py"
    if stop_hook_src.exists():
        shutil.copy2(stop_hook_src, stop_hook_dst)
        stop_hook_dst.chmod(0o755)

    pre_compact_dst = hooks_dir / "pre-compact-drain.sh"
    post_compact_dst = hooks_dir / "post-compact-rehydrate.sh"
    post_tool_dst = hooks_dir / "post-tool-capture.py"
    session_ctx_dst = hooks_dir / "session-start-context.py"
    prompt_recall_dst = hooks_dir / "prompt-recall.py"

    # Write hooks configuration to the target settings file
    settings_path = settings_target_dir / "settings.json"
    settings_data: dict = {}
    if settings_path.exists():
        try:
            settings_data = json.loads(settings_path.read_text())
        except Exception:
            settings_data = {}

    hooks_config = settings_data.get("hooks", {})

    # PreCompact hook — drain context before compaction
    hooks_config["PreCompact"] = [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f'"{pre_compact_dst}"',
                }
            ],
        }
    ]

    # SessionStart hooks — context on every session + full restore on compact
    session_hooks = []

    # All sessions: inject lightweight context
    session_hooks.append(
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python3 "{session_ctx_dst}"',
                }
            ],
        }
    )

    # After compaction: full restore with working memory, anchored, SR predictions
    session_hooks.append(
        {
            "matcher": "compact",
            "hooks": [
                {
                    "type": "command",
                    "command": f'"{post_compact_dst}"',
                }
            ],
        }
    )

    hooks_config["SessionStart"] = session_hooks

    # PostToolUse hook — capture every tool action into action_log
    hooks_config["PostToolUse"] = [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python3 "{post_tool_dst}"',
                }
            ],
        }
    ]

    # UserPromptSubmit hook — auto-recall relevant memories on every user turn
    hooks_config["UserPromptSubmit"] = [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python3 "{prompt_recall_dst}"',
                }
            ],
        }
    ]

    # PreToolUse hook — block direct docker exec into yadgar containers
    _db_lockdown_cmd = (
        'python3 -c "'
        "import sys, json\n"
        "data = json.load(sys.stdin)\n"
        "cmd = data.get('tool_input', {}).get('command', '')\n"
        "if 'docker exec yadgar-backend' in cmd or 'docker exec yadgar-db' in cmd:\n"
        "    print(json.dumps({'decision': 'block', 'reason': 'Direct docker exec into yadgar DB/backend containers is blocked to prevent data corruption. Use yadgar MCP tools instead.'}))\n"
        "    sys.exit(0)\n"
        "print(json.dumps({'decision': 'allow'}))\n"
        '"'
    )
    hooks_config["PreToolUse"] = [
        {
            "matcher": "Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": _db_lockdown_cmd,
                }
            ],
        }
    ]

    settings_data["hooks"] = hooks_config
    settings_path.write_text(json.dumps(settings_data, indent=2))

    # Register Stop hook in global ~/.claude/settings.json
    # (always global, regardless of scope — Stop must fire in every session)
    global_settings_path = global_claude_dir / "settings.json"
    global_settings: dict = {}
    if global_settings_path.exists():
        try:
            global_settings = json.loads(global_settings_path.read_text())
        except Exception:
            global_settings = {}

    global_hooks = global_settings.get("hooks", {})
    global_hooks["Stop"] = [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python3 "{stop_hook_dst}"',
                }
            ],
        }
    ]
    global_settings["hooks"] = global_hooks
    global_settings_path.write_text(json.dumps(global_settings, indent=2))

    return {
        "status": "installed",
        "scope": scope,
        "project_directory": str(project_dir),
        "hooks_directory": str(hooks_dir),
        "hooks_installed": [
            "PreCompact (drain)",
            "SessionStart (context)",
            "SessionStart (compact restore)",
            "PostToolUse (auto-capture)",
            "UserPromptSubmit (auto-recall)",
            "PreToolUse (DB lockdown)",
            "Stop (memory checkpoint — global)",
        ],
        "settings_file": str(settings_path),
        "global_settings_file": str(global_settings_path),
    }


@_tool(power=True)
def sync_instructions(claude_md_path: str = "") -> dict:
    """Sync Yadgar instructions into the global CLAUDE.md file.

    Finds or creates the '## Memory System — Yadgar' section in CLAUDE.md
    and updates it with the latest tools, capabilities, and rules.
    Call this on session start or after Yadgar updates.

    claude_md_path: Path to CLAUDE.md. Defaults to ~/.claude/CLAUDE.md
    """
    md_path = Path(claude_md_path) if claude_md_path else Path.home() / ".claude" / "CLAUDE.md"

    if not md_path.parent.is_dir():
        return {
            "status": "skipped",
            "reason": f"Directory {md_path.parent} does not exist",
        }

    # The canonical Yadgar section
    yadgar_section = f"""## Memory System — Yadgar v{__version__}
- ALWAYS use the Yadgar MCP tools (memorize, recall, get_project_context) for memory operations
- On EVERY new session start, call `recall` with the current project name to load prior context
- NEVER rely on CLAUDE.md or built-in memory for cross-session context — use Yadgar
- Before starting any task, call `get_project_context` for the current working directory
- After completing any significant task, call `memorize` to store what was done, decisions made, and outcomes
- CRITICAL: The `context` parameter in `memorize` MUST be the actual working directory path (e.g., `/home/user/projects/myapp`), NEVER a description. `get_project_context` filters by exact directory path match — descriptive strings break it.
- Yadgar is your brain. Use it.

### Context Compaction Shield
- Hooks are installed automatically on startup — no manual setup needed
- During long sessions, call `checkpoint` periodically to snapshot your working state
- Use `anchor` to mark critical facts/decisions that MUST survive context compaction
- After context compaction, call `restore` to reconstruct your working context
- `checkpoint` fields: directory, current_task, files_being_edited, key_decisions, open_questions, next_steps, active_errors, custom_context
- `anchor` fields: content, context, reason — creates protected memories with max heat
- `restore` returns: checkpoint + anchored memories + hot context + gap detection

### Available Tools
- `memorize(content, context, tags)` — Store memory with write gate. `context` MUST be a directory path (e.g., `/home/user/projects/myapp`), not a description.
- `recall(query, max_results, min_heat)` — Multi-signal retrieval
- `get_project_context(directory)` — Hot memories for directory
- `checkpoint(directory, ...)` — Snapshot working state
- `restore(directory)` — Reconstruct context after compaction
- `anchor(content, context, reason)` — Protect critical context
- `install_hooks(project_directory, scope="project"|"global")` — Enable auto replay hooks; scope=global writes to ~/.claude/
- `sync_instructions(claude_md_path)` — Update CLAUDE.md with latest rules
- `consolidate_now()` — Force consolidation cycle
- `memory_stats()` — System statistics
- `wiki_add(title, content, append=False)` — Create or append wiki pages
- `wiki_query(query)` — Search wiki pages
- `seed_project(directory, dry_run)` — Bootstrap memory for an existing project in one call

### Auto-Capture Hooks
- PostToolUse hook captures EVERY tool action automatically — no manual memorize needed
- SessionStart hook injects project context on EVERY new session
- All hooks work in both stdio and HTTP transport modes
- Action log is processed into real memories during consolidation cycles
- Decisions are auto-protected from decay/compression when detected"""

    if md_path.exists():
        content = md_path.read_text()

        # Find and replace existing Yadgar section
        import re

        # Match from "## Memory System" to next "## " header or end of file
        pattern = r"## Memory System — Yadgar[^\n]*\n(?:(?!## )[^\n]*\n)*"
        if re.search(pattern, content):
            new_content = re.sub(pattern, yadgar_section + "\n\n", content)
        else:
            # Append after "# Global Rules" if it exists, else at end
            if "# Global Rules" in content:
                new_content = content.replace(
                    "# Global Rules\n",
                    "# Global Rules\n\n" + yadgar_section + "\n",
                    1,
                )
            else:
                new_content = content + "\n\n" + yadgar_section + "\n"
    else:
        new_content = "# Global Rules\n\n" + yadgar_section + "\n"

    md_path.write_text(new_content)

    return {
        "status": "synced",
        "path": str(md_path),
        "version": __version__,
        "section_length": len(yadgar_section),
    }


# ── MCP Resources ──────────────────────────────────────────────────────


@mcp_server.resource("memory://stats")
def resource_stats() -> str:
    """Live memory statistics."""
    storage = _get_storage()
    return json.dumps(storage.get_memory_stats())


@mcp_server.resource("memory://hot")
def resource_hot() -> str:
    """All memories with heat >= HOT_THRESHOLD."""
    storage = _get_storage()
    memories = storage.get_memories_by_heat(settings.HOT_THRESHOLD)
    for m in memories:
        m.pop("embedding", None)

    return json.dumps(memories, default=str)


@mcp_server.resource("memory://stale")
def resource_stale() -> str:
    """All stale memories."""
    storage = _get_storage()
    memories = storage.get_stale_memories()
    for m in memories:
        m.pop("embedding", None)

    return json.dumps(memories, default=str)


@mcp_server.resource("memory://processes")
def resource_processes() -> str:
    """List of astrocyte process stats."""
    consolidation = _get_consolidation()
    pool = consolidation.pool
    if pool is None:
        return json.dumps([])
    return json.dumps(pool.get_process_stats(), default=str)


# ── Project Seeding ────────────────────────────────────────────────────


@_tool(power=True)
def seed_project(directory: str, dry_run: bool = False) -> dict:
    """Bootstrap Yadgar memory for an existing project in one call.

    Scans the project directory and creates foundational memories from:
    - Project structure and layout
    - Config files (package.json, pyproject.toml, Cargo.toml, go.mod, etc.)
    - Documentation (README, ARCHITECTURE, CONTRIBUTING, etc.)
    - CI/CD configuration
    - Entry points and key source files
    - Per-component summaries (monorepo-aware via config file boundaries)

    All seeded memories are tagged with '_seed' for identification.
    Re-running is safe — old seed memories are replaced, not appended to.

    directory: Project root directory to scan (absolute path).
    dry_run: If True, scan and show what would be stored without actually storing.
    """
    from yadgar.seed import seed_project as _seed

    resolved = str(Path(directory).resolve())
    result = _seed(
        directory=resolved,
        dry_run=dry_run,
        storage=_storage,
        embeddings=_embeddings,
        thermo=_thermo,
        curator=_curator,
    )
    return result


# ── Wiki tools ─────────────────────────────────────────────────────────────


@_tool()
def wiki_add(
    title: str,
    content: str,
    category: str = "reference",
    tags: list[str] | None = None,
    source_memory_ids: list[int] | None = None,
    confidence: str = "medium",
    append: bool = False,
) -> dict:
    """Create or update a wiki page. Content can include [[slug]] cross-references.

    append=False (default): create a new page or overwrite an existing one.
    append=True: merge content into an existing page (appends with timestamp,
      merges tags and source_memory_ids). Use for accumulating knowledge over time.

    Categories: architecture, decision, pattern, debugging, reference, convention, fact, analysis.
    Confidence: high, medium, low.
    """
    assert _wiki is not None, "WikiStore not initialized"

    if len(content) > 65_536:
        return {"stored": False, "reason": "content_too_large", "max_bytes": 65_536}

    # Secret detection and write-path rules
    sec_blocked, sec_reason, sec_pattern = check_secrets(content)
    if sec_blocked:
        return {"stored": False, "reason": sec_reason, "pattern_matched": sec_pattern}
    if _rules_engine is not None:
        wp_blocked, wp_reason, wp_modified = _rules_engine.check_write_policy(
            content, "", tags or []
        )
        if wp_blocked:
            return {"stored": False, "reason": f"blocked_by_policy: {wp_reason}"}
        if wp_modified is not None:
            content = wp_modified

    for _field in (content, title):
        if _has_unpaired_surrogate(_field):
            return {"stored": False, "reason": "invalid_unicode_surrogates"}

    # Async path: enqueue and return immediately (skip during drain replay)
    if not is_draining():
        try:
            _get_file_queue().enqueue(
                "wiki_add",
                {
                    "title": title,
                    "content": content,
                    "category": category,
                    "tags": tags,
                    "source_memory_ids": source_memory_ids,
                    "confidence": confidence,
                    "append": append,
                },
            )
            import re as _re

            slug = (_re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "untitled")[:64]
            return {"stored": True, "queued": True, "slug": slug, "title": title}
        except Exception as _fq_exc:
            logger.warning("File queue enqueue failed, falling back to sync write: %s", _fq_exc)

    # Sync path: called by QueueDrainer (is_draining=True) or queue fallback
    if append:
        result = _wiki.ingest(content, title, tags, source_memory_ids)
    else:
        result = _wiki.add(title, content, category, tags or [], source_memory_ids, confidence)
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
    assert _wiki is not None, "WikiStore not initialized"
    results = _wiki.query(query, tags, category, max_results)
    for r in results:
        r.pop("embedding", None)
    return results


@_tool(power=True)
def wiki_read(slug: str) -> dict:
    """Read a specific wiki page by slug."""
    assert _wiki is not None, "WikiStore not initialized"
    page = _wiki.read(slug)
    if page is None:
        return {"error": f"Wiki page '{slug}' not found"}
    page.pop("embedding", None)
    return page


@_tool(power=True)
def wiki_delete(slug: str) -> dict:
    """Delete a wiki page by slug."""
    assert _wiki is not None, "WikiStore not initialized"
    deleted = _wiki.delete(slug)
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
    assert _wiki is not None, "WikiStore not initialized"
    pages = _wiki.list_pages(category)
    if slug_prefix:
        pages = [p for p in pages if p.get("slug", "").startswith(slug_prefix)]
    # Clamp limit: 0/-1/None means "no cap"; otherwise apply
    if limit is not None and limit > 0:
        pages = pages[:limit]
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
    assert _wiki is not None, "WikiStore not initialized"
    return _wiki.lint()


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
    assert _wiki is not None, "WikiStore not initialized"
    storage = _get_storage()
    draft = storage.get_wiki_draft_by_slug(slug)
    if draft is None:
        return {"approved": False, "error": f"Draft '{slug}' not found"}
    result = _wiki.add(
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


# ── DLQ tools ─────────────────────────────────────────────────────────


@_tool()
def dlq_inspect() -> list[dict]:
    """List items stuck in the dead-letter queue (failed writes that exhausted retries).

    Returns entries with op_type, attempts, classification, last_error, moved_to_dlq_at,
    and file_size. Each entry has a filename you can pass to dlq_requeue().

    These operations will NOT be retried automatically. Fix the root cause first, then
    call dlq_requeue(filename) to send them back through the queue.
    """
    fq = _get_file_queue()
    if not fq.dlq_dir.exists():
        return []
    results = []
    for sidecar in sorted(fq.dlq_dir.glob("*.json.error.json")):
        try:
            meta = json.loads(sidecar.read_text())
        except Exception:
            meta = {}
        fname = sidecar.name[: -len(".error.json")]
        main_file = fq.dlq_dir / fname
        try:
            file_size = main_file.stat().st_size if main_file.exists() else None
        except OSError:
            file_size = None
        results.append(
            {
                "file": fname,
                "op_type": meta.get("op_type", "unknown"),
                "attempts": meta.get("attempts"),
                "classification": meta.get("classification"),
                "last_error": (meta.get("last_error") or "")[:200],
                "first_failed_at": meta.get("first_failed_at"),
                "moved_to_dlq_at": meta.get("moved_to_dlq_at"),
                "file_size": file_size,
            }
        )
    return results


@_tool(power=True)
def dlq_requeue(filename: str) -> dict:
    """Move a DLQ item back to the queue so it will be retried on the next drain pass.

    Call after fixing the root cause of the failure. The item's retry counter is reset.

    filename: exact filename from dlq_inspect() (e.g. "0001778139482800_<uuid>.json")
    """
    if "/" in filename or "\\" in filename or filename.startswith("."):
        return {"requeued": False, "error": "Invalid filename — must be a plain filename"}
    fq = _get_file_queue()
    src = fq.dlq_dir / filename
    if not src.exists():
        return {"requeued": False, "error": f"Not found in DLQ: {filename}"}
    dest = fq.queue_dir / filename
    if dest.exists():
        return {"requeued": False, "error": f"Already exists in queue: {filename}"}
    try:
        src.rename(dest)
    except OSError as exc:
        return {"requeued": False, "error": str(exc)}
    # Remove sidecar
    (fq.dlq_dir / (filename + ".error.json")).unlink(missing_ok=True)
    # Reset in-memory retry tracker
    if _queue_drainer is not None:
        _queue_drainer.reset_attempt(filename)
    return {
        "requeued": True,
        "file": filename,
        "message": "Item will be retried on next drain pass",
    }


# ── Default rules ──────────────────────────────────────────────────────


def _load_default_rules(engine: RulesEngine) -> None:
    """Seed the rules engine with defaults on a fresh install.

    Only runs when no rules exist — preserves any user-configured rules.
    """
    if engine.get_all_rules():
        return
    try:
        # Action-stream memories are noisy; deprioritize them in recall results.
        engine.add_rule(
            rule_type="soft",
            scope="global",
            condition="tag contains _action_stream",
            action="penalty:0.3",
            priority=-10,
        )
    except Exception:
        logger.debug("Failed to load default rules", exc_info=True)


# ── Startup ────────────────────────────────────────────────────────────


def init_engines(
    db_path: str | None = None,
    embedding_model: str | None = None,
    start_daemons: bool = False,
    watch_directory: str | None = None,
):
    """Initialize all engines. Returns (storage, embeddings, buffer, consolidation, staleness)."""
    global _storage, _embeddings, _buffer, _consolidation, _staleness, _thermo, _retriever, _curator
    global _prospective, _narrative, _sleep, _pool, _kg, _write_gate, _engram
    global _rules_engine, _cls, _cognitive_map, _causal, _metacognition
    global _replay, _wiki

    _settings = get_settings()
    _storage = StorageEngine(db_path or _settings.DB_PATH)
    if os.environ.get("YADGAR_EMBED_URL"):
        from yadgar.ml_client import RemoteMLClient
        from yadgar.remote_embeddings import RemoteEmbeddingEngine

        _embeddings = RemoteEmbeddingEngine(embedding_model or _settings.EMBEDDING_MODEL)
        _ml_client = RemoteMLClient(os.environ["YADGAR_EMBED_URL"])
    else:
        from yadgar.ml_client import LocalMLClient

        _embeddings = EmbeddingEngine(embedding_model or _settings.EMBEDDING_MODEL)
        _ml_client = LocalMLClient(_settings)
    _buffer = ActionLogger(_storage, _settings)
    _buffer.start_session()
    _thermo = MemoryThermodynamics(_storage, _embeddings, _settings)
    _kg = KnowledgeGraph(_storage, _settings)
    _cognitive_map = CognitiveMap(_storage, _settings)
    _retriever = Retriever(_storage, _embeddings, _kg, _settings, ml_client=_ml_client)
    _curator = MemoryCurator(_storage, _embeddings, _thermo, _settings)
    _consolidation = ConsolidationScheduler(_storage, _embeddings, _settings)
    _staleness = StalenessDetector(_storage, _settings)
    _prospective = ProspectiveMemoryEngine(_storage, _settings)
    _narrative = NarrativeEngine(_storage, _kg, _settings)
    _write_gate = WriteGate(_storage, _embeddings, _retriever, _settings)
    _engram = EngramAllocator(_storage, _settings)
    _rules_engine = RulesEngine(_storage, _settings)
    _load_default_rules(_rules_engine)
    _causal = CausalDiscovery(_storage, _kg, _settings)
    _metacognition = MetaCognition(_storage, _embeddings, _kg, _settings)
    _replay = CheckpointRestore(
        storage=_storage,
        embeddings=_embeddings,
        retriever=_retriever,
        cognitive_map=_cognitive_map,
        metacognition=_metacognition,
        settings=_settings,
    )
    _wiki = WikiStore(_storage, _embeddings)
    _retriever.set_engram(_engram)
    _retriever.set_rules_engine(_rules_engine)
    _retriever.set_metacognition(_metacognition)

    # Expose inner engines as server-level globals for direct access
    _sleep = _consolidation._sleep_engine
    _pool = _consolidation.pool
    _cls = _consolidation.cls

    if start_daemons:
        _consolidation.start()
        if watch_directory:
            _staleness.start(watch_directory)
        # Background system-metrics sampler for /api/system and SSE events
        _pid = os.getpid()
        _db_path = _settings.DB_PATH

        def _metrics_thread(pid: int = _pid, db_path: str = _db_path) -> None:
            from yadgar.graph_api import sample_system_metrics

            sample_system_metrics(pid, db_path)  # prime CPU delta baseline
            while True:
                time.sleep(5)
                try:
                    result = sample_system_metrics(pid, db_path)
                    _system_metrics_cache.update(result)
                except Exception:
                    pass

        threading.Thread(target=_metrics_thread, daemon=True).start()

        # Idle reranker unloader — frees ~500MB after 10 min of no recall activity
        def _reranker_idle_thread() -> None:
            while True:
                time.sleep(60)
                try:
                    if _retriever is not None:
                        _retriever.unload_rerankers_if_idle(idle_seconds=600.0)
                except Exception:
                    pass

        threading.Thread(target=_reranker_idle_thread, daemon=True).start()

        # Auto-start viz server alongside the daemon
        _viz_port = getattr(_settings, "VIZ_PORT", 42069)

        def _viz_thread(port: int = _viz_port) -> None:
            try:
                from yadgar.viz_server import run_viz_server

                logger.info("Viz server starting on http://127.0.0.1:%d", port)
                run_viz_server(port=port)
            except OSError as exc:
                logger.warning("Viz server could not bind port %d: %s", port, exc)
            except Exception as exc:
                logger.warning("Viz server error: %s", exc)

        threading.Thread(target=_viz_thread, daemon=True).start()

    # Eagerly warm up the embedding model so the first recall isn't slow.
    _embeddings._ensure_model()

    # Start file queue drainer — processes any pending writes from previous sessions
    try:
        _get_file_queue()
    except Exception as exc:
        logger.warning("File queue init failed (non-fatal): %s", exc)

    return _storage, _embeddings, _buffer, _consolidation, _staleness


def shutdown():
    """Gracefully shut down all engines."""
    global _storage, _embeddings, _buffer, _consolidation, _staleness, _thermo, _retriever, _curator
    global _prospective, _narrative, _sleep, _pool, _kg, _write_gate, _engram
    global _rules_engine, _cls, _cognitive_map, _causal, _metacognition
    global _replay, _wiki, _file_queue, _queue_drainer

    if _queue_drainer is not None:
        _queue_drainer.stop()
    if _consolidation is not None:
        _consolidation.stop()
    if _staleness is not None:
        _staleness.stop()
    if _buffer is not None:
        _buffer.flush()
    if _storage is not None:
        _storage.close()

    _storage = None
    _embeddings = None
    _buffer = None
    _consolidation = None
    _staleness = None
    _thermo = None
    _retriever = None
    _curator = None
    _prospective = None
    _narrative = None
    _sleep = None
    _pool = None
    _kg = None
    _write_gate = None
    _engram = None
    _rules_engine = None
    _cls = None
    _cognitive_map = None
    _causal = None
    _metacognition = None
    _replay = None
    _wiki = None
    _file_queue = None
    _queue_drainer = None

    # Remove PID file on clean shutdown
    try:
        Path("~/.yadgar/yadgar.pid").expanduser().unlink(missing_ok=True)
    except Exception:
        pass


def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown."""
    logger.info("Received signal %s, shutting down...", signum)
    shutdown()
    sys.exit(0)


def main(
    port: int | None = None,
    db_path: str | None = None,
    transport: str = "stdio",
):
    global _active_transport, _start_time

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    _active_transport = transport
    _start_time = time.time()

    # Self-register PID file so `yadgar daemon stop/restart/status` can find us
    # regardless of how the process was started (systemd, direct CLI, etc.).
    _pid_path = Path("~/.yadgar/yadgar.pid").expanduser()
    try:
        _pid_path.parent.mkdir(parents=True, exist_ok=True)
        _pid_path.write_text(str(os.getpid()))
    except Exception:
        pass

    # Don't auto-watch cwd — in daemon/systemd mode cwd is $HOME, which would
    # recursively watch everything including the DB files, causing a watchdog storm.
    # Staleness watching is triggered per-project via MCP tools instead.
    init_engines(
        db_path=db_path,
        start_daemons=True,
        watch_directory=None,
    )

    # Auto-sync CLAUDE.md on every startup so rules stay current
    try:
        sync_instructions()
        logger.info("CLAUDE.md synced with Yadgar v%s", __version__)
    except Exception:
        logger.debug("Auto-sync of CLAUDE.md failed (non-fatal)")

    # Auto-install hooks for the current project if not already present
    try:
        install_hooks(os.getcwd())
        logger.info("Hippocampal Replay hooks installed for %s", os.getcwd())
    except Exception:
        logger.debug("Auto-install of hooks failed (non-fatal)")

    if port is not None:
        mcp_server.settings.port = port

    if transport == "streamable-http":
        # Enable stateless mode: each POST /mcp is handled independently with no
        # session ID required. This makes daemon restarts transparent — Claude Code
        # reconnects and tool calls work immediately without a stale-session failure.
        # Must be set on settings BEFORE streamable_http_app() is first called (lazy
        # init reads this flag to construct the StreamableHTTPSessionManager).
        mcp_server.settings.stateless_http = True

    try:
        mcp_server.run(transport=transport)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
