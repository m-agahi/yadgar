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
from typing import TYPE_CHECKING

from yadgar.astrocyte_pool import AstrocytePool
from yadgar.causal_discovery import CausalDiscovery
from yadgar.cls_store import DualStoreCLS
from yadgar.cognitive_map import CognitiveMap
from yadgar.config import resolve_knob
from yadgar.consolidation import ConsolidationScheduler
from yadgar.curation import MemoryCurator
from yadgar.embeddings import EmbeddingEngine
from yadgar.engram import EngramAllocator
from yadgar.knowledge_graph import KnowledgeGraph
from yadgar.metacognition import MetaCognition
from yadgar.narrative import NarrativeEngine
from yadgar.predictive_coding import WriteGate
from yadgar.prospective import ProspectiveMemoryEngine
from yadgar.rate_limit import TokenBucketRateLimiter
from yadgar.restoration import CheckpointRestore
from yadgar.retrieval import Retriever
from yadgar.rules_engine import RulesEngine
from yadgar.sensory_buffer import ActionLogger
from yadgar.sleep_compute import SleepComputeEngine
from yadgar.staleness import StalenessDetector
from yadgar.storage import StorageEngine
from yadgar.thermodynamics import MemoryThermodynamics
from yadgar.wiki import WikiStore

if TYPE_CHECKING:
    from yadgar.file_queue import FileQueue, QueueDrainer

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
