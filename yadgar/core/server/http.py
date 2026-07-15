"""Bare HTTP routes: health, metrics, hooks, graph/viz API, SSE stream.

All @mcp_server.custom_route decorators live here — they fire at import time,
so this module must be imported in server/__init__.py.

File size justified: single-responsibility route registry. Every function is a
@mcp_server.custom_route handler registering as a side-effect. Splitting would
require server/__init__.py to import each sub-module explicitly; any missed import
silently drops routes. No domain logic — all work delegated to _state + domain modules.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

import yadgar._shared.paths as _paths
import yadgar._shared.runtime.state as _st
import yadgar.core.viz.viz_daemon_health as _vdh  # noqa: F401 — V1c: SSE daemon_health push
from yadgar import __version__
from yadgar._shared.config import resolve_knob
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar.core.sanitize import sanitize_log_field
from yadgar.core.server._app import mcp_server

# T2 Car E3 (census verdict #11): the graph data assembly moved to
# yadgar.backend.graph — the /api/graph* handlers below keep their route
# shells and forward via _forward_viz.
from yadgar.core.server._helpers import (  # noqa: F401
    _bounded_set,
    _build_dlq_alert_text,
    _extract_record_id,
)

logger = logging.getLogger(__name__)

_CORS = {"Cache-Control": "no-cache"}

# ---------------------------------------------------------------------------
# #81 freeze fix: hook recalls run in a DEDICATED BOUNDED pool, NOT
# asyncio.to_thread's unbounded default executor. A hook recall is ~1.5s but
# capped by a 2s wait_for; asyncio.to_thread / run_in_executor work is
# UNCANCELLABLE, so on timeout the coroutine returns but the recall thread keeps
# running. On a 1-CPU core, a burst of agent-lifecycle hooks
# (subagent-start / prompt-recall) would otherwise pile up unbounded GIL-holding
# threads → event-loop starvation → /health/live freeze → P0 SIGKILL (status=137).
# Capping the pool bounds the leak: at most _HOOK_RECALL_POOL_WORKERS recall
# threads ever run concurrently, so the cascade is impossible.
#
# v5.95 (#81 residual): dropped 2 -> 1. Live obs (yadgar_event_loop_lag_max)
# caught a ~17s loop-lag on an agent-spawn under concurrent box load: the core is
# --cpus 1, so a slow (box-saturated) recall thread competes with the event loop
# for the single CPU.
#
# ADR-0077: raised back 1 -> 2. Post-#166 the hook recall thread is a forwarded
# HTTP wait (idle in httpx), NOT a GIL-holding in-core recall, so the v5.95
# loop-vs-thread CPU-competition rationale no longer applies. pool=1 structurally
# starved the second of every concurrent session pair (measured 32-52% hook
# timeout rate: the first forward occupies the single worker for up to 2.0s while
# the second waits queued and times out). Tunable knob (default 2) —
# Settings.HOOK_RECALL_POOL_WORKERS; read once at import, restart to apply.
try:
    from yadgar._shared.config import get_settings as _get_settings

    _HOOK_RECALL_POOL_WORKERS = int(_get_settings().HOOK_RECALL_POOL_WORKERS)
except Exception:  # noqa: BLE001 — defensive: never block route import on config load
    _HOOK_RECALL_POOL_WORKERS = 2
_HOOK_RECALL_POOL = ThreadPoolExecutor(
    max_workers=_HOOK_RECALL_POOL_WORKERS, thread_name_prefix="hook-recall"
)

# ---------------------------------------------------------------------------
# v5.51.0: Hook recall latency budget helper
# ---------------------------------------------------------------------------


@observe(tier="stage")
async def _recall_with_timeout(
    retriever,
    handler_name: str,
    *args,
    **kwargs,
):
    """Wrap asyncio.to_thread(retriever.recall, ...) with asyncio.wait_for timeout.

    On TimeoutError: logs WARN, increments yadgar_hook_recall_timeout_total{handler},
    returns None (caller should treat as empty recall).
    On other exceptions: re-raises so the caller's existing except Exception block fires.

    handler_name: one of "prompt-recall" | "instructions-loaded" | "subagent-start"
    """
    from yadgar._shared.config import get_settings  # noqa: PLC0415

    timeout_s = get_settings().HOOK_RECALL_TIMEOUT_S
    loop = asyncio.get_running_loop()

    recall_fn = functools.partial(retriever.recall, *args, **kwargs)

    # ADR-0077 (D): propagate the OTel context into the executor thread.
    # run_in_executor does NOT carry contextvars across threads, so the
    # forwarded hook recall previously started a NEW trace — orphaning the
    # backend /recall span tree from the hook route span. attach/detach the
    # caller's context around the callable so the spans share one trace.
    # Best-effort: if opentelemetry is unavailable, run the callable bare.
    try:
        from opentelemetry import context as _otel_context  # noqa: PLC0415

        _parent_ctx = _otel_context.get_current()

        def _recall_in_ctx():
            token = _otel_context.attach(_parent_ctx)
            try:
                return recall_fn()
            finally:
                _otel_context.detach(token)

        run_fn = _recall_in_ctx
    except Exception:  # noqa: BLE001 — tracing must never break the hook path
        run_fn = recall_fn

    try:
        # #81: run in the BOUNDED hook-recall pool (not asyncio.to_thread's
        # unbounded default executor) so a slow uncancellable recall that runs
        # past its wait_for timeout cannot accumulate beyond the pool cap.
        return await asyncio.wait_for(
            loop.run_in_executor(_HOOK_RECALL_POOL, run_fn),
            timeout=timeout_s,
        )
    except TimeoutError:
        logger.warning(
            "hook latency budget exceeded",
            extra={
                "event": "hook.recall_timeout",
                "handler": handler_name,
                "timeout_s": timeout_s,
            },
        )
        try:
            from yadgar._shared.observability.metrics import (
                yadgar_hook_recall_timeout_total,  # noqa: PLC0415
            )

            yadgar_hook_recall_timeout_total.labels(handler=handler_name).inc()
        except Exception:  # noqa: BLE001
            pass
        return None  # caller checks for None and returns {"text": ""}


# ---------------------------------------------------------------------------
# v5.113.0: prompt-recall hook → forward to backend /recall
# (docs/plans/hook-recall-forward-2026-07-06.md — reverses forward-only §5.4
#  for prompt-recall ONLY; instructions-loaded + subagent-start stay in-core.)
# ---------------------------------------------------------------------------


@observe(tier="boundary", metric="http._forward_hook_recall")
def _forward_hook_recall(
    query: str,
    *,
    max_results: int,
    min_heat: float,
    directory: str,
    profile: str | None = "fast",
) -> list[dict]:
    """Forward a prompt-recall HOOK recall to the backend /recall endpoint.

    Reuses tools.recall._forward_to_backend (the SAME mechanism the MCP recall
    tool uses) so the forward is not duplicated. Resolves the caller's git branch
    (backend cannot — no host .git in container), then forwards with a SHORT
    httpx timeout (HOOK_RECALL_TIMEOUT_S) so a hung backend cannot keep the hook's
    bounded-pool thread alive past its budget (#81 starvation guard).

    Runs synchronously — the caller (_recall_with_timeout) executes it in the
    bounded hook-recall pool under asyncio.wait_for. On backend error this raises
    (RuntimeError / httpx.HTTPError); the hook handler's except-block degrades to
    {"text": ""}. On timeout, wait_for returns None. Neither blocks the prompt.
    """
    from yadgar._shared.config import get_settings  # noqa: PLC0415
    from yadgar.core.server.tools.recall import _forward_to_backend  # noqa: PLC0415

    # Normalise directory before forwarding — the backend scopes with exact-string
    # is_directory_eligible (no normalisation server-side), and the post-filter in
    # _filter_prompt_recall_results also strips. Mirror recall.py's
    # `(directory or "").strip().rstrip("/")` so a trailing-slash cwd does not
    # silently scope to nothing. (The deployed hook sends a clean cwd; defensive.)
    directory = (directory or "").strip().rstrip("/")

    # Resolve branch context (mirrors recall.py:207-233). Backend must not detect.
    current_branch: str | None = None
    default_branch: str | None = None
    try:
        import yadgar.core.server as _srv  # noqa: PLC0415

        _detect = getattr(_srv, "_detect_branch", None)
        _get_default = getattr(_srv, "_get_default_branch", None)
        if _detect is None or _get_default is None:
            from yadgar.core.server.tools.project import (  # noqa: PLC0415
                _detect_branch as _detect,
            )
            from yadgar.core.server.tools.project import (  # noqa: PLC0415
                _get_default_branch as _get_default,
            )
        current_branch = _detect(directory)
        default_branch = _get_default(directory)
    except Exception:  # noqa: BLE001 — branch is best-effort; backend tolerates None
        current_branch = None
        default_branch = None

    timeout_s = get_settings().HOOK_RECALL_TIMEOUT_S
    return _forward_to_backend(
        query=query,
        max_results=max_results,
        min_heat=min_heat,
        directory=directory,
        current_branch=current_branch,
        default_branch=default_branch,
        type_filter="all",
        tags=None,
        mode=None,
        profile=profile,
        timeout_s=timeout_s,
        # ADR-0077: forward the client budget so the backend aborts pipeline
        # stages once this hook has already given up (partial-result contract).
        deadline_ms=int(timeout_s * 1000),
    )


class _HookRecallForwarder:
    """Adapter exposing a .recall(...) surface so ALL hook handlers can reuse
    _recall_with_timeout (bounded pool + wait_for guard) verbatim while executing
    the forward-to-backend path instead of the in-core Retriever.recall.

    ADR-0078: hooks are HTTP forwards only — no core DB path remains. Bound to
    the caller directory; pass "" when the hook has none (instructions-loaded):
    the backend converts an empty scope directory to caller_dir=None, which is
    the legacy whole-DB eligibility mode, preserving that hook's old semantics.
    The .recall signature matches what the hooks pass:
    recall(query, max_results, min_heat, profile).
    """

    __slots__ = ("_directory",)

    def __init__(self, directory: str) -> None:
        self._directory = directory

    def recall(
        self,
        query: str,
        max_results: int = 5,
        min_heat: float = 0.0,
        profile: str | None = "fast",
        **_ignored,
    ) -> list[dict]:
        return _forward_hook_recall(
            query,
            max_results=max_results,
            min_heat=min_heat,
            directory=self._directory,
            profile=profile,
        )


# ---------------------------------------------------------------------------
# v5.51.0: /api/stats TTL cache
# ---------------------------------------------------------------------------

_stats_cache: dict = {}  # keys: "data", "cached_at", "project"


# ---------------------------------------------------------------------------
# v5.10.6: session-end sentinel helpers
# ---------------------------------------------------------------------------

_SENTINEL_MAX_RETRIES = 3


@observe(tier="stage")
def _sentinel_memorize(content: str, directory_context: str) -> None:
    """Import one sentinel record into memory. Extracted for patching in tests.

    v5.42.3: passes branch_hint from daemon-side _detect_branch so the sentinel
    memorize carries branch context. Internal path — daemon has access to cwd git.
    """
    import yadgar.core.server as _srv  # noqa: PLC0415

    # v5.42.3: detect branch for internal path; pass as branch_hint  # _internal-only
    _branch_hint: str | None = None
    try:
        _branch_hint = _srv._detect_branch(directory_context)
    except Exception:
        pass

    result = _srv.memorize(
        content=content,
        context=directory_context,
        tags=["_session_end_sentinel", "session_end"],
        branch_hint=_branch_hint,
    )
    if not result.get("stored") and not result.get("queued"):
        # Raise so the caller's retry logic triggers
        raise RuntimeError(f"memorize rejected sentinel: {result}")


@observe(tier="stage")
def _sentinel_handle_failure(marker: Path, record: dict, retries: int, failed_dir: Path) -> None:
    """Handle a failed sentinel import: increment retries or move to failed/."""
    record["retries"] = retries
    if retries >= _SENTINEL_MAX_RETRIES:
        try:
            failed_dir.mkdir(parents=True, exist_ok=True)
            marker.rename(failed_dir / marker.name)
        except Exception as mv_e:
            logger.warning("sentinel move to failed/ error: %s", mv_e)
    else:
        try:
            tmp = marker.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            tmp.rename(marker)
        except Exception as wb_e:
            logger.warning("sentinel retry write-back error: %s", wb_e)


@observe(tier="stage")
def _import_pending_sentinels(sentinel_dir_path: str) -> None:
    """Scan sentinel dir, import each unprocessed *.json file into memory.

    - On success: file deleted (consumed).
    - On failure: retries field incremented; after _SENTINEL_MAX_RETRIES, moved to failed/.
    - Never raises — errors are logged.
    """
    sentinel_dir = Path(sentinel_dir_path)
    if not sentinel_dir.exists():
        return

    failed_dir = sentinel_dir / "failed"

    for marker in sorted(sentinel_dir.glob("*.json")):
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("sentinel parse error for %s: %s", marker, e)
            continue

        cwd = record.get("cwd", "global")
        retries = int(record.get("retries", 0))

        try:
            _sentinel_memorize(content=json.dumps(record), directory_context=cwd)
            marker.unlink()  # consumed
        except Exception as e:
            retries += 1
            logger.warning("sentinel import failed for %s (attempt %d): %s", marker, retries, e)
            _sentinel_handle_failure(marker, record, retries, failed_dir)


@observe(tier="stage")
def _vacuum_stale_sentinels(retention_days: int | None = None) -> int:
    """Delete _session_end_sentinel memory rows older than retention_days.

    T2 Car E1 (ADR-0078): the read+delete compute is stateless-over-DB and
    runs backend-side (``vacuum_stale_sentinels`` /admin op); this shell only
    forwards. Returns count of deleted rows. Never raises.
    """
    from yadgar.core.server.tools._forward import _forward_admin  # noqa: PLC0415

    try:
        result = _forward_admin("vacuum_stale_sentinels", {"retention_days": retention_days})
        return int(result.get("deleted", 0))
    except Exception as e:  # noqa: BLE001 — ops path: log, never raise
        logger.warning("sentinel vacuum error: %s", e)
        return 0


@observe(tier="stage")
def _hook_observe(hook: str, t0: float, exc: BaseException | None = None) -> None:
    """Record hook execution duration + failure metrics. Never raises."""
    try:
        from yadgar._shared.observability.metrics import (  # noqa: PLC0415
            hook_record_failure,
            yadgar_hook_execution_duration_ms,
        )

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        yadgar_hook_execution_duration_ms.labels(hook=hook).observe(elapsed_ms)
        if exc is not None:
            hook_record_failure(hook, exc=exc)
    except Exception:  # noqa: BLE001
        pass


@observe(tier="stage")
def _hook_observe_response(hook: str, status_code: int) -> None:
    """Increment failure counter if status_code >= 500. Never raises."""
    if status_code >= 500:
        try:
            from yadgar._shared.observability.metrics import hook_record_failure  # noqa: PLC0415

            hook_record_failure(hook, status_code=status_code)
        except Exception:  # noqa: BLE001
            pass


# C2 P1 (obs-train, docs/plans/observability-health-otlp-fix.md): outer bound on the
# whole /health handler body so it can never exceed this even if a dependency probe
# hangs. The container healthcheck uses --health-timeout 5s; with db+embed probed
# CONCURRENTLY (asyncio.gather, ~2s not the old serial ~4s) plus this hard cap, the
# handler returns within budget (degraded/503 on timeout, never a hang).
# v5.95: config.yaml-authoritative (HEALTH_HANDLER_TIMEOUT_SEC) — read at use-site
# via get_settings() so a yaml/UI change takes effect on restart.
def _health_handler_timeout_sec() -> float:
    from yadgar._shared.config import get_settings  # noqa: PLC0415 -- avoid import cycle

    return float(get_settings().HEALTH_HANDLER_TIMEOUT_SEC)


# #74 fix #1 — readiness anti-flap. A single transient dependency-probe miss
# (busy backend timing out the 2s embed probe once) must NOT flip /health to 503
# (which P0's curl-kill would act on). We require N CONSECUTIVE misses before the
# readiness handler degrades; a single probe success resets the counter. This
# REVERSES the prior "stateless on purpose — anti-flap delegated to docker
# --health-retries" decision: P0 now watches /health/live (which never probes the
# backend), and /health readiness owns its own anti-flap so transient backend
# busyness can't cascade into a 503 storm.
_readiness_consecutive_failures: int = 0


def _readiness_fail_threshold() -> int:
    return max(
        1,
        resolve_knob(
            "YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "HEALTH_READINESS_FAIL_THRESHOLD", int, 3
        ),
    )


def _reset_readiness_state() -> None:
    """Reset the readiness anti-flap counter (test hook + startup)."""
    global _readiness_consecutive_failures
    _readiness_consecutive_failures = 0


@observe(tier="stage")
async def _probe_dependency(client, url: str) -> bool:
    """Probe a dependency's /health. True iff it returns HTTP 200; never raises."""
    try:
        r = await client.get(f"{url}/health")
        return r.status_code == 200
    except Exception:
        return False


def _uptime_seconds() -> float:
    return round(time.time() - _st._start_time, 1) if _st._start_time else 0


@observe(tier="stage")
async def _build_health_payload() -> dict:
    """Build the /health payload, probing db + embed CONCURRENTLY (C2 P1).

    Total latency is bounded by the slowest single probe (~2s), not the sum of
    both (~4s, the old serial behaviour). Caller wraps this in asyncio.wait_for.
    """
    import httpx  # noqa: PLC0415

    session_count = 0
    if mcp_server._session_manager is not None:
        session_count = len(mcp_server._session_manager._server_instances)

    db_url = os.environ.get("YADGAR_DB_URL")
    embed_url = os.environ.get("YADGAR_EMBED_URL")
    db_ok = None
    embed_ok = None

    # §9 Q5: async httpx client to avoid blocking the event loop.
    # v5.95: probe timeout config.yaml-authoritative (HEALTH_PROBE_TIMEOUT_SEC).
    from yadgar._shared.config import get_settings  # noqa: PLC0415 -- avoid import cycle

    _probe_timeout = float(get_settings().HEALTH_PROBE_TIMEOUT_SEC)
    async with httpx.AsyncClient(timeout=_probe_timeout) as _aclient:
        tasks = []
        if db_url:
            tasks.append(("db", _probe_dependency(_aclient, db_url)))
        if embed_url:
            tasks.append(("embed", _probe_dependency(_aclient, embed_url)))
        if tasks:
            results = await asyncio.gather(*(coro for _, coro in tasks))
            for (label, _), ok in zip(tasks, results, strict=True):
                if label == "db":
                    db_ok = ok
                else:
                    embed_ok = ok

    payload: dict = {
        "status": "ok",
        "version": __version__,
        "transport": _st._active_transport,
        "uptime_seconds": _uptime_seconds(),
        "active_sessions": session_count,
    }
    if db_ok is not None:
        payload["db"] = db_ok
    if embed_ok is not None:
        payload["embed"] = embed_ok

    # Fix A O2 GATE (daemon-offload-A): degrade (→ 503) on tool-pool saturation.
    _apply_tool_pool_health(payload)

    # #74 fix #1 — readiness anti-flap. Only degrade on dependency outage after N
    # CONSECUTIVE probe misses; a single success resets. A transiently-busy backend
    # (one 2s embed-probe miss) stays 200, so it can never cascade into a 503 that
    # P0 (now watching /health/live) would not act on anyway. The pool-saturation
    # degrade above is NOT anti-flapped — a wedged pool is a real, local stall.
    global _readiness_consecutive_failures
    dependency_down = db_ok is False or embed_ok is False
    if dependency_down:
        _readiness_consecutive_failures += 1
        threshold = _readiness_fail_threshold()
        payload["readiness_consecutive_failures"] = _readiness_consecutive_failures
        if _readiness_consecutive_failures >= threshold:
            payload["status"] = "degraded"
    else:
        _readiness_consecutive_failures = 0
    return payload


@observe(tier="stage")
def _apply_tool_pool_health(payload: dict) -> None:
    """Fold tool-offload pool occupancy into the /health payload (Fix A O2).

    Goes degraded (→ 503 in the handler) when the pool is SATURATED — exhausted
    with nothing completing for > grace, i.e. wedged workers holding every slot.
    Without this a pool-dead-but-loop-alive daemon answers /health 200 and P0's
    `curl -f` health-kill can no longer catch it — a net regression vs the deployed
    P0. Completion-staleness (not full-since) means a healthy draining peak is
    never flagged. Never raises — health must not crash on the pool probe.
    """
    try:
        from yadgar._shared.runtime.offload import pool_saturated, pool_stats  # noqa: PLC0415

        _pstats = pool_stats()
        if _pstats.get("enabled"):
            payload["tool_pool"] = _pstats
        if pool_saturated():
            payload["status"] = "degraded"
            payload["tool_pool_saturated"] = True
    except Exception:  # noqa: BLE001
        pass


@mcp_server.custom_route("/health", methods=["GET"])
@trace_span()
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint."""
    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    try:
        try:
            # C2 P1: hard outer bound — a hung probe trips this and yields the
            # degraded/503 path below instead of stalling the handler.
            payload = await asyncio.wait_for(
                _build_health_payload(), timeout=_health_handler_timeout_sec()
            )
        except TimeoutError:
            payload = {
                "status": "degraded",
                "version": __version__,
                "transport": _st._active_transport,
                "uptime_seconds": _uptime_seconds(),
                "active_sessions": 0,
                "error": "health probe timed out",
            }

        # C1 (obs-train): 503 on any non-ok status so monitoring detects db/embed
        # outages instead of reading them healthy. #74 fix #1 REVERSES the prior
        # "stateless on purpose" stance: readiness now anti-flaps in-handler (N
        # consecutive misses, see _build_health_payload) so a transiently-busy
        # backend can't 503-storm. The P0 container `--health-on-failure=kill`
        # healthcheck now watches /health/live (liveness, no backend probe) — NOT
        # this readiness endpoint — so a degraded readiness no longer SIGKILLs the
        # core; it is a monitoring signal only.
        _resp = JSONResponse(payload, status_code=200 if payload["status"] == "ok" else 503)
        _hook_observe_response("health", _resp.status_code)
        return _resp
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        _hook_observe("health", _t0, _caught_exc)


@mcp_server.custom_route("/health/live", methods=["GET"])
@trace_span()
async def liveness_check(request: Request) -> JSONResponse:
    """LIVENESS probe (#74 fix #1) — answerable from the core's own loop ALONE.

    The container P0 healthcheck (`curl -f --health-on-failure=kill`) watches THIS,
    not /health (readiness). Liveness makes NO outbound dependency probe — so a
    transiently-busy backend (saturated by concurrent reranks) can NEVER make the
    core SIGKILL itself, which was the #74 root cause.

    Returns 200 normally; 503 ONLY when the tool pool is genuinely WEDGED
    (`pool_saturated()` — in-memory occupancy counters, no network). This preserves
    the O2 P0-kill for a truly dead daemon: a wedged pool with nothing completing
    past the grace still trips 503 → P0 kills. A busy-but-draining pool (completions
    landing) is NOT saturated, so concurrent in-flight reranks keep liveness 200.

    Exempt from bearer-token auth (the P0 curl carries no token).
    """
    saturated = False
    try:
        from yadgar._shared.runtime.offload import pool_saturated  # noqa: PLC0415

        saturated = pool_saturated()
    except Exception:  # noqa: BLE001 — liveness must never crash on the pool probe
        saturated = False

    payload = {
        "status": "degraded" if saturated else "ok",
        "version": __version__,
        "transport": _st._active_transport,
        "uptime_seconds": _uptime_seconds(),
    }
    if saturated:
        payload["tool_pool_saturated"] = True
    return JSONResponse(payload, status_code=503 if saturated else 200)


@mcp_server.custom_route("/metrics", methods=["GET"])
@trace_span()
async def metrics_endpoint(request: Request):
    """Prometheus metrics endpoint (§15).

    Exempt from bearer-token auth (loopback Prometheus scrapers don't carry tokens).
    Returns 404 when YADGAR_METRICS_ENABLED=False / 0.
    """
    from yadgar._shared.observability.metrics import metrics_handler

    return await metrics_handler(request)


@mcp_server.custom_route("/hooks/pre-compact", methods=["POST"])
@trace_span()
async def hook_pre_compact(request: Request) -> JSONResponse:
    """Called by PreCompact hook before context compaction."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    directory = body.get("cwd", os.getcwd())
    # HOOKS Car 2: thread transcript_path through so the backend drain can parse
    # in-flight orchestration state. hook_runner POSTs the full stdin payload
    # (which carries transcript_path); this handler previously extracted only
    # `cwd` and dropped it. Forward it (optional — None degrades to pre-Car-2).
    transcript_path = body.get("transcript_path")
    # Car fix-drain-inflight: the PreCompact hook runner parses in_flight
    # HOST-SIDE and includes it in the POST body (the core container cannot see
    # the host .claude transcript / git tree). Forward it verbatim; the backend
    # persists it as-is and only falls back to an in-container parse when absent.
    in_flight = body.get("in_flight")

    # T2 Car B: CheckpointRestore lives backend-side now — the drain writes
    # (epoch increment + auto-checkpoint upsert) run via the /admin forward.
    # Lazy import mirrors the tools.recall import at :181 (avoids the
    # http ⇄ tools package import cycle at module load).
    from yadgar.core.server.tools._forward import _forward_admin  # noqa: PLC0415

    try:
        result = await asyncio.to_thread(
            _forward_admin,
            "pre_compact_drain",
            {
                "directory": directory,
                "transcript_path": transcript_path,
                "in_flight": in_flight,
            },
        )
    except Exception as e:
        logger.exception("hook_pre_compact forward error: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=503)

    # Also trigger consolidation
    if _st._consolidation is not None:
        try:
            _st._consolidation.force_consolidate()
        except Exception:
            logger.debug("Emergency consolidation failed during pre-compact")

    return JSONResponse(result)


@mcp_server.custom_route("/hooks/post-compact", methods=["GET"])
@trace_span()
async def hook_post_compact(request: Request) -> JSONResponse:
    """Called by SessionStart hook after compaction. Returns restoration context."""
    directory = request.query_params.get("directory", os.getcwd())

    # T2 Car B: restore compute runs backend-side behind POST /restore.
    # Lazy import mirrors the tools.recall import at :181 (avoids the
    # http ⇄ tools package import cycle at module load).
    from yadgar.core.server.tools._forward import _forward_restore  # noqa: PLC0415

    try:
        result = await asyncio.to_thread(_forward_restore, directory)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("hook_post_compact error: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@mcp_server.custom_route("/hooks/block-reflect", methods=["GET"])
@trace_span()
async def hook_block_reflect(request: Request) -> JSONResponse:
    """Re-inject updated block contents after a block_* MCP write tool call (v5.35.1).

    Called by the block-reflect PostToolUse hook in hook_runner.py when any of
    block_create / block_update / block_delete / block_replace / block_append fires.

    Query params:
        directory: project directory (optional, defaults to cwd)
    Returns: {"text": "...markdown blocks section..."}
    """
    from yadgar._shared.blocks_render import render_blocks_section  # noqa: PLC0415

    directory = request.query_params.get("directory", os.getcwd())
    storage = _st._storage
    if storage is None:
        return JSONResponse({"text": ""})

    try:
        blocks = await asyncio.to_thread(
            storage.list_blocks, scope=None, directory=directory if directory else None
        )
        text = render_blocks_section(blocks, directory)
        return JSONResponse({"text": text})
    except Exception as _e:
        logger.debug("block-reflect hook error: %s", _e)
        return JSONResponse({"text": ""})


@mcp_server.custom_route("/hooks/auto-capture", methods=["POST"])
@trace_span()
async def hook_auto_capture(request: Request) -> JSONResponse:
    """Capture a tool action from PostToolUse hook (HTTP transport).

    Accepts JSON: {tool_name, summary, directory, session_id}
    Writes directly to action_log table — no write gate, no embeddings.
    """
    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    try:
        try:
            body = await request.json()
        except Exception:
            _resp = JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)
            _hook_observe_response("auto_capture", _resp.status_code)
            return _resp

        storage = _st._storage
        if storage is None:
            _resp = JSONResponse(
                {"status": "error", "message": "Storage not initialized"}, status_code=503
            )
            _hook_observe_response("auto_capture", _resp.status_code)
            return _resp

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

        # T2 Car E1 (ADR-0078): the flushed batch rides the file-queue seam —
        # the backend drainer replays it via run_action_log_replay. Enqueue is
        # disk IO, so it stays off the event loop via asyncio.to_thread.
        from yadgar.core.lifecycle import _get_file_queue  # noqa: PLC0415

        await asyncio.to_thread(
            _get_file_queue().enqueue,
            "action_log",
            {
                "tool_name": f"batch[{combined_tools}]",
                "summary": combined_summary[:500],
                "directory": directory,
                "session_id": session_id,
                "timestamp": ts,
            },
        )

        if _st._consolidation is not None:
            _st._consolidation.record_activity()

        return JSONResponse({"status": "captured", "batch_size": 5})
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        _hook_observe("auto_capture", _t0, _caught_exc)


@observe(tier="stage")
async def _task_list_restore_nudge(directory: str, branch_hint: str | None) -> str:
    """Return the task-list restore-nudge line, or "" when no page exists.

    If a saved "<project>-task-list" wiki page exists for `directory`, return a
    one-line pointer telling the instance to restore its open tasks via
    TaskCreate. A server-side existence pre-check (a metadata row read, parity
    cost with the checkpoint hint) means zero dead nudges on projects that never
    saved a list.

    MAIN-THREAD-ONLY by construction: the sole caller is hook_session_context,
    reached by SessionStart only — never by a subagent (SubagentStart /
    agent_dispatch_prelude do not call it), and NOT via project_brief
    (subagent-callable → would leak).

    The page is written CANONICALLY (branch=None) by the stop-hook step, so the
    existence check resolves it under any caller branch via §25 step-2
    (dir + branch IS NULL) — a default-branch-pinned row would be unreachable
    from a feature-branch session (memory 531352 / ADR-log branch-pin bug class).

    Fail-open: any error returns "" so session-start is never blocked.
    """
    try:
        from pathlib import Path as _Path  # noqa: PLC0415

        from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

        project = _Path(directory).name if directory else ""
        if not project:
            return ""
        storage = _get_storage()
        if storage is None:
            return ""
        slug = f"{project}-task-list"
        page = await asyncio.to_thread(
            storage.get_wiki_page_by_slug_directory_branch,
            slug,
            directory,
            branch_hint,
        )
        if not page:
            return ""
        return (
            f"\n[yadgar] Saved task list found ({slug}). To restore: "
            f'wiki_read("{slug}", directory="{directory}"), then recreate '
            "the open tasks (status pending / in_progress) with TaskCreate "
            "before proceeding (skip completed).\n"
        )
    except Exception as _te:
        logger.debug("session-context task-list nudge error: %s", _te)
        return ""


@observe(tier="stage", metric="http._persist_dir_branch_context_from_request")
def _persist_dir_branch_context_from_request(request: Request, directory: str) -> None:
    """Extract the Car 0 trusted git facts from the request + persist them.

    ``gitness`` absent = a pre-Car-0 hook → skip (never clobber a known dir's
    durable row on a legacy hook). Runs on a worker thread (blocking forward).
    """
    gitness_param = request.query_params.get("gitness", None)
    if gitness_param is None:
        return
    _persist_dir_branch_context(
        directory,
        gitness_param == "true",
        request.query_params.get("default_branch", "") or None,
    )


@observe(tier="stage", metric="http._persist_dir_branch_context")
def _persist_dir_branch_context(directory: str, gitness: bool, default_branch: str | None) -> None:
    """Durably persist the TRUSTED per-directory git-context + bust the core cache.

    Car 0 §0.2-§0.3: the SessionStart context endpoint is the SOLE set-channel.
    Writes the durable directory-keyed row via the backend admin op (ADR-0078:
    core never touches the DB directly), then fires the Manual cache invalidate so
    a gitness change is picked up on the next write. Best-effort — a failure here
    must never break the session-context render (the write path fail-safes to
    "require branch_hint" when the store is unreadable).
    """
    try:
        from yadgar.core.server.tools._forward import _forward_admin  # noqa: PLC0415

        _forward_admin(
            "upsert_dir_branch_context",
            {
                "directory": directory,
                "gitness": bool(gitness),
                "default_branch": default_branch,
            },
        )
    except Exception as _exc:  # noqa: BLE001 — never break session-context on this
        logger.warning("dir_branch_context durable upsert failed for %s: %s", directory, _exc)
    try:
        from yadgar.core.server.tools import _dir_branch  # noqa: PLC0415

        _dir_branch.invalidate(directory)
    except Exception:  # noqa: BLE001
        logger.debug("dir_branch_context invalidate failed for %s", directory, exc_info=True)


@mcp_server.custom_route("/hooks/session-context", methods=["GET"])
@trace_span()
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
        source: SessionStart source field (v5.7.9); values: "compact",
            "clear", "startup", "resume". Missing/unknown → treated as
            "startup". "compact" suppresses restore hint (compact handler
            owns auto-restore via /hooks/post-compact).
    Returns: {"text": "...markdown..."}
    """
    directory = request.query_params.get("directory", os.getcwd())
    mode = request.query_params.get("mode", "catalog")
    branch_hint = request.query_params.get("branch", "") or None
    # v5.7.9: read source for per-source hint copy and compact suppression.
    # Unknown/missing values fall through to the "startup" default.
    source = request.query_params.get("source", "") or "startup"
    _KNOWN_SOURCES = frozenset({"compact", "clear", "startup", "resume"})
    if source not in _KNOWN_SOURCES:
        source = "startup"

    # Record timestamp for prompt-recall throttling (bounded dict)
    _bounded_set(_st._last_session_context, directory, time.monotonic())

    # Car 0 §0.1-§0.3: this endpoint is the SOLE set-channel for the TRUSTED
    # per-directory git facts (gitness/default_branch), computed host-side by the
    # SessionStart hook. Persist them DURABLY + Manual-invalidate the core cache
    # (all guarding lives in the helper; "gitness" absent → pre-Car-0 hook → skip).
    await asyncio.to_thread(_persist_dir_branch_context_from_request, request, directory)

    # v5.10.6: import any pending session-end sentinel files before project_brief query.
    _sentinel_dir_env = os.environ.get("YADGAR_SESSION_END_DIR", "")
    _sentinel_dir = _sentinel_dir_env if _sentinel_dir_env else str(_paths.SESSION_ENDS_DIR)
    try:
        _import_pending_sentinels(_sentinel_dir)
    except Exception as _se:
        logger.debug("sentinel import error in session-context: %s", _se)

    # v5.7.9: source-aware prefix — context line before the brief.
    _SOURCE_PREFIX = {
        "compact": "[yadgar] Session compacted — context restored by compact handler.\n",
        "clear": "[yadgar] Session cleared — previous context wiped.\n",
        "startup": "[yadgar] Session starting.\n",
        "resume": "[yadgar] Resuming session.\n",
    }

    # Dedup: on source=compact the /hooks/post-compact handler already owns the
    # whole restore inject (restore() prepends blocks + emits the checkpoint +
    # the project_brief catalog). The _render catalog assembled below is NOT
    # otherwise guarded, so on compact it double-injects (~500-tok duplicate
    # alongside restore()'s markdown). Early-return here with ONLY the v5.7.9
    # compaction note (a one-line marker restore() does not emit) and skip the
    # catalog. Placed AFTER the recall-throttle write (:869) and sentinel import
    # (:875) so those side effects still fire on every compact.
    if source == "compact":
        return JSONResponse({"text": _SOURCE_PREFIX["compact"]})

    try:
        # Look up via yadgar.server so patch.object(srv, "project_brief", ...) takes effect
        import sys as _sys  # noqa: PLC0415

        _srv = _sys.modules.get("yadgar.core.server")
        _pb = getattr(_srv, "project_brief", None) if _srv else None
        if _pb is None:
            from yadgar.core.server.tools.project import project_brief as _pb  # noqa: PLC0415
        brief = await asyncio.to_thread(_pb, directory, mode=mode, branch_hint=branch_hint)
        render = brief.get("_render", "")

        render = _SOURCE_PREFIX.get(source, "") + render

        # v5.35.1: prepend memory blocks (always-injected named containers).
        # Skipped for source=compact — /hooks/post-compact already calls restore()
        # which prepends blocks via _prepend_blocks.
        if source != "compact":
            try:
                from yadgar._shared.blocks_render import (
                    render_blocks_section as _rbs,  # noqa: PLC0415
                )
                from yadgar._shared.runtime.lifecycle import _get_storage as _gs2  # noqa: PLC0415

                _storage2 = _gs2()
                if _storage2 is not None:
                    _blocks = await asyncio.to_thread(
                        _storage2.list_blocks, scope=None, directory=directory or None
                    )
                    _bsection = _rbs(_blocks, directory)
                    if _bsection:
                        render = _bsection + "\n" + render
            except Exception as _be:
                logger.debug("session-context blocks inject error: %s", _be)

        # v5.6.5 / v5.7.9: append checkpoint resume hint.
        # SUPPRESSED for source=compact — the compact handler (/hooks/post-compact)
        # already calls replay.restore() automatically. Emitting a hint here would
        # create a confusing duplicate "to resume call restore()" alongside the
        # already-restored context.
        # For all other sources: hint only — never auto-call restore().
        if source != "compact":
            try:
                from yadgar._shared.runtime.lifecycle import _get_storage as _gs  # noqa: PLC0415

                _storage = _gs()
                _cp = await asyncio.to_thread(_storage.get_active_checkpoint, directory)
                if _cp:
                    _task = _cp.get("current_task", "")
                    _ts = _cp.get("created_at", "")
                    _source_hint_prefix = {
                        "clear": "Session cleared — call restore() if needed.\n",
                        "startup": "Call restore() to pick up where you left off.\n",
                        "resume": "Checkpoint available — call restore() to load context.\n",
                    }.get(source, "")
                    _hint = (
                        f"\n[yadgar] Active checkpoint for {directory}:\n"
                        f"  Task: {_task}\n"
                        f"  Time: {_ts}\n"
                        + (f"  {_source_hint_prefix}" if _source_hint_prefix else "")
                        + f'To resume: call `restore(directory="{directory}")`\n'
                    )
                    render = render + _hint
            except Exception as _ce:
                logger.debug("session-context checkpoint hint error: %s", _ce)

        # Task-list mirror restore-nudge (MAIN-THREAD-ONLY; existence-checked).
        # Gated source != "compact" (inherits the enclosing block). Extracted to
        # _task_list_restore_nudge to keep this handler under the I13 complexity
        # cap; that helper is fail-open (returns "" on any error).
        if source != "compact":
            render = render + await _task_list_restore_nudge(directory, branch_hint)

        return JSONResponse({"text": render})
    except Exception as _e:
        logger.debug("session-context hook error: %s", _e)
        return JSONResponse({"text": ""})


@observe(tier="stage")
def _filter_prompt_recall_results(results: list[dict], directory: str | None) -> list[dict]:
    """Post-filter retriever results by caller directory for prompt-recall.

    v5.65 Fix D: hook_prompt_recall previously forwarded all retriever results to
    the response without directory scoping.  The retriever runs in a container and
    cannot filter by host directory on its own — we must apply is_directory_eligible()
    here after retrieval.

    When directory is None or empty (param absent / not passed by hook script),
    scoping is skipped with a warning rather than using os.getcwd() (container path
    would mis-scope results).
    """
    from yadgar._shared.storage.directory import is_directory_eligible  # noqa: PLC0415

    if not directory or not directory.strip():
        logger.warning(
            "prompt-recall: directory param absent — skipping directory filter "
            "(container cannot detect host cwd; pass ?directory= in hook script)"
        )
        return results
    caller_dir = directory.strip().rstrip("/")
    return [r for r in results if is_directory_eligible(r.get("directory_context"), caller_dir)]


@mcp_server.custom_route("/hooks/prompt-recall", methods=["GET"])
@trace_span()
async def hook_prompt_recall(request: Request) -> JSONResponse:
    """Return auto-recall markdown for UserPromptSubmit hook (daemon mode).

    Query params: query, directory (optional)
    Returns: {"text": "...markdown..."}
    """
    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    _observed = False
    try:
        query = request.query_params.get("query", "")
        # v5.65 Fix D: do NOT fall back to os.getcwd() — daemon runs in a container;
        # container cwd would mis-scope retriever results.  directory may be None if
        # hook script does not pass ?directory=; handled by _filter_prompt_recall_results.
        directory = request.query_params.get("directory") or None

        if not query or len(query) < 2:
            return JSONResponse({"text": ""})

        # Throttle: skip if session-context ran < 3 min ago (already loaded context)
        now = time.monotonic()
        throttle_key = directory or ""
        if now - _st._last_session_context.get(throttle_key, 0) < 180:
            return JSONResponse({"text": "", "skipped": "session_context_recent"})
        # Throttle: max 1 recall per 2 minutes per directory
        if now - _st._last_prompt_recall.get(throttle_key, 0) < 120:
            return JSONResponse({"text": "", "skipped": "rate_limited"})

        try:
            # v5.113.0 (#166) forwarded this hook; ADR-0078 now forwards ALL THREE
            # hook sites — hooks are HTTP forwards only, no core DB path remains
            # (the old in-core short-circuit and the directory-less in-core
            # fallback are deleted). A directory-less request forwards with "" —
            # the backend treats an empty scope directory as legacy whole-DB
            # eligibility, preserving the old fallback's semantics server-side.
            #
            # _HookRecallForwarder exposes a .recall(...) surface so we reuse
            # _recall_with_timeout VERBATIM: the bounded hook-recall pool
            # (#81 freeze fix; ADR-0077: 2 workers so a concurrent session pair
            # no longer serializes into a timeout) + asyncio.wait_for guard + the
            # None-on-timeout degradation are all preserved. The httpx timeout
            # inside the forward is ALSO HOOK_RECALL_TIMEOUT_S, and the forward
            # carries deadline_ms so the backend aborts stages once this client
            # has given up (ADR-0077).
            # profile="fast": backend runs memory-only BM25+HNSW+fusion
            # (no CE/NLI/MP, no wiki fanout, no engram links — ADR-0077).
            results = await _recall_with_timeout(
                _HookRecallForwarder(directory or ""),
                "prompt-recall",
                query,
                max_results=5,
                min_heat=0.0,
                profile="fast",
            )
        except Exception as e:
            logger.debug("prompt-recall hook error: %s", e)
            _hook_observe("prompt_recall", _t0, e)
            _observed = True
            return JSONResponse({"text": ""})
        if results is None:
            return JSONResponse({"text": ""})

        # v5.65 Fix D: directory post-filter. The backend already scopes with the
        # SAME is_directory_eligible predicate, so this is idempotent on forwarded
        # rows — kept as the defense-in-depth contract (#166 Trap 2).
        results = _filter_prompt_recall_results(results, directory)

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

        _bounded_set(_st._last_prompt_recall, throttle_key, time.monotonic())
        return JSONResponse({"text": "\n".join(lines)})
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        if not _observed:
            _hook_observe("prompt_recall", _t0, _caught_exc)


@mcp_server.custom_route("/hooks/subagent-stop", methods=["POST"])
@trace_span()
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
    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    try:
        try:
            body = await request.json()
        except Exception:
            _resp = JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)
            _hook_observe_response("subagent_stop", _resp.status_code)
            return _resp

        agent_type = sanitize_log_field(str(body.get("agent_type", "general-purpose")), max_len=64)
        cwd = sanitize_log_field(str(body.get("cwd", os.getcwd())), max_len=500)
        findings = body.get("findings", [])

        if not isinstance(findings, list):
            _resp = JSONResponse(
                {"status": "error", "message": "findings must be a list"}, status_code=400
            )
            _hook_observe_response("subagent_stop", _resp.status_code)
            return _resp

        # Validate agent_type before use as provenance_agent
        import re as _re

        _AGENT_TYPE_RE = _re.compile(r"^[A-Za-z0-9_-]{1,64}$")
        if not agent_type or not _AGENT_TYPE_RE.match(agent_type):
            agent_type = "general-purpose"

        if not findings:
            return JSONResponse({"status": "ok", "stored": 0})

        # Import memorize at call time to avoid circular import at module load
        import sys as _sys

        _srv = _sys.modules.get("yadgar.core.server")
        _memorize = getattr(_srv, "memorize", None) if _srv else None
        if _memorize is None:
            from yadgar.core.server.tools.memorize import memorize as _memorize  # noqa: PLC0415

        tags = ["from-subagent", f"agent-type:{agent_type}"]
        stored = 0
        errors = []

        # v5.42.3: detect branch for internal path  # _internal-only
        _branch_hint: str | None = None
        try:
            import yadgar.core.server as _srv_mod  # noqa: PLC0415

            _branch_hint = await asyncio.to_thread(_srv_mod._detect_branch, cwd)
        except Exception:
            pass

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
                    branch_hint=_branch_hint,
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
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        _hook_observe("subagent_stop", _t0, _caught_exc)


@mcp_server.custom_route("/hooks/seed-anchor", methods=["POST"])
@trace_span()
async def hook_seed_anchor(request: Request) -> JSONResponse:
    """Seed a single protected anchor into memory (v5.46.15).

    Called by `yadgar seed --anchors` CLI to write canonical anchors via the
    daemon rather than in-process SQLite (dead pre-SurrealDB path removed).

    Accepts JSON body:
        {
            "content": "anchor text",
            "tags": ["_anchor", "..."],
            "is_protected": true,
            "context": "/path/to/project"
        }

    Response:
        {"status": "ok", "created": 1}   — new anchor stored
        {"status": "ok", "created": 0}   — deduped by similarity gate (skipped)
        {"status": "error", ...}          — on validation failure (400)
    """
    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    try:
        try:
            body = await request.json()
        except Exception:
            _resp = JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)
            _hook_observe_response("seed_anchor", _resp.status_code)
            return _resp

        content = sanitize_log_field(str(body.get("content", "")), max_len=10000)
        if not content:
            _resp = JSONResponse(
                {"status": "error", "message": "content is required"}, status_code=400
            )
            _hook_observe_response("seed_anchor", _resp.status_code)
            return _resp

        raw_tags = body.get("tags", [])
        if not isinstance(raw_tags, list):
            raw_tags = []
        tags = [sanitize_log_field(str(t), max_len=200) for t in raw_tags if t]
        if "_anchor" not in tags:
            tags.append("_anchor")

        is_protected = bool(body.get("is_protected", True))
        context = sanitize_log_field(str(body.get("context", os.getcwd())), max_len=500)

        import sys as _sys

        _srv = _sys.modules.get("yadgar.core.server")
        _memorize = getattr(_srv, "memorize", None) if _srv else None
        if _memorize is None:
            from yadgar.core.server.tools.memorize import memorize as _memorize  # noqa: PLC0415

        result = await asyncio.to_thread(
            _memorize,
            content=content,
            context=context,
            tags=tags,
            is_protected=is_protected,
            tier="conditional",
        )

        created = 0
        if isinstance(result, dict):
            status = result.get("status", "")
            if status in ("stored", "created", "ok"):
                created = 1
            elif status in ("duplicate", "skipped", "deduped"):
                created = 0
            else:
                # Treat any non-error response as stored
                created = 1 if "error" not in str(status).lower() else 0

        return JSONResponse({"status": "ok", "created": created})
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        _hook_observe("seed_anchor", _t0, _caught_exc)


@mcp_server.custom_route("/hooks/seed-agent-prompts", methods=["POST"])
@trace_span()
async def hook_seed_agent_prompts(request: Request) -> JSONResponse:
    """Seed the 5 built-in starter agent-prompts via daemon (v5.85 S8).

    Called by `yadgar seed --agent-prompts` CLI after daemon start.
    Accepts an empty POST body (no required fields).

    Response:
        {"status": "ok", "created": N, "skipped": M}
    """
    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    try:
        import sys as _sys  # noqa: PLC0415

        _srv = _sys.modules.get("yadgar.core.server.tools.agent_prompts")
        _seed_fn = getattr(_srv, "seed_agent_prompts", None) if _srv else None
        if _seed_fn is None:
            from yadgar.core.server.tools.agent_prompts import (
                seed_agent_prompts as _seed_fn,  # noqa: PLC0415
            )

        result = await asyncio.to_thread(_seed_fn)
        created = result.get("created", 0)
        skipped = result.get("skipped", 0)
        return JSONResponse({"status": "ok", "created": created, "skipped": skipped})
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        _hook_observe("seed_agent_prompts", _t0, _caught_exc)


@mcp_server.custom_route("/hooks/file-changed", methods=["POST"])
@trace_span()
async def hook_file_changed(request: Request) -> JSONResponse:
    """FileChanged hook endpoint — mirrors team_inbox JSONL and PLAN_*.md changes.

    Called by yadgar/hooks/file-changed.py when Claude Code fires FileChanged.

    Query params:
        path: URL-encoded absolute path of the changed file (from hook script)
    Body (JSON):
        {
            "file_path": "/absolute/path/to/file",
            "file_action": "created" | "modified"
        }

    Dispatch:
      - team_inbox/**/*.jsonl → read new JSONL lines, write action_log per message
      - docs/plans/<slug>.md  → read file content, memorize with _plan tag (excl. archive/)
      - other paths           → 200 OK no-op (forward-compat)
    """
    import re as _re
    import urllib.parse as _urlparse

    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}

        # Accept path from query param OR body (hook script sends both)
        file_path = request.query_params.get("path", "") or body.get("file_path", "")
        if file_path:
            try:
                file_path = _urlparse.unquote(file_path)
            except Exception:
                pass
        body.get("file_action", "modified")

        if not file_path:
            _resp = JSONResponse(
                {"status": "error", "message": "missing file_path"}, status_code=400
            )
            _hook_observe_response("file_changed", _resp.status_code)
            return _resp

        storage = _st._storage
        if storage is None:
            _resp = JSONResponse(
                {"status": "error", "message": "Storage not initialized"}, status_code=503
            )
            _hook_observe_response("file_changed", _resp.status_code)
            return _resp

        # ── team_inbox filter ───────────────────────────────────────────────────
        _TEAM_INBOX_RE = _re.compile(
            r"[/\\]\.claude[/\\]team_inbox[/\\]([^/\\]+)[/\\]([^/\\]+)[/\\]([^/\\]+)\.jsonl$"
        )
        _PLAN_FILE_RE = _re.compile(r"[/\\]docs[/\\]plans[/\\]([^/\\]+\.md)$")

        inbox_match = _TEAM_INBOX_RE.search(file_path)
        plan_match = _PLAN_FILE_RE.search(file_path)

        if inbox_match:
            return await _handle_team_inbox(file_path, inbox_match, storage)
        elif plan_match:
            return await _handle_plan_file(file_path, plan_match, storage)
        else:
            # Unknown path — no-op, forward-compat
            return JSONResponse({"status": "skipped", "reason": "path_not_watched"})
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        _hook_observe("file_changed", _t0, _caught_exc)


@trace_span()
async def _handle_team_inbox(file_path: str, match, storage) -> JSONResponse:
    """Read new JSONL lines from a team_inbox file and write action_log entries."""
    import asyncio as _asyncio
    from datetime import UTC, datetime

    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    try:
        project_id = match.group(1)
        team_name = match.group(2)
        agent_name = match.group(3)

        from pathlib import Path as _Path

        p = _Path(file_path)
        if not p.exists():
            return JSONResponse({"status": "skipped", "reason": "file_not_found"})

        # Track file position to only read NEW lines since last call
        current_pos = _st._team_inbox_positions.get(file_path, 0)

        new_lines = []
        try:
            with p.open("r", encoding="utf-8", errors="ignore") as fh:
                fh.seek(current_pos)
                new_lines = fh.readlines()
                new_pos = fh.tell()
        except Exception as _e:
            logger.debug("team_inbox read error %s: %s", file_path, _e)
            _resp = JSONResponse({"status": "error", "message": str(_e)[:100]}, status_code=500)
            _hook_observe_response("team_inbox", _resp.status_code)
            return _resp

        # Update position — cap dict to 10_000 entries
        _st._team_inbox_positions[file_path] = new_pos
        if len(_st._team_inbox_positions) > 10_000:
            # Evict oldest entry
            _st._team_inbox_positions.popitem(last=False)

        stored = 0
        skipped = 0
        ts = datetime.now(UTC).isoformat()

        for raw_line in new_lines:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                msg = json.loads(raw_line)
            except json.JSONDecodeError as _jde:
                from yadgar._shared.observability.exception_telemetry import (
                    record_exception,  # noqa: PLC0415
                )

                record_exception("server.http.team_inbox", _jde)
                logger.warning("team_inbox malformed JSONL in %s — skipping line", file_path)
                skipped += 1
                continue

            msg.get("subagent_type") or msg.get("agent_type") or "unknown"
            content_snippet = str(msg.get("content") or msg.get("text") or msg.get("message") or "")
            summary = (
                content_snippet[:200] if content_snippet else f"team_message from {agent_name}"
            )

            try:
                # T2 Car E1 (ADR-0078): team-inbox rows ride the file-queue seam
                # (backend drainer replays via run_action_log_replay).
                from yadgar.core.lifecycle import _get_file_queue  # noqa: PLC0415

                await _asyncio.to_thread(
                    _get_file_queue().enqueue,
                    "action_log",
                    {
                        "tool_name": "team_message",
                        "summary": sanitize_log_field(summary, max_len=500),
                        "directory": sanitize_log_field(file_path, max_len=500),
                        "session_id": sanitize_log_field(
                            f"team:{project_id}/{team_name}/{agent_name}", max_len=100
                        ),
                        "timestamp": ts,
                    },
                )
                stored += 1
            except Exception as _e:
                logger.debug("team_inbox action_log insert failed: %s", _e)
                skipped += 1

        return JSONResponse(
            {
                "status": "ok",
                "stored": stored,
                "skipped": skipped,
                "new_lines": len(new_lines),
            }
        )
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        _hook_observe("team_inbox", _t0, _caught_exc)


@trace_span()
async def _handle_plan_file(file_path: str, match, storage) -> JSONResponse:
    """Read plan-file content (docs/plans/<slug>.md) and memorize with _plan tag (hash-dedup)."""
    import asyncio as _asyncio
    import hashlib as _hashlib
    from pathlib import Path as _Path

    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    try:
        p = _Path(file_path)
        if not p.exists():
            return JSONResponse({"status": "skipped", "reason": "file_not_found"})

        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as _e:
            logger.debug("PLAN file read error %s: %s", file_path, _e)
            _resp = JSONResponse({"status": "error", "message": str(_e)[:100]}, status_code=500)
            _hook_observe_response("plan_file", _resp.status_code)
            return _resp

        if not content.strip():
            return JSONResponse({"status": "skipped", "reason": "empty_file"})

        # Hash-dedup — skip if content unchanged since last memorize
        file_hash = _hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
        if _st._plan_file_hashes.get(file_path) == file_hash:
            return JSONResponse({"status": "skipped", "reason": "unchanged"})

        _st._plan_file_hashes[file_path] = file_hash

        # Attempt to capture current git commit ref for provenance
        git_ref = ""
        try:
            import subprocess as _sp

            _git_args = ["git", "-C", str(p.parent.parent), "rev-parse", "--short", "HEAD"]
            result = await _asyncio.to_thread(
                lambda: _sp.run(_git_args, capture_output=True, text=True, timeout=3)
            )
            if result.returncode == 0:
                git_ref = result.stdout.strip()
        except Exception:
            pass

        filename = match.group(1)
        snippet = content[:800].strip()
        memo_content = (
            f"PLAN file {filename} (git:{git_ref}):\n{snippet}"
            if git_ref
            else f"PLAN file {filename}:\n{snippet}"
        )

        import sys as _sys

        _srv = _sys.modules.get("yadgar.core.server")
        _memorize = getattr(_srv, "memorize", None) if _srv else None
        if _memorize is None:
            from yadgar.core.server.tools.memorize import memorize as _memorize  # noqa: PLC0415

        # v5.42.3: detect branch for internal path  # _internal-only
        _plan_branch_hint: str | None = None
        try:
            import yadgar.core.server as _srv_mod2  # noqa: PLC0415

            _plan_branch_hint = await _asyncio.to_thread(_srv_mod2._detect_branch, str(p.parent))
        except Exception:
            pass

        try:
            result = await _asyncio.to_thread(
                _memorize,
                content=memo_content,
                context=str(p.parent),
                tags=["_plan", "plan-file"],
                is_protected=False,
                branch_hint=_plan_branch_hint,
            )
            return JSONResponse(
                {"status": "ok", "memorized": True, "file": filename, "git_ref": git_ref}
            )
        except Exception as _e:
            logger.debug("PLAN memorize failed for %s: %s", file_path, _e)
            _resp = JSONResponse({"status": "error", "message": str(_e)[:100]}, status_code=500)
            _hook_observe_response("plan_file", _resp.status_code)
            return _resp
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        _hook_observe("plan_file", _t0, _caught_exc)


@mcp_server.custom_route("/hooks/instructions-loaded", methods=["GET"])
@trace_span()
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
    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    _observed = False
    try:
        file_path = request.query_params.get("file_path", "")
        load_reason = request.query_params.get("load_reason", "")

        # Build a query from the filename + load_reason for relevant memories
        import pathlib as _pathlib

        filename = _pathlib.Path(file_path).name if file_path else "CLAUDE.md"
        query = f"{filename} {load_reason} instructions context".strip()

        try:
            # ADR-0078: forwards to the backend /recall path like its siblings —
            # no core DB path remains. This hook has NO caller directory (only
            # file_path/load_reason), so it forwards with directory "": the
            # backend treats an empty scope directory as legacy whole-DB
            # eligibility, preserving this hook's historical unscoped behavior.
            # profile="fast": memory-only BM25+HNSW+fusion (ADR-0077).
            # v5.51.0: wrapped in _recall_with_timeout (asyncio.wait_for) to bound latency.
            # On timeout, _recall_with_timeout returns None (logs WARN + increments counter).
            results = await _recall_with_timeout(
                _HookRecallForwarder(""),
                "instructions-loaded",
                query,
                max_results=3,
                min_heat=0.0,
                profile="fast",
            )
        except Exception as _e:
            logger.debug("instructions-loaded hook recall error: %s", _e)
            _hook_observe("instructions_loaded", _t0, _e)
            _observed = True
            return JSONResponse({"text": ""})
        if results is None:
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
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        if not _observed:
            _hook_observe("instructions_loaded", _t0, _caught_exc)


@mcp_server.custom_route("/hooks/subagent-start", methods=["POST"])
@trace_span()
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
    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    _observed = False
    try:
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

        # P11: count the dispatch now that we know the agent_type is valid.
        try:
            from yadgar._shared.observability.metrics import (
                yadgar_subagent_dispatch_count,  # noqa: PLC0415
            )

            yadgar_subagent_dispatch_count.labels(agent_type=agent_type).inc()
        except Exception:
            pass

        # Use description as primary query; fall back to agent_type if empty
        query = description.strip() or f"agent {agent_type}"

        try:
            # ADR-0078: forwards to the backend /recall path like its siblings —
            # no core DB path remains. Forwards bound to the subagent's cwd: the
            # backend scopes server-side (is_directory_eligible). NAMED behavior
            # shift accepted by ADR-0078: this hook used to run whole-DB unscoped;
            # it is now directory-scoped to the dispatching project (+ global).
            # profile="fast": memory-only BM25+HNSW+fusion (ADR-0077).
            # v5.51.0: wrapped in _recall_with_timeout (asyncio.wait_for) to bound latency.
            # On timeout, _recall_with_timeout returns None (logs WARN + increments counter).
            results = await _recall_with_timeout(
                _HookRecallForwarder(cwd or ""),
                "subagent-start",
                query,
                max_results=5,
                min_heat=0.0,
                profile="fast",
            )
        except Exception as _e:
            logger.debug("subagent-start hook recall error: %s", _e)
            _hook_observe("subagent_start", _t0, _e)
            _observed = True
            return JSONResponse({"text": ""})
        # None (timeout) or empty list → no context to inject
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
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        if not _observed:
            _hook_observe("subagent_start", _t0, _caught_exc)


@mcp_server.custom_route("/api/graph", methods=["GET"])
@trace_span()
async def api_graph(request: Request) -> JSONResponse:
    """Return full knowledge graph (nodes + edges) for visualization."""
    _t0_hook = time.perf_counter()
    _caught_exc: BaseException | None = None
    try:
        if _st._storage is None:
            _resp = JSONResponse({"nodes": [], "edges": []}, status_code=503)
            _hook_observe_response("api_graph", _resp.status_code)
            return _resp
        from yadgar._shared.config import get_settings  # noqa: PLC0415

        _cfg = get_settings()
        # Caps are configurable knobs (VIZ_MAX_*); query params override per-request.
        # 0 or -1 = unlimited (graph_api omits the LIMIT / skips the entity slice).
        try:
            max_mem = int(request.query_params.get("max_memories", _cfg.VIZ_MAX_MEMORIES))
        except (ValueError, TypeError) as _e:
            max_mem = _cfg.VIZ_MAX_MEMORIES
        try:
            top_k = int(request.query_params.get("top_k", 8))
        except (ValueError, TypeError) as _e:
            top_k = 8
        try:
            max_wiki = int(request.query_params.get("max_wiki", _cfg.VIZ_MAX_WIKI))
        except (ValueError, TypeError) as _e:
            max_wiki = _cfg.VIZ_MAX_WIKI
        try:
            max_entities = int(request.query_params.get("max_entities", _cfg.VIZ_MAX_ENTITIES))
        except (ValueError, TypeError) as _e:
            max_entities = _cfg.VIZ_MAX_ENTITIES
        _t0 = time.time()
        # T2 Car E3: the assembly (+ cached-layout attach) runs backend-side.
        from yadgar.core.server.tools._forward import _forward_viz  # noqa: PLC0415

        data = await asyncio.to_thread(
            _forward_viz,
            "graph",
            {
                "max_memories": max_mem,
                "top_k": top_k,
                "max_wiki": max_wiki,
                "max_entities": max_entities,
            },
        )
        _elapsed_ms = (time.time() - _t0) * 1000.0
        try:
            from yadgar._shared.observability.metrics import (
                yadgar_viz_api_graph_duration_ms,  # noqa: PLC0415
            )

            yadgar_viz_api_graph_duration_ms.observe(_elapsed_ms)
        except Exception:
            pass
        return JSONResponse(data, headers=_CORS)
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        _hook_observe("api_graph", _t0_hook, _caught_exc)


@mcp_server.custom_route("/api/stats", methods=["GET"])
@trace_span()
async def api_stats(request: Request) -> JSONResponse:
    """Return memory statistics as JSON (used by `yadgar stats` CLI when daemon is running).

    v5.51.0: TTL-cached to avoid live get_memory_stats on every request.
    Cache TTL = STATS_CACHE_TTL_S (default 5s). 0 = disabled (recompute every request).
    Cache is keyed by project param. Response includes cache_age_seconds.
    """
    if _st._storage is None:
        return JSONResponse({}, status_code=503)
    project = request.query_params.get("project")

    from yadgar._shared.config import get_settings  # noqa: PLC0415

    ttl_s = get_settings().STATS_CACHE_TTL_S
    now = time.monotonic()

    # Check cache
    if ttl_s > 0:
        cached_project = _stats_cache.get("project")
        cached_at = _stats_cache.get("cached_at")
        if cached_at is not None and cached_project == project and (now - cached_at) < ttl_s:
            # Cache hit — copy to avoid mutating cached entry
            response_data = dict(_stats_cache["data"])
            response_data["cache_age_seconds"] = round(now - cached_at, 3)
            if project:
                response_data["project_filter"] = project
            return JSONResponse(response_data, headers=_CORS)

    # Cache miss — recompute
    data = await asyncio.to_thread(_st._storage.get_memory_stats)

    if ttl_s > 0:
        _stats_cache["data"] = data
        _stats_cache["cached_at"] = now
        _stats_cache["project"] = project

    response_data = dict(data)
    response_data["cache_age_seconds"] = 0
    if project:
        response_data["project_filter"] = project
    return JSONResponse(response_data, headers=_CORS)


@mcp_server.custom_route("/api/graph/stats", methods=["GET"])
@trace_span()
async def api_graph_stats(request: Request) -> JSONResponse:
    """Return graph statistics: counts + top entities by heat."""
    if _st._storage is None:
        return JSONResponse({}, status_code=503)
    # T2 Car E3: assembly runs backend-side.
    from yadgar.core.server.tools._forward import _forward_viz  # noqa: PLC0415

    data = await asyncio.to_thread(_forward_viz, "graph_stats", {})
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/graph/edges", methods=["GET"])
@trace_span()
async def api_graph_edges_lazy(request: Request) -> JSONResponse:
    """On-demand edge computation for lazy edge types (v5.54.3).

    Query params:
      type: edge type to compute (e.g. 'semantic'). Must be in LAZY_EDGE_TYPES.
      max_memories: limit for memory nodes (default 500).
      top_k: top-K neighbours per node for KNN (default 8).

    Returns {"edges": [...]} — no nodes. The frontend merges into existing graphData.
    Semantic edges are O(n²) KNN — not in the default /api/graph payload.
    """
    if _st._storage is None:
        return JSONResponse({"edges": []}, status_code=503)
    edge_type = request.query_params.get("type", "")
    if not edge_type:
        return JSONResponse(
            {"edges": [], "error": "Missing required query param: type"},
            status_code=400,
            headers=_CORS,
        )
    try:
        max_mem = int(request.query_params.get("max_memories", 500))
    except (ValueError, TypeError):  # fmt: skip
        max_mem = 500
    try:
        top_k = int(request.query_params.get("top_k", 8))
    except (ValueError, TypeError):  # fmt: skip
        top_k = 8
    # T2 Car E3: assembly runs backend-side.
    from yadgar.core.server.tools._forward import _forward_viz  # noqa: PLC0415

    data = await asyncio.to_thread(
        _forward_viz,
        "graph_edges",
        {"edge_type": edge_type, "max_memories": max_mem, "top_k": top_k},
    )
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/graph/neighborhood/{node_id}", methods=["GET"])
@trace_span()
async def api_graph_neighborhood(request: Request) -> JSONResponse:
    """Return 1–2 hop subgraph around a node."""
    if _st._storage is None:
        return JSONResponse({"nodes": [], "edges": []}, status_code=503)
    node_id = request.path_params.get("node_id", "")
    try:
        hops = int(request.query_params.get("hops", 2))
    except (ValueError, TypeError) as _e:
        hops = 2
    # T2 Car E3: assembly runs backend-side.
    from yadgar.core.server.tools._forward import _forward_viz  # noqa: PLC0415

    data = await asyncio.to_thread(
        _forward_viz, "graph_neighborhood", {"node_id": node_id, "hops": hops}
    )
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/system", methods=["GET"])
@trace_span()
async def api_system(request: Request) -> JSONResponse:
    """Return current system and process metrics."""
    # §9 Q6: snapshot under lock before serialising to avoid torn reads.
    with _st._metrics_lock:
        snapshot = dict(_st._system_metrics_cache)
    # Add daemon uptime (not sampled by background thread — compute live from start time)
    snapshot["uptime_s"] = round(time.time() - _st._start_time, 1) if _st._start_time else None
    return JSONResponse(snapshot, headers=_CORS)


@mcp_server.custom_route("/api/info", methods=["GET"])
@trace_span()
async def api_info(request: Request) -> JSONResponse:
    """Return version and Python runtime info for the viz Info tab."""
    import sys as _sys  # noqa: PLC0415

    return JSONResponse(
        {"version": __version__, "python_version": _sys.version.split()[0]},
        headers=_CORS,
    )


@mcp_server.custom_route("/api/metrics/heat-histogram", methods=["GET"])
@trace_span()
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
@trace_span()
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
                "memories_archived, memories_deleted, memify_pruned, "
                "cls_promoted, duration_ms "
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
                "pruned": int(r.get("memify_pruned") or 0),
                "promoted": int(r.get("cls_promoted") or 0),
                "duration_ms": int(r.get("duration_ms") or 0),
            }
            for r in rows
        ]

    data = await asyncio.to_thread(_fetch)
    return JSONResponse(data, headers=_CORS)


@observe(
    exempt="async generator (SSE event stream); @observe sync-wraps and would fire the signal at generator creation not exhaustion"
)
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
    last_health_push = 0.0
    client_id = id(request)

    # P11: SSE client gauge — inc on entry, dec on any exit path.
    try:
        from yadgar._shared.observability.metrics import (
            yadgar_viz_sse_clients as _sse_g,  # noqa: PLC0415
        )

        _sse_g.inc()
    except Exception:
        _sse_g = None  # type: ignore[assignment]
    try:
        while True:
            # PR-I: heartbeat (Option A) — shared gauge updated by most-recent active client.
            # Per-client heartbeat would explode label cardinality; single label tracks that
            # at least one SSE client iteration is alive.
            try:
                from yadgar._shared.observability.metrics import (
                    loop_heartbeat as _lhb,  # noqa: PLC0415
                )

                _lhb("sse_event_stream")
            except Exception:  # noqa: BLE001
                pass

            # Exit cleanly if the client disconnected before we yield anything.
            if await request.is_disconnected():
                logger.debug("SSE client %s disconnected; closing stream", client_id)
                return

            now = time.time()
            try:
                # Drain new graph events
                with _st._event_lock:
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
                # Push daemon health every 5 s — V1c.
                if now - last_health_push >= 5.0 and _vdh._health_cache is not None:
                    last_health_push = now
                    yield f"data: {json.dumps({'event': 'daemon_health', 'data': _vdh._health_cache})}\n\n"
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
    finally:
        try:
            if _sse_g is not None:
                _sse_g.dec()
        except Exception:
            pass


@mcp_server.custom_route("/api/graph/events", methods=["GET"])
@trace_span()
async def api_graph_events(request: Request) -> StreamingResponse:
    """SSE stream of incremental graph update events + system metrics every 5s."""
    headers = {**_CORS, "Content-Type": "text/event-stream", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        _make_event_stream(request), media_type="text/event-stream", headers=headers
    )


@mcp_server.custom_route("/api/wiki/read", methods=["GET"])
@trace_span()
async def api_wiki_read(request: Request) -> JSONResponse:
    """Read a single wiki page by slug for the viz detail panel.

    GET /api/wiki/read?slug=<slug>

    Returns {slug, title, content, category, tags, updated_at} or 404.
    """
    slug = (request.query_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "slug required"}, status_code=400, headers=_CORS)
    wiki = _st._wiki
    if wiki is None:
        return JSONResponse({"error": "wiki not initialized"}, status_code=503, headers=_CORS)
    try:
        page = await asyncio.to_thread(wiki.read, slug)
    except Exception as _exc:
        logger.debug("api_wiki_read error for slug=%s: %s", slug, _exc)
        return JSONResponse({"error": str(_exc)}, status_code=500, headers=_CORS)
    if page is None:
        return JSONResponse({"error": "not found"}, status_code=404, headers=_CORS)
    return JSONResponse(
        {
            "slug": page.get("slug", slug),
            "title": page.get("title", ""),
            "content": page.get("content", ""),
            "category": page.get("category", ""),
            "tags": page.get("tags") or [],
            "updated_at": str(page.get("updated_at") or ""),
        },
        headers=_CORS,
    )


@observe(tier="stage")
async def _viz_exact_title_node_ids(q: str) -> list[str]:
    """Resolve memories whose content exactly/prefix-matches `q` → ['mem:<id>', ...].

    P0.2 (viz-fix-plan-2026-06-27): api_viz_search routes through recall()
    (WRRF-ranked, capped at top-5), so a memory whose content EXACTLY (or by
    prefix) matches the query can drop out of the top-5 and never highlight —
    the user searches a title and the wrong node lights up. These exact matches
    are PREPENDED to the search result so they always win, independent of WRRF
    ranking. Memories have no `title` column; the viz node label is content[:60],
    so "title" == leading content here. Whole-DB by design (BC-VZ2).

    Extracted from api_viz_search to keep that handler under the I30/C901
    complexity caps. Storage/DB errors are swallowed (best-effort precedence).
    """
    storage = _st._storage
    if storage is None:
        return []
    node_ids: list[str] = []
    try:
        rows = await asyncio.to_thread(
            storage._q,
            "SELECT id, content FROM memory "
            "WHERE string::lowercase(content) = string::lowercase($q) "
            "OR string::starts_with(string::lowercase(content), string::lowercase($q)) "
            "ORDER BY heat DESC LIMIT 20",
            {"q": q},
        )
    except Exception as _exc:
        logger.debug("viz_search exact-title error: %s", _exc)
        return []
    # Guard: only iterate a real list (mocked/None storage → skip).
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_id = _extract_record_id(row.get("id"))
        if raw_id is not None:
            node_ids.append(f"mem:{raw_id}")
    return node_ids


@mcp_server.custom_route("/api/viz/search", methods=["GET"])
@trace_span()
async def api_viz_search(request: Request) -> JSONResponse:
    """Semantic search for viz graph: return node IDs matching query.

    GET /api/viz/search?q=<query>

    Dispatches recall() + wiki_query() (capped at 5 each) and returns
    matching node IDs so the frontend can pin/highlight them in the graph.

    Response: {"node_ids": ["mem:42", "wiki:7", ...], "query": "<q>"}
    """
    _t0 = time.perf_counter()
    _caught_exc: BaseException | None = None
    try:
        q = (request.query_params.get("q") or "").strip()
        if not q:
            return JSONResponse({"node_ids": [], "query": ""}, headers=_CORS)

        # P0.2 — exact/prefix-title matches first (extracted to keep this handler
        # under the I30/C901 complexity cap). These PREPEND ahead of WRRF recall
        # so an exact-title node can't be displaced out of recall's top-5.
        node_ids: list[str] = await _viz_exact_title_node_ids(q)

        # BC-VZ2 — INTENTIONAL whole-DB (unscoped) search for the viz god's-eye overlay.
        #
        # The viz is a localhost, auth-gated admin tool that renders ALL projects' nodes
        # in a single graph.  Search-highlight must find any node visible in that graph
        # regardless of which project directory it belongs to — scoping the query to a
        # single directory would silently exclude nodes that are already drawn on screen,
        # breaking the UX.
        #
        # Mechanism: Retriever.recall() has no `directory` parameter (whole-DB by design);
        # WikiStore.query() also has no `directory` parameter.  Neither call passes one.
        # This is NOT a BC-B3 violation: BC-B3's directory-scoping requirement lives at the
        # MCP-tool layer (the scoped `recall` / `wiki_query` MCP tools), not at the
        # in-process method level used here.  Cross-project exposure is acceptable here
        # because this endpoint is localhost-only and admin-gated.
        #
        # Do NOT add a directory= filter here without revisiting BC-VZ2 first.

        # Memory recall — T2 Car E2: forwarded to the backend /recall like the
        # hook siblings (retrieval sank to the backend; _st._retriever is None
        # in the core process). _HookRecallForwarder("") = whole-DB eligibility
        # (empty scope directory) — exactly the BC-VZ2 god's-eye semantics.
        # v5.25.3: lightweight "fast" profile (BM25+HNSW only, no CE/NLI/MP) —
        # full rerank causes 2.5-10s CPU bursts; fast is sufficient for lookup.
        try:
            mem_results = await asyncio.to_thread(
                _HookRecallForwarder("").recall, q, max_results=5, min_heat=0.0, profile="fast"
            )
        except Exception as _exc:
            logger.debug("viz_search recall error: %s", _exc)
            mem_results = []
        for r in mem_results or []:
            raw_id = r.get("id")
            if raw_id is not None:
                try:
                    node_ids.append(f"mem:{int(raw_id)}")
                except (TypeError, ValueError):  # fmt: skip
                    pass

        # Wiki query — also whole-DB, same intentional bypass as recall above (BC-VZ2).
        wiki = _st._wiki
        if wiki is not None:
            try:
                wiki_results = await asyncio.to_thread(wiki.query, q, None, None, 5)
                for wp in wiki_results or []:
                    raw_id = wp.get("id")
                    if raw_id is not None:
                        # id may be a RecordID — extract numeric part
                        nid = _extract_record_id(raw_id)
                        if nid is not None:
                            node_ids.append(f"wiki:{nid}")
            except Exception as _exc:
                logger.debug("viz_search wiki_query error: %s", _exc)

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_ids: list[str] = []
        for nid in node_ids:
            if nid not in seen:
                seen.add(nid)
                unique_ids.append(nid)

        return JSONResponse({"node_ids": unique_ids, "query": q}, headers=_CORS)
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        _hook_observe("viz_search", _t0, _caught_exc)


@mcp_server.custom_route("/api/viz/config", methods=["GET"])
@trace_span()
async def api_viz_config(request: Request) -> JSONResponse:
    """Return active viz configuration as nested JSON.

    GET /api/viz/config

    All values come from Settings (config.yaml + env overrides).
    Frontend fetches this before graph init and applies to all viz constants.
    Fallback: if this endpoint is unreachable, frontend uses hardcoded defaults.

    Response: nested dict with keys: node, edge, physics, layout, search, legend.
    v5.50.13: added legend block; category_colors built by iterating CATEGORIES.
    """
    from yadgar._shared.config import get_settings  # noqa: PLC0415
    from yadgar.core.viz.viz_meta import (  # noqa: PLC0415
        build_category_colors,
        build_edge_colors,
        build_legend,
    )

    s = get_settings()
    category_colors = build_category_colors(s)
    edge_color = build_edge_colors(s)

    data = {
        "node": {
            "size_3d": s.VIZ_NODE_SIZE_3D,
            "size_2d": s.VIZ_NODE_SIZE_2D,
            # wiki_shape: config default only; mesh renderer deferred (see PLAN_V5_10_7_3)
            "wiki_shape": s.VIZ_WIKI_SHAPE,
            "category_colors": category_colors,
            "heat": {
                "hue_start": s.VIZ_HEAT_HUE_START,
                "hue_end": s.VIZ_HEAT_HUE_END,
                "sat_base": s.VIZ_HEAT_SAT_BASE,
                "sat_gain": s.VIZ_HEAT_SAT_GAIN,
                "light_base": s.VIZ_HEAT_LIGHT_BASE,
                "light_gain": s.VIZ_HEAT_LIGHT_GAIN,
            },
        },
        "edge": {
            "color": edge_color,
            "width_3d_multiplier": s.VIZ_EDGE_WIDTH_3D_MULTIPLIER,
            "arrow_len": s.VIZ_EDGE_ARROW_LEN,
            "opacity": s.VIZ_EDGE_OPACITY,
            "variant": s.VIZ_EDGE_VARIANT,
        },
        "physics": {
            "charge_strength": s.VIZ_PHYSICS_CHARGE_STRENGTH,
            "link_distance_2d": s.VIZ_PHYSICS_LINK_DISTANCE_2D,
            "link_distance_3d": s.VIZ_PHYSICS_LINK_DISTANCE_3D,
        },
        "layout": {
            "auto_zoom_fit_tick_threshold": s.VIZ_LAYOUT_ZOOM_FIT_TICK,
            "zoom_fit_padding": s.VIZ_LAYOUT_ZOOM_FIT_PADDING,
            "zoom_fit_transition_ms": s.VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS,
        },
        "search": {
            "match_color": s.VIZ_SEARCH_MATCH_COLOR,
            "pinned_color": s.VIZ_SEARCH_PINNED_COLOR,
            "dim_opacity": s.VIZ_SEARCH_DIM_OPACITY,
        },
        "legend": build_legend(s),
    }
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/graph", methods=["GET"])
@trace_span()
async def graph_view(request: Request) -> FileResponse:
    """3D memory force graph visualization."""
    static_dir = Path(__file__).parent.parent / "static"
    return FileResponse(static_dir / "graph.html")
