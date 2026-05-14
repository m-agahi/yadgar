import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


class YamlConfigSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings]):
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        config_path = Path("~/.yadgar/config.yaml").expanduser()
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
            pass  # silently skip bad config

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
    DB_PATH: str = "~/.yadgar/surreal_db"

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
    CROSS_ENCODER_TOP_K: int = 20
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
    NLI_RERANKING_ENABLED: bool = True
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

    # File queue — async write queue base directory
    DATA_DIR: str = str(Path.home() / ".yadgar")
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

    # vacuum settings
    # Number of pre-vacuum DB snapshots to retain. Older ones are pruned by
    # scripts/cleanup-backups.sh after a successful vacuum.
    VACUUM_SNAPSHOT_RETENTION: int = 3

    # v4.9: threshold auto-trigger for vacuum
    # Fire vacuum automatically when DB exceeds this size (default 2 GiB).
    VACUUM_AUTO_THRESHOLD_BYTES: int = 2_147_483_648
    # Local time window for automatic vacuum (HH:MM, 24-hour, end is exclusive).
    VACUUM_AUTO_WINDOW_START: str = "19:00"
    VACUUM_AUTO_WINDOW_END: str = "23:00"
    # Set to False to disable threshold auto-trigger.
    VACUUM_AUTO_ENABLED: bool = True

    model_config = {"env_prefix": "YADGAR_"}

    @field_validator("VACUUM_AUTO_WINDOW_START", "VACUUM_AUTO_WINDOW_END")
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", v):
            raise ValueError(f"must be HH:MM (00:00-23:59), got {v!r}")
        return v

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
