"""Module-level singleton state shared across all server submodules.

Leaf module — no imports from other yadgar.server.* modules.
All module-level globals that tests access as srv._xxx live here and are
re-exported from yadgar.server.__init__ so that 'import yadgar.server as srv'
resolves them correctly.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict, deque
from typing import Any

from yadgar._shared.astrocyte_pool import AstrocytePool
from yadgar._shared.config import resolve_knob
from yadgar._shared.contracts.engram import EngramAllocator
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.metacognition import MetaCognition
from yadgar._shared.rate_limit import TokenBucketRateLimiter
from yadgar._shared.rules_engine import RulesEngine
from yadgar._shared.runtime.sr_session import SRTransitionRecorder
from yadgar._shared.sensory_buffer import ActionLogger
from yadgar._shared.storage import StorageEngine
from yadgar._shared.thermodynamics import MemoryThermodynamics
from yadgar._shared.wiki.store import WikiStore

# R2a Car B/C + R3 Car 1 H: the non-shared engine slots are annotated `Any` so
# this shared leaf module carries NO `yadgar.core` / `yadgar.backend` import.
# StalenessDetector (yadgar.core.staleness) is constructed by core/bootstrap.
# FileQueue/QueueDrainer live in yadgar.backend.queue_drainer. The consolidation
# compute engines — ConsolidationScheduler, MemoryCurator, ProspectiveMemoryEngine,
# NarrativeEngine, SleepComputeEngine, WriteGate, DualStoreCLS, CausalDiscovery —
# moved to yadgar.backend.* (R3 Car 1) and are constructed backend-side (the
# /consolidate service singleton + the /recall slim engine set); their core slots
# stay None on the core process (the consolidation entrypoints forward to the
# backend). The concrete types are not needed at the slot-declaration site — this
# keeps state.py free of any edge into core.* or backend.*.

# Global instances — initialized in main()
_storage: StorageEngine | None = None
# Engine #2 (MariaDB, ADR-0195) — the SECOND concrete storage class, built by
# lifecycle._init_sql_storage. `Any` for the same reason as the slots below: the
# annotation must not drag `yadgar._shared.storage.sql` (and through it
# `sqlalchemy`, an OPTIONAL extra) into this leaf module's import graph.
# Stays None on core by design (ADR-0078/ADR-0200: core opens no database) and
# on any backend where MariaDB failed to come up — entrypoint-backend.sh treats
# that as a WARNING, not a fatal, so this slot's absence must not be fatal either.
_sql_storage: Any = None
_embeddings: EmbeddingEngine | None = None
_buffer: ActionLogger | None = None
_consolidation: Any = None  # backend: ConsolidationScheduler (None core-side)
_staleness: Any = None  # core: StalenessDetector
_thermo: MemoryThermodynamics | None = None
# T2 Car E2: Retriever is a BACKEND engine now (yadgar.backend.retrieval) —
# composed lazily by backend.retrieval.compose.ensure_retrieval_engine. The slot
# is Any (like the other core-invisible engines) so _shared never imports it.
_retriever: Any = None
# T2 Car E2: the selected ML client (Local in backend / Remote in core) — stored
# so the backend retriever composer can inject the same concrete the composition
# root selected (it is no longer constructed inside lifecycle).
_ml_client: Any = None
_curator: Any = None  # backend: MemoryCurator (None core-side)
_prospective: Any = None  # backend: ProspectiveMemoryEngine (None core-side)
_narrative: Any = None  # backend: NarrativeEngine (None core-side)
_sleep: Any = None  # backend: SleepComputeEngine (None core-side)
_pool: AstrocytePool | None = None
_kg: KnowledgeGraph | None = None
_write_gate: Any = None  # backend: WriteGate (None core-side)
_engram: EngramAllocator | None = None
_rules_engine: RulesEngine | None = None
_cls: Any = None  # backend: DualStoreCLS (None core-side)
# T2 Car B: core holds the session-side SRTransitionRecorder; the backend
# upgrades this slot to the full CognitiveMap (its subclass, built by
# yadgar.backend.restoration.ensure_restoration_engines).
_cognitive_map: SRTransitionRecorder | None = None
_causal: Any = None  # backend: CausalDiscovery (None core-side)
_metacognition: MetaCognition | None = None
_replay: Any = None  # backend: CheckpointRestore (None core-side — T2 Car B)
_wiki: WikiStore | None = None
_file_queue: Any = None  # core: FileQueue
_queue_drainer: Any = None  # core: QueueDrainer
_queue_lock = threading.Lock()
_event_lock = threading.Lock()
_metrics_lock = threading.Lock()  # §9 Q6: guard _system_metrics_cache

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
# Bounded to 1000 sessions to prevent unbounded growth
_action_batch: OrderedDict[str, list] = OrderedDict()
# §9 Q2: Lock protecting _action_batch — async handler, concurrent requests
_action_batch_lock = asyncio.Lock()
# §4: Project roots registered via seed_project — file hash whitelist.
_project_roots: set[str] = set()

# §7 Auto-capture rate limiter (token-bucket, keyed on directory)


def _get_auto_capture_rate_limit() -> int:
    """Resolve AUTO_CAPTURE_RATE_LIMIT via resolve_knob (env > yaml > default 30)."""
    return resolve_knob("YADGAR_AUTO_CAPTURE_RATE_LIMIT", "AUTO_CAPTURE_RATE_LIMIT", int, 30)


_auto_capture_limiter = TokenBucketRateLimiter(max_per_minute=_get_auto_capture_rate_limit())

# Throttle timestamps: directory → monotonic time (bounded to prevent unbounded growth)
_last_session_context: OrderedDict[str, float] = OrderedDict()
_last_prompt_recall: OrderedDict[str, float] = OrderedDict()

# ── Visualization event queue ──────────────────────────────────────────────
# Ring buffer of the last 500 events; SSE clients poll with a sequence cursor.
_event_queue: deque = deque(maxlen=500)
_event_seq: int = 0
_system_metrics_cache: dict = {}

# F2 backend→core event relay: core polls the backend /viz "events" op and
# re-stamps new backend events onto its OWN _event_queue (the backend push path
# — memory_added/wiki_added/heat_updated — runs in a different process whose
# queue no core SSE client can read). These two globals live in CORE only.
#   _backend_event_cursor: last BACKEND seq consumed (-1 = not yet seeded; the
#     first poll seeds it to the backend head so a fresh client is not flooded
#     with up-to-500 stale backlog events).
#   _backend_poll_lock: serializes the poll so N concurrent SSE clients issue at
#     most one backend round-trip per loop tick AND the read-cursor→fetch→advance
#     stays atomic (a plain _event_lock would not span the HTTP call).
_backend_event_cursor: int = -1
_backend_poll_lock = threading.Lock()

# Session state for transition tracking (bounded to prevent unbounded growth)
_last_recalled_ids: OrderedDict[str, int] = OrderedDict()  # session_id → last recalled memory_id
_DICT_MAX_SIZE = 1000  # max entries for all bounded dicts
_shutdown_done: bool = False  # Q16: idempotency guard for shutdown()

# Transport type used by the running server
_active_transport: str = "sse"

# Server start timestamp for uptime tracking
_start_time: float = 0.0

# DB-size warning throttle — stores the calendar hour (0–23) when the last
# WARN was emitted.  -1 means "never logged".  Reset to -1 at midnight by
# the consolidation cycle.  Guarded by the GIL (int write is atomic enough).
_db_size_warn_last_logged_hour: int = -1

# ── FileChanged hook state ──────────────────────────────────────────────────
# Team-inbox JSONL file positions: path → byte offset of last read.
# Tracks how far we've read each file so re-reads only ingest NEW lines.
# NOTE: resets to 0 on daemon restart; old lines are re-ingested once then
# tracked. Bounded to 10 000 entries (one per watched file is realistic).
_team_inbox_positions: OrderedDict[str, int] = OrderedDict()

# PLAN_*.md hash dedup: path → sha256 hex of last memorized content.
# Prevents duplicate memorize calls when hook fires without real content change.
_plan_file_hashes: dict[str, str] = {}

# ── Nightly maintenance mode (v5.50.3) ──────────────────────────────────────
# When True, every DB-backed MCP tool fast-fails with a structured maintenance
# error — no DB call, no hang. Toggled via POST /api/control/maintenance/enter
# and /exit. Nightly cycle flips this instead of stop/starting the daemon so
# connected MCP clients don't lose their connection.
_maintenance_mode: bool = False

# Monotonic deadline for the window above, or None = no expiry (task:0113).
# ``cmd_vacuum_impl``'s finally covers returns, exceptions and sys.exit — not
# SIGKILL / OOM-kill / power loss, and post-task:0111 the core no longer restarts
# during a vacuum, so a clear-on-start reset would never fire.  This deadline is
# the only backstop that fires unconditionally: ``_app.py::_maintenance()``
# treats an expired one as "not in maintenance" and self-clears, loudly.
_maintenance_deadline: float | None = None

# Monotonic timestamp of the enter that opened the current window (task:0113).
# Only used to say HOW LONG the gate was held in the TTL-expiry WARN — a fired
# TTL means a vacuum died without cleanup and the operator needs that number.
_maintenance_entered_at: float | None = None
