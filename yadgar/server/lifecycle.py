"""Singleton getter functions, init_engines, shutdown, signal handler, and main.

All module-level singleton state lives in _state.py.
Getters here provide typed access with assertions.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

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
                from yadgar.file_queue import FileQueue, QueueDrainer

                _settings = get_settings()
                base = Path(os.environ.get("YADGAR_DATA_DIR", _settings.DATA_DIR))
                # Build FileQueue first, then drainer, then start() — assign _file_queue
                # only after start() succeeds so a failed start leaves _file_queue=None.
                fq = FileQueue(base, wiki_prefix=_settings.WIKI_SLUG_PREFIX)
                drainer = QueueDrainer(
                    fq,
                    _get_storage,
                    drain_interval=float(_settings.QUEUE_DRAIN_INTERVAL),
                    max_permanent_attempts=_settings.QUEUE_MAX_PERMANENT_ATTEMPTS,
                    max_transient_attempts=_settings.QUEUE_MAX_TRANSIENT_ATTEMPTS,
                    backoff_base_s=float(_settings.QUEUE_BACKOFF_BASE_S),
                    backoff_max_s=float(_settings.QUEUE_BACKOFF_MAX_S),
                    dlq_retention_days=_settings.QUEUE_DLQ_RETENTION_DAYS,
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


# ── Startup ────────────────────────────────────────────────────────────


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
    if os.environ.get("YADGAR_EMBED_URL"):
        from yadgar.ml_client import RemoteMLClient
        from yadgar.remote_embeddings import RemoteEmbeddingEngine

        _st._embeddings = RemoteEmbeddingEngine(embedding_model or _settings.EMBEDDING_MODEL)
        _ml_client = RemoteMLClient(os.environ["YADGAR_EMBED_URL"])
    else:
        from yadgar.ml_client import LocalMLClient

        _st._embeddings = EmbeddingEngine(embedding_model or _settings.EMBEDDING_MODEL)
        _ml_client = LocalMLClient(_settings)
    _st._buffer = ActionLogger(_st._storage, _settings)
    _st._buffer.start_session()
    _st._thermo = MemoryThermodynamics(_st._storage, _st._embeddings, _settings)
    _st._kg = KnowledgeGraph(_st._storage, _settings)
    _st._cognitive_map = CognitiveMap(_st._storage, _settings)
    _st._retriever = Retriever(
        _st._storage, _st._embeddings, _st._kg, _settings, ml_client=_ml_client
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

    if start_daemons:
        _st._consolidation.start()
        if watch_directory:
            _st._staleness.start(watch_directory)
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
                    # §9 Q6: update under lock to prevent torn reads.
                    with _st._metrics_lock:
                        _st._system_metrics_cache.update(result)
                except Exception:
                    pass

        threading.Thread(target=_metrics_thread, daemon=True).start()

        # Idle reranker unloader — frees ~500MB after 10 min of no recall activity
        def _reranker_idle_thread() -> None:
            while True:
                time.sleep(60)
                try:
                    if _st._retriever is not None:
                        _st._retriever.unload_rerankers_if_idle(idle_seconds=600.0)
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
    _st._embeddings._ensure_model()

    # Start file queue drainer — processes any pending writes from previous sessions
    try:
        _get_file_queue()
    except Exception as exc:
        logger.warning("File queue init failed (non-fatal): %s", exc)

    return _st._storage, _st._embeddings, _st._buffer, _st._consolidation, _st._staleness


def shutdown():
    """Gracefully shut down all engines. Idempotent — safe to call twice (Q16)."""
    if _st._shutdown_done:
        return
    _st._shutdown_done = True

    if _st._queue_drainer is not None:
        _st._queue_drainer.stop()
    if _st._consolidation is not None:
        _st._consolidation.stop()
    if _st._staleness is not None:
        _st._staleness.stop()
    if _st._buffer is not None:
        _st._buffer.flush()
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
    from yadgar.server._app import mcp_server

    _st._active_transport = transport
    _st._start_time = time.time()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Self-register PID file so `yadgar daemon stop/restart/status` can find us
    # regardless of how the process was started (systemd, direct CLI, etc.).
    _pid_path = Path("~/.yadgar/yadgar.pid").expanduser()
    try:
        _pid_path.parent.mkdir(parents=True, exist_ok=True)
        _pid_path.write_text(str(os.getpid()))
    except Exception:
        pass

    # H-7: Fail fast if REQUIRE_AUTH=True but no token configured.
    # A server that requires auth but has no token is silently broken — every
    # request would get 503 "Admin token not configured" rather than a useful error.
    _auth_settings = get_settings()
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
