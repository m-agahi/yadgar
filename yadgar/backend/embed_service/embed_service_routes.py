"""DB-forward backend routes for the embed_service app.

Split out of ``embed_service.py`` (C1, module-standardization train #18): the
five routes that forward a compute/storage op to the backend engine stack —
``/recall`` (fan-out pipeline), ``/restore``, ``/consolidate``, ``/admin``,
``/viz`` — plus the two recall helpers ``_run_landscape_backend`` and
``_forked_boost_write``.

MODULE-OBJECT ACCESS (recipe crux): these routes call ``_ensure_recall_engines``
through the ``embed_service`` module object (``_es._ensure_recall_engines``), NOT
a ``from embed_service import _ensure_recall_engines`` binding. Tests patch it
via ``setattr(es, "_ensure_recall_engines", ...)`` and set
``es._recall_engines_ready = True`` on the canonical submodule; the writer +
guard-global live in ``embed_service.py``, so reaching them through the module
object is what makes the patch/rebind visible here. Same reason ``app`` is taken
from ``_es.app`` — the routes register on the one canonical app instance.

Imported at the BOTTOM of ``embed_service.py`` (after ``app`` +
``_ensure_recall_engines`` exist) so the ``@app.post`` decorators register on the
live app. Re-exported symbols (``recall_route`` etc.) stay resolvable as
``embed_service.embed_service.<name>``.

RELOAD-AWARE: ``importlib.reload(embed_service)`` builds a fresh ``app`` but does
NOT re-execute an already-imported sibling. embed_service.py therefore
force-reloads THIS module on its reload (guarded by the ``_YADGAR_ES_LOADED``
sentinel at the bottom) so ``app = _es.app`` rebinds to the new app and the
``@app.post`` decorators re-register on it — otherwise these five routes would be
silently dropped from the reloaded app (test-ordering pollution).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from fastapi import Depends, HTTPException

import yadgar.backend.embed_service.embed_service as _es
from yadgar._shared.observability.observe import observe
from yadgar.backend.embed_service.embed_service_models import (
    AdminRequest,
    AdminResponse,
    ConsolidateRequest,
    ConsolidateResponse,
    ReadQueryRequest,
    ReadQueryResponse,
    RecallRequest,
    RecallResponse,
    RestoreRequest,
    RestoreResponse,
    VizRequest,
    VizResponse,
)

# Defense-in-depth parse-guard keywords for POST /read_query. NOTE: this is NOT
# the primary guard — the RO VIEWER DB connection rejects writes at the DB
# regardless of query text (ADR-0078). SurrealQL is multi-statement, so
# "SELECT 1; DELETE memory" defeats a naive prefix check; this keyword scan is a
# cheap early-reject layer only.
_WRITE_KEYWORDS: tuple[str, ...] = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "CREATE",
    "DEFINE",
    "REMOVE",
    "RELATE",
    "UPSERT",
)

logger = logging.getLogger(__name__)

app = _es.app
_require_admin_token = _es._require_admin_token


# ---------------------------------------------------------------------------
# Train 1: backend /recall route — runs the fan-out pipeline backend-side
# ---------------------------------------------------------------------------


@observe(tier="stage", metric="backend.recall.landscape")
def _run_landscape_backend(query: str, max_results: int, directory: str, storage) -> list[dict]:
    """Backend-side landscape recall via AstrocytePool.consensus_retrieve.

    Phase 1 §5.1/§3.2: mirrors core _landscape_recall (recall.py:45-91) but runs
    inside the backend process where the AstrocytePool is available after
    init_engines(local_engines=True). The 400 guard at the route level is removed;
    this function is called when req.mode=="landscape".

    Returns [] gracefully when _pool is None (pool unavailable / disabled).
    Directory post-filter via is_directory_eligible (same predicate as fanout path).
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415
    from yadgar._shared.storage.directory import is_directory_eligible  # noqa: PLC0415

    if _st._pool is None:
        logger.debug("landscape_backend: pool unavailable — returning []")
        return []

    raw = _st._pool.consensus_retrieve(query, top_k=max_results)
    scoped = [r for r in raw if is_directory_eligible(r.get("directory_context"), directory)]
    return scoped[:max_results]


@observe(tier="stage")
async def _forked_boost_write(storage, boosted_ids: list[int], now: str) -> None:
    """T3 Car 2: the forked backend heat DB write (the ~407ms recall tail).

    Runs the batched ``storage.boost_memories_access`` off the recall response
    critical path (as a tracked task via ``schedule_db_write``, or awaited inline
    under backpressure). The ``recall.side_effects.db`` span nests under the
    recall request trace because the task is created while that span is current
    (contextvars carry the OTEL parent across ``create_task``).
    """
    from yadgar._shared.observability.tracing import span as _span  # noqa: PLC0415

    with _span("recall.side_effects.db", results=len(boosted_ids)):
        await asyncio.to_thread(storage.boost_memories_access, boosted_ids, now)


@app.post("/recall", response_model=RecallResponse)
@observe(tier="boundary", metric="backend.recall")
async def recall_route(
    req: RecallRequest, _: None = Depends(_require_admin_token)
) -> RecallResponse:
    """Run the fan-out recall pipeline backend-side and return ranked results.

    Phase 1 (backend contract widening, §5.1):
      - mode=None: _fanout_recall with optional profile/rerank_level threading.
      - mode="landscape": _landscape_recall via backend-local AstrocytePool.

    The two 400 guards for mode=landscape and profile= are removed — the backend
    now serves every recall variant. Existing callers (mode=None, profile=None)
    are unaffected (additive change, not a breaking change).

    Called by the core thin forwarder when RECALL_BACKEND_ENABLED=True.
    Applies the DB-side bookkeeping half (_apply_recall_db_side_effects) for
    the fanout path. Landscape side-effects use _apply_recall_db_side_effects too
    (heat boost + thermo), mirroring the core landscape path.

    Session-side bookkeeping (SR transitions, action buffer, replay counter)
    runs in the core process on the returned results — NOT here.

    Returns:
        RecallResponse with the ranked result list.
    """
    # Bootstrap engines (idempotent, guarded by lock). Module-object access so the
    # test-time ``setattr(es, "_ensure_recall_engines")`` / ``_recall_engines_ready``
    # rebind on the canonical submodule is honoured.
    await asyncio.to_thread(_es._ensure_recall_engines)

    from yadgar._shared.runtime.lifecycle import (
        _get_storage as _backend_get_storage,  # noqa: PLC0415
    )
    from yadgar._shared.runtime.recall_side_effects_fork import (  # noqa: PLC0415
        schedule_db_write,
    )
    from yadgar.backend.retrieval.recall_pipeline import (  # noqa: PLC0415
        _compute_db_boost,
        _fanout_recall,
    )

    # ADR-0077: convert the client's compute budget to a monotonic deadline ONCE,
    # at route entry — the pipeline checks it between stages and aborts remaining
    # work (partial results) when exceeded. None = no deadline.
    deadline: float | None = (
        time.monotonic() + req.deadline_ms / 1000.0 if req.deadline_ms else None
    )

    # Run the RETRIEVAL + the response-feeding heat mutations in a thread
    # (CPU-bound + IO-bound mix; don't block the event loop). T3 Car 2: the
    # in-place heat/last_accessed mutations stay INLINE here (they feed the
    # response payload — must be byte-identical), but the batched DB WRITE
    # (~407ms tail) is forked off the response path below.
    def _run_pipeline() -> tuple[list[dict], list[int], str]:
        storage = _backend_get_storage()

        if req.mode == "landscape":
            # §5.1 landscape dispatch: backend-hosted consensus_retrieve via AstrocytePool.
            # Mirrors core _landscape_recall (recall.py:45-91): consensus_retrieve →
            # directory post-filter → apply DB side-effects.
            merged = _run_landscape_backend(
                query=req.query,
                max_results=req.max_results,
                directory=req.directory,
                storage=storage,
            )
        else:
            # Default fanout path — thread profile/rerank_level.
            merged = _fanout_recall(
                query=req.query,
                max_results=req.max_results,
                min_heat=req.min_heat,
                directory=req.directory,
                type_filter=req.type,
                tags=req.tags,
                profile=req.profile,
                deadline=deadline,
            )

        # Inline, latency-safe: mutate heat/last_accessed in place + thermo record.
        boosted_ids, now = _compute_db_boost(merged, storage)
        return merged, boosted_ids, now

    results, boosted_ids, boost_now = await asyncio.to_thread(_run_pipeline)

    # T3 Car 2: fork the batched heat DB write off the response critical path.
    # create_task runs while THIS request span is current → contextvars carry the
    # OTEL parent so recall.side_effects.db nests under the recall trace. If the
    # fork is disabled OR the in-flight cap is hit, await the SAME coroutine
    # inline (backpressure — the side-effect always executes, never dropped).
    if boosted_ids:
        storage = _backend_get_storage()
        _coro = _forked_boost_write(storage, boosted_ids, boost_now)
        if not schedule_db_write(_coro):
            await _coro

    return RecallResponse(results=results)


# ---------------------------------------------------------------------------
# T2 Car B: backend /restore route — runs the restore COMPUTE backend-side
# (CheckpointRestore + CognitiveMap SR navigation, census verdict #7). The
# core restore MCP tool, the post-compact hook, and the CLI restore subcommand
# forward here via the core _forward_restore helper. Live-proven motivation:
# restore() on core's 1 CPU exceeded the 95s tool-offload ceiling; the SR
# matrix compute now runs next to the DB on the backend's CPUs.
# ---------------------------------------------------------------------------


@app.post("/restore", response_model=RestoreResponse)
@observe(tier="boundary", metric="backend.restore")
async def restore_route(
    req: RestoreRequest, _: None = Depends(_require_admin_token)
) -> RestoreResponse:
    """Run the restore compute backend-side and return the restore payload.

    Mirrors the /recall route: lazily builds the slim engine set (plus the
    restoration engines) via _ensure_recall_engines, then runs the compute in a
    worker thread so the event loop is not blocked (SR matrix build + inversion
    is CPU-bound). Called by the core thin forwarder (_forward_restore).
    """
    from yadgar.backend.restoration import run_restore  # noqa: PLC0415

    # Bootstrap engines (idempotent, guarded by lock).
    await asyncio.to_thread(_es._ensure_recall_engines)

    result = await asyncio.to_thread(run_restore, req.directory)
    return RestoreResponse(result=result)


# ---------------------------------------------------------------------------
# R3 Car 1 D2: backend /consolidate route — runs the consolidation COMPUTE
# backend-side (it uses the backend curator + phase engines). The core
# orchestrator forwards here and layers its viz/admin tail on the result.
# ---------------------------------------------------------------------------


@app.post("/consolidate", response_model=ConsolidateResponse)
@observe(tier="boundary", metric="backend.consolidate")
async def consolidate_route(
    req: ConsolidateRequest, _: None = Depends(_require_admin_token)
) -> ConsolidateResponse:
    """Run the consolidation compute cycle backend-side and return the stats.

    Mirrors the /recall route: lazily builds the backend engine set (the
    consolidation service reuses the slim /recall engines + builds its own
    scheduler singleton), then runs one cycle in a worker thread so the event
    loop is not blocked by the CPU/IO-bound compute (light ~30s, full 5–15 min).

    Called by the core consolidation orchestrator (forward-only, R3 Car 1 D3).
    """
    from yadgar.backend.consolidation.service import (  # noqa: PLC0415
        run_consolidation_cycle,
    )

    stats = await asyncio.to_thread(run_consolidation_cycle, req.mode)
    return ConsolidateResponse(stats=stats)


# ---------------------------------------------------------------------------
# R3 Car 3a (R5 forward pattern): backend /admin route — runs the storage-WRITE
# half of the pure-CRUD MCP tools (bookmarks, blocks, …). Core keeps the @_tool
# shell + validation + secret-gate and forwards the write here via the core
# _forward_admin helper. Goal: core touches zero DB directly.
# ---------------------------------------------------------------------------


@app.post("/admin", response_model=AdminResponse)
@observe(tier="boundary", metric="backend.admin")
async def admin_route(req: AdminRequest, _: None = Depends(_require_admin_token)) -> AdminResponse:
    """Run a single admin op's storage-write body backend-side and return its result.

    Mirrors the /recall + /consolidate routes: lazily builds the slim engine set
    (which includes storage) via _ensure_recall_engines, then dispatches the op.

    Engine-#2 car B: dispatch goes through ``run_admin_op_async``, which keeps a
    SYNC op body on the ``asyncio.to_thread`` path (unchanged — the event loop is
    still not blocked by storage IO) and awaits an ASYNC op body directly on the
    loop. No existing op changed shape.

    Car B (ADR-0053, §15.2): every response carries a ``scope_versions`` envelope
    field — ``{kind: per_kind_epoch, ...}`` for the kinds Cars D/F/I care about
    (``config``, ``ledger``). Core holds its own snapshot and compares; a
    bumped epoch means its PTC entries for that kind are unreachable, with
    zero extra round-trips in steady state. The same epoch survives in the
    backend ``ScopeVersions`` singleton, so the response is cheap (one lock
    acquire).

    op must be a registered admin op (yadgar.backend.admin_exec.run_admin_op).
    Unknown ops → 400. Called by the core thin forwarders (_forward_admin).
    """
    from yadgar.backend.admin_exec import run_admin_op_async  # noqa: PLC0415
    from yadgar.backend.cache.scope_versions import get_scope_versions  # noqa: PLC0415

    # Bootstrap engines (idempotent, guarded by lock) — the op needs storage.
    await asyncio.to_thread(_es._ensure_recall_engines)

    try:
        result = await run_admin_op_async(req.op, req.payload)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Car B piggyback: per-kind scope-version epochs the core PTC keys by.
    # Cheap (single lock acquire on the singleton).
    scope_versions = get_scope_versions().kind_epochs_snapshot(("config", "ledger"))

    return AdminResponse(result=result, scope_versions=scope_versions)


# ---------------------------------------------------------------------------
# T2 Car E3 (census verdict #11): backend /viz route — runs the DB-heavy graph
# data assembly (GraphAPI) + cached-layout attach backend-side. The core
# /api/graph* endpoints keep their route shells and forward here via the core
# _forward_viz helper. Mirrors /admin + run_admin_op (reads-flavored twin).
# ---------------------------------------------------------------------------


@app.post("/viz", response_model=VizResponse)
@observe(tier="boundary", metric="backend.viz")
async def viz_route(req: VizRequest, _: None = Depends(_require_admin_token)) -> VizResponse:
    """Run a single viz op's graph-assembly body backend-side and return its result.

    Mirrors the /admin route: lazily builds the slim engine set (which includes
    storage) via _ensure_recall_engines, then runs the op in a worker thread so
    the event loop is not blocked by the assembly IO/compute.

    op must be a registered viz op (yadgar.backend.viz_exec.run_viz_op).
    Unknown ops → 400. Called by the core thin forwarders (_forward_viz).
    """
    from yadgar.backend.viz_exec import run_viz_op  # noqa: PLC0415

    # Bootstrap engines (idempotent, guarded by lock) — the op needs storage.
    await asyncio.to_thread(_es._ensure_recall_engines)

    try:
        result = await asyncio.to_thread(run_viz_op, req.op, req.payload)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return VizResponse(result=result)


# ---------------------------------------------------------------------------
# Sanctioned read-only DB inspection surface (ADR-0078 named debug read path).
# The query runs on the VIEWER-role RO DB connection (_q_ro) — a write over that
# connection does NOT persist regardless of query text (the REAL guard; VIEWER may
# silently no-op or hard-error). The keyword parse-guard below is defense-in-depth
# only. Core forwards here via _forward_read_query.
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _contains_write_keyword(query: str) -> bool:
    """Return True if *query* contains any write keyword as a whole word.

    Defense-in-depth only (NOT the primary guard — the RO connection is). Uses
    word boundaries so identifiers like ``updated_at`` do not false-positive.
    """
    upper = query.upper()
    for kw in _WRITE_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            return True
    return False


@app.post("/read_query", response_model=ReadQueryResponse)
@observe(tier="boundary", metric="backend.read_query")
async def read_query_route(
    req: ReadQueryRequest, _: None = Depends(_require_admin_token)
) -> ReadQueryResponse:
    """Run a read-only ad-hoc query against SurrealDB and return the rows.

    Safety = the query runs on the VIEWER-authed RO DB connection (``_q_ro``); a
    write over that connection does NOT persist regardless of query text
    (ADR-0078; VIEWER may silently no-op or hard-error). The keyword parse-guard
    below is DEFENSE-IN-DEPTH only — SurrealQL is multi-statement, so a prefix
    check is defeatable; the RO connection is the real guard.

    Row-capped at 500 (module constant ``_RO_QUERY_ROW_CAP``) and bounded by
    ``req.timeout_ms``. Called by the core thin forwarder ``_forward_read_query``
    (and thus the ``db_inspect`` MCP tool). Unknown/malformed query → the DB
    surfaces an error → 400.
    """
    # Defense-in-depth: cheap early-reject. The RO connection is the real guard.
    if _contains_write_keyword(req.query):
        raise HTTPException(
            status_code=400,
            detail=(
                "read_query rejects statements containing write keywords "
                "(defense-in-depth; the read-only DB connection is the real guard)."
            ),
        )

    # Bootstrap engines (idempotent, guarded by lock) — we need the storage engine.
    await asyncio.to_thread(_es._ensure_recall_engines)

    from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

    storage = _get_storage()

    def _run() -> tuple[list[dict], bool]:
        return storage._q_ro(req.query, req.params or None, timeout_ms=req.timeout_ms)

    try:
        rows, truncated = await asyncio.to_thread(_run)
    except RuntimeError as exc:
        # SurrealDB-level error (incl. a write rejected by the VIEWER role, or a
        # malformed query) → 400 with the DB's message.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ReadQueryResponse(rows=rows, row_count=len(rows), truncated=truncated)


# Sentinel: set on first import so embed_service.py knows to force-reload this
# sibling (re-running the @app.post decorators on the new app) whenever
# importlib.reload(embed_service) creates a fresh app. See embed_service.py bottom.
_YADGAR_ES_LOADED = True
