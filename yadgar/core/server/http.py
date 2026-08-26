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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

import yadgar._shared.paths as _paths
import yadgar._shared.runtime.state as _st
import yadgar.core.viz.viz_daemon_health as _vdh  # noqa: F401 — V1c: SSE daemon_health push
from yadgar import __version__
from yadgar._shared.config import resolve_knob
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar._shared.runtime.maintenance import apply_maintenance_health
from yadgar._shared.server_helpers import _push_event  # F2: re-stamp relayed backend events
from yadgar.core.forward import _forward_viz  # F2: poll backend /viz events op
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


@observe(exempt="thin resolver wrapper; no I/O of its own")
@observe(exempt="single query-param read with a falsy-to-None coercion; no I/O")
def _hook_query_project(request) -> str | None:
    """Read the hook's explicit ``?project=``, or ``None``.

    Car C7. A one-line read, extracted only because inlining it added a branch
    to two handlers that were already at the I30 cyclomatic cap. Kept as a named
    function rather than an allowlist entry so the cap keeps meaning something.
    """
    try:
        return request.query_params.get("project") or None
    except Exception:  # noqa: BLE001 — a hook must never raise on a param read
        return None


@trace_span()
@observe(tier="stage", metric="http.hook_project_id")
def hook_project_id(directory: str | None, project: str | None = None) -> str:
    """Resolve the hook caller's project id, or fail loud. NEVER guesses.

    Car C7. Hooks carry no session transport — the host-side mint module records
    that "MCP calls carry no session key… nothing to infer from" — so a project
    can only arrive as an explicit ``?project=`` query parameter. (That module is
    referred to indirectly on purpose: ADR-0227's reachability guard under
    ``tests/hooks/`` detects the layer breach TEXTUALLY, by scanning file
    contents rather than imports — so spelling the module's name anywhere in
    core-server prose trips the guard exactly as a real import would.)

    THE DIRECTORY CANNOT SUPPLY IT, and that is not an oversight in this
    function — C5 deleted the ``derive_project_id(cwd=…)`` tier from
    ``resolve_effective_project`` outright (ADR-0227: "a directory is not an
    identity — it is a filesystem hint that happened to be adjacent to one, and
    the process reading it cannot see the tree it names"). There is no
    derivation left anywhere in the tree to fall back to.

    Three cases, and the difference between the last two is the whole point:

    * **``project`` supplied** → use it. This is the path hook scripts should
      take; they run on the HOST and can read the git remote the container
      cannot.
    * **No project and no directory** → ``""``, i.e. EXPLICITLY UNSCOPED. Three
      call sites construct ``_HookRecallForwarder("")`` deliberately (the
      instructions-loaded hook and its siblings) and their documented contract
      is "an empty scope directory means legacy whole-DB eligibility mode".
      Preserved verbatim — an empty string is the caller stating it has no
      scope, not a guess at one.
    * **A directory but no project** → RAISE. This is the case C7 changes, and
      it degrades hook recall to an empty injection until the hook scripts send
      ``?project=``. That is deliberate: the alternative is forwarding
      unscoped, which would inject ANOTHER PROJECT'S memories into this
      project's prompt — precisely the leak v5.65 was written to close. Losing
      an injection is recoverable; leaking one is not. All five hook call sites
      wrap the forward in ``except Exception: return JSONResponse({"text": ""})``,
      so the raise degrades cleanly rather than breaking the prompt.
    """
    from yadgar.core.server.tools._project_param import (  # noqa: PLC0415
        resolve_effective_project,
    )

    _dir = (directory or "").strip().rstrip("/")
    if project is None and not _dir:
        return ""

    if project is None:
        logger.warning(
            "hook recall: directory=%r supplied but no project= — C7 scopes on "
            "project_id and C5 deleted directory derivation, so this recall "
            "cannot be scoped and will NOT be widened. Pass ?project=owner/repo "
            "from the hook script.",
            _dir,
        )

    return resolve_effective_project(
        project=project,
        directory=_dir or None,
        session_project=None,
        tool="hook_recall",
    )


@observe(tier="boundary", metric="http._forward_hook_recall")
def _forward_hook_recall(
    query: str,
    *,
    max_results: int,
    min_heat: float,
    directory: str,
    profile: str | None = "fast",
    project: str | None = None,
) -> list[dict]:
    """Forward a prompt-recall HOOK recall to the backend /recall endpoint.

    Reuses tools.recall._forward_to_backend (the SAME mechanism the MCP recall
    tool uses) so the forward is not duplicated, with a SHORT httpx timeout
    (HOOK_RECALL_TIMEOUT_S) so a hung backend cannot keep the hook's bounded-pool
    thread alive past its budget (#81 starvation guard).

    Runs synchronously — the caller (_recall_with_timeout) executes it in the
    bounded hook-recall pool under asyncio.wait_for. On backend error this raises
    (RuntimeError / httpx.HTTPError); the hook handler's except-block degrades to
    {"text": ""}. On timeout, wait_for returns None. Neither blocks the prompt.
    """
    from yadgar._shared.config import get_settings  # noqa: PLC0415
    from yadgar.core.server.tools.recall import _forward_to_backend  # noqa: PLC0415

    # Normalise directory before deriving the project — mirror recall.py's
    # `(directory or "").strip().rstrip("/")` so a trailing-slash cwd does not
    # silently derive a different project. (The deployed hook sends a clean cwd;
    # defensive.)
    directory = (directory or "").strip().rstrip("/")

    # Car C7 (0047 §5 C7): the backend's RecallRequest now REQUIRES project_id —
    # it is the scope key, and an absent value would mean an unscoped
    # corpus-wide read. Resolve it here from the hook's directory. A failure to
    # resolve raises, which the hook handler's except-block degrades to
    # {"text": ""} — the designed behaviour for a hook that cannot be scoped
    # (ADR-0227: fail loud rather than silently widen).
    _resolved_project = hook_project_id(directory, project)
    timeout_s = get_settings().HOOK_RECALL_TIMEOUT_S
    return _forward_to_backend(
        query=query,
        max_results=max_results,
        min_heat=min_heat,
        directory=directory,
        project_id=_resolved_project,
        # Car H1 (§1.3): ``hook_project_id`` returns "" ONLY from the explicit
        # "no project, no directory" case (see its docstring) — never as a
        # failed-resolution placeholder, which always raises instead. That is
        # the one place this fact is actually known, so the whole-corpus
        # intent is spelled here rather than re-derived from falsiness
        # downstream (the backend route, the clause builder's own callers).
        unscoped=(_resolved_project == ""),
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

    __slots__ = ("_directory", "_project")

    def __init__(self, directory: str, project: str | None = None) -> None:
        self._directory = directory
        # Car C7: the hook's explicit ``?project=``, when it sent one. See
        # ``hook_project_id`` — the directory can no longer supply an identity.
        self._project = project

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
            project=self._project,
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


class SentinelPermanentError(RuntimeError):
    """The sentinel write was REFUSED, not merely unavailable — retrying cannot help.

    Car F9. The distinction is the whole point of the class: ``RuntimeError``
    (daemon down, queue unwritable) is transient and earns the retry ladder; a
    REFUSAL — an unresolvable identity, a malformed override — is a property of
    the record itself, so replaying byte-identical input three more times just
    delays the same outcome by three session starts and buries it under
    WARNINGs. This is surfaced at ERROR and retired to ``failed/`` at once.
    """


@observe(tier="stage")
def _sentinel_memorize(content: str, project_id: str | None) -> dict:
    """Import one sentinel record into memory. Extracted for patching in tests.

    Car F9 (c1): ``project`` is supplied from the record's own minted identity.
    Before this, the call named no project at all, so C10 (f)'s identity
    contract refused EVERY sentinel write::

        memorize rejected sentinel: {'stored': False, 'error': 'unresolved_project',
                                     'fix': 'pass project="owner/repo"'}

    and no sentinel row was ever written. There is deliberately no derivation
    from the record's ``cwd`` here (ADR-0227): this process cannot see the tree
    that path names, and a key it invented would be indistinguishable from a
    real one at read time.

    ``context=`` is NOT passed: C10 (f) redefined it as an optional real FILE
    path used only for staleness hashing. The sentinel's ``cwd`` is a directory
    and contributes nothing but a misleading hash input.

    KNOWN RESIDUAL — the async rejection window. ``memorize`` defaults to
    ``wait=False``, so ``queued`` means the job reached the file queue, not that
    it was stored: the drainer re-validates the stamp — shape only, in
    ``QueueDrainer._validate_project_id`` (Car 5: the guard this used to name
    never ran) — and can DLQ an accepted job. F9 makes that window reachable — before it,
    every sentinel write was refused SYNCHRONOUSLY. It is deliberately not
    closed here: ``wait=True`` would block the SessionStart handler for up to
    ``WIKI_WRITE_WAIT_TIMEOUT_SECONDS`` (5s) behind a hook whose ``urlopen``
    timeout is 2s, trading an invisible rejection for a timed-out session
    start. A DLQ'd sentinel is not lost — the payload survives in the DLQ and
    ``project_brief``'s ``pending_rejections_count`` / ``review_rejections``
    action surfaces it — and the ``queue_id`` logged on consume is the thread
    back from a DLQ entry to the sentinel file that produced it.

    Returns:
        The ``memorize`` result envelope (the caller logs its ``queue_id``).

    Raises:
        SentinelPermanentError: the write was refused (identity or policy).
        RuntimeError: the write did not land for a transient reason.
    """
    import yadgar.core.server as _srv  # noqa: PLC0415

    if not project_id:
        raise SentinelPermanentError(
            "sentinel carries no project_id (error=unresolved_project): the SessionEnd "
            "hook could not mint an identity for this session and nothing downstream "
            "may derive one (ADR-0227)"
        )

    result = _srv.memorize(
        content=content,
        tags=["_session_end_sentinel", "session_end"],
        project=project_id,
    )
    if not result.get("stored") and not result.get("queued"):
        if result.get("error"):
            # An error envelope is a REFUSAL — the write was evaluated and
            # rejected. Never retried; surfaced verbatim so the reason is in
            # the log rather than inferred from a repeat count.
            raise SentinelPermanentError(f"memorize refused sentinel: {result}")
        raise RuntimeError(f"memorize rejected sentinel: {result}")
    return result


@observe(tier="stage")
def _sentinel_retire_to_failed(marker: Path, failed_dir: Path) -> None:
    """Move a sentinel out of the inbox into failed/. Never raises."""
    try:
        failed_dir.mkdir(parents=True, exist_ok=True)
        marker.rename(failed_dir / marker.name)
    except Exception as mv_e:
        logger.warning("sentinel move to failed/ error: %s", mv_e)


@observe(tier="stage")
def _sentinel_handle_failure(marker: Path, record: dict, retries: int, failed_dir: Path) -> None:
    """Handle a failed sentinel import: increment retries or move to failed/."""
    record["retries"] = retries
    if retries >= _SENTINEL_MAX_RETRIES:
        _sentinel_retire_to_failed(marker, failed_dir)
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
    - On a REFUSAL (SentinelPermanentError): logged at ERROR and moved to failed/
      immediately — a record the write path evaluated and rejected will be
      rejected identically on every replay (Car F9).
    - On a transient failure: retries field incremented; after
      _SENTINEL_MAX_RETRIES, moved to failed/.
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

        retries = int(record.get("retries", 0))

        try:
            _result = _sentinel_memorize(
                content=json.dumps(record),
                project_id=record.get("project_id"),
            )
            marker.unlink()  # consumed
            # Car F9: the marker is gone the moment the job is QUEUED, so the
            # queue_id is the only remaining link from a later DLQ entry back to
            # the sentinel file it came from. Logged at INFO for that trace.
            logger.info(
                "sentinel consumed: %s -> queue_id=%s",
                marker.name,
                (_result or {}).get("queue_id"),
            )
        except SentinelPermanentError as perm_e:
            # Car F9 observability: a refused write is a defect, not weather.
            # ERROR (not WARNING), the reason verbatim, and retired at once so
            # it stops presenting as "still pending, will converge".
            logger.error(
                "sentinel import REFUSED for %s — retiring to failed/ without retry: %s",
                marker,
                perm_e,
            )
            _sentinel_retire_to_failed(marker, failed_dir)
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
    from yadgar.core.forward import _forward_admin  # noqa: PLC0415

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
def _apply_readiness_antiflap(payload: dict, db_ok: bool | None, embed_ok: bool | None) -> None:
    """Stamp ``status`` from the anti-flap counter; per-field db/embed are RAW probe values.

    Pre-PR-#65-review: the function anti-flapped ``status`` AND ``db`` / ``embed``
    together via ``bool(probe) or readiness_healthy``. PR #65 review finding #6
    splits the two: ``status`` keeps the anti-flap grace (it drives P0 503 —
    O2 / O1 satisfied; a single transient probe miss must not self-kill the
    core), BUT per-field ``db`` / ``embed`` are diagnostic and MUST surface the
    real probe outcome so an operator looking at ``curl /health`` can tell
    whether the backend is actually down vs. the probe is just anti-flapping.

    Counter still drives the ``status`` field (the only signal that matters
    to the P0 healthcheck). Counter still resets on a single probe success.
    ``None`` (no probe configured) stays out of the payload — same shape as
    before for callers that only check ``status``.
    """
    global _readiness_consecutive_failures
    dependency_down = db_ok is False or embed_ok is False
    if dependency_down:
        # PR #65 review finding #9: threshold is only meaningful on the
        # failure path (the verdict-flip branch). The healthy path is the
        # common path; calling ``_readiness_fail_threshold`` on every probe
        # is a hot-path cost with no behavioural benefit — it resolves env
        # / settings / YAML / default each time. Move the lookup inside the
        # failure branch so a healthy probe skips it entirely.
        threshold = _readiness_fail_threshold()
        _readiness_consecutive_failures += 1
        payload["readiness_consecutive_failures"] = _readiness_consecutive_failures
        if _readiness_consecutive_failures >= threshold:
            payload["status"] = "degraded"
    else:
        _readiness_consecutive_failures = 0
    # PR #65 review finding #6: per-field truth, not anti-flapped. The
    # diagnostic that tells the operator WHY status still says ok.
    if db_ok is not None:
        payload["db"] = bool(db_ok)
    if embed_ok is not None:
        payload["embed"] = bool(embed_ok)


@observe(tier="stage")
async def _build_health_payload() -> dict:  # noqa: PLR0915 -- 1 boundary stage; fixes #74 + #67 in lockstep
    """Build the /health payload, probing db + embed CONCURRENTLY (C2 P1).

    Total latency is bounded by the slowest single probe (~2s), not the sum of
    both (~4s, the old serial behaviour). Caller wraps this in asyncio.wait_for.
    """
    import httpx  # noqa: PLC0415

    # mcp 2.0.0: the ``_session_manager`` attr became the ``session_manager``
    # property, which raises RuntimeError until ``streamable_http_app()`` has
    # been called. Treat pre-serve RuntimeError as zero rather than 500-ing.
    session_count = 0
    try:
        _session_manager = mcp_server.session_manager
    except RuntimeError:
        _session_manager = None
    if _session_manager is not None:
        session_count = len(_session_manager._server_instances)

    db_url = os.environ.get("YADGAR_DB_URL")
    embed_url = os.environ.get("YADGAR_EMBED_URL")
    db_ok = None
    embed_ok = None

    # §9 Q5: async httpx client. Probe timeout config.yaml-authoritative.
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
        # Car F (task #61) — version handshake. Peer = backend, probed at the
        # embed URL (the only one that exposes ``version``). Missing version
        # is reported as "unverifiable" rather than "incompatible".
        "versions_compatible": _handshake_block(embed_url),
    }

    # #74 fix #1 (C4 task 67) — readiness anti-flap. Update the counter, THEN
    # stamp status / db / embed from one snapshot. The earlier 2-step gate
    # produced ``embed: false`` for ~2 intervals while ``status: ok`` still
    # said ready — a contradiction ``curl /health`` reported as broken.
    _apply_readiness_antiflap(payload, db_ok, embed_ok)

    # Fix A O2 GATE (daemon-offload-A): degrade (→ 503) on tool-pool saturation.
    _apply_tool_pool_health(payload)

    # Car 1 (2026-08-20 train): report the MCP write-gate. /health read `ok`
    # throughout a live vacuum, so the one signal an operator reaches for
    # contradicted every gated tool. ADDITIVE — never touches `status`.
    apply_maintenance_health(payload)

    return payload


@observe(tier="stage")
def _handshake_block(peer_url: str | None) -> dict:
    """Build the ``versions_compatible`` block Car F (task #61) added.

    Probes the peer's ``/health`` for its declared version, then asks
    :mod:`yadgar._shared.version_compat` whether the (self, peer) pair
    is within the supported window. The probe is best-effort — a peer
    we cannot reach (or one that does not advertise a version) is
    reported as ``unverifiable`` rather than ``incompatible``, so a
    transient probe failure does not 503 the readiness handler.
    """
    import httpx as _httpx  # noqa: PLC0415

    from yadgar import __version__  # noqa: PLC0415
    from yadgar._shared.version_compat import handshake_status  # noqa: PLC0415

    if not peer_url:
        # No peer configured (single-process dev mode) — unverifiable, not
        # incompatible. Mirrors the daemon's own "fresh install" self-check.
        return handshake_status(__version__, "unknown", side="core")

    try:
        with _httpx.Client(timeout=1.5) as _c:
            _r = _c.get(f"{peer_url}/health")
        if _r.status_code != 200:
            return handshake_status(__version__, "unknown", side="core")
        _peer_version = _r.json().get("version") or "unknown"
    except Exception:
        return handshake_status(__version__, "unknown", side="core")
    return handshake_status(__version__, _peer_version, side="core")


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
            # Car 1 (2026-08-20 train): the fallback payload is built from
            # scratch, so it misses the block _build_health_payload adds. This
            # path is MORE likely during a window, not less — the backend is
            # stopped, so a dependency probe is exactly what exhausts the budget.
            apply_maintenance_health(payload)

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
    from yadgar.core.forward import _forward_admin  # noqa: PLC0415

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
    """Called by SessionStart hook after compaction. Returns restoration context.

    C10g (0047 PR#40 §5): accepts an optional ``?project=owner/repo``. Restore's
    anchor / hot-memory / gap sinks are keyed on the project_id now, so a hook
    that sends only ``?directory=`` gets the checkpoint and memory blocks (both
    still path-keyed) and empty memory buckets.

    Unlike ``hook_project_id``, a missing project does NOT raise here. That
    function raises because widening its recall would LEAK another project's
    memories into the prompt; restore cannot leak — the sinks return empty —
    and raising would additionally throw away the checkpoint, which is the part
    of a post-compact restore that cannot be reconstructed from anywhere else.
    """
    directory = request.query_params.get("directory", os.getcwd())
    project_id = request.query_params.get("project")

    # T2 Car B: restore compute runs backend-side behind POST /restore.
    # Lazy import mirrors the tools.recall import at :181 (avoids the
    # http ⇄ tools package import cycle at module load).
    from yadgar.core.forward import _forward_restore  # noqa: PLC0415

    try:
        result = await asyncio.to_thread(_forward_restore, directory, project_id)
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
        directory: project directory (optional).
        project: owner/repo identity, when the hook script minted one (Car 8
            — bug train Car 2 threads a client-side ``?project=`` through the
            hook script; before this car the server read only ``directory``
            and silently dropped ``project``, so that half of Car 2's fix was
            inert — the request reached here but never used the value).

    POLICY (Car 8, deliberately NOT ``hook_project_id``'s hard-raise): the
    prompt-recall hook raises on a directory arriving without ``project``
    because an unscoped recall runs FUZZY/semantic search, and widening it
    could leak another project's memories into this project's prompt
    (ADR-0227, the v5.65 leak). This handler's read
    (``storage.list_blocks`` → ``_block_project_clause``) is the opposite
    shape: an EXACT match against the stored ``project_id``/``directory``
    columns, never a semantic search — a block can only surface here if its
    own stored key literally equals the caller's, so there is no leak vector
    for a raise to guard against. block-reflect is also PostToolUse, firing
    on every block_* write in a session rather than once per prompt like
    UserPromptSubmit — raising here would spam observability every time a
    caller's hook script predates Car 2's mint, for no safety benefit.
    Degrade gracefully instead: forward ``project`` when the caller supplied
    one, otherwise keep the pre-Car-8 directory-only behavior unchanged.

    Also removes the ``os.getcwd()`` default that used to backstop a missing
    ``directory`` (v5.65 Fix D precedent, applied there to prompt-recall for
    the same reason): the daemon runs in a container, so that default
    resolves to the CONTAINER's cwd, never the caller's tree. Not currently
    reachable — the deployed hook always sends ``directory`` — but a trap for
    the next caller that omits it.

    Returns: {"text": "...markdown blocks section..."}
    """
    from yadgar._shared.blocks_render import render_blocks_section  # noqa: PLC0415

    directory = request.query_params.get("directory") or None
    project = request.query_params.get("project") or None
    storage = _st._storage
    if storage is None:
        return JSONResponse({"text": ""})

    try:
        blocks = await asyncio.to_thread(
            storage.list_blocks, scope=None, directory=directory, project_id=project
        )
        # render_blocks_section's 2nd arg is presentation-only (C9a / ADR-0225
        # renamed it project_id) — it labels the "Project blocks" header and
        # selects/scopes nothing. Prefer the resolved identity; fall back to
        # directory so the label is not blank when only the legacy arm fired.
        text = render_blocks_section(blocks, project or directory or "")
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
            # C4 (0047 PR#40 §5): carried from the host-side hook runner
            # (core/cli/hook.py). This process is the daemon container and
            # cannot mint one (ADR-0227) — an absent value stays absent, and
            # the consolidation summariser skips-and-counts the row rather
            # than bucketing it under a guess.
            "project_id": sanitize_log_field(str(body.get("project_id", "")), max_len=200),
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

        # C4 originally collapsed the whole batch onto ONE identity, taken only
        # when every action agreed — so a batch spanning two projects was
        # enqueued with ``project_id=""``. C5 then made that a permanent
        # drainer rejection, which is where the 1,094-entry DLQ backlog came
        # from. The batch holds the identities; split on them instead.
        _groups = _split_batch_by_project(to_flush)
        if not _groups:
            _observe_dropped_actions(len(to_flush), len(to_flush))
            return JSONResponse(
                {
                    "status": "dropped",
                    "reason": "no_project_id",
                    "batch_size": len(to_flush),
                    "dropped_unattributed": len(to_flush),
                }
            )
        from datetime import UTC

        ts = datetime.now(UTC).isoformat()

        # T2 Car E1 (ADR-0078): the flushed batch rides the file-queue seam —
        # the backend drainer replays it via run_action_log_replay. Enqueue is
        # disk IO, so it stays off the event loop via asyncio.to_thread.
        from yadgar.core.lifecycle import _get_file_queue  # noqa: PLC0415

        _fq = _get_file_queue()
        for _pid, _actions in _groups:
            _tools = ",".join(a["tool_name"] for a in _actions)
            _summary = " | ".join(a["summary"] for a in _actions if a.get("summary"))
            await asyncio.to_thread(
                _fq.enqueue,
                "action_log",
                {
                    "tool_name": f"batch[{_tools}]",
                    "summary": _summary[:500],
                    # Per group, not per batch: the last action of the WHOLE
                    # batch may belong to a different project entirely.
                    "directory": _actions[-1]["directory"],
                    "session_id": session_id,
                    "project_id": _pid,
                    "timestamp": ts,
                },
            )

        if _st._consolidation is not None:
            _st._consolidation.record_activity()

        _dropped = len(to_flush) - sum(len(a) for _, a in _groups)
        _observe_dropped_actions(_dropped, len(to_flush))
        return JSONResponse(
            {
                "status": "captured",
                "batch_size": len(to_flush),
                "rows": len(_groups),
                "dropped_unattributed": _dropped,
            }
        )
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        _hook_observe("auto_capture", _t0, _caught_exc)


@observe(tier="stage")
async def _task_list_legacy_wiki_nudge(directory: str) -> str:
    """Legacy wiki-page parser for the without-Car-D branch.

    Extracted from `_task_list_restore_nudge` to keep the handler under the
    C901 complexity cap. Returns "" on any error (fail-open). Deleted at
    master 0047 cutover time.

    Car C2: this reader deliberately keeps the BASENAME key. Its pages were
    minted under `<basename>-task-list` by the pre-ADR-0227 stop hook, so a
    legacy reader must use the legacy key — handing it the minted `owner/repo`
    would build `m-agahi/yadgar-task-list`, which is not a slug shape (slugs
    replace `/` with `_`, ADR-0202) and matches nothing. The LIVE path
    (`_task_list_restore_nudge`) uses the minted key; only this soon-to-be
    deleted compatibility arm looks backwards.
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

    storage = _get_storage()
    if storage is None:
        return ""
    legacy_key = _Path(directory).name if directory else ""
    if not legacy_key:
        return ""
    slug = f"{legacy_key}-task-list"
    try:
        page = await asyncio.to_thread(
            storage.get_wiki_page_by_slug_directory,
            slug,
            directory,
        )
    except Exception as _ge:
        logger.debug("task-list page read failed: %s", _ge)
        return ""
    if not page:
        return ""
    import re as _re  # noqa: PLC0415

    _TASK_RE = _re.compile(r"^## task:(?:([\w-]+/)?([0-9a-hj-np-tv-z]+))", _re.MULTILINE)
    _STATUS_RE = _re.compile(r"^- status:\s*(\S+)", _re.MULTILINE)
    _SUBJECT_RE = _re.compile(r"^- subject:\s*(.+)", _re.MULTILINE)
    _content = page.get("content", "") or ""
    _sections = _TASK_RE.split(_content)
    _open_tasks: list[tuple[str, str, str]] = []
    _i = 1
    while _i + 1 < len(_sections):
        _task_id = _sections[_i].strip()
        _body = _sections[_i + 1]
        _sm = _STATUS_RE.search(_body)
        _status = _sm.group(1).lower() if _sm else ""
        if _status in ("pending", "in_progress"):
            _subm = _SUBJECT_RE.search(_body)
            _subject = _subm.group(1).strip() if _subm else "(no subject)"
            _open_tasks.append((_task_id, _subject, _status))
        _i += 2
    _CAP = 12
    if not _open_tasks:
        return (
            f"\n[yadgar] Saved task list ({slug}) — all tasks complete.\n"
            f'Full list: wiki_read("{slug}", directory="{directory}")\n'
        )
    _shown = _open_tasks[:_CAP]
    _overflow = len(_open_tasks) - len(_shown)
    _k = len(_open_tasks)
    _lines = [
        "\n[yadgar] ACTION REQUIRED — restore your task list BEFORE any other work.",
        f"{_k} open task(s) from the last session ({slug}). Call TaskCreate for EACH one now:",
    ]
    for _tid, _subj, _st_open in _shown:
        _lines.append(f"  - [{_tid}] {_subj} ({_st_open})")
    if _overflow > 0:
        _lines.append(f"  …and {_overflow} more")
    _lines.append(
        f'Full descriptions: wiki_read("{slug}", directory="{directory}"). '
        "Preserve the `[N]` prefix at the start of each TaskCreate subject "
        "so task ids reconcile across sessions. "
        "Recreate every open task (pending / in_progress) with TaskCreate "
        "before proceeding; skip completed. Do this FIRST.\n"
    )
    return "\n".join(_lines)


@observe(
    exempt=(
        "pure formatter; runs once per SessionStart nudge assembly. No I/O, "
        "no storage side effects — just iterates pre-fetched ledger rows and "
        "assembles a markdown snippet. Observability would add a span sample "
        "with zero diagnostic value."
    )
)
def _format_task_list_nudge_rows(rows: list[dict], cap: int, project: str) -> str:
    """Format a ledger-read result list into the restore nudge.

    Car C part 2 — this is now the FALLBACK. The primary mechanism is the
    SessionStart hook seeding the harness store on disk
    (``yadgar.core.hooks.task_seed``); this text is only reached when a seeder
    guard trips or the hook predates Car C.

    Two things changed, both about cost. The old form ordered a full hand
    mirror — "Call TaskCreate for EACH one now" over every open task, each
    carrying a subject, a description, an activeForm and a metadata blob. That
    measured 16,705 output tokens across ~81 tasks. The instruction is now
    exactly one call shape, ``TaskCreate(subject="{id}: {title}")``, at roughly
    80 chars per task instead of 400. And the inline window is ordered
    ``in_progress`` first: with real titles averaging ~70 chars a naive
    id-ordered slice spends the whole budget on pending rows and hides the
    tasks that were actually being worked on.

    The ``[N]`` bracket prefix is gone with it. It existed so a hand-created
    harness task could be reconciled back to a ledger id; the seeder makes the
    ledger id BE the harness id, and the bare ``{id}:`` form matches what the
    seeder writes, so both paths produce the same subject.

    Returns "" when rows is empty.
    """
    if not rows:
        return ""
    # `number` was a dead fallback — the task table has no such column, so a
    # row missing `id` rendered as "?", an id that can never be reconciled.
    # A row we cannot identify is now dropped loudly instead.
    _renderable = [_r for _r in rows if _r.get("id") is not None]
    if len(_renderable) != len(rows):
        logger.warning(
            "task-list nudge: %d of %d open rows carry no id and were omitted",
            len(rows) - len(_renderable),
            len(rows),
        )
    if not _renderable:
        return ""
    # in_progress first — the inline window is small and those rows are the
    # ones that answer "what was I doing".
    _ordered = sorted(_renderable, key=lambda _r: _r.get("status") != "in_progress")
    _shown = _ordered[:cap]
    _overflow = len(_ordered) - len(_shown)
    _lines = [
        f"\n[yadgar] ACTION REQUIRED — restore your task list first. "
        f"{len(_ordered)} open for {project}.",
        'For each: TaskCreate(subject="{id}: {title}") — subject only, nothing else.',
    ]
    for _row in _shown:
        _subj = _row.get("title") or _row.get("subject") or "(no subject)"
        _flag = " (in_progress)" if _row.get("status") == "in_progress" else ""
        _lines.append(f"  {_row['id']}: {_subj}{_flag}")
    if _overflow > 0:
        _lines.append(f'  +{_overflow} more: task_list(project_id="{project}")')
    return "\n".join(_lines) + "\n"


@observe(
    exempt=(
        "pure projection; three dict lookups per row, no I/O. Runs once per "
        "seed-capable SessionStart render."
    )
)
def _task_list_payload(rows: list[dict]) -> list[dict]:
    """Project ledger rows down to what the on-disk seeder actually writes.

    Car C. Only ``id`` / ``title`` / ``status`` reach the hook: the harness
    record has no home for ``state``, ``plan_path`` or the timestamps, and this
    payload crosses the wire on every session start.

    Car E adds ``blocked_by`` / ``blocks``, which the harness record DOES have
    a home for and which the seeder wrote as ``[]`` for its whole life because
    nothing could read them. Omitted per row when absent, so a core talking to
    a backend that predates the edge read sends the Car C shape rather than a
    row asserting the task has no dependencies.
    """
    out: list[dict] = []
    for _r in rows:
        if _r.get("id") is None:
            continue
        _rec = {"id": _r.get("id"), "title": _r.get("title"), "status": _r.get("status")}
        for _key in ("blocked_by", "blocks"):
            if _r.get(_key) is not None:
                _rec[_key] = _r[_key]
        out.append(_rec)
    return out


@observe(tier="stage")
async def _read_open_task_rows(ledger: Any, project: str) -> list[dict]:
    """Read the project's open task rows, surviving a ``with_edges`` skew.

    Car E asks the ledger for the ``task_blocked_by`` join (``with_edges=True``)
    because the seeder writes the harness record's ``blockedBy`` / ``blocks``
    arrays. It costs one extra query on a path that already does exactly one
    read — worth it while the parameter is there to ask for.

    The narrow ``TypeError`` arm is the point of this helper. A TypeError from
    this call is what a call that did not BIND looks like: the ledger on the
    other side has no ``with_edges`` parameter, which is precisely the shape of
    a core/backend version skew — routine here, since a deployed core and the
    branch that adds a kwarg are not upgraded in the same instant. Under the
    blanket ``except Exception -> []`` that used to sit here
    the skew rendered an EMPTY nudge — byte-identical to a ledger with no open
    tasks. So the nudge was dead-by-exception while every session looked clean,
    which is the exact failure this path already suffered once.

    Hence: retry WITHOUT the edges rather than render nothing (the edges are a
    seeder nicety; the nudge is the fallback that has to survive), and say so
    at WARNING rather than debug. A genuinely empty ledger still returns ``[]``
    silently — only a read that FAILED is worth a line.

    Extracted from ``_task_list_restore_nudge``: the retry arm pushed that
    function to nesting=6, over the I13 hard cap of 4.
    """
    _kw: dict[str, Any] = {"project_id": project, "status": ["pending", "in_progress"]}
    try:
        return await asyncio.to_thread(ledger.task_list, **_kw, with_edges=True)
    except TypeError as _sig:
        logger.warning(
            "task_list raised TypeError on the with_edges read (%s); retrying "
            "without edges — the seeder loses blocked_by/blocks but the restore "
            "nudge still renders",
            _sig,
        )
    except Exception as _le:
        # Still fail-open (session start is never blocked), but audible: a read
        # that raised is not the same event as a ledger with no open tasks, and
        # only one of those two should be silent.
        logger.warning("task-list ledger read failed: %s", _le)
        return []

    try:
        return await asyncio.to_thread(ledger.task_list, **_kw)
    except Exception as _re:
        logger.warning("task-list ledger retry without edges failed: %s", _re)
        return []


@observe(tier="stage")
async def _task_list_restore_nudge(directory: str, project: str = "") -> tuple[str, list[dict]]:
    """Return ``(nudge text, open task rows)`` — either may be empty.

    Car C returns the ROWS alongside the rendered nudge because the SessionStart
    hook can now seed the harness task store mechanically
    (``yadgar.core.hooks.task_seed``) instead of ordering the model to
    hand-mirror every task. The rows are the seeder's input; the nudge is the
    fallback used only when a guard trips. The read happens once either way.

    The rows are NOT capped — the cap belongs to the rendered nudge alone. A
    seeder that only wrote the first 12 open tasks would silently hide the rest.

    Car E (0047 spine train): reads open tasks from the ``task`` ledger table
    (Car D ships the schema + tools) instead of parsing the `{project}-task-list`
    wiki page. The forcing-nudge form (ADR-0137 Option B) is preserved: imperative
    + enumerated + hoisted FIRST by the caller (http.py:1105).

    The D11 prefix-preserve instruction is part of the nudge payload so the
    harness ``TaskCreate`` subject retains the ``[N]`` prefix and the next
    session's reconcile can match it.

    Graceful fallback: if Car D's task symbols (``task_list`` / ``list_task_rows``)
    are not yet present, the function returns the legacy wiki-page nudge — so
    the rewire is non-breaking on the without-Car-D branch.

    MAIN-THREAD-ONLY by construction: the sole caller is hook_session_context,
    reached by SessionStart only — never by a subagent (SubagentStart /
    agent_dispatch_prelude do not call it), and NOT via project_brief
    (subagent-callable → would leak).

    ADR-0215 removed branch scoping; the existence check resolves by
    directory alone and is reachable from any working tree.

    Car C2 / ADR-0227: ``project`` is the project_id MINTED BY THE SESSION HOOK
    and forwarded as a query parameter — core-server derives nothing. It used
    to be ``Path(directory).name``, which is not an identity: every checkout
    named ``yadgar`` addressed the same ledger rows, and no checkout addressed
    the rows written under the real ``owner/repo`` key. An ABSENT project means
    a caller with no identity, and the correct response is no project-scoped
    read at all — never a guess (ADR-0227: "never defaulted, never inferred,
    never silently substituted").

    Fail-open: any error returns "" so session-start is never blocked.
    """
    try:
        if not project:
            return "", []

        # ── Car E primary path: read the task ledger (Car D ships the table + tools)
        try:
            from yadgar.core.server.tools import task as _task_tools  # noqa: PLC0415

            _ledger = _task_tools
        except ImportError:
            _ledger = None

        if _ledger is not None and hasattr(_ledger, "task_list"):
            _rows = await _read_open_task_rows(_ledger, project)
            _open = [_r for _r in (_rows or []) if isinstance(_r, dict)]
            # Cap 5, down from 12: real ledger titles average ~76 chars, so a
            # 12-row window costs ~1,255 chars of every session's context for a
            # FALLBACK path. 5 lands at ~670 and still leads with in_progress.
            return _format_task_list_nudge_rows(_open, 5, project), _open

        # ── Legacy path: wiki-page parse. Removed once Car D lands.
        return await _task_list_legacy_wiki_nudge(directory), []
    except Exception as _te:
        logger.debug("session-context task-list nudge error: %s", _te)
        return "", []


_CURRENT_PROJECT_BLOCK = "current_project"


@observe(tier="stage")
def _upsert_current_project_block(directory: str, project: str) -> None:
    """Persist the session's minted project_id into an always-injected block.

    Car C2 / ADR-0227. The SessionStart banner is a one-shot line: compaction
    eats it, and after that the agent has no way to recover the identity — MCP
    calls carry no session key, so there is nothing to look it up from. Memory
    blocks ARE re-injected on every session-context render, so writing the key
    into ``current_project`` is what makes it survive a compaction inside a
    long session.

    Writes only what the caller minted. Fail-open (blocks are an ergonomic
    aid, not the transport): a storage error must never break session start.
    """
    if not project or not directory:
        return
    content = (
        f"project_id: {project}\n"
        f'Pass project="{project}" on yadgar tool calls that take it. '
        "A different value is deliberate cross-project work."
    )
    try:
        from yadgar.core.server.tools import blocks as _blocks  # noqa: PLC0415

        result = _blocks.block_update(
            _CURRENT_PROJECT_BLOCK, content, scope="project", directory=directory
        )
        if isinstance(result, dict) and result.get("ok") is False:
            _blocks.block_create(
                _CURRENT_PROJECT_BLOCK, content, scope="project", directory=directory
            )
    except Exception as _be:  # noqa: BLE001 — never block session start
        logger.debug("current_project block upsert failed: %s", _be)


@observe(tier="stage")
def _code_graph_suggest_line(directory: str, blocks: list[dict]) -> str:
    """Return the code_graph SessionStart soft-suggest tail, or "" (Car D, #83).

    A ONE-LINE nudge appended when code_graph is enabled for ``directory``, not
    opted out, AND no ``code_graph`` digest block exists yet in ``blocks``. When a
    digest block already exists it is already injected (render_blocks_section) →
    "". Never forces or auto-runs anything.

    ``blocks`` is the list_blocks result the caller already fetched (no extra I/O).
    Fail-open: any error returns "" so session-start is never blocked. Default-safe
    in the read-only container (no host repos, flag absent → "").
    """
    try:
        from yadgar.core.code_graph.config import session_suggest_line  # noqa: PLC0415
        from yadgar.core.server.tools._runtime_config import config_get  # noqa: PLC0415

        # DAEMON-side: inject the in-process resolver so the daemon reads the
        # code_graph.enabled flag from its OWN DB (ADR-0163 container-blindness fix).
        line = session_suggest_line(directory, blocks, resolver=config_get)
        return f"\n{line}\n" if line else ""
    except Exception as _cge:
        logger.debug("session-context code_graph suggest error: %s", _cge)
        return ""


@observe(
    exempt=(
        "pure assembly; three dict keys over already-fetched values. No I/O, "
        "no storage side effects."
    )
)
def _seed_capable_payload(render: str, nudge: str, rows: list[dict]) -> dict:
    """Response body for a caller that can seed the harness task store itself.

    Car C. The nudge travels as its OWN key rather than prepended to ``text``:
    a hook that seeds mechanically must not ALSO print an order to hand-create
    the tasks it just wrote. It is still sent so the hook can print it when a
    seeder guard trips.
    """
    return {"text": render, "task_nudge": nudge, "tasks": _task_list_payload(rows)}


@observe(tier="stage")
async def _checkpoint_resume_hint(directory: str, source: str) -> str:
    """Return the active-checkpoint resume hint, or "" when there is none.

    Extracted from ``hook_session_context`` (unchanged behaviour) to keep that
    handler under the I30 function-length cap after Car C added the seeding
    branch. Fail-open: any storage error yields "".
    """
    try:
        from yadgar._shared.runtime.lifecycle import _get_storage as _gs  # noqa: PLC0415

        _storage = _gs()
        _cp = await asyncio.to_thread(_storage.get_active_checkpoint, directory)
        if not _cp:
            return ""
        _task = _cp.get("current_task", "")
        _ts = _cp.get("created_at", "")
        _source_hint_prefix = {
            "clear": "Session cleared — call restore() if needed.\n",
            "startup": "Call restore() to pick up where you left off.\n",
            "resume": "Checkpoint available — call restore() to load context.\n",
        }.get(source, "")
        return (
            f"\n[yadgar] Active checkpoint for {directory}:\n"
            f"  Task: {_task}\n"
            f"  Time: {_ts}\n"
            + (f"  {_source_hint_prefix}" if _source_hint_prefix else "")
            + f'To resume: call `restore(directory="{directory}")`\n'
        )
    except Exception as _ce:
        logger.debug("session-context checkpoint hint error: %s", _ce)
        return ""


@mcp_server.custom_route("/hooks/session-context", methods=["GET"])
@trace_span()
async def hook_session_context(request: Request) -> JSONResponse:
    """Return project_brief markdown for session-start hook (§28 pipe).

    Calls project_brief(directory, mode="catalog") and pipes the _render
    markdown field to the hook's stdin. All curation lives server-side.

    Query params:
        directory: project directory (optional, defaults to cwd)
        mode: brief mode (optional, defaults to "catalog")
        source: SessionStart source field (v5.7.9); values: "compact",
            "clear", "startup", "resume". Missing/unknown → treated as
            "startup". "compact" suppresses restore hint (compact handler
            owns auto-restore via /hooks/post-compact).
        seed: Car C capability flag. When set (and source != "compact") the
            caller is declaring it can seed the harness task store itself, so
            the task-restore nudge is returned OUT-OF-BAND rather than
            prepended to the render.
    Returns: {"text": "...markdown..."} — plus {"task_nudge", "tasks"} when the
        caller sent seed=1.
    """
    directory = request.query_params.get("directory", os.getcwd())
    # Car C2 / ADR-0227: the project_id is MINTED HOST-SIDE by the SessionStart
    # hook and arrives here as an explicit parameter. This process cannot derive
    # it (no git, no repo mounted in the container) and must not try.
    project = request.query_params.get("project", "")
    mode = request.query_params.get("mode", "catalog")
    # v5.7.9: read source for per-source hint copy and compact suppression.
    # Unknown/missing values fall through to the "startup" default.
    source = request.query_params.get("source", "") or "startup"
    # Car C: capability flag, sent only by a hook that can seed the harness task
    # store on disk. Absent → the response shape is exactly what it was before
    # Car C, so an older installed hook (hooks are COPIED into ~/.claude/hooks,
    # they can lag the daemon) keeps getting the nudge inside `text`.
    seed_capable = request.query_params.get("seed", "") not in ("", "0")
    _KNOWN_SOURCES = frozenset({"compact", "clear", "startup", "resume"})
    if source not in _KNOWN_SOURCES:
        source = "startup"

    # Record timestamp for prompt-recall throttling (bounded dict)
    _bounded_set(_st._last_session_context, directory, time.monotonic())

    # Car C2: persist the minted identity BEFORE the compact early-return below,
    # so a compaction refreshes the block too (that is the case the block exists
    # for). No-op + fail-open when the caller sent no project.
    _upsert_current_project_block(directory, project)

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
        brief = await asyncio.to_thread(_pb, directory, mode=mode)
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

                    # Car D (#83, ADR-0162): code_graph SessionStart soft-suggest.
                    # Reuses the _blocks list just fetched; the helper is fail-open and
                    # returns "" when the digest block already exists / opted out / off.
                    render = render + _code_graph_suggest_line(directory, _blocks)
            except Exception as _be:
                logger.debug("session-context blocks inject error: %s", _be)

        # v5.6.5 / v5.7.9: append checkpoint resume hint.
        # SUPPRESSED for source=compact — the compact handler (/hooks/post-compact)
        # already calls replay.restore() automatically. Emitting a hint here would
        # create a confusing duplicate "to resume call restore()" alongside the
        # already-restored context.
        # For all other sources: hint only — never auto-call restore().
        if source != "compact":
            render = render + await _checkpoint_resume_hint(directory, source)

        # Task-list mirror restore-nudge (MAIN-THREAD-ONLY; existence-checked).
        # Gated source != "compact" (inherits the enclosing block). Extracted to
        # _task_list_restore_nudge to keep this handler under the I13 complexity
        # cap; that helper is fail-open (returns "" on any error).
        if source != "compact":
            _nudge, _task_rows = await _task_list_restore_nudge(directory, project)
            if seed_capable:
                return JSONResponse(_seed_capable_payload(render, _nudge, _task_rows))
            # Hoisted FIRST (v5.149): the task-restore nudge led the render so it is
            # not buried under the project-brief catalog — the advisory tail form was
            # ignored. Prepend keeps it the first thing the model reads this session.
            # This arm stays byte-identical for hooks predating Car C.
            render = _nudge + render

        return JSONResponse({"text": render})
    except Exception as _e:
        logger.debug("session-context hook error: %s", _e)
        return JSONResponse({"text": ""})


@observe(tier="stage")
def _filter_prompt_recall_results(results: list[dict], project_id: str | None) -> list[dict]:
    """Post-filter retriever results by caller PROJECT for prompt-recall.

    v5.65 Fix D: hook_prompt_recall previously forwarded all retriever results
    to the response with no scoping at all.

    Car C7 (0047 §5 C7) re-keys the predicate from ``directory_context`` onto
    ``project_id`` + the ``global`` reach tag, matching the stage-1 WHERE clause
    exactly. It remains a POST-filter here on purpose: this endpoint hands back
    whatever the forwarded recall produced, and re-issuing a scoped query would
    double the hook's latency against ADR-0077's budget.

    When project_id is absent, scoping is skipped with a warning rather than
    guessing — a container's ``os.getcwd()`` is ``/app`` and would mis-scope.
    """
    from yadgar._shared.storage.directory import is_project_eligible  # noqa: PLC0415

    if not project_id or not project_id.strip():
        logger.warning(
            "prompt-recall: project_id absent — skipping project filter "
            "(container cannot detect host cwd; pass the resolved project id)"
        )
        return results
    caller_project = project_id.strip()
    return [
        r
        for r in results
        if is_project_eligible(r.get("project_id"), r.get("tags"), caller_project)
    ]


#: Rows prompt-recall INJECTS into the prompt (unchanged — the 3000-char budget
#: below is sized for this many).
_PROMPT_RECALL_INJECTED = 5
#: Rows prompt-recall ASKS the backend for. Car 8 (task 283): the anchor filter
#: below removes rows AFTER retrieval, so a 5-row page would frequently collapse
#: to an empty injection (measured: all 5 top rows were anchors). Over-fetching
#: is cheap here — the backend's `rerank_pool` is already
#: `max(max_results, RERANKER_TOP_K=50, CROSS_ENCODER_TOP_K)`, so the extra rows
#: cost a longer trim, not extra hydration, and profile="fast" runs no CE/NLI.
_PROMPT_RECALL_CANDIDATES = 15


@observe(tier="stage")
def _drop_anchor_rows(results: list[dict]) -> list[dict]:
    """Drop anchored memories from a PROMPT-RECALL result page.

    Car 8 (task 283). Anchors are ALREADY in the context window on BOTH
    injection paths: ``hook_session_context`` renders ``project_brief`` in
    ``catalog`` mode, whose ``_render`` prints ``top_anchors_global`` +
    ``top_anchors_project`` (``tools/project.py:158``), and the post-compact
    path calls ``restore()``, which emits the same ``_anchor`` bucket.
    Re-surfacing them here spends the hook's entire 3000-char budget restating
    context the model already has — measured 2026-08-20, all five rows a live
    prompt-recall returned were ``_anchor`` tagged, ``is_protected``, heat 1.0.

    ``project_brief``'s own hot-memories query already excludes this bucket
    (``_get_hot_memories``: ``'anchor' NOTINSIDE tags AND '_anchor' NOTINSIDE
    tags``) for the same reason — anchors are surfaced once, deliberately, and
    every other surface skips them. prompt-recall was the outlier.

    Scoped to THIS hook on purpose. An explicit ``recall("what did we decide
    about X")`` must still return anchors: there the model asked for them and
    they are not duplicated. Nothing else calls this.

    Both anchor markings are honoured — the ``_anchor`` tag ``anchor()`` writes,
    and bare ``is_protected`` (legacy anchors and ``memorize(is_protected=True)``
    predate the tag).
    """
    kept: list[dict] = []
    for r in results:
        tags = r.get("tags") or []
        if r.get("is_protected") or (isinstance(tags, list | tuple) and "_anchor" in tags):
            continue
        kept.append(r)
    if len(kept) != len(results):
        logger.debug(
            "prompt-recall: dropped %d anchored row(s) — already injected by SessionStart",
            len(results) - len(kept),
        )
    return kept


# Car C10 (task #340): tokeniser for the content-dedupe throttle. Lives at
# module scope so both the pre-recall gate and the post-emit recording path
# can call it. Light-weight on purpose — this runs once per UserPromptSubmit
# event, allocation-light via str.translate + set, no new dependencies.
_PROMPT_RECALL_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "i",
        "you",
        "we",
        "they",
        "it",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "with",
        "by",
        "from",
        "as",
        "into",
        "about",
        "up",
        "out",
        "so",
        "no",
        "not",
        "do",
        "does",
        "did",
        "if",
        "then",
        "than",
        "how",
        "what",
        "when",
        "where",
        "why",
        "who",
        "which",
        "should",
        "would",
        "could",
        "can",
        "will",
        "shall",
        "may",
        "might",
        "must",
    }
)
_PROMPT_RECALL_TOKEN_RE = __import__("re").compile(r"[a-z0-9][a-z0-9-]{1,}")


def _prompt_recall_topic_tokens(text: str) -> set[str]:
    """Lower-cased, stopword-stripped alphanumeric tokens for dedupe comparison.

    Hyphenated words are kept whole (``co-recall``) so the gate can tell two
    prompts apart on a domain-specific term even when the loose tokens would
    overlap. Tokens shorter than 3 chars are dropped — they show up in every
    prompt (``let``, ``get``, ``use``, ``run``) and would silently inflate the
    overlap score.
    """
    raw = _PROMPT_RECALL_TOKEN_RE.findall(text.lower())
    return {t for t in raw if t not in _PROMPT_RECALL_STOPWORDS and len(t) >= 3}


# Car C10 (task #340): the throttle gates and emission recorder are extracted
# from ``hook_prompt_recall`` so the route handler stays under the 150-line
# fn_loc HARD cap. Each helper has ONE job: return ``None`` when the caller
# should proceed, or a ``JSONResponse`` to short-circuit.
_PROMPT_RECALL_DEDUPE_THRESHOLD = 0.80
_PROMPT_RECALL_RATE_LIMIT_S = 120
_PROMPT_RECALL_INJECT_MAX_CHARS = 3000


@observe(tier="stage", metric="hooks.prompt_recall.content_dedupe")
def _prompt_recall_content_dedupe_check(query: str, throttle_key: str) -> JSONResponse | None:
    """Primary throttle gate (task #340).

    Returns ``None`` when the prompt should proceed to recall, or a
    ``JSONResponse`` carrying ``{"skipped": "content_dedupe", "overlap": <j>}``
    when the current prompt's topic-set overlaps ≥80% (Jaccard) with the last
    emission's topic-set for this directory.

    The first emission in a session (no prior emission stored) always
    proceeds — there is no prior content to dedupe against, and silently
    skipping would waste a perfectly good recall on the session's first
    prompt.
    """
    current_tokens = _prompt_recall_topic_tokens(query)
    prior_topics = _st._last_emitted_topics.get(throttle_key)
    if not (prior_topics and current_tokens):
        return None
    prior_set = set(prior_topics)
    intersection = len(prior_set & current_tokens)
    union = len(prior_set | current_tokens)
    jaccard = intersection / union if union else 0.0
    if jaccard < _PROMPT_RECALL_DEDUPE_THRESHOLD:
        return None
    return JSONResponse({"text": "", "skipped": "content_dedupe", "overlap": round(jaccard, 2)})


@observe(tier="stage", metric="hooks.prompt_recall.rate_limit")
def _prompt_recall_rate_limit_check(throttle_key: str) -> JSONResponse | None:
    """Secondary hard cap (task #340).

    Stays as a runaway-hook safety net — an operator's session looping on
    submit must not hammer the backend. Returns ``None`` when the call is
    within the rate limit, or the pre-existing ``{"skipped": "rate_limited",
    "retry_after_seconds": <n>}`` envelope otherwise.

    Same shape as the pre-C10 envelope so client-side retry-after accounting
    keeps working.
    """
    now = time.monotonic()
    last = _st._last_prompt_recall.get(throttle_key, 0)
    if now - last >= _PROMPT_RECALL_RATE_LIMIT_S:
        return None
    retry_after = max(0, int(_PROMPT_RECALL_RATE_LIMIT_S - (now - last)))
    logger.warning(
        "prompt-recall throttled for directory=%s retry_after=%ds",
        throttle_key or "<empty>",
        retry_after,
    )
    return JSONResponse({"text": "", "skipped": "rate_limited", "retry_after_seconds": retry_after})


@observe(tier="stage", metric="hooks.prompt_recall.record_emission")
def _prompt_recall_record_emission(results: list[dict], throttle_key: str) -> None:
    """Record the emitted topic-set so the NEXT call's dedupe gate has data.

    Car C10 (task #340). Tokens come from the EMITTED CONTENT (not the query)
    because the dedupe is "would I inject the same thing again" — two
    queries can share a vocabulary while their emitted memory sets have
    nothing in common, and a tokeniser on the query would falsely dedupe them.
    """
    emitted_text = " ".join(m.get("content", "") for m in results)
    _bounded_set(
        _st._last_emitted_topics,
        throttle_key,
        _prompt_recall_topic_tokens(emitted_text),
    )


@observe(tier="stage", metric="hooks.prompt_recall.format_injection")
def _prompt_recall_format_injection(results: list[dict], directory: str | None) -> list[str]:
    """Render the recall rows into the markdown lines that the hook injects.

    Car C10 (task #340) extracted this from ``hook_prompt_recall`` to keep
    that handler under the fn_loc HARD cap (150). The formatting is identical
    to the pre-extraction inline block — 3000-char budget, per-row
    project-name suffix, trailing count footer.
    """
    max_chars = _PROMPT_RECALL_INJECT_MAX_CHARS
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
    return lines


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
        # Car C7: the hook's EXPLICIT project. C5 deleted directory derivation
        # (ADR-0227), so this is the only signal that can scope a hook recall.
        # Absent + a directory present → hook_project_id raises and the
        # except-block below degrades to an empty injection, which is the
        # deliberate choice: no injection beats another project's memories in
        # this project's prompt (the v5.65 leak).
        _hook_project = _hook_query_project(request)

        if not query or len(query) < 2:
            return JSONResponse({"text": ""})

        # Throttle (C10 task #340): primary gate is CONTENT-DEDUPE, with the
        # time-only gate demoted to a secondary hard cap. Both checks live in
        # module-scope helpers so hook_prompt_recall stays under the fn_loc
        # HARD cap (150) — see ``_prompt_recall_content_dedupe_check`` /
        # ``_prompt_recall_rate_limit_check`` for the rationale.
        throttle_key = directory or ""
        _dedupe = _prompt_recall_content_dedupe_check(query, throttle_key)
        if _dedupe is not None:
            return _dedupe
        _rate = _prompt_recall_rate_limit_check(throttle_key)
        if _rate is not None:
            return _rate

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
                _HookRecallForwarder(directory or "", _hook_project),
                "prompt-recall",
                query,
                # Car 8: over-fetch so _drop_anchor_rows below has headroom.
                max_results=_PROMPT_RECALL_CANDIDATES,
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

        # v5.65 Fix D / Car C7: PROJECT post-filter. The backend already scopes
        # with the SAME project_id + 'global'-tag predicate in its stage-1 WHERE,
        # so this is idempotent on forwarded rows — kept as the defense-in-depth
        # contract (#166 Trap 2). Resolution failure degrades to unfiltered
        # forwarded rows, which the backend already scoped.
        try:
            _scoped = hook_project_id(directory, _hook_project)
        except Exception:  # noqa: BLE001 — hook must never raise into the prompt
            _scoped = None
        results = _filter_prompt_recall_results(results, _scoped)
        # Car 8 (task 283): anchors are already in the window via SessionStart —
        # drop them, THEN trim to the injected page size. Order matters: trimming
        # first would throw away the non-anchor rows the filter exists to keep.
        results = _drop_anchor_rows(results)[:_PROMPT_RECALL_INJECTED]

        if not results:
            return JSONResponse({"text": ""})

        lines = _prompt_recall_format_injection(results, directory)
        # Prepend DLQ alerts if any items are stuck
        dlq_text = _build_dlq_alert_text()
        if dlq_text:
            lines = [dlq_text, ""] + lines

        _bounded_set(_st._last_prompt_recall, throttle_key, time.monotonic())
        _prompt_recall_record_emission(results, throttle_key)
        return JSONResponse({"text": "\n".join(lines)})
    except Exception as _exc:
        _caught_exc = _exc
        raise
    finally:
        if not _observed:
            _hook_observe("prompt_recall", _t0, _caught_exc)


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
                #
                # C4 (0047 PR#40 §5): deliberately NO ``project_id`` stamp. The
                # ``project_id`` in scope here is ``match.group(1)`` — a segment
                # of the team-inbox FILE PATH, not an identity key (the same
                # name-collision trap C3 hit with ``wiki_write_task_list.project``,
                # which is a slug component). Reusing it would mint a namespace
                # from a directory name, which is precisely what ADR-0227 deletes.
                # These rows arrive unattributed and are skipped-and-counted by
                # the consolidation summariser until the team-inbox writer
                # carries a real session identity.
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

        try:
            result = await _asyncio.to_thread(
                _memorize,
                content=memo_content,
                context=str(p.parent),
                tags=["_plan", "plan-file"],
                is_protected=False,
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
        # Car C7: the subagent's EXPLICIT project. ``cwd`` defaults to a real
        # path here, and a directory can no longer produce an identity (C5
        # deleted derivation), so without this the hook cannot be scoped.
        _sa_project = _hook_query_project(request)

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
                _HookRecallForwarder(cwd or "", _sa_project),
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
        # #89: opt-in weak-edge render (count<2 transitions). Default OFF preserves
        # the prior payload; the frontend toggle passes ?include_weak=1.
        _iw = request.query_params.get("include_weak", "")
        include_weak = _iw.lower() in ("1", "true", "yes", "on")
        _t0 = time.time()
        # T2 Car E3: the assembly (+ cached-layout attach) runs backend-side.
        from yadgar.core.forward import _forward_viz  # noqa: PLC0415

        data = await asyncio.to_thread(
            _forward_viz,
            "graph",
            {
                "max_memories": max_mem,
                "top_k": top_k,
                "max_wiki": max_wiki,
                "max_entities": max_entities,
                "include_weak": include_weak,
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
        return JSONResponse({"error": "storage unavailable"}, status_code=503)
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
        return JSONResponse({"error": "storage unavailable"}, status_code=503)
    # T2 Car E3: assembly runs backend-side.
    from yadgar.core.forward import _forward_viz  # noqa: PLC0415

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
    from yadgar.core.forward import _forward_viz  # noqa: PLC0415

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
    from yadgar.core.forward import _forward_viz  # noqa: PLC0415

    data = await asyncio.to_thread(
        _forward_viz, "graph_neighborhood", {"node_id": node_id, "hops": hops}
    )
    return JSONResponse(data, headers=_CORS)


@mcp_server.custom_route("/api/graph/relayout", methods=["POST", "OPTIONS"])
@trace_span()
async def api_graph_relayout(request: Request) -> JSONResponse:
    """ADR-0152 Car C: recompute galaxy positions with per-request slider params.

    Body (JSON): {"arms": int, "spiral_pitch": float, "core_density": float}.
    Returns {"positions": {id:[x,y,z]}, "membership": {id:{loose,arm}}, ...}.
    Read-compute-return only — does NOT mutate the canonical layout cache (R3).
    """
    if request.method == "OPTIONS":
        return JSONResponse({}, headers=_CORS)
    if _st._storage is None:
        return JSONResponse({"positions": {}}, status_code=503, headers=_CORS)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → empty overrides (defaults)
        body = {}
    payload: dict = {}
    for key in ("arms", "spiral_pitch", "core_density"):
        if isinstance(body, dict) and body.get(key) is not None:
            payload[key] = body[key]
    # T2 Car E3: layout compute runs backend-side.
    from yadgar.core.forward import _forward_viz  # noqa: PLC0415

    data = await asyncio.to_thread(_forward_viz, "graph_relayout", payload)
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


@mcp_server.custom_route("/api/runtime-config/{key}", methods=["GET"])
@trace_span()
async def api_runtime_config(request: Request) -> JSONResponse:
    """Resolve a runtime_config value (ADR-0163, Car G3).

    The host-side fail-open client (``yadgar.core.runtime_config_client``) hits
    this route. Resolution (per-dir → global → default) runs core-side via the G2
    resolver, which is itself fail-safe (storage down → default), so this returns
    ``{"value": null}`` rather than a 5xx when the DB is unavailable — that IS the
    fail-open contract end-to-end.

    Path param:
        key: the config key.
    Query param:
        directory: absolute project path for a per-dir lookup (optional; omitted =
            global).
    Returns: ``{"key": ..., "directory": ..., "value": <resolved>}``.
    """
    from yadgar.core.server.tools._runtime_config import config_get  # noqa: PLC0415

    key = request.path_params.get("key", "")
    directory = request.query_params.get("directory", None) or None
    value = await asyncio.to_thread(config_get, key, directory, None)
    return JSONResponse({"key": key, "directory": directory, "value": value})


@mcp_server.custom_route("/api/runtime-config/{key}", methods=["POST"])
@trace_span()
async def api_runtime_config_set(request: Request) -> JSONResponse:
    """Persist a runtime_config value — host WRITE path (ADR-0163, Car G5).

    The host-side write client (``runtime_config_client.set``) hits this route.
    It mirrors the ``config_set`` MCP tool by calling the SHARED
    ``_apply_config_set`` helper (validate scope + value → ``_forward_admin`` →
    ``invalidate_config_cache``), so tool and route cannot drift. A validation
    failure (bad scope / missing project directory / non-serializable value)
    returns 400; a successful write returns 200 with the written row.

    Auth: same bearer middleware as every ``/api/`` route (protected prefix).

    Path param:
        key: the config key.
    JSON body:
        ``{value, scope="global"|"project", directory}``.
    """
    from yadgar.core.server.tools.runtime_config import _apply_config_set  # noqa: PLC0415

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    key = request.path_params.get("key", "")
    value = body.get("value")
    scope = body.get("scope", "global")
    directory = body.get("directory")
    result = await asyncio.to_thread(_apply_config_set, key, value, scope, directory)
    status = 400 if isinstance(result, dict) and result.get("ok") is False else 200
    return JSONResponse(result, status_code=status)


@mcp_server.custom_route("/api/runtime-config/{key}", methods=["DELETE"])
@trace_span()
async def api_runtime_config_delete(request: Request) -> JSONResponse:
    """Delete a runtime_config row — host WRITE path (ADR-0163, Car G5).

    Mirrors the ``config_delete`` MCP tool via the shared ``_apply_config_delete``
    helper. Scope + directory come from the query string (``?scope=…&directory=…``)
    so a body-less DELETE works. A validation failure returns 400; success 200.
    """
    from yadgar.core.server.tools.runtime_config import _apply_config_delete  # noqa: PLC0415

    key = request.path_params.get("key", "")
    scope = request.query_params.get("scope", "global")
    directory = request.query_params.get("directory", None) or None
    result = await asyncio.to_thread(_apply_config_delete, key, scope, directory)
    status = 400 if isinstance(result, dict) and result.get("ok") is False else 200
    return JSONResponse(result, status_code=status)


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
        return JSONResponse({"error": "storage unavailable"}, status_code=503)
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", 30))))
    except (ValueError, TypeError) as _e:
        limit = 30

    def _fetch() -> list:
        # Bug 9: legacy rows carry a NONE timestamp (+ NONE data columns). NONE
        # sorts FIRST ascending, so `ORDER BY timestamp ASC LIMIT N` returned N
        # all-zero legacy rows → the chart plotted a permanent flat zero. Fetch
        # the NEWEST non-NONE-timestamp rows (DESC), then reverse to ascending
        # for the chart. Same raw-`_q` seam as before (no new DB read).
        rows = (
            _st._storage._q(
                "SELECT timestamp, memories_added, memories_updated, "
                "memories_archived, memories_deleted, memify_pruned, "
                "cls_promoted, duration_ms "
                "FROM consolidation_log WHERE timestamp IS NOT NONE "
                "ORDER BY timestamp DESC LIMIT $lim",
                {"lim": limit},
            )
            or []
        )
        rows = list(reversed(rows))  # DESC newest-window → ascending for display
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
    span=False,
    metric="core.viz.backend_event_relay",
)
def _poll_backend_events() -> None:
    """Relay backend-process SSE events onto core's own queue (F2).

    memory_added / wiki_added / wiki_updated (write-exec) and heat_updated (heat
    decay) are pushed in the BACKEND process, into ITS process-local
    ``_event_queue`` — a buffer no core SSE client can read. This helper polls
    the backend ``/viz`` ``events`` op for entries past a process-global cursor
    and re-pushes each onto core's queue via ``_push_event``, which RE-STAMPS a
    fresh core seq. The backend ``seq`` is stripped before re-push so it cannot
    overwrite the core seq (``{"seq": core, **event}`` would otherwise let the
    backend value win) and corrupt the client cursor.

    Runs synchronously (blocking httpx) — callers MUST invoke via
    ``asyncio.to_thread`` so the event loop is never blocked (ADR-0018). The
    poll-lock serializes concurrent SSE clients to one backend round-trip per
    tick and keeps the read-cursor→fetch→advance atomic across the HTTP call.
    Best-effort: any backend/transport error is swallowed (viz relay is
    non-critical; the periodic full-reload path still shows the data).
    """
    if not _st._backend_poll_lock.acquire(blocking=False):
        return  # another client is already polling this tick; skip.
    try:
        since = _st._backend_event_cursor
        try:
            result = _forward_viz("events", {"since": max(since, 0)}, timeout_s=5.0)
        except Exception as exc:  # noqa: BLE001 — relay is best-effort
            logger.debug("backend event relay poll failed (%s): %s", type(exc).__name__, exc)
            return
        latest = int(result.get("latest_seq", 0))
        if since < 0:
            # First poll: seed the cursor to the backend head. Do NOT replay the
            # existing backlog (up to 500 stale events) onto a fresh client.
            _st._backend_event_cursor = latest
            return
        for e in result.get("events", []):
            if int(e.get("seq", 0)) <= since:
                continue
            _push_event({k: v for k, v in e.items() if k != "seq"})
        _st._backend_event_cursor = max(latest, since)
    finally:
        _st._backend_poll_lock.release()


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

            # F2 relay: pull backend-process events (memory_added/wiki_added/
            # heat_updated) onto core's own queue so they drain below. Runs in a
            # worker thread (sync httpx) — never block the event loop (ADR-0018).
            await asyncio.to_thread(_poll_backend_events)

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


# ---------------------------------------------------------------------------
@observe(tier="hot", span=False, metric="http.auto_capture.split_batch")
def _split_batch_by_project(actions: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group a flushed action batch by ``project_id``, dropping unattributed rows.

    One flushed batch used to become ONE action_log row, and the identity was
    taken only when every action in it agreed — a batch spanning two repos got
    ``project_id=""`` and was enqueued anyway. Since C5 widened the drainer's
    ``missing_project_id`` gate to every op_type, such a row is a PERMANENT
    failure: 1,094 of them reached the live DLQ at roughly 600/day.

    The batch already carries the identities, so splitting preserves them
    instead of discarding them. Actions with no identity are DROPPED rather
    than emitted: the drainer would reject them anyway, and a dropped
    telemetry row costs nothing while a DLQ entry costs a human clearing it.

    Order is preserved — both the group order (first appearance) and the
    actions within each group — so the flushed summaries still read
    chronologically.
    """
    grouped: dict[str, list[dict]] = {}
    for action in actions:
        pid = (action.get("project_id") or "").strip()
        if not pid:
            continue
        grouped.setdefault(pid, []).append(action)
    return list(grouped.items())


@observe(tier="hot", span=False, metric="http.auto_capture.observe_dropped")
def _observe_dropped_actions(dropped: int, batch_size: int) -> None:
    """Make ``_split_batch_by_project``'s discards loud and countable.

    Car 20 (ledger task 303). DROPPING an unattributed action is right — the
    drainer would reject the row and it would cost a human a DLQ entry. The
    SILENCE was wrong: the whole-batch drop used a ``logger.debug`` (invisible
    at the container's INFO level) plus a countless HTTP 200, and the partial
    drop logged nothing at all. So when the host-side hook stopped sending
    ``project_id``, capture died for six days behind 536 HTTP 200s and an empty
    DLQ — failure rendered as well-formed success. Both branches call this.

    Mechanism is deliberately NOT new: ``observe_project_id_skip`` beside a
    WARNING is what this failure class already uses one stage later, in
    ``backend/consolidation/cleanup.py``'s ``action_log_group`` skip. Distinct
    ``writer`` label keeps the two stages separable.

    The response stays 200: every client of ``/hooks/auto-capture`` catches
    ``HTTPError``, closes it and returns, so a non-200 changes nothing
    observable at the hook while adding a failure path to a fire-and-forget
    call made on every tool use. The count rides the 200 body instead.

    No-op when nothing was dropped — a warning that fires on the healthy path
    is one operators learn to ignore.
    """
    if dropped <= 0:
        return
    from yadgar._shared.storage._project_id_writer import (  # noqa: PLC0415
        observe_project_id_skip,
    )

    observe_project_id_skip("auto_capture_batch", dropped)
    logger.warning(
        "auto-capture: dropped %d of %d action(s) — no project_id to route them to. "
        "The PostToolUse hook must mint one host-side (ADR-0227: the daemon cannot). "
        "If this is every action, the wired hook is not sending project_id.",
        dropped,
        batch_size,
    )


# Car B (0047 §3.2) — POST /session_bind
#
# Non-MCP route that consumes a single-use nonce → project_id binding minted
# by the SessionStart hook (see ``yadgar/_shared/runtime/session_bind.py``).
# Returns a fresh opaque ``session_token`` that the caller then carries in
# the ``Mcp-Session-Id`` header on subsequent MCP calls — the tool wrapper
# in ``_app._instrumented_async`` reads the header, looks up the project_id
# from the pool, and stamps the ContextVar that ``resolve_effective_project``
# tier 2 reads.
#
# Why NON-MCP: this endpoint exchanges a pre-minted nonce; it is not a
# tool call. Registering it as a tool would (a) widen the MCP surface
# with a control-plane operation and (b) put the route INSIDE the
# streamable_http_app's session manager, which would force it to share
# a session lifecycle with the JSON-RPC tool calls. The whole point is
# to mint a session_token BEFORE the MCP call lands, so the route lives
# at the same Starlette level as the other @custom_route hooks but is
# dispatched in its own coroutine — no session-manager membership.
#
# Error envelope: unknown / evicted / forged nonces return a structured
# ``session_not_bound`` with a fix-text line. A caller that sees this
# must re-run the SessionStart hook (which re-mints) rather than retrying.
# ---------------------------------------------------------------------------


@observe(tier="stage", metric="http.session_bind")
async def _session_bind(request: Request) -> JSONResponse:
    """POST /session_bind — consume nonce, return session_token.

    Body: ``{"nonce": "...", "project_id": "..."}``
    Returns: ``{"ok": true, "session_token": "..."}``
    On unknown / evicted / forged nonce: ``{"ok": false, "error": "session_not_bound",
    "fix": "re-run SessionStart hook to mint a fresh nonce"}``.

    Wire-up note: the route looks up (or mints) the ``Mcp-Session-Id`` for
    the calling connection and stores ``sid -> project_id`` in the
    in-process binding registry. The ``SessionBindMiddleware`` reads the
    same registry on subsequent requests, stamps the per-request
    ContextVar, and the tool wrapper in ``_app._instrumented_async``
    surfaces the value to ``resolve_effective_project`` tier 2.

    On the FIRST call (the /session_bind itself), there is no
    Mcp-Session-Id yet — the SDK has not initialised the transport. The
    route treats that as a one-shot: it returns the session_token so the
    caller can pass it on the next MCP call as ``Mcp-Session-Id``. The
    binding is then established by the transport on the first JSON-RPC
    request, which is when the SessionBindMiddleware can stamp the
    ContextVar.

    For simplicity and to keep the route self-contained, the route ALSO
    binds the caller's identity to a synthetic ``sid`` derived from the
    nonce itself (the nonce is unique per mint). The transport will
    ignore that and use its own session_id on the next request — at
    which point the binding is established by the SessionBindMiddleware
    reading the X-Yadgar-Project-Id header that the caller adds to
    subsequent requests.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "session_not_bound", "fix": "POST a JSON body"},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "session_not_bound", "fix": "POST a JSON object"},
            status_code=400,
        )

    nonce = body.get("nonce", "")
    project_id = body.get("project_id", "")

    if not isinstance(nonce, str) or not nonce or not isinstance(project_id, str) or not project_id:
        return JSONResponse(
            {
                "ok": False,
                "error": "session_not_bound",
                "fix": (
                    "re-run the SessionStart hook to mint a fresh nonce; "
                    "the request body must include both nonce and project_id"
                ),
            },
            status_code=400,
        )

    # Defensive: only the project_id that was REGISTERED against this nonce
    # is acceptable here. The caller echoes project_id back so a forged body
    # with a valid nonce but a different project cannot pass.
    from yadgar._shared.runtime.session_bind import (  # noqa: PLC0415
        get_nonce_pool,
        mint_session_token,
    )

    _pool = get_nonce_pool()
    bound = _pool.consume(nonce)
    if bound is None or bound != project_id:
        logger.warning(
            "session_bind reject: nonce=%s... project_id=%s bound=%s",
            (nonce or "")[:8],
            project_id,
            bound,
        )
        return JSONResponse(
            {
                "ok": False,
                "error": "session_not_bound",
                "fix": (
                    "re-run the SessionStart hook to mint a fresh nonce; the "
                    "supplied nonce is unknown, evicted, or already consumed"
                ),
            },
            status_code=404,
        )

    # Establish the binding. We use the nonce itself as a stable
    # session_id surrogate for callers that POST /session_bind and
    # then turn around with that exact nonce as their Mcp-Session-Id
    # (the SessionStart hook is the canonical consumer). The transport
    # ignores nonce-shaped session_ids unless they pass the SDK's
    # SESSION_ID_PATTERN; if they fail, the SessionBindMiddleware falls
    # back to the X-Yadgar-Project-Id header the client sets.
    _session_token = mint_session_token()
    _register_session_binding(_session_token, project_id)
    _register_session_binding(nonce, project_id)

    return JSONResponse(
        {"ok": True, "session_token": _session_token},
        status_code=200,
    )


# In-process ``sid -> project_id`` registry backing the SessionBindMiddleware.
# Module-level dict guarded by a single lock; the dict is intentionally
# process-local (Car B does not cross process boundaries — the daemon is
# single-instance for the relevant transport). Eviction is the caller's
# responsibility (the transport evicts on session close; tests can call
# ``_clear_session_bindings()``).
_BINDINGS: dict[str, str] = {}
_BINDINGS_LOCK = threading.Lock()
_BINDINGS_MAX = 4096  # hard cap; FIFO eviction past this point


@trace_span()
def _register_session_binding(sid: str, project_id: str) -> None:
    """Add or update ``sid -> project_id`` in the in-process binding registry.

    Idempotent: a second register with the same sid overwrites the prior
    project_id (the SDK transports rotate session_ids on certain events,
    and the caller may re-bind intentionally).
    """
    if not isinstance(sid, str) or not sid or not isinstance(project_id, str) or not project_id:
        return
    with _BINDINGS_LOCK:
        if sid in _BINDINGS:
            _BINDINGS[sid] = project_id
            return
        if len(_BINDINGS) >= _BINDINGS_MAX:
            # FIFO evict one entry. dict preserves insertion order in py3.7+.
            try:
                _old_sid = next(iter(_BINDINGS))
                if _old_sid != sid:
                    del _BINDINGS[_old_sid]
            except StopIteration:
                pass
        _BINDINGS[sid] = project_id


@trace_span()
def lookup_session_binding(sid: str) -> str | None:
    """Return the project_id bound to ``sid``, or ``None`` if not bound.

    Read-only accessor for the SessionBindMiddleware. Thread-safe.
    """
    if not isinstance(sid, str) or not sid:
        return None
    with _BINDINGS_LOCK:
        return _BINDINGS.get(sid)


@trace_span()
def _clear_session_bindings() -> None:
    """Test-only: drop every binding. Never call from production code."""
    with _BINDINGS_LOCK:
        _BINDINGS.clear()


# Register the route directly on the same Starlette app as the MCP transport,
# but outside the streamable_http_app's session manager. The custom_route
# decorator would route through the MCP layer; the post() helper is the
# non-session-manager path used by /health, /metrics, etc. — and /session_bind
# is the same kind of non-MCP control-plane route. Mirror that pattern.
@trace_span()
def _register_session_bind_route() -> None:
    """Attach ``POST /session_bind`` to the Starlette app that ``mcp_server``
    would expose via streamable_http_app — but at a layer OUTSIDE the
    streamable session manager.

    Called once at module import time (mirrors _patch_uvicorn_shutdown_timeout).
    Idempotent: skips re-registration if a previous import already wired it.
    """
    try:
        _app = mcp_server.streamable_http_app()  # type: ignore[attr-defined]
    except Exception:
        # Pre-serve / test context: streamable_http_app may not be available
        # (it raises RuntimeError on a fresh MCPServer until the first serve).
        # The route will be wired on the next /session_bind request via the
        # ASGI wrapper instead — see _app.py for the lifecycle hook.
        return
    if any(getattr(r, "path", None) == "/session_bind" for r in _app.routes):
        return
    _app.add_route("/session_bind", _session_bind, methods=["POST"])


# Defer wiring until the MCP server's streamable_http_app is buildable.
# In yadgar's startup path this happens before the first request, so the
# late-bound registration is invisible to operators. The route is always
# wired before serve() returns, matching the other @custom_route hooks
# (those are registered at module-import time of http.py; the test harness
# exercises them via TestClient(streamable_http_app()) which forces the
# build). Idempotent — calling _register_session_bind_route twice is a no-op.
try:
    _register_session_bind_route()
except Exception:  # noqa: BLE001
    pass  # non-fatal; a follow-up will retry at first request
