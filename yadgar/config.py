import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

import yadgar.paths as _paths


class YamlConfigSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings]):
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        from yadgar.config_yaml import get_config_path  # noqa: PLC0415

        config_path = get_config_path()
        if not config_path.exists():
            return
        try:
            from ruamel.yaml import YAML

            y = YAML()
            with open(config_path) as f:
                raw = y.load(f)
            if isinstance(raw, dict):
                self._data = {k.upper(): v for k, v in raw.items() if v is not None}
        except Exception:
            import logging

            logging.getLogger(__name__).warning("YAML config load failed", exc_info=True)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        val = self._data.get(field_name)
        return val, field_name, self.field_is_complex(field)

    def __call__(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in self._data.items()
            if k in self.settings_cls.model_fields and v is not None
        }


class Settings(BaseSettings):
    HOST: str = "127.0.0.1"
    PORT: int = 8765
    IDLE_THRESHOLD_SECONDS: int = 300
    DECAY_FACTOR: float = 0.9995  # ~34% heat after 3 months without access
    COLD_THRESHOLD: float = 0.02  # Archive memories below this heat (~6 months of no access)
    ACTION_STREAM_COLD_THRESHOLD: float = (
        0.1  # Archive action-stream memories below this heat (~weeks of no access)
    )
    HOT_THRESHOLD: float = 0.0  # All memories accessible (zero threshold policy)
    PROJECT_CONTEXT_MIN_HEAT: float = 0.01  # Filter nearly-cold memories from session context
    MAX_EPISODE_TOKENS: int = 50000
    OVERLAP_TOKENS: int = 2000
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    DAEMON_CHECK_INTERVAL: float = 30
    DB_PATH: str = str(_paths.DB_PATH)

    # v2 settings
    IMPORTANCE_DECAY_FACTOR: float = 0.9999  # ~81% heat after 3 months for important memories
    SURPRISE_BOOST: float = 0.3
    EMOTIONAL_DECAY_RESISTANCE: float = 0.5
    DREAM_REPLAY_PAIRS: int = 20
    FRACTAL_LEVELS: int = 3
    CLUSTER_SIMILARITY_THRESHOLD: float = 0.7
    PPR_DAMPING: float = 0.85
    PPR_ITERATIONS: int = 50
    CAUSAL_THRESHOLD: int = 3
    SYNAPTIC_WINDOW_MINUTES: int = 30
    SYNAPTIC_BOOST: float = 0.2
    NUM_ASTROCYTE_PROCESSES: int = 4
    NARRATIVE_INTERVAL_HOURS: int = 24
    CONTEXTUAL_PREFIX_ENABLED: bool = True
    CURATION_SIMILARITY_THRESHOLD: float = 0.95  # Only merge near-exact duplicates
    SIMILARITY_LINK_THRESHOLD: float = 0.78  # Min cosine to create a memory_similarity_link
    MAX_SIMILARITY_LINKS_PER_MEMORY: int = 15  # Degree cap — bounds memory_similarity_link size

    # v3 frontier settings
    HOPFIELD_BETA: float = 8.0  # Hopfield sharpness (low=blended, high=precise)
    HOPFIELD_MAX_PATTERNS: int = 5000  # Max patterns in Hopfield energy store
    RECONSOLIDATION_LOW_THRESHOLD: float = 0.3  # Below this: no modification on recall
    RECONSOLIDATION_HIGH_THRESHOLD: float = 0.7  # Above this: archive old + create new
    PLASTICITY_SPIKE: float = 0.3  # How much plasticity increases on access
    PLASTICITY_HALF_LIFE_HOURS: float = 6.0  # Plasticity decay half-life
    STABILITY_INCREMENT: float = 0.1  # Stability increase per successful retrieval
    EXCITABILITY_HALF_LIFE_HOURS: float = 6.0  # Engram excitability decay half-life
    EXCITABILITY_BOOST: float = 0.5  # Excitability increase on slot activation
    WRITE_GATE_THRESHOLD: float = 0.0  # Store everything — no write gate filtering
    COMPRESSION_GIST_AGE_HOURS: float = 168.0  # 7 days before gist compression
    COMPRESSION_TAG_AGE_HOURS: float = 720.0  # 30 days before tag compression
    SR_DISCOUNT: float = 0.9  # Successor representation discount factor γ
    SR_UPDATE_RATE: float = 0.1  # Incremental SR update learning rate
    COGNITIVE_LOAD_LIMIT: int = 4  # Max chunks in active context (Cowan's 4±1)
    CRDT_AGENT_ID: str = "default"  # Agent identifier for multi-agent CRDT

    # logging
    CORE_LOG_LEVEL: str = "warn"
    BACKEND_LOG_LEVEL: str = "warn"

    # v5.0 observability
    METRICS_ENABLED: bool = True  # YADGAR_METRICS_ENABLED — expose /metrics endpoint
    # I14 (v5.4.2): default changed 'human' → 'json' for structured log ingest
    # Set YADGAR_LOG_FORMAT=text for local dev human-readable output.
    LOG_FORMAT: str = "json"  # YADGAR_LOG_FORMAT — "json" | "text" | "human"

    # v5.7.11 OTLP exporter knobs (formerly env-only, now yaml-overridable)
    OTLP_ENDPOINT: str = ""  # HTTP endpoint, e.g. http://tempo:4318/v1/traces. Empty = disabled.
    OTLP_HEADERS: str = ""  # Comma-separated k=v pairs for auth/tenant headers.
    OTLP_TIMEOUT_SEC: int = 3  # Exporter timeout (s). Short so a dead collector fails fast.
    OTLP_INSECURE: bool = True  # True → plain HTTP (default). False → TLS.

    # v5.7.11 backend cache knob (formerly env-only, now yaml-overridable)
    DBSIZE_CACHE_TTL_SEC: int = 60  # /admin/dbsize cache TTL in seconds. 0 = disabled.

    # v4: Hippocampal Replay settings
    REPLAY_MAX_RESTORE_MEMORIES: int = 8  # Max memories to include in restoration
    REPLAY_ANCHOR_HEAT: float = 1.0  # Heat assigned to anchored memories
    REPLAY_CHECKPOINT_AUTO_INTERVAL: int = 50  # Auto-checkpoint every N tool calls

    # v5: Zero-gap memory persistence settings
    WRITE_GATE_CONTINUITY_DISCOUNT: float = 0.15  # Threshold reduction for task-continuous content
    WRITE_GATE_CONTINUITY_WINDOW: int = 10  # Number of recent stores to track for continuity
    MICRO_CHECKPOINT_ENABLED: bool = True  # Auto-checkpoint on significant events
    MICRO_CHECKPOINT_COOLDOWN: int = 5  # Min tool calls between micro-checkpoints
    SESSION_COHERENCE_BONUS: float = 0.2  # Heat bonus for current-session memories
    SESSION_COHERENCE_WINDOW_HOURS: float = 4.0  # How long the session coherence lasts
    REINJECTION_ENABLED: bool = True  # Auto-surface related context on remember
    REINJECTION_MAX_RESULTS: int = 3  # Max related memories to reinject
    DECISION_AUTO_PROTECT: bool = True  # Auto-protect detected decisions from decay
    ACTION_STREAM_ENABLED: bool = True  # Capture tool actions in sensory buffer

    # v6: WRRF (Weighted Reciprocal Rank Fusion) settings
    WRRF_K: int = 60  # RRF constant k
    WRRF_CANDIDATE_MULTIPLIER: int = 10  # Candidate pool = max_results * this
    WRRF_VECTOR_WEIGHT: float = 1.0
    WRRF_FTS_WEIGHT: float = 0.5
    WRRF_PPR_WEIGHT: float = 0.5
    WRRF_SPREADING_WEIGHT: float = 0.3
    RERANKER_ENABLED: bool = True
    RERANKER_TOP_K: int = 50
    RETRIEVAL_PROFILE: str = "balanced"  # fast | balanced | full

    # v7: Query routing settings
    QUERY_ROUTING_ENABLED: bool = True
    TEMPORAL_KEYWORDS: str = "yesterday,today,last week,last month,last session,recently,before,after,when,during,while,since,until,earlier,later,previous,next,morning,evening,night,ago,back then"
    CODE_KEYWORDS: str = "function,class,method,variable,import,error,bug,fix,refactor,implement,API,endpoint,database,schema,test,deploy"
    RELATIONAL_KEYWORDS: str = "relationship,connection,related,between,link,cause,effect,impact,influence,depend,lead to,result in"

    # v8: Confidence gating settings
    CONFIDENCE_GATING_ENABLED: bool = True
    CONFIDENCE_MIN_RESULTS: int = 3
    CONFIDENCE_SCORE_SPREAD_THRESHOLD: float = 0.15
    CONFIDENCE_TOP_SCORE_THRESHOLD: float = 0.5
    CONFIDENCE_FALLBACK_STRATEGY: str = "expand"

    # v9: Temporal retrieval settings
    TEMPORAL_RETRIEVAL_ENABLED: bool = True
    TEMPORAL_BOOST_WEIGHT: float = 0.4
    TEMPORAL_DECAY_DAYS: int = 30
    TEMPORAL_EXACT_MATCH_BOOST: float = 2.0

    # v10: Cross-encoder reranking settings
    CROSS_ENCODER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    CROSS_ENCODER_ENABLED: bool = True  # FlashRank ONNX is fast enough for CPU
    CROSS_ENCODER_TOP_K: int = 10
    CROSS_ENCODER_WEIGHT: float = 0.6  # CE weight in blend (retrieval gets 1-this)

    # v12: Graph signal optimization settings
    GRAPH_MAX_HOPS: int = 2
    GRAPH_MIN_EDGE_WEIGHT: float = 0.1
    GRAPH_SPREADING_DECAY: float = 0.5
    GRAPH_SPREADING_MAX_DEPTH: int = 2
    GRAPH_ENTITY_MIN_LENGTH: int = 3

    # v13: Adversarial protection settings
    ADVERSARIAL_DETECTION_ENABLED: bool = True
    ADVERSARIAL_SCORE_GAP_THRESHOLD: float = 0.05
    ADVERSARIAL_DIVERSITY_ENFORCEMENT: bool = True
    ADVERSARIAL_MIN_CONFIDENCE: float = 0.3

    # v14: Embedding enhancement settings
    CANDIDATE_POOL_MULTIPLIER: int = 20
    EMBEDDING_CACHE_SIZE: int = 128
    QUERY_PREFIX: str = ""

    # v5.51.0: Fast profile candidate pool override (I25 three-way registered).
    # Candidate pool = max_results * multiplier. Global default=20 is too large for
    # fast/hook profile (fetches 100 candidates at max_results=5). Fast profile uses
    # this smaller multiplier to bound latency without sacrificing fast-profile signals.
    FAST_PROFILE_CANDIDATE_MULTIPLIER: int = 3

    # v5.54.1: Precomputed graph prior weight (I25 three-way registered).
    # Additive boost applied to fused scores in ALL profiles (including fast).
    # The prior is a precomputed scalar stored on each memory row during consolidation
    # — O(1) read at fusion time, no per-query graph traversal (I8/I9 safe).
    # Weight 0.2 is a secondary nudge; must not dominate vector(1.0)/fts(0.5).
    # Set to 0.0 to disable entirely; memories with graph_prior=NULL are unaffected.
    WRRF_GRAPH_PRIOR_WEIGHT: float = 0.2

    # v5.54.2: Precomputed co-recall (transition-edge) prior weight (I25 three-way registered).
    # Additive boost applied to fused scores in ALL profiles (including fast).
    # The prior is computed from memory_transition table co-recall frequency during
    # consolidation — O(1) read at fusion time, NO transition-table traversal on request path.
    # "Recalled together before" = learned association. Weight 0.15 is smaller than
    # graph_prior (0.2) — co-recall is a weaker structural signal than entity centrality.
    # Set to 0.0 to disable entirely; memories with cofire_prior=NULL are unaffected.
    WRRF_COFIRE_PRIOR_WEIGHT: float = 0.15

    # v16: Query expansion (pseudo-HyDE) settings
    QUERY_EXPANSION_ENABLED: bool = True

    # v15: Fusion optimization settings
    COMBMNZ_ENABLED: bool = False  # CombMNZ: multiply fused score by signal count
    FUSION_NORM: str = "zscore"  # "zscore", "minmax", or "raw"

    # v17 Index-Time Enrichment Settings
    INDEX_ENRICHMENT_ENABLED: bool = True
    CONCEPTNET_ENRICHMENT_ENABLED: bool = True
    CONCEPTNET_MIN_EDGE_WEIGHT: float = 1.0
    CONCEPTNET_MAX_TERMS: int = 10
    CONCEPTNET_RELATIONS: str = (
        "IsA,UsedFor,HasProperty,AtLocation,MotivatedByGoal,CausesDesire,CapableOf"
    )
    COMET_ENRICHMENT_ENABLED: bool = True
    COMET_QUERY_EXPANSION_ENABLED: bool = False  # COMET at query time for open_domain
    COMET_MODEL: str = "mismayil/comet-bart-ai2"
    COMET_NUM_BEAMS: int = 5
    COMET_TOP_K_PER_RELATION: int = 3
    COMET_MIN_CONFIDENCE: float = 0.3
    COMET_RELATIONS: str = "xAttr,xIntent,xWant"
    DOC2QUERY_ENRICHMENT_ENABLED: bool = True
    DOC2QUERY_MODEL: str = "doc2query/msmarco-t5-small-v1"
    DOC2QUERY_NUM_QUERIES: int = 5
    LOGIC_ENRICHMENT_ENABLED: bool = True
    FPA_SIMILARITY_THRESHOLD: float = 0.25
    ENRICHMENT_MIN_CONTENT_LENGTH: int = 20

    # v18 Structured Profiles (Memobase)
    PROFILE_EXTRACTION_ENABLED: bool = True
    PROFILE_CONFIDENCE_DIRECT: float = 0.7
    PROFILE_CONFIDENCE_INFERRED: float = 0.4
    PROFILE_SUMMARY_ENABLED: bool = True

    # v19 Derived Beliefs (Hindsight)
    DERIVED_BELIEFS_ENABLED: bool = True
    BELIEF_MIN_CONFIDENCE: float = 0.3
    BELIEF_HIGH_CONFIDENCE_BOOST: float = 1.2
    BELIEF_SEARCH_PRIORITY_FOR_OPEN_DOMAIN: bool = True

    # v20 Comparison Query Routing
    COMPARISON_DUAL_SEARCH_ENABLED: bool = True
    COMPARISON_TOP_K_PER_OPTION: int = 10

    # v21 Fusion Method
    FUSION_METHOD: str = "convex"

    # v22 Advanced Reranking — GTE-Reranker
    GTE_RERANKER_ENABLED: bool = True
    GTE_RERANKER_MODEL: str = "Alibaba-NLP/gte-reranker-modernbert-base"
    GTE_RERANKER_MAX_LENGTH: int = 512
    GTE_RERANKER_FALLBACK_TO_FLASHRANK: bool = True

    # v23 NLI Entailment Scoring
    # v5.6.6: default changed True → False — NLI averages 55s/call on CPU, marginal
    # quality gain over CE alone. Set YADGAR_NLI_RERANKING_ENABLED=true to re-enable.
    NLI_RERANKING_ENABLED: bool = False
    NLI_MODEL: str = "cross-encoder/nli-deberta-v3-base"
    NLI_WEIGHT: float = 0.3
    NLI_ONLY_FOR_OPEN_DOMAIN: bool = True

    # v24 Multi-Passage Evidence Aggregation
    MULTI_PASSAGE_RERANKING_ENABLED: bool = True
    MULTI_PASSAGE_CLUSTER_OVERLAP_THRESHOLD: float = 0.3
    MULTI_PASSAGE_MAX_CLUSTER_SIZE: int = 3

    # v25 Dual-Vector Architecture (prep only, not active until DualCSE trained)
    DUAL_VECTORS_ENABLED: bool = False
    IMPLICIT_EMBEDDING_MODEL: str = ""

    # v5.41.2: wiki write wait timeout (opt-in read-your-writes path)
    # Maximum seconds wiki_add(wait=True) may block before returning a timeout error.
    # Only applies when wait=True is passed explicitly; default async path is unaffected.
    WIKI_WRITE_WAIT_TIMEOUT_SECONDS: float = 5.0

    # v5.39.0: wiki similarity gate knobs
    # Master switch — set to False to disable the gate entirely (WIKI_SIM_GATE_ENABLED=0).
    WIKI_SIM_GATE_ENABLED: bool = True
    # Minimum cosine similarity for combined (title+content) embedding to flag a duplicate.
    # Calibrated 2026-06-01 with all-MiniLM-L6-v2 on 7 sample pairs (test_wiki_sim_calibration.py):
    #   Near-duplicate pairs: 0.9560 (roadmap A vs B), 0.9931 (arch vs paraphrase)
    #   Distinct pairs:       0.4392–0.7135 (arch/hooks/bench/config cross-pairs)
    #   Min near-dup: 0.9560 | Max distinct: 0.7135 | Separation margin: 0.2425
    # 0.80 sits cleanly between the two clusters with ≥0.15 gap on each side.
    WIKI_SIM_CONTENT_THRESHOLD: float = 0.80
    # Title-similarity threshold. Currently unused (single combined embedding stored),
    # reserved for future schema upgrade that stores title-only embedding separately.
    WIKI_SIM_TITLE_THRESHOLD: float = 0.85
    # Gate enforcement mode: "hard" (reject) or "soft" (warn + allow).
    WIKI_SIM_MODE: str = "hard"
    # How many candidate duplicates to return in the rejection response.
    WIKI_SIM_TOP_K: int = 5

    # v5.42.1: embedding failure behaviour knob (I25 three-way registered).
    # Default False: log WARN + counter, wiki_add proceeds with NULL embedding (backward compat).
    # True: wiki_add fails when _compute_embedding returns None / raises.
    # Flip to True after operator confidence that embed service is reliable.
    WIKI_EMBED_FAILURE_BLOCKS_WRITE: bool = False

    # v5.42.6: enforcement knobs (I25 three-way registered).
    # Default True: strict enforcement — missing directory/branch rejects the write.
    # False: relax enforcement, emit WARN log + metric instead of rejecting.
    # Set to False as a migration escape hatch if legacy callers lack branch/directory.
    DIRECTORY_ENFORCEMENT: bool = True
    BRANCH_ENFORCEMENT: bool = True

    # v5.51.0: Hook recall latency budget (I25 three-way registered).
    # Maximum seconds asyncio.wait_for may wait for retriever.recall in hook handlers.
    # On timeout: WARN log + yadgar_hook_recall_timeout_total incremented + empty returned.
    # Same defensive class as v5.50.10 OTEL shutdown bound. Default 2.0s is conservative
    # (p99=10s; 2s cuts ~1% slowest calls). Raise to 5s if counter rate too high.
    HOOK_RECALL_TIMEOUT_S: float = 2.0

    # v5.51.0: /api/stats TTL cache (I25 three-way registered).
    # Seconds before a cached /api/stats result is invalidated and recomputed.
    # 0 = disabled (recompute every request). Default 5s.
    STATS_CACHE_TTL_S: int = 5

    # v5.53.1: stale wiki count cache TTL (I25 three-way registered).
    # Seconds before stale_wiki_count is recomputed from disk scan.
    # 0 = disabled (scan every signals call). Default 300s (5 minutes) — cheap
    # enough to keep signals hot path fast (I8/I9) while staying fresh.
    STALE_COUNT_CACHE_TTL_S: int = 300

    # File queue — async write queue base directory
    DATA_DIR: str = str(_paths.DATA_DIR)
    # Optional prefix for wiki .md archive filenames (e.g. "myproject" → "myproject-overview.md")
    WIKI_SLUG_PREFIX: str = ""
    # Drain interval in seconds — how long queue entries stay visible before being flushed to DB
    QUEUE_DRAIN_INTERVAL: int = 30
    # DLQ / retry policy
    QUEUE_MAX_PERMANENT_ATTEMPTS: int = 3  # 4xx failures → DLQ after this many tries
    QUEUE_MAX_TRANSIENT_ATTEMPTS: int = 20  # 5xx / network failures → DLQ after this many tries
    QUEUE_BACKOFF_BASE_S: int = 30  # initial retry delay in seconds
    QUEUE_BACKOFF_MAX_S: int = 3600  # maximum retry delay cap
    QUEUE_DLQ_RETENTION_DAYS: int = 90  # prune DLQ entries older than this

    # batch_writes chunk size — each transaction is capped at this many SQL
    # statements to avoid SurrealDB's recursive serialiser blowing the stack
    # on large batches (e.g. full-table heat decay).
    MAX_BATCH_STATEMENTS: int = 500

    # batch_writes byte cap — each transaction is also capped at this many
    # serialised bytes to prevent HTTP 413 Payload Too Large responses from
    # SurrealDB.  The estimate accounts for JSON-serialised parameter values
    # (which dominate when content fields are large).  Default 1 MB is well
    # below SurrealDB's compiled-in body limit so there is comfortable slack
    # for framing overhead.  Whichever limit fires first (statements or bytes)
    # starts a new chunk.
    MAX_BATCH_BYTES: int = 1_000_000

    # Consolidation cooldown — idle-triggered cycles fire at most once per this
    # many seconds, preventing back-to-back runs when last_activity stays stale
    # after a cycle completes.  Set to 0 to restore legacy back-to-back behaviour.
    CONSOLIDATION_COOLDOWN_SECONDS: int = 1800

    # check_invariants per-table query timeout in seconds.  On timeout the table
    # is skipped (logged at WARN) and the remaining tables still run.
    CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS: int = 60

    # DB-size telemetry — warn when total surreal_db/ size exceeds this threshold.
    # Default 1 GiB.  The warning fires at most once per hour.
    DB_SIZE_WARNING_BYTES: int = 1_073_741_824

    # memory_archive retention — v5.49.0 Phase 1 (purge_expired_archives).
    # Set to 0 to disable permanent deletion entirely.
    MEMORY_ARCHIVE_RETENTION_DAYS: int = 90
    # Maximum rows purged in a single purge_expired_archives() call (circuit-breaker).
    MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER: int = 500
    # Skip archives whose created_at is more recent than this many days ago
    # (prevents thrash-purging recently-created archives that landed old archived_at).
    MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS: int = 7

    # action_log retention — processed rows older than this are pruned each
    # consolidation cycle to prevent unbounded table growth.
    ACTION_LOG_RETENTION_DAYS: int = 7

    # episode retention — rows older than this are pruned each consolidation
    # cycle.  Keeps the episode table bounded so _check_temporal_order stays
    # fast (it now scans only the 2000 most recent episodes regardless).
    EPISODE_RETENTION_DAYS: int = 14

    # similarity matrix candidate cap — _link_similar_memories and _merge_duplicates
    # restrict the embedding matrix to at most this many memories (most-recently-
    # accessed first) before building the N×N float32 matrix.  Prevents OOM at scale.
    SIMILARITY_MATRIX_MAX_CANDIDATES: int = 4000

    # CLS pattern detection candidate cap — find_recurring_patterns restricts its
    # episodic memory scan to at most this many most-recently-accessed memories.
    CLS_PATTERN_MAX_CANDIDATES: int = 2000

    # action-stream memory retention — _memify_prune Pass 5 deletes unaccessed
    # memories tagged "_action_stream" that are older than this many days.
    # These summaries start at heat=0.4, too warm for Pass 1 (heat<0.01).
    # Set to 0 to disable the action-stream age cap.
    ACTION_STREAM_MAX_AGE_DAYS: int = 14

    # auto-generated memory retention — _memify_prune deletes cold unaccessed
    # memories tagged "auto-generated" that are older than this many days.
    # Set to 0 to disable the auto-generated prune pass.
    AUTO_GENERATED_MEMORY_MAX_AGE_DAYS: int = 30

    # auto-abstracted memory retention — _memify_prune deletes cold unaccessed
    # memories tagged "auto-abstracted" (CLS semantic promotions, action-stream
    # pattern noise) that are older than this many days.
    # Set to 0 to disable the auto-abstracted prune pass.
    AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS: int = 30

    # dream insight retention — _memify_prune deletes unaccessed dream memories
    # older than this many days regardless of heat level.
    # Set to 0 to disable the dream insight age cap.
    DREAM_INSIGHT_MAX_AGE_DAYS: int = 21

    # Table retention windows — rows older than these thresholds are pruned
    # each consolidation cycle.  Set to 0 to disable for a specific table.
    NARRATIVE_ENTRY_RETENTION_DAYS: int = 90
    ASTROCYTE_PROCESS_RETENTION_DAYS: int = 7
    MEMORY_CLUSTER_RETENTION_DAYS: int = 30
    DERIVED_BELIEF_RETENTION_DAYS: int = 30
    PROSPECTIVE_MEMORY_RETENTION_DAYS: int = 30

    # caused_by relationship ceiling — rows older than newest are pruned when this
    # limit is exceeded.  Default 100_000.  Set to 0 to disable ceiling check.
    MAX_CAUSED_BY_ROWS: int = 100_000

    # v5.15.0 D1: per-phase consolidation duration alerting.
    # When any _consolidation_cycle() phase exceeds this threshold in milliseconds,
    # a CRITICAL log is emitted with the phase name and actual duration.
    # Default 60000ms (1 minute). Set to 0 to disable phase duration alerting.
    PHASE_DURATION_WARN_MS: int = 60_000

    # v5.3.9 N1: backend HTTP timeouts
    # Short timeout for all non-import backend calls (health, /sql, /admin/dbsize).
    BACKEND_HTTP_TIMEOUT_SEC: int = 5
    # v5.6.6: dedicated timeout for /rerank endpoint calls.
    # CE inference can take 8-46s on CPU; the general 5s timeout caused spurious
    # CB-1 opens. Set to 0 to fall back to BACKEND_HTTP_TIMEOUT_SEC.
    RERANK_BACKEND_TIMEOUT_SEC: int = 90
    # Long timeout for the vacuum /import POST and /export GET (bulk data ops).
    BACKEND_IMPORT_TIMEOUT_SEC: int = 300
    # Separate timeout for schema migration HTTP calls during StorageEngine.__init__.
    # Migrations can be slower than operational reads (lock contention, backfill queries).
    MIGRATION_HTTP_TIMEOUT_SEC: int = 30

    # vacuum settings
    # Number of pre-vacuum DB snapshots to retain. Older ones are pruned by
    # scripts/cleanup-backups.sh after a successful vacuum.
    VACUUM_SNAPSHOT_RETENTION: int = 3

    # v4.9: threshold-driven auto-trigger for vacuum (emergency backstop only from v5.7.0).
    #
    # Trigger precedence (v5.7.0+):
    #   1. PRIMARY   — nightly cron at 19:00 UTC (yadgar-vacuum.timer, ships in PR-1a/1b).
    #                  Runs vacuum unconditionally regardless of DB size.
    #   2. BACKSTOP  — this threshold path (ConsolidationScheduler._maybe_auto_vacuum()).
    #                  Fires only when DB exceeds VACUUM_AUTO_THRESHOLD_BYTES AND the local
    #                  clock falls inside [VACUUM_AUTO_WINDOW_START, VACUUM_AUTO_WINDOW_END).
    #                  Role: catch runaway DB growth between nightly cron cycles — not the
    #                  normal workhorse.
    #   3. MANUAL    — vacuum_now() MCP tool (writes trigger file, see PR-4 / YADGAR_VACUUM_TRIGGER_PATH).
    #
    # The cooldown logic in _maybe_auto_vacuum() prevents double-fires within the same day,
    # so cron and backstop coexist safely.
    #
    # Emergency backstop threshold: fire when DB exceeds this size (default 2 GiB).
    VACUUM_AUTO_THRESHOLD_BYTES: int = 2_147_483_648
    # Local time window for the backstop trigger (HH:MM, 24-hour, end is exclusive).
    VACUUM_AUTO_WINDOW_START: str = "19:00"
    VACUUM_AUTO_WINDOW_END: str = "23:00"
    # Set to False to disable the backstop threshold trigger entirely.
    VACUUM_AUTO_ENABLED: bool = True

    # v5.0 perf settings
    # TTL for the entity-set cache in WriteGate._compute_temporal_novelty and
    # _compute_structural_novelty.  Avoids a get_all_entities() DB call on every
    # write-gate evaluation.  Cache is also invalidated explicitly on entity
    # add/delete via WriteGate.invalidate_entity_cache().
    # Default: 300 seconds (5 minutes). Set to 0 to disable caching.
    PREDICTIVE_CODING_ENTITY_TTL_SECONDS: int = 300

    # v5.0 security settings
    # Bearer-token auth for /api/* and /hooks/* routes.
    # When False: middleware is a no-op; logs WARN at startup.
    # When True (default): all /api/* and /hooks/* requests require a valid bearer token.
    # Override to False during initial deployment before YADGAR_MCP_AUTH_TOKEN is provisioned.
    REQUIRE_AUTH: bool = True
    # The bearer token clients must present in the Authorization header.
    # Must be set when REQUIRE_AUTH=True; ignored when False.
    MCP_AUTH_TOKEN: str = ""
    # Allowed CORS origins for the MCP HTTP transport.
    # Comma-separated list. Defaults to loopback only.
    ALLOWED_ORIGINS: str = "http://127.0.0.1:8765,http://localhost:8765"
    # Maximum file size (bytes) for path-based memorize hashing.
    # Files exceeding this threshold are skipped entirely.
    MAX_HASH_BYTES: int = 10_485_760  # 10 MiB
    # Auto-capture rate limit: max requests per directory key per minute.
    AUTO_CAPTURE_RATE_LIMIT: int = 30

    # §25 branch-aware retrieval
    # Convex-combination boost weight for current-branch memories.
    # boosted = score + (1 - score) * BRANCH_BOOST_WEIGHT
    # Keeps final scores in [0, 1]; 0.2 ≈ soft 1.2x at score=0.5.
    BRANCH_BOOST_WEIGHT: float = 0.2

    # §22 project_brief — layered bootstrap
    # Hard character cap for _project_init memory content.
    PROJECT_INIT_CAP_CHARS: int = 2000
    # Default mode for project_brief. Options: "catalog" | "full".
    BRIEF_MODE_DEFAULT: str = "catalog"
    # v5.7.12: signals mode thresholds + anchor cap
    # Hours before active_work is considered stale (triggers refresh_active_work action).
    ACTIVE_WORK_STALE_HOURS: float = 24.0
    # Hours before checkpoint is considered stale (triggers refresh_checkpoint action).
    CHECKPOINT_STALE_HOURS: float = 24.0
    # Maximum number of anchors returned in restore mode top_anchors list.
    PROJECT_BRIEF_MAX_ANCHORS: int = 12
    # v5.10.1: soft warning tier thresholds + watchdog opt-in
    # Hours before consider_refresh_active_work soft action fires (must be < ACTIVE_WORK_STALE_HOURS).
    ACTIVE_WORK_WARN_HOURS: float = 12.0
    # Hours before consider_refresh_checkpoint soft action fires (must be < CHECKPOINT_STALE_HOURS).
    CHECKPOINT_WARN_HOURS: float = 12.0
    # When True (opt-in): watchdog auto-writes stub _active_work on stale detection.
    # Default OFF — preserves user-curated _active_work semantic. Enable via systemd unit env.
    AUTO_REFRESH_ACTIVE_WORK: bool = False
    # Token-budget upper bound for signals mode payload (tokens ≈ len(json) // 4).
    # Default 350 covers 2 soft actions + suggested_call fields with headroom.
    # Raise if new action types push the payload above this ceiling.
    SIGNALS_TOKEN_BUDGET_SOFT: int = 350

    # v5.8.0: anchor hygiene TTL knobs
    # Default valid_until offset (days) for tier=conditional anchors.
    ANCHOR_CONDITIONAL_TTL_DAYS: int = 90
    # Default valid_until offset (days) for tier=ephemeral anchors.
    ANCHOR_EPHEMERAL_TTL_DAYS: int = 14
    # Require non-empty reason when anchor(tier='semantic_immortal') is called.
    ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON: bool = True
    # v5.8.0 PR-B: anchor hygiene signals + recommended_actions knobs
    # Minimum cosine similarity for a pair to appear in anchor_redundancy_candidates.
    ANCHOR_REDUNDANCY_COSINE: float = 0.92
    # Minimum word count for an anchor to be a promote-to-wiki candidate.
    ANCHOR_PROMOTE_WORDS: int = 500
    # Minimum markdown header count for an anchor to be a promote-to-wiki candidate.
    ANCHOR_PROMOTE_HEADERS: int = 2
    # anchor_count_project threshold above which audit_anchors action is emitted.
    ANCHOR_AUDIT_THRESHOLD: int = 15
    # v5.9.0: anchor audit pass in consolidate_now() (anchor_audit section)
    # Toggle anchor pass inside consolidate_now().
    ANCHOR_AUDIT_CONSOLIDATION_ENABLED: bool = True
    # Hard cap on actions returned per audit run (token budget).
    ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN: int = 20
    # How long _audit_anchors snapshots are retained for history (days).
    ANCHOR_AUDIT_HISTORY_RETENTION_DAYS: int = 30
    # v5.21.0: cross-project anchor dedup — minimum cosine for cross-project pair detection.
    # Higher than within-project ANCHOR_REDUNDANCY_COSINE (0.92) to avoid false positives.
    ANCHOR_CROSS_PROJECT_COSINE: float = 0.95

    # v5.10.6: SESSION_END_CAPTURE sentinel-marker pattern
    # Kill switch for entire session-end capture feature.
    SESSION_END_CAPTURE_ENABLED: bool = True
    # Auto-prune sentinel memory rows older than this many days during vacuum.
    SESSION_END_RETENTION_DAYS: int = 30
    # Last N human turns to embed in sentinel for rotation resilience.
    SESSION_END_SNIPPET_TURNS: int = 5
    # Skip sentinel write if session had fewer than this many human messages.
    SESSION_END_MIN_MESSAGES: int = 2

    # C2 — Recall-frequency-modulated decay (MemoryBank parity, v5.3.3)
    # Per-access heat boost applied during each consolidation decay cycle.
    # Formula: new_heat = min(compute_decay(mem, hours) + access_count_since_decay * RECALL_BOOST, 1.0)
    # Set to 0.0 to disable boost and revert to pure exponential decay (back-compat).
    RECALL_BOOST: float = 0.05

    # Q2 — Postmortem/incident tag retrieval boost (v5.3.5)
    # When the recall query contains an action verb (deploy, push, merge, etc.) AND
    # a candidate memory has tag _postmortem or _incident, its score is boosted via
    # the same convex formula as branch boost:
    #   boosted = score + (1 - score) * POSTMORTEM_BOOST_FACTOR
    # Set to 0.0 to disable (back-compat).
    POSTMORTEM_BOOST_FACTOR: float = 0.3
    POSTMORTEM_BOOST_KEYWORDS: tuple = (
        "deploy",
        "push",
        "merge",
        "restart",
        "vacuum",
        "rollback",
        "upgrade",
        "migrate",
        "bump",
        "release",
    )

    # N2 — ASGI graceful shutdown timeout (v5.3.9)
    # Caps uvicorn's wait for in-flight requests to drain on SIGTERM.
    # 0 = unlimited (uvicorn default); ≥1 = abandon after this many seconds.
    ASGI_SHUTDOWN_TIMEOUT_SEC: int = 5

    # v5.7.7 — Viz health-refresh cadence
    # How often the viz_daemon_health background scraper refreshes daemon metrics.
    # Default 5.0 s matches original hardcoded value; lower for faster debug refresh.
    VIZ_HEALTH_REFRESH_SEC: float = 5.0

    # v5.6.6 — Heavy-rerank kill switch for CPU-only hosts
    # When False: all CE/NLI/MP reranking is skipped; retrieval falls back to
    # BM25+HNSW fusion only (same as RETRIEVAL_PROFILE=fast but always applied).
    # Set YADGAR_HEAVY_RERANK_ENABLED=false to eliminate all CPU burst from reranking.
    HEAVY_RERANK_ENABLED: bool = True

    # v5.4 P7 — Write-time reinjection gate (default OFF per I1/I9)
    # When OFF, the retriever.recall() block in memorize() is skipped entirely,
    # saving 30–50ms of sync vector search per write.  Enable only if write-time
    # related-context surface is explicitly needed.
    REINJECT_ON_WRITE: bool = False

    # N4 — Backend ML circuit breaker (v5.3.10 hotfix)
    # When /rerank repeatedly times out or errors, the breaker opens to stop
    # saturating the backend.  Off = zero overhead per I3.
    CIRCUIT_BREAKER_ENABLED: bool = True
    # Open OPEN state after this many consecutive per-endpoint failures.
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 3
    # Stay OPEN for this many seconds before allowing a single probe attempt.
    CIRCUIT_BREAKER_OPEN_DURATION_SEC: int = 60
    # v5.4.2 CB-1 probe fixes:
    # Short HTTP timeout for HALF_OPEN probe calls (faster fail when backend saturated).
    CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC: float = 2.0
    # Maximum cooldown ceiling for exponential backoff on repeated probe failures.
    CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC: float = 600.0
    # Backoff multiplier — each failed probe multiplies cooldown by this factor.
    CIRCUIT_BREAKER_BACKOFF_FACTOR: float = 2.0

    # v5.4.2 F5-A — Concurrent-inference semaphore for /rerank endpoints (backend)
    # Max concurrent inference threads per rerank mode (ce/nli/pair).
    # N=1 ensures probes fast-fail via TimeoutError instead of queueing behind a live inference.
    RERANK_MAX_CONCURRENCY: int = 1
    # Seconds to wait for semaphore before returning 503.
    # Should be ≤ CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC so probes always fail fast.
    RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC: float = 2.0

    # backend v5.5.0 — model preload warm-up
    MODEL_PRELOAD: bool = (
        True  # YADGAR_MODEL_PRELOAD — preload rerank models in background after startup
    )
    MODEL_PRELOAD_DELAY_SEC: int = (
        10  # YADGAR_MODEL_PRELOAD_DELAY_SEC — seconds to wait before loading
    )

    # backend v5.4.0 — CE + embed LRU cache knobs
    # Kill switch for CE score cache (false/0 = disabled, pre-v5.4.0 behaviour).
    CE_CACHE_ENABLED: bool = True
    # Kill switch for embedding vector cache.
    EMBED_CACHE_ENABLED: bool = True
    # Maximum entries in CE score LRU cache. 0 = disabled.
    CE_CACHE_MAX_ENTRIES: int = 100000
    # Maximum entries in embedding vector LRU cache. 0 = disabled.
    EMBED_CACHE_MAX_ENTRIES: int = 100000
    # Interval in seconds between periodic cache snapshots to disk.
    CACHE_SNAPSHOT_INTERVAL_SEC: int = 600
    # Directory for cache snapshot files (ce.snap, embed.snap).
    CACHE_SNAPSHOT_DIR: str = "/data/cache"

    # v5.35.1 — Memory block caps (I25: env + yaml + registry, formerly module constants)
    # Max blocks per (scope, directory) tuple.
    MEMORY_BLOCK_MAX_PER_SCOPE: int = 10
    # Default per-block character limit when none specified.
    MEMORY_BLOCK_DEFAULT_CHAR_LIMIT: int = 2000
    # Absolute maximum per-block character limit (hard cap).
    MEMORY_BLOCK_HARD_CHAR_LIMIT: int = 8000
    # Total character budget across all blocks at restore-time (prevents context-bombing).
    MEMORY_BLOCK_TOTAL_BUDGET_CHARS: int = 12000

    # ── v5.11.0 — Viz knobs (configurable via config.yaml) ──────────────────
    # Node sizing
    VIZ_NODE_SIZE_3D: float = 8.0  # nodeRelSize in 3D mode (default = ForceGraph3D 2× = 8)
    VIZ_NODE_SIZE_2D: float = 4.0  # base radius in 2D canvas draw
    # Heat colour HSL params (heatColor function)
    VIZ_HEAT_HUE_START: int = 240  # hue at h=0 (cool, blue)
    VIZ_HEAT_HUE_END: int = 0  # hue at h=1 (hot, red)
    VIZ_HEAT_SAT_BASE: int = 60  # saturation base %
    VIZ_HEAT_SAT_GAIN: int = 30  # saturation gain %
    VIZ_HEAT_LIGHT_BASE: int = 40  # lightness base %
    VIZ_HEAT_LIGHT_GAIN: int = 20  # lightness gain %
    # Wiki category colours (WIKI_CAT_COLOR object)
    VIZ_CAT_COLOR_ARCHITECTURE: str = "#58a6ff"
    VIZ_CAT_COLOR_DECISION: str = "#ffa657"
    VIZ_CAT_COLOR_PATTERN: str = "#3fb950"
    VIZ_CAT_COLOR_DEBUGGING: str = "#f85149"
    VIZ_CAT_COLOR_REFERENCE: str = "#8b949e"
    VIZ_CAT_COLOR_CONVENTION: str = "#d2a8ff"
    VIZ_CAT_COLOR_FACT: str = "#a5d6ff"
    VIZ_CAT_COLOR_ANALYSIS: str = "#d29922"
    # Edge colours (EDGE_COLOR object)
    VIZ_EDGE_COLOR_SEMANTIC: str = "#1f6feb"
    VIZ_EDGE_COLOR_TEMPORAL: str = "#6e40c9"
    VIZ_EDGE_COLOR_TRANSITION: str = "#3fb950"
    VIZ_EDGE_COLOR_WIKI_CROSSREF: str = "#d2a8ff"
    VIZ_EDGE_COLOR_MEMORY_WIKI: str = "#ffa657"
    # Edge sizing — v5.50.0 Variant C defaults (width 1.8, opacity 0.9)
    VIZ_EDGE_WIDTH_3D_MULTIPLIER: float = 1.8  # _linkWidth(l) * N in 3D mode
    VIZ_EDGE_ARROW_LEN: int = 5  # arrowLen for directional edge types
    VIZ_EDGE_OPACITY: float = 0.9  # linkOpacity for all edges (Variant C)
    VIZ_EDGE_VARIANT: str = "C"  # informational: edge style variant in use
    # Node shape — v5.50.0 (config only; mesh renderer deferred, see PLAN_V5_10_7_3)
    VIZ_WIKI_SHAPE: str = "octahedron"  # desired shape for wiki nodes; renderer not wired pending v5.10.7.3 resolution
    # Physics — v5.50.0: charge -18.0 (from -12.0) for better node spread
    VIZ_PHYSICS_CHARGE_STRENGTH: float = -18.0
    VIZ_PHYSICS_LINK_DISTANCE_2D: float = 30.0
    VIZ_PHYSICS_LINK_DISTANCE_3D: float = 36.0
    # Layout / zoom-fit
    VIZ_LAYOUT_ZOOM_FIT_TICK: int = 80  # tick threshold to trigger auto-zoom-fit
    VIZ_LAYOUT_ZOOM_FIT_PADDING: int = 50  # padding passed to zoomToFit()
    VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS: int = 800  # transition duration ms for zoomToFit()
    # Search highlight
    VIZ_SEARCH_MATCH_COLOR: str = "#ffffff"  # stroke for matched (search-pinned) nodes
    VIZ_SEARCH_PINNED_COLOR: str = "#ffd700"  # stroke for pinned (clicked-to-pin) nodes
    VIZ_SEARCH_DIM_OPACITY: float = 0.18  # opacity for non-matched dimmed nodes

    # v5.48.0 — Update mechanism (I25 three-way registered)
    # Privacy: auto-check is OFF by default. Enable explicitly in config.yaml.
    UPDATE_CHECK_ON_START: bool = False  # opt-in: probe PyPI on daemon start
    UPDATE_CHECK_TIMEOUT_SECONDS: int = 5  # httpx timeout for PyPI probe
    UPDATE_PYPI_URL: str = "https://pypi.org/pypi/yadgar/json"  # PyPI JSON API URL
    UPDATE_USER_AGENT_TEMPLATE: str = (
        "yadgar/{version}"  # UA template; {version} replaced at runtime
    )
    # Gate for /api/control/update endpoint. Set to "on" to enable.
    # Default OFF — endpoint is for power users and Control-tab integration (v5.50).
    UPDATE_DEBUG_APIS_ENABLED: str = "off"
    # v5.50.2 — Umbrella gate for /api/control/{config,action,restart}/* endpoints.
    # Bearer token alone is insufficient — this gate must also be on.
    # Default OFF — these are powerful debug/admin APIs.
    DEBUG_APIS_ENABLED: bool = False
    # v5.49.0 — Upgrade snapshot retention (I25 three-way registered)
    # Keep the N most recent upgrade snapshots; older snapshots are pruned on next upgrade.
    UPDATE_SNAPSHOT_RETENTION: int = 3
    # v5.49.0 Phase 9 — Orchestrator knobs (I25 three-way registered)
    # Gate for run_install(). Default OFF (opt-in safety: avoid accidental self-upgrades).
    # Set to true in ~/.yadgar/config.yaml after reading docs/PLAN_V5_49_0.md § Rollout.
    UPDATE_INSTALL_ENABLED: bool = False
    # Maximum age in seconds for an upgrade lock before it's treated as stale.
    # Default 3600 = 1 hour.  Allows recovery if the upgrader process was killed mid-run.
    UPDATE_LOCK_MAX_AGE_SECONDS: int = 3600

    model_config = {"env_prefix": "YADGAR_"}

    @field_validator("VACUUM_AUTO_WINDOW_START", "VACUUM_AUTO_WINDOW_END")
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", v):
            raise ValueError(f"must be HH:MM (00:00-23:59), got {v!r}")
        return v

    @field_validator("LOG_FORMAT")
    @classmethod
    def _validate_log_format(cls, v: str) -> str:
        allowed = {"json", "text", "human"}
        if v.lower() not in allowed:
            raise ValueError(f"LOG_FORMAT must be one of {allowed}, got {v!r}")
        return v.lower()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple:
        return (
            init_settings,
            env_settings,
            YamlConfigSource(settings_cls),
            file_secret_settings,
        )

    @property
    def db_path_resolved(self) -> Path:
        return Path(self.DB_PATH).expanduser()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
