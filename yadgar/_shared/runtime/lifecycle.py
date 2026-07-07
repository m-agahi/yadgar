"""Singleton getter functions, init_engines, shutdown, and signal handler.

All module-level singleton state lives in _state.py.
Getters here provide typed access with assertions.

Car 3 (folder-split #17): the MCP-server ``main()`` entry point moved to
``yadgar.server._startup`` (core side) — it imported ``server._app`` +
``server.tools.misc``, which are ``_shared → server`` edges. The pure engine
lifecycle stays here; it has no ``yadgar.server`` imports.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yadgar._shared.paths as _paths
import yadgar._shared.runtime.state as _st  # sibling; avoid server/__init__ re-entry (Car 1)
from yadgar._shared.causal_discovery import CausalDiscovery
from yadgar._shared.cognitive_map import CognitiveMap
from yadgar._shared.config import get_settings, resolve_knob
from yadgar._shared.curation import MemoryCurator
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.engram import EngramAllocator
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.metacognition import MetaCognition
from yadgar._shared.narrative import NarrativeEngine
from yadgar._shared.observability.observe import observe
from yadgar._shared.predictive_coding import WriteGate
from yadgar._shared.prospective import ProspectiveMemoryEngine
from yadgar._shared.restoration import CheckpointRestore
from yadgar._shared.retrieval import Retriever
from yadgar._shared.rules_engine import RulesEngine
from yadgar._shared.sensory_buffer import ActionLogger
from yadgar._shared.staleness import StalenessDetector
from yadgar._shared.storage import StorageEngine
from yadgar._shared.thermodynamics import MemoryThermodynamics
from yadgar._shared.wiki import WikiStore
from yadgar.core.consolidation import ConsolidationScheduler

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

settings = get_settings()


@observe(
    exempt="span factory: returns an OTel start_as_current_span context manager, not a work unit; wrapping the factory call adds a spurious span with no body timing"
)
def _lifecycle_span(name: str):
    """Context manager: OTel root span for lifecycle background threads.

    Falls back to nullcontext when OTel is unavailable (I21).
    """
    try:
        from opentelemetry import trace as _ot  # noqa: PLC0415

        return _ot.get_tracer("yadgar.lifecycle").start_as_current_span(name)
    except Exception:
        return contextlib.nullcontext()


# ── PR-I: loop telemetry helpers ────────────────────────────────────────


@observe(tier="stage")
def _lc_heartbeat(loop: str) -> None:
    """PR-I: set loop heartbeat gauge. Never raises."""
    try:
        from yadgar._shared.metrics import loop_heartbeat  # noqa: PLC0415

        loop_heartbeat(loop)
    except Exception:  # noqa: BLE001
        pass


@observe(tier="stage")
def _lc_record_exc(loop: str, exc: BaseException) -> None:
    """PR-I: increment loop error counter. Never raises."""
    try:
        from yadgar._shared.metrics import loop_record_exception  # noqa: PLC0415

        loop_record_exception(loop, exc)
    except Exception:  # noqa: BLE001
        pass


# ── Getters ────────────────────────────────────────────────────────────


def _get_storage() -> StorageEngine:
    assert _st._storage is not None, "StorageEngine not initialized"
    return _st._storage


@observe(tier="stage")
def _get_embeddings() -> EmbeddingEngine:
    # §13: raise RuntimeError (not AssertionError — assert can be stripped with -O)
    if _st._embeddings is None:
        raise RuntimeError("EmbeddingEngine not initialized")
    return _st._embeddings


def _get_buffer() -> ActionLogger:
    assert _st._buffer is not None, "ActionLogger not initialized"
    return _st._buffer


def _get_consolidation() -> ConsolidationScheduler:
    assert _st._consolidation is not None, "ConsolidationScheduler not initialized"
    return _st._consolidation


def _get_staleness() -> StalenessDetector:
    assert _st._staleness is not None, "StalenessDetector not initialized"
    return _st._staleness


def _get_thermo() -> MemoryThermodynamics:
    assert _st._thermo is not None, "MemoryThermodynamics not initialized"
    return _st._thermo


def _get_retriever() -> Retriever:
    assert _st._retriever is not None, "Retriever not initialized"
    return _st._retriever


def _get_write_gate() -> WriteGate:
    assert _st._write_gate is not None, "WriteGate not initialized"
    return _st._write_gate


def _get_engram() -> EngramAllocator:
    assert _st._engram is not None, "EngramAllocator not initialized"
    return _st._engram


def _get_replay() -> CheckpointRestore:
    assert _st._replay is not None, "CheckpointRestore not initialized"
    return _st._replay


@observe(tier="stage")
def _get_file_queue():
    if _st._file_queue is None:
        with _st._queue_lock:
            if _st._file_queue is None:
                from yadgar.core.file_queue import DrainerConfig, FileQueue, QueueDrainer

                _settings = get_settings()
                base = Path(os.environ.get("YADGAR_DATA_DIR", _settings.DATA_DIR))
                # Build FileQueue first, then drainer, then start() — assign _file_queue
                # only after start() succeeds so a failed start leaves _file_queue=None.
                fq = FileQueue(base, wiki_prefix=_settings.WIKI_SLUG_PREFIX)
                drainer = QueueDrainer(
                    fq,
                    _get_storage,
                    drain_interval=float(_settings.QUEUE_DRAIN_INTERVAL),
                    config=DrainerConfig(
                        max_permanent_attempts=_settings.QUEUE_MAX_PERMANENT_ATTEMPTS,
                        max_transient_attempts=_settings.QUEUE_MAX_TRANSIENT_ATTEMPTS,
                        backoff_base_s=float(_settings.QUEUE_BACKOFF_BASE_S),
                        backoff_max_s=float(_settings.QUEUE_BACKOFF_MAX_S),
                        dlq_retention_days=_settings.QUEUE_DLQ_RETENTION_DAYS,
                    ),
                )
                drainer.start()  # may raise — do NOT assign globals before this
                _st._queue_drainer = drainer
                _st._file_queue = fq
                # Sync back to the server module's __dict__ so tests that
                # monkeypatch.setattr(server, "_queue_drainer", None) and then
                # call _get_file_queue() see the live objects instead of stale None.
                import sys as _sys  # noqa: PLC0415

                _srv = _sys.modules.get("yadgar.core.server")
                if _srv is not None:
                    # Use setattr so _ServerModule.__setattr__ keeps both
                    # server.__dict__ and _state.__dict__ in sync.
                    _srv._queue_drainer = drainer
                    _srv._file_queue = fq
    return _st._file_queue


# ── Default rules ──────────────────────────────────────────────────────


@observe(tier="stage")
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


# ── Startup helpers ────────────────────────────────────────────────────


@observe(tier="stage")
def _run_wiki_embedding_backfill(wiki) -> None:
    """Backfill NULL-embedding wiki_page rows (migration_014, v5.42.1).

    Extracted from init_engines to keep cyclomatic complexity within I13 cap.
    Called after both StorageEngine and EmbeddingEngine are ready. Idempotent.
    Failures are non-fatal — logged as WARNING; startup proceeds normally.

    Post-backfill: if NULL-embedding rows remain (embed service unavailable),
    emits a CRITICAL log so operators know the similarity gate is degraded.
    """
    try:
        null_count = wiki.backfill_null_embeddings()
        if null_count > 0:
            logger.info(
                "migration_014 backfill: %d wiki_page embeddings computed at startup",
                null_count,
            )
    except Exception as exc:
        logger.warning("migration_014 backfill failed (non-fatal): %s", exc)

    # Post-backfill audit: CRITICAL if NULL rows remain (gate still degraded).
    try:
        remaining = wiki._storage.get_wiki_pages_without_embedding()
        if remaining:
            logger.critical(
                "%d wiki_page rows still have embedding=NULL after backfill attempt — "
                "similarity gate is degraded (embed service may be unavailable). "
                "Re-run will retry automatically at next startup.",
                len(remaining),
            )
    except Exception:
        pass


# ── Startup ────────────────────────────────────────────────────────────


@observe(tier="stage")
def _emit_sd_ready() -> None:
    """v5.49.4: emit READY=1 via sd_notify after init_engines() completes.

    Extracted to keep init_engines() under the I13 cyclomatic-complexity cap (≤15).
    Silent no-op when NOTIFY_SOCKET is unset (outside systemd / container surrogate).
    """
    try:
        from yadgar.core import sd_notify as _sd_notify  # noqa: PLC0415

        _sd_notify.ready()
    except Exception:  # noqa: BLE001
        pass


@observe(tier="stage")
def _init_embedding_client(embedding_model: str | None, _settings, local_engines: bool = False):
    """Init embedding engine + ML client based on YADGAR_EMBED_URL env var.

    Returns (embeddings, ml_client). Extracted from init_engines to reduce
    cyclomatic complexity (each branch imports different client classes).

    local_engines: when True, force in-process LOCAL engines (EmbeddingEngine +
        LocalMLClient) and SKIP the offload guard.  This is the BACKEND
        recall-bootstrap context (#44): the backend IS the embed service, so it
        has no YADGAR_EMBED_URL and correctly wants local torch engines.  The
        offload guard is a CORE concern — core offloads tool bodies onto a thread
        pool and needs REMOTE engines for GIL-safety.  The backend is a
        single-purpose ML service that does NOT run the core tool pool, so local
        engines are GIL-safe there.  Default False keeps the guard firing on
        every core path (guard unchanged for core).
    """
    if os.environ.get("YADGAR_EMBED_URL"):
        from yadgar._shared.remote_embeddings import RemoteEmbeddingEngine  # noqa: PLC0415
        from yadgar.backend.ml_client import RemoteMLClient  # noqa: PLC0415

        embeddings = RemoteEmbeddingEngine(embedding_model or _settings.EMBEDDING_MODEL)
        ml_client = RemoteMLClient(os.environ["YADGAR_EMBED_URL"])
    else:
        # Fix A Claim-1: tool-body offload is only GIL-safe when the hot paths are
        # remote httpx (socket IO releases the GIL). The local torch
        # EmbeddingEngine + LocalMLClient run CPU inference on whatever thread the
        # tool body lands on — on a worker that still holds the GIL during
        # pure-python glue, defeating the offload premise. Fail loud rather than
        # silently ship a broken foundation.
        #
        # local_engines=True bypasses the guard: the BACKEND recall bootstrap (#44)
        # runs local engines by design (it IS the embed service) and is NOT the
        # core tool-body pool, so the GIL premise does not apply there.
        if not local_engines:
            from yadgar._shared.runtime.offload import offload_enabled  # noqa: PLC0415

            if offload_enabled():
                raise RuntimeError(
                    "YADGAR_OFFLOAD_TOOLS is enabled but no YADGAR_EMBED_URL is set, so "
                    "local in-process torch engines would be selected. Tool-body offload "
                    "is only GIL-safe with REMOTE engines (YADGAR_EMBED_URL + "
                    "YADGAR_DB_URL). Set YADGAR_EMBED_URL or disable offload."
                )

        from yadgar.backend.ml_client import LocalMLClient  # noqa: PLC0415

        embeddings = EmbeddingEngine(embedding_model or _settings.EMBEDDING_MODEL)
        ml_client = LocalMLClient(_settings)
    return embeddings, ml_client


@observe(tier="stage")
def _init_secondary_engines(_settings) -> None:
    """Assign all secondary engine singletons in their required init order.

    Must be called after _st._storage and _st._embeddings are set.
    Extracted from init_engines to reduce LOC and cognitive load; no
    branching — pure linear construction.
    """
    _st._buffer = ActionLogger(_st._storage, _settings)
    _st._buffer.start_session()
    _st._thermo = MemoryThermodynamics(_st._storage, _st._embeddings, _settings)
    _st._kg = KnowledgeGraph(_st._storage, _settings)
    _st._cognitive_map = CognitiveMap(_st._storage, _settings)


@observe(tier="stage")
def _init_retriever_and_post_engines(
    _settings, ml_client, engine_set: Literal["slim", "full"] = "full"
) -> None:
    """Init retriever, write-gate, engram, rules, causal, metacognition, replay, wiki.

    Called after _init_secondary_engines(). Wires cross-engine dependencies
    (set_engram, set_rules_engine, set_metacognition) and exposes
    consolidation sub-engines as server globals.

    engine_set (Car 3, folder-split #17): "full" (default) builds every engine —
    the CORE path (core/memorize/consolidation) is unchanged. "slim" builds ONLY
    the 14 engines the BACKEND ``/recall`` bootstrap needs and SKIPS the 10
    CORE-ONLY engines unreachable from ``recall_pipeline`` / ``embed_service``
    (``_staleness, _curator, _prospective, _narrative, _sleep, _write_gate,
    _causal, _cls, _file_queue, _queue_drainer``). ``_consolidation`` is still
    constructed in slim because ``_pool`` (part of the 14) is one of its
    attributes; ConsolidationScheduler.__init__ starts no daemon threads without
    ``start_daemons=True``, so constructing it in slim is inert.
    """
    _full = engine_set == "full"
    # Car 2 (folder-split #17): inject the REAL process-global `ce` cache singleton
    # at the composition root — the same registry instance the deleted lazy
    # Reranker fallback fetched, so recall output is byte-identical (live CE dedup).
    from yadgar.backend.cache import get_ce_cache  # noqa: PLC0415

    _st._retriever = Retriever(
        _st._storage,
        _st._embeddings,
        _st._kg,
        _settings,
        ml_client=ml_client,
        ce_cache=get_ce_cache(),
    )
    # CORE-ONLY (skipped in slim): _curator
    if _full:
        _st._curator = MemoryCurator(_st._storage, _st._embeddings, _st._thermo, _settings)
    # _consolidation is one of the 14 (backend needs its `_pool` attribute).
    _st._consolidation = ConsolidationScheduler(_st._storage, _st._embeddings, _settings)
    # CORE-ONLY (skipped in slim): _staleness, _prospective, _narrative, _write_gate
    if _full:
        _st._staleness = StalenessDetector(_st._storage, _settings)
        _st._prospective = ProspectiveMemoryEngine(_st._storage, _settings)
        _st._narrative = NarrativeEngine(_st._storage, _st._kg, _settings)
        _st._write_gate = WriteGate(_st._storage, _st._embeddings, _st._retriever, _settings)
    _st._engram = EngramAllocator(_st._storage, _settings)
    _st._rules_engine = RulesEngine(_st._storage, _settings)
    _load_default_rules(_st._rules_engine)
    # CORE-ONLY (skipped in slim): _causal
    if _full:
        _st._causal = CausalDiscovery(_st._storage, _st._kg, _settings)
    _st._metacognition = MetaCognition(_st._storage, _st._embeddings, _st._kg, _settings)
    _st._replay = CheckpointRestore(
        storage=_st._storage,
        embeddings=_st._embeddings,
        retriever=_st._retriever,
        cognitive_map=_st._cognitive_map,
        metacognition=_st._metacognition,
        settings=_settings,
    )
    _st._wiki = WikiStore(_st._storage, _st._embeddings)
    _st._retriever.set_engram(_st._engram)
    _st._retriever.set_rules_engine(_st._rules_engine)
    _st._retriever.set_metacognition(_st._metacognition)

    # Expose inner engines as server-level globals for direct access.
    # _pool is one of the 14 (backend recall uses it); _sleep + _cls are CORE-ONLY.
    _st._pool = _st._consolidation.pool
    if _full:
        _st._sleep = _st._consolidation._sleep_engine
        _st._cls = _st._consolidation.cls


@observe(
    exempt="opens a manual start_as_current_span/OTel root span in-body for a background thread; @observe would double-span the work unit (I21)"
)
def _metrics_loop(pid: int, db_path: str, storage: object) -> None:
    """Background thread: sample system metrics every 5 s (PR-I).

    Extracted from init_engines closure to module level so it is a named
    function (improves traceability in thread dumps). Captures pid/db_path/
    storage via explicit args (same semantics as the previous default-arg closure).
    """
    from yadgar.core.graph_api import sample_system_metrics  # noqa: PLC0415

    sample_system_metrics(pid, db_path, storage)  # prime CPU delta baseline
    while True:
        time.sleep(5)
        try:
            with _lifecycle_span("lifecycle.metrics_sample"):
                result = sample_system_metrics(pid, db_path, storage)
                # §9 Q6: update under lock to prevent torn reads.
                with _st._metrics_lock:
                    _st._system_metrics_cache.update(result)
        except Exception:
            pass


@observe(
    exempt="opens a manual start_as_current_span/OTel root span in-body for a background thread; @observe would double-span the work unit (I21)"
)
def _reranker_idle_loop() -> None:
    """Background thread: unload idle rerankers (PR-I).

    Extracted from init_engines closure; frees ~500 MB after the idle-unload
    threshold of no recall activity. Emits heartbeat + error counter via PR-I
    helpers. Check interval + unload threshold are config.yaml-authoritative
    (RERANKER_IDLE_CHECK_INTERVAL_SEC / RERANKER_IDLE_UNLOAD_SEC) — v5.95.
    """
    from yadgar._shared.config import get_settings  # noqa: PLC0415 -- avoid import cycle

    while True:
        _lc_heartbeat("model_unload")  # PR-I: heartbeat at top of every iteration
        _cfg = get_settings()
        time.sleep(_cfg.RERANKER_IDLE_CHECK_INTERVAL_SEC)
        try:
            with _lifecycle_span("lifecycle.reranker_idle_check"):
                if _st._retriever is not None:
                    _st._retriever.unload_rerankers_if_idle(
                        idle_seconds=_cfg.RERANKER_IDLE_UNLOAD_SEC
                    )
        except Exception as _exc:
            _lc_record_exc("model_unload", _exc)  # PR-I: loop error counter


@observe(tier="stage")
def _viz_loop(host: str, port: int) -> None:
    """Background thread: run the viz server (auto-started with daemon).

    Extracted from init_engines closure. Binds the same interface as the
    MCP server (settings.HOST). Containers override via YADGAR_HOST=0.0.0.0
    so the host-side docker port mapping (-p 127.0.0.1:42069:42069) works.
    OSError is caught separately to emit a specific port-conflict warning.
    """
    try:
        from yadgar.core.viz_server import run_viz_server  # noqa: PLC0415

        logger.info("Viz server starting on http://%s:%d", host, port)
        run_viz_server(host=host, port=port)
    except OSError as exc:
        logger.warning("Viz server could not bind port %d: %s", port, exc)
    except Exception as exc:
        logger.warning("Viz server error: %s", exc)


@observe(tier="stage")
def _start_daemon_threads(watch_directory: str | None, _settings) -> None:
    """Start background daemon threads (metrics, reranker-idle, viz).

    Called only when start_daemons=True. Extracted from init_engines to
    reduce its cyclomatic complexity; preserves exact thread startup order.
    """
    # v5.7.0 PR-0: consolidation daemon removed; cron takes over in PR-1.
    # _st._consolidation.start() intentionally removed.
    if watch_directory:
        _st._staleness.start(watch_directory)

    # Background system-metrics sampler for /api/system and SSE events
    _pid = os.getpid()
    _db_path = _settings.DB_PATH
    _storage_ref = _st._storage  # capture at call time
    threading.Thread(target=_metrics_loop, args=(_pid, _db_path, _storage_ref), daemon=True).start()

    # Idle reranker unloader — frees ~500 MB after 10 min of no recall activity
    threading.Thread(target=_reranker_idle_loop, daemon=True).start()

    # Auto-start viz server alongside the daemon.
    _viz_port = getattr(_settings, "VIZ_PORT", 42069)
    _viz_host = getattr(_settings, "HOST", "127.0.0.1")
    threading.Thread(target=_viz_loop, args=(_viz_host, _viz_port), daemon=True).start()


@observe(tier="stage")
def _init_file_queue() -> None:
    """Start the file queue drainer; non-fatal on failure.

    Extracted from init_engines to reduce its cyclomatic complexity.
    """
    try:
        _get_file_queue()
    except Exception as exc:
        logger.warning("File queue init failed (non-fatal): %s", exc)


@observe(tier="stage")
def _inject_storage_caches(storage) -> None:
    """Inject the REAL backend cache singletons into StorageEngine (Car 2, #17).

    The composition root is the single sanctioned ``_shared → backend`` edge: it
    fetches the same process-global registry instances the deleted lazy storage
    resolvers used and assigns them to the StorageEngine's DI attributes. Because
    the injected object IS the registry singleton the lazy path would have
    materialised, recall/read output is byte-identical (live caching preserved).

    Bare-constructed StorageEngines (tests) skip this and fall back to the
    ``_shared`` NullCache / NullScopeVersions defaults (all-miss ≡ uncached read).
    """
    from yadgar.backend.cache import (  # noqa: PLC0415
        get_engram_slot_cache,
        get_graph_cache,
        get_memory_doc_cache,
        get_scope_versions,
    )

    storage._memory_doc_cache = get_memory_doc_cache()
    storage._engram_slot_cache = get_engram_slot_cache()
    storage._graph_cache = get_graph_cache()
    storage._scope_versions = get_scope_versions()


@observe(tier="boundary")
def init_engines(
    db_path: str | None = None,
    embedding_model: str | None = None,
    start_daemons: bool = False,
    watch_directory: str | None = None,
    local_engines: bool = False,
    engine_set: Literal["slim", "full"] = "full",
):
    """Initialize all engines. Returns (storage, embeddings, buffer, consolidation, staleness).

    local_engines: when True, force local in-process ML engines and skip the
        core offload guard.  Used by the BACKEND recall bootstrap (#44) — the
        backend is the embed service (no YADGAR_EMBED_URL) and is not the core
        tool-body pool, so local engines are correct + GIL-safe there.  Default
        False keeps every core init path on the guarded selection logic.

    engine_set (Car 3, folder-split #17): "full" (default) builds every engine —
        core/memorize/consolidation are unchanged. "slim" builds ONLY the 14
        engines the BACKEND ``/recall`` bootstrap needs and skips the 10
        CORE-ONLY engines (incl. the file-queue drainer). Behavior-neutral for
        recall: the backend ``/recall`` output is byte-identical to full because
        every engine the recall path touches is in the 14. A missing engine
        surfaces immediately as a None-crash on the first ``/recall`` (caught by
        the backend-recall parity smoke).
    """
    # Q16: reset shutdown flag so a re-initialized server can shut down cleanly
    _st._shutdown_done = False

    _settings = get_settings()
    _st._storage = StorageEngine(db_path or _settings.DB_PATH)
    _inject_storage_caches(_st._storage)
    _st._embeddings, _ml_client = _init_embedding_client(
        embedding_model, _settings, local_engines=local_engines
    )

    _init_secondary_engines(_settings)
    _init_retriever_and_post_engines(_settings, _ml_client, engine_set=engine_set)

    if start_daemons:
        _start_daemon_threads(watch_directory, _settings)

    # Eagerly warm up the embedding model so the first recall isn't slow.
    _st._embeddings._ensure_model()

    # migration_014 backfill: encode NULL-embedding wiki_page rows.
    # Runs after both StorageEngine + EmbeddingEngine are ready. Idempotent.
    _run_wiki_embedding_backfill(_st._wiki)

    # File queue drainer (_file_queue + _queue_drainer) is CORE-ONLY — the
    # backend /recall bootstrap (engine_set="slim") never writes, so skip it.
    if engine_set == "full":
        _init_file_queue()

    # v5.49.4: emit READY=1 — all engines initialised, server accepting requests.
    _emit_sd_ready()

    return _st._storage, _st._embeddings, _st._buffer, _st._consolidation, _st._staleness


@observe(tier="boundary")
def shutdown():
    """Gracefully shut down all engines. Idempotent — safe to call twice (Q16)."""
    if _st._shutdown_done:
        return
    _st._shutdown_done = True

    # v5.49.0 Phase 6: signal sd_notify STOPPING=1 immediately
    try:
        from yadgar.core import sd_notify as _sd_notify  # noqa: PLC0415

        _sd_notify.stopping()
    except Exception:  # noqa: BLE001
        pass

    # Fix A (daemon-offload-A): tear down the tool-offload pool (O10). Non-blocking
    # on wedged in-flight workers (they can't be killed; cancel queued work) so a
    # stuck git can't hang graceful stop past the systemd stop-timeout.
    try:
        from yadgar._shared.runtime.offload import shutdown_pool  # noqa: PLC0415

        shutdown_pool()
    except Exception:  # noqa: BLE001
        pass

    # v5.50.10: tear down OTEL with a hard time bound — a dead/unreachable OTLP
    # collector must never hang shutdown (it used to retry the final span flush
    # past the systemd stop-timeout → SIGKILL/exit-137 on every restart).
    try:
        from yadgar._shared.tracing import shutdown_tracing as _shutdown_tracing  # noqa: PLC0415

        _shutdown_tracing(timeout_sec=3.0)
    except Exception:  # noqa: BLE001
        pass

    # v5.49.0 Phase 6: flush file queue before tearing down storage
    if _st._queue_drainer is not None:
        _st._queue_drainer.flush_barrier(timeout=10.0)
        _st._queue_drainer.stop()
    # v5.7.0 PR-0: consolidation daemon removed; no stop() needed.
    if _st._staleness is not None:
        _st._staleness.stop()
    if _st._buffer is not None:
        _st._buffer.flush()
    # v5.49.0 Phase 6: snapshot embed caches before closing storage
    try:
        from yadgar.core.drain import snapshot_embed_caches as _snap  # noqa: PLC0415

        _snap()
    except Exception:  # noqa: BLE001
        pass
    if _st._storage is not None:
        _st._storage.close()

    _st._storage = None
    _st._embeddings = None
    _st._buffer = None
    _st._consolidation = None
    _st._staleness = None
    _st._thermo = None
    _st._retriever = None
    _st._curator = None
    _st._prospective = None
    _st._narrative = None
    _st._sleep = None
    _st._pool = None
    _st._kg = None
    _st._write_gate = None
    _st._engram = None
    _st._rules_engine = None
    _st._cls = None
    _st._cognitive_map = None
    _st._causal = None
    _st._metacognition = None
    _st._replay = None
    _st._wiki = None
    _st._file_queue = None
    _st._queue_drainer = None

    # Remove PID file on clean shutdown
    try:
        _paths.PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass


_SENSITIVE_DRAIN_POLL_SEC = 0.05  # poll interval while draining (models drain.py)


@observe(tier="stage")
def _drain_sensitive_lock(timeout: float) -> bool:
    """Bounded synchronous wait for an in-process sensitive job to release its lock.

    Models ``yadgar.drain.drain_in_flight_requests`` (poll-until-clear with a
    deadline) but synchronous — the signal handler runs in the main thread, not an
    event loop.  Returns True if the lock cleared (released or became stale) before
    the timeout, False on timeout.  NEVER shuts down on timeout — the caller
    REFUSES the shutdown instead, so a still-running swap is never interrupted.
    """
    from yadgar.core import sensitive_lock  # noqa: PLC0415

    deadline = time.monotonic() + timeout
    while sensitive_lock.is_held_by_live_job():
        if time.monotonic() >= deadline:
            logger.warning(
                "sensitive-job drain timed out after %.1fs — REFUSING shutdown "
                "(job still holds the lock; will not interrupt mid-swap)",
                timeout,
            )
            return False
        time.sleep(_SENSITIVE_DRAIN_POLL_SEC)
    logger.info("sensitive-job lock cleared — proceeding with shutdown")
    return True


@observe(tier="stage")
def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown.

    v5.69 P3 — sensitive-job drain:  if a sensitive job (vacuum) holds the lock,
    an EXTERNAL shutdown signal must NOT interrupt it mid-swap (the 06-16
    data-loss mode).  We distinguish the vacuum's OWN teardown from an external
    operator stop via the lock's pid:

      * lock held by a LIVE job whose ``pid == os.getpid()``  → the sensitive job
        runs in THIS process; an external signal targeting it would interrupt the
        swap → DRAIN (bounded) and only shut down once the lock clears; on timeout
        REFUSE (return without shutting down).
      * lock held by a LIVE job whose ``pid != os.getpid()``  → the job runs in a
        SEPARATE process (the vacuum runs as ``yadgar-vacuum.service``, not inside
        core).  That separate vacuum stops core via ``ServiceController.stop()``
        (systemctl stop yadgar yadgar-backend), which delivers core THIS same
        SIGTERM.  We must let that teardown proceed — blocking it would deadlock
        the vacuum's own stop → SIGKILL.  So we PROCEED (immediate shutdown).
      * no lock, or a STALE lock (dead pid / TTL-expired)     → behave exactly as
        before P3: immediate shutdown.

    DOCUMENTED RESIDUAL RACE (narrow, accepted for 5.69):  an EXTERNAL operator
    ``systemctl stop yadgar`` arriving at core WHILE a separate-process vacuum
    holds the lock is indistinguishable here from the vacuum's own
    ``ServiceController.stop()`` — both are a SIGTERM to core with the lock pid !=
    core's pid — so we proceed and core shuts down.  This is acceptable because
    the vacuum's atomic-swap design (P2) never leaves the canonical empty/partial
    even if core dies (the swap is gated behind a verified side-build; crash
    mid-swap is recovered at next start).  The clean fix is systemd
    ``RefuseManualStop`` on yadgar.service — out of scope for 5.69, tracked as a
    follow-up.
    """
    logger.info("Received signal %s", signum)
    try:
        from yadgar.core import sensitive_lock  # noqa: PLC0415

        payload = sensitive_lock.read()
        if sensitive_lock.is_held_by_live_job():
            in_process = (payload or {}).get("pid") == os.getpid()
            if in_process:
                # External stop targeting THIS process while it runs a sensitive
                # job → drain before shutting down; refuse on timeout.
                _settings = get_settings()
                timeout = float(getattr(_settings, "SENSITIVE_DRAIN_TIMEOUT_SEC", 300.0))
                logger.warning(
                    "signal %s arrived while an in-process sensitive job (job=%s) "
                    "holds the lock — draining up to %.1fs before shutdown",
                    signum,
                    (payload or {}).get("job"),
                    timeout,
                )
                if not _drain_sensitive_lock(timeout):
                    # REFUSED: do not shut down, do not exit — never interrupt
                    # the swap.  systemd will eventually SIGKILL if it must, but
                    # we will not voluntarily empty the store mid-vacuum.
                    return
            else:
                # Separate-process job holds the lock (vacuum stopping core) —
                # proceed so the vacuum's own teardown is not deadlocked.
                logger.info(
                    "signal %s while a separate-process sensitive job (pid=%s) holds "
                    "the lock — proceeding (vacuum-initiated stop is authorized)",
                    signum,
                    (payload or {}).get("pid"),
                )
    except Exception:  # noqa: BLE001 — never let lock logic block a real shutdown
        logger.debug("sensitive-lock check in signal handler failed (non-fatal)", exc_info=True)

    logger.info("shutting down (signal %s)", signum)
    shutdown()
    sys.exit(0)


@observe(tier="stage")
def _run_update_check() -> None:
    """Background thread target: probe PyPI for a newer yadgar version.

    Non-fatal: any exception is logged at WARNING and swallowed so the
    calling thread (daemon startup) is not affected.

    Runs once on daemon start when UPDATE_CHECK_ON_START=True.
    No periodic scheduling — v5.49+ candidate.
    """
    try:
        from yadgar import __version__  # noqa: PLC0415
        from yadgar.core.update.check import probe_latest_version  # noqa: PLC0415

        _settings = settings  # module-level singleton
        result = probe_latest_version(
            url=_settings.UPDATE_PYPI_URL,
            timeout=_settings.UPDATE_CHECK_TIMEOUT_SECONDS,
        )
        if result.available_version != __version__:
            logger.warning(
                "yadgar update available: %s → %s  |  run: %s",
                __version__,
                result.available_version,
                "yadgar update --check  (for upgrade command)",
            )
        else:
            logger.info("yadgar is up to date (%s)", __version__)
    except Exception as exc:  # noqa: BLE001
        logger.warning("update check failed (non-fatal): %s", exc)


@observe(tier="stage")
def _maybe_auto_check_for_update() -> None:
    """Spawn a background update-check thread if UPDATE_CHECK_ON_START=True.

    The thread is daemon=True so it does not prevent process exit.
    Returns immediately — probe latency does NOT block daemon startup.

    Reads env directly (bypasses lru_cache) so tests can monkeypatch the env
    and observe the correct behavior without restarting the process.
    """
    check_on_start = resolve_knob(
        "YADGAR_UPDATE_CHECK_ON_START",
        "UPDATE_CHECK_ON_START",
        lambda s: s.strip().lower() in ("1", "true", "yes", "on"),
        False,
    )
    if not check_on_start:
        return

    t = threading.Thread(
        target=_run_update_check,
        name="yadgar-update-check",
        daemon=True,
    )
    t.start()
    logger.debug("update check thread started (daemon=True)")


@observe(tier="stage")
def _emit_startup_diagnostics(settings) -> None:
    """Emit startup config diagnostics and the BC-EN2b COMET-dormant warning.

    The config-dump (emit_startup_config_log) and gauge-seeding (_set_config_gauges)
    are best-effort and may raise in some container environments. They MUST NOT
    be able to swallow the COMET dormant warning — historically all three shared
    one try/except whose `except` logged at DEBUG, so a raise in either sibling
    silently skipped warn_comet_dormant (the v5.x "silent dogfood" observation).

    Each concern now has its OWN try/except so warn_comet_dormant always fires
    regardless of the other two. The import itself is also isolated so an import
    failure cannot skip the warning either.
    """
    try:
        from yadgar._shared.config_registry import (  # noqa: PLC0415
            _set_config_gauges,
            emit_startup_config_log,
            warn_comet_dormant,
        )
    except Exception:
        logger.debug("config_registry import failed (non-fatal)", exc_info=True)
        return

    try:
        emit_startup_config_log()
    except Exception:
        logger.debug("emit_startup_config_log failed (non-fatal)", exc_info=True)

    try:
        _set_config_gauges()
    except Exception:
        logger.debug("_set_config_gauges failed (non-fatal)", exc_info=True)

    # BC-EN2b: announce COMET dormant state exactly once at startup (ADR-0004).
    # Own try/except — must always fire even if the calls above raised.
    try:
        warn_comet_dormant(settings)
    except Exception:
        logger.debug("warn_comet_dormant failed (non-fatal)", exc_info=True)


# NOTE (Car 3, folder-split #17): ``main()`` — the MCP-server app entry point —
# moved to ``yadgar.server._startup`` (a CORE module). It imported
# ``server._app.mcp_server`` + ``server.tools.misc`` (sync_instructions /
# install_hooks), which are ``_shared → server`` edges. Pure engine lifecycle
# (init_engines / shutdown / signal handler / startup diagnostics) stays here in
# ``_shared`` — none of it touches ``yadgar.server``. ``main()`` calls back into
# these via a core→_shared import (allowed by the layered contract).
