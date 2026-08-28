"""Singleton getter functions, init_engines, shutdown, and signal handler.

All module-level singleton state lives in _state.py.
Getters here provide typed access with assertions.

Car 3 (folder-split #17): the MCP-server ``main()`` entry point moved to
``yadgar.server._startup`` (core side) — it imported ``server._app`` +
``server.tools.misc``, which are ``_shared → server`` edges. The pure engine
lifecycle stays here; it has no ``yadgar.server`` imports.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Literal

import yadgar._shared.paths as _paths
import yadgar._shared.runtime.state as _st  # sibling; avoid server/__init__ re-entry (Car 1)
from yadgar._shared.astrocyte_pool import AstrocytePool
from yadgar._shared.config import get_settings
from yadgar._shared.contracts.engram import EngramAllocator
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.metacognition import MetaCognition
from yadgar._shared.observability.observe import observe
from yadgar._shared.rules_engine import RulesEngine
from yadgar._shared.runtime.sr_session import SRTransitionRecorder
from yadgar._shared.sensory_buffer import ActionLogger
from yadgar._shared.storage import StorageEngine
from yadgar._shared.thermodynamics import MemoryThermodynamics
from yadgar._shared.wiki.store import WikiStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

settings = get_settings()


# ── Getters ────────────────────────────────────────────────────────────


def _get_storage() -> StorageEngine:
    assert _st._storage is not None, "StorageEngine not initialized"
    return _st._storage


def _get_sql_storage() -> Any:  # MariaStorageEngine | None
    """Engine #2 (MariaDB, ADR-0195), or ``None`` when it is not available.

    DELIBERATELY does not assert, unlike every other getter here. Absence is a
    normal state, not a programming error, in two distinct ways:

      * on CORE it is always None — ADR-0078/ADR-0200 keep core off every
        database, so ``init_engines`` is only asked for engine #2 by the
        backend;
      * on the BACKEND it is None whenever MariaDB did not come up.
        ``entrypoint-backend.sh`` treats every engine-#2 failure as a WARNING
        and leaves the container healthy (``MARIADB_PID=""``, not on the
        HEALTHCHECK, not in the closing ``wait -n``). An asserting getter would
        convert that deliberate non-fatal into a crash for its callers.

    Callers must branch on None rather than assume a handle.
    """
    return _st._sql_storage


@observe(tier="stage")
def _get_embeddings() -> EmbeddingEngine:
    # §13: raise RuntimeError (not AssertionError — assert can be stripped with -O)
    if _st._embeddings is None:
        raise RuntimeError("EmbeddingEngine not initialized")
    return _st._embeddings


def _get_buffer() -> ActionLogger:
    assert _st._buffer is not None, "ActionLogger not initialized"
    return _st._buffer


# R2a Car B: return type is Any (was ConsolidationScheduler, a yadgar.core type).
# ConsolidationScheduler is built by core/bootstrap.py (full-path only); lifecycle
# no longer imports it, so annotating the concrete type here would reintroduce the
# _shared -> core.consolidation edge this Car removes.
def _get_consolidation() -> Any:
    assert _st._consolidation is not None, "ConsolidationScheduler not initialized"
    return _st._consolidation


def _get_staleness() -> Any:  # core: StalenessDetector
    assert _st._staleness is not None, "StalenessDetector not initialized"
    return _st._staleness


def _get_thermo() -> MemoryThermodynamics:
    assert _st._thermo is not None, "MemoryThermodynamics not initialized"
    return _st._thermo


def _get_retriever() -> Any:  # backend: Retriever (composed by backend.retrieval.compose)
    assert _st._retriever is not None, "Retriever not initialized"
    return _st._retriever


def _get_write_gate() -> Any:  # core: WriteGate
    assert _st._write_gate is not None, "WriteGate not initialized"
    return _st._write_gate


def _get_engram() -> EngramAllocator:
    assert _st._engram is not None, "EngramAllocator not initialized"
    return _st._engram


# T2 Car B: return type is Any (was CheckpointRestore, now a yadgar.backend type).
# CheckpointRestore moved to yadgar.backend.restoration behind POST /restore and
# is constructed backend-side (ensure_restoration_engines); annotating the
# concrete type here would reintroduce a _shared -> backend edge outside the
# ADR-0056 waivers. The slot stays None in the core process — core forwards.
def _get_replay() -> Any:  # backend: CheckpointRestore
    assert _st._replay is not None, "CheckpointRestore not initialized"
    return _st._replay


# R2a Car D2: _get_file_queue moved to yadgar.core.lifecycle — it imported
# yadgar.backend.queue_drainer (a _shared → core edge). The core-side home re-exports
# via yadgar.core.server so all `server._get_file_queue` callers are unchanged.


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
    except Exception:  # noqa: BLE001 — default-rule seeding at startup: add_rule drives the rules engine over storage, which raises with no common base, and a daemon that cannot seed its defaults must still boot
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
    except Exception as exc:  # noqa: BLE001 — startup backfill: it drives the embed service and storage, whose failures share no common base, and a degraded similarity gate must not stop the daemon from booting (the audit below reports it)
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
    except Exception:  # noqa: BLE001 — the audit that reports the degradation above; it reaches storage with no common base, and a failed audit must not stop startup
        pass


# ── Startup ────────────────────────────────────────────────────────────


# R2a Car D2: _emit_sd_ready moved to yadgar.core.lifecycle — it imported
# yadgar.core.daemon.sd_notify (a _shared → core edge). READY=1 is a CORE concern; the
# core composition root (core.bootstrap.core_init_engines) emits it after the full
# engine set is built. The backend /recall slim path never emits READY (backend
# has no sd_notify — NOTIFY_SOCKET is unset there, so the old emit was a no-op).


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
        from yadgar._shared.embeddings.remote_embeddings import (
            RemoteEmbeddingEngine,  # noqa: PLC0415
        )
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
    # T2 Car B: the shared root builds only the SESSION-SIDE transition recorder
    # (census verdict #5 — the core recall seam records SR transitions). The
    # numpy compute subclass (backend CognitiveMap) is composed backend-side by
    # yadgar.backend.restoration.ensure_restoration_engines, which UPGRADES this
    # slot in the backend process.
    _st._cognitive_map = SRTransitionRecorder(_st._storage)


@observe(tier="stage")
def _build_astrocyte_pool(_settings):
    """Build the AstrocytePool standalone at the composition root (R2a Car A).

    Decouples AstrocytePool construction from ConsolidationScheduler so the
    backend SLIM path can populate ``_st._pool`` without importing
    ``yadgar.core.consolidation``. Must run AFTER _init_secondary_engines
    (needs _st._kg + _st._thermo).

    Replicates ConsolidationScheduler's original guard + try/except → None
    fallback verbatim so behavior is neutral when ASTROCYTE_POOL_ENABLED=False
    or construction raises. The resulting object (real pool OR None) is injected
    into ConsolidationScheduler via ``pool=`` so ``_st._pool`` and
    ``_st._consolidation.pool`` are the SAME object in every branch.
    """
    if not getattr(_settings, "ASTROCYTE_POOL_ENABLED", True):
        logger.warning(
            "AstrocytePool is DISABLED (ASTROCYTE_POOL_ENABLED=False). "
            "Domain-aware consolidation will not run."
        )
        return None
    try:
        pool = AstrocytePool(_st._storage, _st._embeddings, _st._kg, _st._thermo, _settings)
        pool.init_processes()
        return pool
    except Exception:
        logger.exception("Failed to initialize AstrocytePool")
        return None


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
    # R2a Car B: this SHARED composition root now builds ONLY the shared engines
    # (retriever/engram/rules/metacognition/replay/wiki + the standalone _pool).
    # The 9 CORE-ONLY engines (curator, consolidation, staleness, prospective,
    # narrative, write_gate, causal, sleep, cls) are built by
    # yadgar.core.bootstrap.core_init_engines, which calls this fn first (core ->
    # _shared, legal) and then constructs them. `engine_set` is retained for API
    # parity; slim and full build the identical shared set here — the full-only
    # engines live in bootstrap, past the shared boundary.
    # T2 Car E2: Retriever construction moved OUT of the shared root — the
    # retrieval impl is a backend engine now (yadgar.backend.retrieval), composed
    # lazily by ensure_retrieval_engine (Car B ensure_restoration_engines
    # precedent). Store the selected ML client so the backend composer injects
    # the same concrete this root picked; reset the slot so a stale instance
    # from a previous engine build never leaks past init_engines.
    _st._ml_client = ml_client
    _st._retriever = None
    # R2a Car A: build the AstrocytePool STANDALONE here (both slim + full) so the
    # backend SLIM path can populate _st._pool without importing consolidation.
    # core/bootstrap injects THIS exact object into ConsolidationScheduler(pool=...)
    # so _st._consolidation.pool IS _st._pool (no double-build, no second
    # init_processes).
    _st._pool = _build_astrocyte_pool(_settings)
    _st._engram = EngramAllocator(_st._storage, _settings)
    _st._rules_engine = RulesEngine(_st._storage, _settings)
    _load_default_rules(_st._rules_engine)
    _st._metacognition = MetaCognition(_st._storage, _st._embeddings, _st._kg, _settings)
    # T2 Car B: CheckpointRestore construction moved OUT of the shared root —
    # the impl is a backend engine now (yadgar.backend.restoration, behind
    # POST /restore). _st._replay stays None in the core process (core forwards
    # restore/pre_compact_drain over HTTP); the backend composes it via
    # ensure_restoration_engines. Reset the slot so a stale instance from a
    # previous engine build (test re-init) never leaks past init_engines —
    # pre-Car-B this line assigned a fresh CheckpointRestore on every init.
    _st._replay = None
    _st._wiki = WikiStore(_st._storage, _st._embeddings)
    # (engram/rules/metacognition wiring onto the retriever happens in the
    # backend composer — ensure_retrieval_engine — which builds the retriever.)


# R2a Car D2: _init_file_queue moved to yadgar.core.lifecycle with _get_file_queue.
# The file-queue drainer is CORE-ONLY (backend /recall slim never writes), so the
# core composition root (core.bootstrap.core_init_engines) starts it on the full
# path.


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


@observe(tier="stage")
def _init_sql_storage() -> Any:  # MariaStorageEngine | None
    """Build engine #2 (MariaDB, ADR-0195), or return None if it is unavailable.

    THE IMPORT IS LAZY AND MUST STAY LAZY. ``sqlalchemy`` / ``asyncmy`` live in
    the ``sql`` extra; ``Dockerfile.ci:116`` bakes only ``--extra test --extra
    ml`` and ``yadgar-ci`` has no auto-sync pipeline. A module-scope import here
    would fail EVERY CI test the moment this lands, because the composition root
    is on every import path. Lazy keeps the blast radius to callers that
    actually want a database.

    NON-FATAL BY DESIGN. Mirrors what ``entrypoint-backend.sh`` already chose:
    mysqld is started outside the container HEALTHCHECK and outside the closing
    ``wait -n``, and every failure there is a WARNING. Nothing reads engine #2
    yet (car D lands the ``config`` schema; the knob train repoints reads), so a
    missing option file, a missing extra or a malformed file all degrade to None
    with a warning rather than taking the process down.

    Connectionless: this only constructs the ``AsyncEngine``. See
    ``MariaStorageEngine`` — ``init_engines`` is sync and runs inside a worker
    thread on the backend boot path, where opening a connection would bind the
    pool to an event loop that dies with the thread.
    """
    try:
        from yadgar._shared.storage.sql import (  # noqa: PLC0415 — lazy by design
            MariaStorageEngine,
            default_option_file_path,
        )
    except ImportError as exc:
        logger.warning("engine #2 unavailable: %s (install the 'sql' extra)", exc)
        return None

    cnf = default_option_file_path()
    if not cnf.is_file():
        logger.info("engine #2 not configured: no client option file at %s", cnf)
        return None

    try:
        engine = MariaStorageEngine.from_option_file(cnf)
    except Exception as exc:  # noqa: BLE001 — engine #2 absence must not be fatal
        logger.warning("engine #2 unavailable: could not build engine from %s: %s", cnf, exc)
        return None

    logger.info("engine #2 composed: %s", engine.url)
    return engine


@observe(tier="boundary")
def init_engines(
    db_path: str | None = None,
    embedding_model: str | None = None,
    start_daemons: bool = False,
    watch_directory: str | None = None,
    local_engines: bool = False,
    engine_set: Literal["slim", "full"] = "full",
    sql_storage: bool = False,
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

    sql_storage (engine #2, ADR-0195 car C): when True, ALSO compose the second
        concrete storage class — ``MariaStorageEngine`` over the container-local
        MariaDB socket — into ``_st._sql_storage``. Two concrete classes side by
        side, no ABC and no shared MRO (ADR-0195; see the sql package docstring
        for what a shared MRO cost PR #32).

        Defaults False, and that default is load-bearing: ``init_engines`` is
        the composition root for BOTH processes, and ADR-0078/ADR-0200 forbid
        core touching either database. Only the BACKEND passes True
        (``embed_service._ensure_recall_engines``). Never on by inference —
        core and backend both bind-mount the same data root, so "the socket is
        reachable" does not distinguish them.

        Failure is non-fatal: the slot stays None and every caller branches on
        it (see ``_get_sql_storage``).
    """
    # Q16: reset shutdown flag so a re-initialized server can shut down cleanly
    _st._shutdown_done = False

    _settings = get_settings()
    _st._storage = StorageEngine(db_path or _settings.DB_PATH)
    _inject_storage_caches(_st._storage)
    _st._sql_storage = _init_sql_storage() if sql_storage else None
    _st._embeddings, _ml_client = _init_embedding_client(
        embedding_model, _settings, local_engines=local_engines
    )

    _init_secondary_engines(_settings)
    _init_retriever_and_post_engines(_settings, _ml_client, engine_set=engine_set)

    # R2a Car D1: daemon-thread startup moved to core.bootstrap.core_init_engines
    # (it runs after the core-only engines incl. _st._staleness are built). The
    # start_daemons/watch_directory params remain for signature stability — the
    # backend slim bootstrap calls this directly and never sets start_daemons.

    # Eagerly warm up the embedding model so the first recall isn't slow.
    _st._embeddings._ensure_model()

    # migration_014 backfill: encode NULL-embedding wiki_page rows.
    # Runs after both StorageEngine + EmbeddingEngine are ready. Idempotent.
    _run_wiki_embedding_backfill(_st._wiki)

    # R2a Car D2: the CORE-ONLY file-queue drainer start (_init_file_queue) and the
    # READY=1 sd_notify emit (_emit_sd_ready) moved to yadgar.core.lifecycle and are
    # driven by the core composition root (core.bootstrap.core_init_engines) on the
    # FULL path — both imported yadgar.core.* (the last _shared → core edges). The
    # backend /recall slim path (which calls this directly) never wrote to the queue
    # and never had a NOTIFY_SOCKET, so dropping both here is behavior-neutral for it.

    return _st._storage, _st._embeddings, _st._buffer, _st._consolidation, _st._staleness


@observe(tier="boundary")
def shutdown(on_stopping=None, snapshot_caches=None):
    """Gracefully shut down all engines. Idempotent — safe to call twice (Q16).

    R2a Car D2 — shutdown SPLIT: this shared teardown no longer imports
    ``yadgar.core.daemon.sd_notify`` / ``yadgar.core.daemon.drain`` (the last two ``_shared → core``
    edges in ``shutdown``). Instead it accepts two OPTIONAL callbacks that the core
    wrapper (``yadgar.core.lifecycle.shutdown``) injects:

      * ``on_stopping``     — fired at the SAME position the inline
        ``sd_notify.stopping()`` occupied (very first step, before pool teardown).
      * ``snapshot_caches`` — fired at the SAME position the inline
        ``drain.snapshot_embed_caches()`` occupied (after ``buffer.flush``, before
        ``storage.close``).

    The ``_st._shutdown_done`` guard stays INSIDE this function, so both callbacks
    fire exactly once across the double-call path (main() ``finally`` + signal
    handler). Teardown order is byte-identical to the pre-D2 inline sequence.
    Both default to None so a bare-shared ``shutdown()`` (tests that call the
    shared entry directly) is a no-op for the two core steps — never crashes.
    """
    if _st._shutdown_done:
        return
    _st._shutdown_done = True

    # v5.49.0 Phase 6: signal sd_notify STOPPING=1 immediately (core callback)
    if on_stopping is not None:
        try:
            on_stopping()
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
        from yadgar._shared.observability.tracing import (
            shutdown_tracing as _shutdown_tracing,  # noqa: PLC0415
        )

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
    # T3 Car 2: drain deferred recall session side-effects (SR transition storage
    # writes, buffer captures, replay ticks) BEFORE _buffer.flush() AND before
    # storage.close() — the deferred worker both appends to _st._buffer (so it must
    # drain before the flush or the capture is lost) and writes through
    # _st._storage (so it must drain before close). Best-effort so a wedged
    # side-effect can't hang graceful stop past the systemd stop-timeout.
    try:
        from yadgar._shared.runtime.recall_side_effects_fork import (  # noqa: PLC0415
            drain_session_side_effects,
        )

        drain_session_side_effects(timeout=10.0)
    except Exception:  # noqa: BLE001 — shutdown must proceed
        pass
    if _st._buffer is not None:
        _st._buffer.flush()
    # v5.49.0 Phase 6: snapshot embed caches before closing storage (core callback)
    if snapshot_caches is not None:
        try:
            snapshot_caches()
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
    _st._ml_client = None
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
    except OSError:
        pass


# R2a Car D2: _drain_sensitive_lock + _signal_handler moved to
# yadgar.core.lifecycle — both imported yadgar.core.sensitive_lock (a _shared →
# core edge), and _signal_handler must call the CORE shutdown wrapper (which
# injects the sd_notify/drain callbacks). main() (core.server._startup) binds the
# handler; the core-side home re-exports via yadgar.core.server so callers/tests
# using server._signal_handler are unchanged.


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
        from yadgar._shared.config.config_registry import (  # noqa: PLC0415
            _set_config_gauges,
            emit_startup_config_log,
            warn_comet_dormant,
        )
    except ImportError:
        logger.debug("config_registry import failed (non-fatal)", exc_info=True)
        return

    try:
        emit_startup_config_log()
    except Exception:  # noqa: BLE001 — startup config telemetry: emit_startup_config_log walks the whole settings surface and the logging stack, which raise with no common base, and the three sibling calls below each have their own guard precisely so one failure does not silence the others
        logger.debug("emit_startup_config_log failed (non-fatal)", exc_info=True)

    try:
        _set_config_gauges()
    except Exception:  # noqa: BLE001 — startup config gauges: the prometheus set() path plus a full settings walk, with no common base; same one-guard-per-call contract as above
        logger.debug("_set_config_gauges failed (non-fatal)", exc_info=True)

    # BC-EN2b: announce COMET dormant state exactly once at startup (ADR-0004).
    # Own try/except — must always fire even if the calls above raised.
    try:
        warn_comet_dormant(settings)
    except Exception:  # noqa: BLE001 — the ADR-0004 COMET-dormant announcement: its own guard so it always fires even when the two calls above raised, which is the stated reason for the split
        logger.debug("warn_comet_dormant failed (non-fatal)", exc_info=True)


# NOTE (Car 3, folder-split #17): ``main()`` — the MCP-server app entry point —
# moved to ``yadgar.server._startup`` (a CORE module). It imported
# ``server._app.mcp_server`` + ``server.tools.misc`` (sync_instructions /
# install_hooks), which are ``_shared → server`` edges. Pure engine lifecycle
# (init_engines / shutdown / signal handler / startup diagnostics) stays here in
# ``_shared`` — none of it touches ``yadgar.server``. ``main()`` calls back into
# these via a core→_shared import (allowed by the layered contract).
