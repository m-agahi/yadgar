"""Singleton getter functions, init_engines, shutdown, signal handler, and main.

All module-level singleton state lives in _state.py.
Getters here provide typed access with assertions.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yadgar.paths as _paths
import yadgar.server._state as _st
from yadgar.causal_discovery import CausalDiscovery
from yadgar.cognitive_map import CognitiveMap
from yadgar.config import get_settings
from yadgar.consolidation import ConsolidationScheduler
from yadgar.curation import MemoryCurator
from yadgar.embeddings import EmbeddingEngine
from yadgar.engram import EngramAllocator
from yadgar.knowledge_graph import KnowledgeGraph
from yadgar.metacognition import MetaCognition
from yadgar.narrative import NarrativeEngine
from yadgar.predictive_coding import WriteGate
from yadgar.prospective import ProspectiveMemoryEngine
from yadgar.restoration import CheckpointRestore
from yadgar.retrieval import Retriever
from yadgar.rules_engine import RulesEngine
from yadgar.sensory_buffer import ActionLogger
from yadgar.staleness import StalenessDetector
from yadgar.storage import StorageEngine
from yadgar.thermodynamics import MemoryThermodynamics
from yadgar.wiki import WikiStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

settings = get_settings()


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


def _lc_heartbeat(loop: str) -> None:
    """PR-I: set loop heartbeat gauge. Never raises."""
    try:
        from yadgar.metrics import loop_heartbeat  # noqa: PLC0415

        loop_heartbeat(loop)
    except Exception:  # noqa: BLE001
        pass


def _lc_record_exc(loop: str, exc: BaseException) -> None:
    """PR-I: increment loop error counter. Never raises."""
    try:
        from yadgar.metrics import loop_record_exception  # noqa: PLC0415

        loop_record_exception(loop, exc)
    except Exception:  # noqa: BLE001
        pass


# ── Getters ────────────────────────────────────────────────────────────


def _get_storage() -> StorageEngine:
    assert _st._storage is not None, "StorageEngine not initialized"
    return _st._storage


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


def _get_file_queue():
    if _st._file_queue is None:
        with _st._queue_lock:
            if _st._file_queue is None:
                from yadgar.file_queue import DrainerConfig, FileQueue, QueueDrainer

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

                _srv = _sys.modules.get("yadgar.server")
                if _srv is not None:
                    # Use setattr so _ServerModule.__setattr__ keeps both
                    # server.__dict__ and _state.__dict__ in sync.
                    _srv._queue_drainer = drainer
                    _srv._file_queue = fq
    return _st._file_queue


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


# ── Startup helpers ────────────────────────────────────────────────────


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


def _emit_sd_ready() -> None:
    """v5.49.4: emit READY=1 via sd_notify after init_engines() completes.

    Extracted to keep init_engines() under the I13 cyclomatic-complexity cap (≤15).
    Silent no-op when NOTIFY_SOCKET is unset (outside systemd / container surrogate).
    """
    try:
        from yadgar import sd_notify as _sd_notify  # noqa: PLC0415

        _sd_notify.ready()
    except Exception:  # noqa: BLE001
        pass


def _init_embedding_client(embedding_model: str | None, _settings):
    """Init embedding engine + ML client based on YADGAR_EMBED_URL env var.

    Returns (embeddings, ml_client). Extracted from init_engines to reduce
    cyclomatic complexity (each branch imports different client classes).
    """
    if os.environ.get("YADGAR_EMBED_URL"):
        from yadgar.backend.ml_client import RemoteMLClient  # noqa: PLC0415
        from yadgar.remote_embeddings import RemoteEmbeddingEngine  # noqa: PLC0415

        embeddings = RemoteEmbeddingEngine(embedding_model or _settings.EMBEDDING_MODEL)
        ml_client = RemoteMLClient(os.environ["YADGAR_EMBED_URL"])
    else:
        from yadgar.backend.ml_client import LocalMLClient  # noqa: PLC0415

        embeddings = EmbeddingEngine(embedding_model or _settings.EMBEDDING_MODEL)
        ml_client = LocalMLClient(_settings)
    return embeddings, ml_client


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


def _init_retriever_and_post_engines(_settings, ml_client) -> None:
    """Init retriever, write-gate, engram, rules, causal, metacognition, replay, wiki.

    Called after _init_secondary_engines(). Wires cross-engine dependencies
    (set_engram, set_rules_engine, set_metacognition) and exposes
    consolidation sub-engines as server globals.
    """
    _st._retriever = Retriever(
        _st._storage, _st._embeddings, _st._kg, _settings, ml_client=ml_client
    )
    _st._curator = MemoryCurator(_st._storage, _st._embeddings, _st._thermo, _settings)
    _st._consolidation = ConsolidationScheduler(_st._storage, _st._embeddings, _settings)
    _st._staleness = StalenessDetector(_st._storage, _settings)
    _st._prospective = ProspectiveMemoryEngine(_st._storage, _settings)
    _st._narrative = NarrativeEngine(_st._storage, _st._kg, _settings)
    _st._write_gate = WriteGate(_st._storage, _st._embeddings, _st._retriever, _settings)
    _st._engram = EngramAllocator(_st._storage, _settings)
    _st._rules_engine = RulesEngine(_st._storage, _settings)
    _load_default_rules(_st._rules_engine)
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

    # Expose inner engines as server-level globals for direct access
    _st._sleep = _st._consolidation._sleep_engine
    _st._pool = _st._consolidation.pool
    _st._cls = _st._consolidation.cls


def _metrics_loop(pid: int, db_path: str, storage: object) -> None:
    """Background thread: sample system metrics every 5 s (PR-I).

    Extracted from init_engines closure to module level so it is a named
    function (improves traceability in thread dumps). Captures pid/db_path/
    storage via explicit args (same semantics as the previous default-arg closure).
    """
    from yadgar.graph_api import sample_system_metrics  # noqa: PLC0415

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


def _reranker_idle_loop() -> None:
    """Background thread: unload idle rerankers every 60 s (PR-I).

    Extracted from init_engines closure; frees ~500 MB after 10 min of
    no recall activity. Emits heartbeat + error counter via PR-I helpers.
    """
    while True:
        _lc_heartbeat("model_unload")  # PR-I: heartbeat at top of every iteration
        time.sleep(60)
        try:
            with _lifecycle_span("lifecycle.reranker_idle_check"):
                if _st._retriever is not None:
                    _st._retriever.unload_rerankers_if_idle(idle_seconds=600.0)
        except Exception as _exc:
            _lc_record_exc("model_unload", _exc)  # PR-I: loop error counter


def _viz_loop(host: str, port: int) -> None:
    """Background thread: run the viz server (auto-started with daemon).

    Extracted from init_engines closure. Binds the same interface as the
    MCP server (settings.HOST). Containers override via YADGAR_HOST=0.0.0.0
    so the host-side docker port mapping (-p 127.0.0.1:42069:42069) works.
    OSError is caught separately to emit a specific port-conflict warning.
    """
    try:
        from yadgar.viz_server import run_viz_server  # noqa: PLC0415

        logger.info("Viz server starting on http://%s:%d", host, port)
        run_viz_server(host=host, port=port)
    except OSError as exc:
        logger.warning("Viz server could not bind port %d: %s", port, exc)
    except Exception as exc:
        logger.warning("Viz server error: %s", exc)


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


def _init_file_queue() -> None:
    """Start the file queue drainer; non-fatal on failure.

    Extracted from init_engines to reduce its cyclomatic complexity.
    """
    try:
        _get_file_queue()
    except Exception as exc:
        logger.warning("File queue init failed (non-fatal): %s", exc)


def init_engines(
    db_path: str | None = None,
    embedding_model: str | None = None,
    start_daemons: bool = False,
    watch_directory: str | None = None,
):
    """Initialize all engines. Returns (storage, embeddings, buffer, consolidation, staleness)."""
    # Q16: reset shutdown flag so a re-initialized server can shut down cleanly
    _st._shutdown_done = False

    _settings = get_settings()
    _st._storage = StorageEngine(db_path or _settings.DB_PATH)
    _st._embeddings, _ml_client = _init_embedding_client(embedding_model, _settings)

    _init_secondary_engines(_settings)
    _init_retriever_and_post_engines(_settings, _ml_client)

    if start_daemons:
        _start_daemon_threads(watch_directory, _settings)

    # Eagerly warm up the embedding model so the first recall isn't slow.
    _st._embeddings._ensure_model()

    # migration_014 backfill: encode NULL-embedding wiki_page rows.
    # Runs after both StorageEngine + EmbeddingEngine are ready. Idempotent.
    _run_wiki_embedding_backfill(_st._wiki)

    _init_file_queue()

    # v5.49.4: emit READY=1 — all engines initialised, server accepting requests.
    _emit_sd_ready()

    return _st._storage, _st._embeddings, _st._buffer, _st._consolidation, _st._staleness


def shutdown():
    """Gracefully shut down all engines. Idempotent — safe to call twice (Q16)."""
    if _st._shutdown_done:
        return
    _st._shutdown_done = True

    # v5.49.0 Phase 6: signal sd_notify STOPPING=1 immediately
    try:
        from yadgar import sd_notify as _sd_notify  # noqa: PLC0415

        _sd_notify.stopping()
    except Exception:  # noqa: BLE001
        pass

    # v5.50.10: tear down OTEL with a hard time bound — a dead/unreachable OTLP
    # collector must never hang shutdown (it used to retry the final span flush
    # past the systemd stop-timeout → SIGKILL/exit-137 on every restart).
    try:
        from yadgar.tracing import shutdown_tracing as _shutdown_tracing  # noqa: PLC0415

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
        from yadgar.drain import snapshot_embed_caches as _snap  # noqa: PLC0415

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


def _drain_sensitive_lock(timeout: float) -> bool:
    """Bounded synchronous wait for an in-process sensitive job to release its lock.

    Models ``yadgar.drain.drain_in_flight_requests`` (poll-until-clear with a
    deadline) but synchronous — the signal handler runs in the main thread, not an
    event loop.  Returns True if the lock cleared (released or became stale) before
    the timeout, False on timeout.  NEVER shuts down on timeout — the caller
    REFUSES the shutdown instead, so a still-running swap is never interrupted.
    """
    from yadgar import sensitive_lock  # noqa: PLC0415

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
        from yadgar import sensitive_lock  # noqa: PLC0415

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


def _run_update_check() -> None:
    """Background thread target: probe PyPI for a newer yadgar version.

    Non-fatal: any exception is logged at WARNING and swallowed so the
    calling thread (daemon startup) is not affected.

    Runs once on daemon start when UPDATE_CHECK_ON_START=True.
    No periodic scheduling — v5.49+ candidate.
    """
    try:
        from yadgar import __version__  # noqa: PLC0415
        from yadgar.update.check import probe_latest_version  # noqa: PLC0415

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


def _maybe_auto_check_for_update() -> None:
    """Spawn a background update-check thread if UPDATE_CHECK_ON_START=True.

    The thread is daemon=True so it does not prevent process exit.
    Returns immediately — probe latency does NOT block daemon startup.

    Reads env directly (bypasses lru_cache) so tests can monkeypatch the env
    and observe the correct behavior without restarting the process.
    """
    # Read env directly to bypass lru_cache (important for testability)
    raw = os.environ.get("YADGAR_UPDATE_CHECK_ON_START", "false").lower()
    check_on_start = raw in ("1", "true", "yes")
    if not check_on_start:
        return

    t = threading.Thread(
        target=_run_update_check,
        name="yadgar-update-check",
        daemon=True,
    )
    t.start()
    logger.debug("update check thread started (daemon=True)")


def main(
    port: int | None = None,
    db_path: str | None = None,
    transport: str = "stdio",
):
    from yadgar.server._app import mcp_server

    _st._active_transport = transport
    _st._start_time = time.time()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Self-register PID file so `yadgar daemon stop/restart/status` can find us
    # regardless of how the process was started (systemd, direct CLI, etc.).
    _pid_path = _paths.PID_PATH
    try:
        _pid_path.parent.mkdir(parents=True, exist_ok=True)
        _pid_path.write_text(str(os.getpid()))
    except Exception:
        pass

    # H-7: Fail fast if REQUIRE_AUTH=True but no token configured.
    # A server that requires auth but has no token is silently broken — every
    # request would get 503 "Admin token not configured" rather than a useful error.
    # Use Settings() directly (bypass lru_cache) so the check always reflects the
    # current environment — important for tests that reload yadgar.config.
    from yadgar.config import Settings as _Settings  # noqa: PLC0415

    _auth_settings = _Settings()
    if _auth_settings.REQUIRE_AUTH and not _auth_settings.MCP_AUTH_TOKEN:
        raise RuntimeError(
            "REQUIRE_AUTH=1 requires YADGAR_MCP_AUTH_TOKEN to be set. "
            "Source /etc/yadgar/secrets.env or run `yadgar setup`."
        )

    # Don't auto-watch cwd — in daemon/systemd mode cwd is $HOME, which would
    # recursively watch everything including the DB files, causing a watchdog storm.
    # Staleness watching is triggered per-project via MCP tools instead.
    init_engines(
        db_path=db_path,
        start_daemons=True,
        watch_directory=None,
    )

    # v5.6.7 PR-J: emit startup config-dump log + seed config gauges
    try:
        from yadgar.config_registry import (  # noqa: PLC0415
            _set_config_gauges,
            emit_startup_config_log,
        )

        emit_startup_config_log()
        _set_config_gauges()
    except Exception:
        logger.debug("startup config-dump failed (non-fatal)", exc_info=True)

    # Auto-sync CLAUDE.md on every startup so rules stay current
    try:
        from yadgar.server.tools.misc import sync_instructions

        sync_instructions()
        from yadgar import __version__

        logger.info("CLAUDE.md synced with Yadgar v%s", __version__)
    except Exception:
        logger.debug("Auto-sync of CLAUDE.md failed (non-fatal)")

    # Auto-install hooks for the current project if not already present
    try:
        from yadgar.server.tools.misc import install_hooks

        install_hooks(os.getcwd())
        logger.info("Hippocampal Replay hooks installed for %s", os.getcwd())
    except Exception:
        logger.debug("Auto-install of hooks failed (non-fatal)")

    # v5.48.0: opt-in auto-check for updates on daemon start (default OFF)
    _maybe_auto_check_for_update()

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
