"""Core-side composition root: build the CORE-ONLY engines (R2a Car B).

``yadgar._shared.runtime.lifecycle.init_engines`` is now the SHARED composition
root — it builds only the engines the backend ``/recall`` bootstrap needs
(retriever/engram/rules/metacognition/replay/wiki + the standalone AstrocytePool)
and has NO ``yadgar.core`` import for the engine set. That removed the
``_shared -> core.consolidation`` edge.

R3 Car 1 F: the consolidation compute engines moved to the BACKEND
(ConsolidationScheduler + MemoryCurator, NarrativeEngine, WriteGate,
ProspectiveMemoryEngine, CausalDiscovery, and the inner sleep/cls engines).
Core no longer imports or instantiates them — they are built backend-side (the
``/consolidate`` service singleton + the ``/recall`` slim engine set), and the
core consolidation entrypoints FORWARD to the backend rather than reaching those
slots. The only CORE-ONLY engine still built HERE is StalenessDetector.

``core_init_engines`` is the seam ``yadgar.core.server.init_engines`` resolves to
(re-exported from ``core.server.__init__``). It calls the shared
``lifecycle.init_engines`` first (``core -> _shared``, legal), then constructs the
core-only engine (StalenessDetector) on the shared ``_state`` module.

engine_set="slim" short-circuits: the shared call builds the slim set and the
core-only block is skipped, so ``core_init_engines(slim)`` is byte-identical to
``lifecycle.init_engines(slim)`` (the path the backend calls DIRECTLY at
embed_service.py). Keeping them identical prevents a false-green where the
test-facing seam builds engines the real backend does not.

The StalenessDetector class is imported from ``yadgar.core.staleness``
(``core -> core``, in-layer).
"""

from __future__ import annotations

import yadgar._shared.runtime.state as _st
from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import init_engines as _shared_init_engines
from yadgar.core.staleness import StalenessDetector


@observe(tier="stage")
def _build_core_only_engines(_settings) -> None:
    """Construct the CORE-ONLY engines onto _state (full-path only).

    R3 Car 1 F: the consolidation compute engines moved to the BACKEND
    (ConsolidationScheduler, MemoryCurator, NarrativeEngine, WriteGate,
    ProspectiveMemoryEngine, CausalDiscovery, and the inner sleep/cls engines).
    They are built backend-side (the ``/consolidate`` service singleton +
    ``/recall`` slim engine set); core no longer imports or instantiates them.
    Their core ``_st`` slots stay None on the core process — the consolidation
    entrypoints forward to the backend rather than reaching those slots.

    StalenessDetector stays core (host-side stale-file detection); it is the sole
    surviving core-only engine and is still built here.

    Preconditions: the shared engines are already built (storage, embeddings,
    thermo, kg, retriever, and the standalone _st._pool).
    """
    _st._staleness = StalenessDetector(_st._storage, _settings)


@observe(tier="boundary")
def core_init_engines(
    db_path: str | None = None,
    embedding_model: str | None = None,
    start_daemons: bool = False,
    watch_directory: str | None = None,
    local_engines: bool = False,
    engine_set: str = "full",
):
    """Full composition root: shared engines + the 9 core-only engines.

    Delegates the shared engine set (and file-queue/sd_ready orchestration) to
    ``lifecycle.init_engines``, then builds the core-only engines when
    engine_set="full". For engine_set="slim" the core-only block is skipped, so
    this is identical to calling ``lifecycle.init_engines`` directly.

    R2a Car D1: the daemon-thread startup (metrics/reranker-idle/viz) moved from
    ``lifecycle.init_engines`` to HERE — it is triggered after the core-only
    engines (incl. ``_st._staleness``) are built, which is strictly safer than the
    old call site that ran before ``_st._staleness`` existed on the full path.
    ``start_daemons`` only fires on the full path (slim returns early, so slim
    never starts daemons — behavior-neutral for the backend bootstrap).

    Returns the same 5-tuple lifecycle returns, re-read after the core-only
    engines are populated so the consolidation/staleness slots are non-None on the
    full path.
    """
    result = _shared_init_engines(
        db_path=db_path,
        embedding_model=embedding_model,
        start_daemons=start_daemons,
        watch_directory=watch_directory,
        local_engines=local_engines,
        engine_set=engine_set,
    )
    if engine_set != "full":
        return result

    _build_core_only_engines(get_settings())

    # R2a Car D2: the CORE-ONLY file-queue drainer start + the READY=1 sd_notify
    # emit moved here from lifecycle.init_engines (they imported yadgar.core.* —
    # the last _shared → core edges). Both fire ONLY on the full path: the backend
    # /recall slim bootstrap returns above and never writes to the queue / signals
    # READY.
    #
    # ORDERING (vs pre-D2, flagged): pre-D2 the fq-init + READY emit ran at the END
    # of the shared init_engines, i.e. BEFORE the core-only engines/daemons. Post-D2
    # fq-init is kept BEFORE the daemon-thread start (fq-before-daemons preserved);
    # only the READY=1 emit now fires LAST — after the FULL 24-engine set + daemons
    # are live. This is behavior-neutral: _get_file_queue self-inits lazily, nothing
    # enqueues during init (the server accepts requests only after this returns), and
    # NO core-only engine ctor / D1 daemon target touches the queue at startup
    # (verified). READY-fires-once-everything-is-live is strictly more correct.
    from yadgar.core.lifecycle import _emit_sd_ready, _init_file_queue

    _init_file_queue()

    # Car G2 (ADR-0163): warm the runtime_config read-through cache from all stored
    # rows now that storage is live (built by _shared_init_engines above) and before
    # daemon threads / request serving. Best-effort — warmup swallows its own errors
    # so a warmup failure never blocks daemon start.
    from yadgar.core.server.tools._runtime_config import warmup_runtime_config_cache

    warmup_runtime_config_cache(_st._storage)

    # R2a Car D1: start daemon threads after the core-only engines exist.
    if start_daemons:
        from yadgar.core.daemon.daemons import _start_daemon_threads

        _start_daemon_threads(watch_directory, get_settings())

    # v5.49.4: emit READY=1 last — full engine set + daemons live, server ready.
    _emit_sd_ready()

    return _st._storage, _st._embeddings, _st._buffer, _st._consolidation, _st._staleness
