import os
import re
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

import yadgar._shared.paths as _paths
from yadgar._shared.observability.observe import observe


class YamlConfigSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings]):
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        self._load()

    @observe(tier="stage")
    def _load(self) -> None:
        from yadgar._shared.config.config_yaml import get_config_path  # noqa: PLC0415

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

    @observe(tier="hot")
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
    # v5.95: promoted from registry-only (env-only allowlist) to a real Settings
    # field so config.yaml is authoritative. Seconds a loaded model may sit idle in
    # the backend before eviction; 0 = never evict (backend RemoteMLClient reads it).
    MODEL_IDLE_EVICTION_SECONDS: int = 0
    DB_PATH: str = str(_paths.DB_PATH)

    # v2 settings
    IMPORTANCE_DECAY_FACTOR: float = 0.9999  # ~81% heat after 3 months for important memories
    SURPRISE_BOOST: float = 0.3
    EMOTIONAL_DECAY_RESISTANCE: float = 0.5
    DREAM_REPLAY_PAIRS: int = 20
    CLUSTER_SIMILARITY_THRESHOLD: float = 0.7
    PPR_DAMPING: float = 0.85
    PPR_ITERATIONS: int = 50
    CAUSAL_THRESHOLD: int = 3
    SYNAPTIC_WINDOW_MINUTES: int = 30
    SYNAPTIC_BOOST: float = 0.2
    NUM_ASTROCYTE_PROCESSES: int = 4
    ASTROCYTE_POOL_ENABLED: bool = True  # Set False to disable domain-aware consolidation
    NARRATIVE_INTERVAL_HOURS: int = 24
    CONTEXTUAL_PREFIX_ENABLED: bool = True
    CURATION_SIMILARITY_THRESHOLD: float = 0.95  # Only merge near-exact duplicates
    SIMILARITY_LINK_THRESHOLD: float = 0.78  # Min cosine to create a memory_similarity_link
    MAX_SIMILARITY_LINKS_PER_MEMORY: int = 15  # Degree cap — bounds memory_similarity_link size
    # v5.86 (OT-C4): incremental similarity-linking. Default OFF — production
    # behavior is the full N×N pass every cycle until this is enabled.
    # (I25 three-way registered: env YADGAR_SIMILARITY_LINKING_INCREMENTAL_ENABLED, yaml, registry)
    SIMILARITY_LINKING_INCREMENTAL_ENABLED: bool = False
    # Days between mandatory full-reconcile passes (safety net for re-embedding
    # that mutates old↔old similarity, which incremental-by-created_at misses).
    SIMILARITY_LINKING_RECONCILE_INTERVAL_DAYS: int = 7

    # v3 frontier settings
    HOPFIELD_BETA: float = 8.0  # Hopfield sharpness (low=blended, high=precise)
    HOPFIELD_MAX_PATTERNS: int = 5000  # Max patterns in Hopfield energy store
    EXCITABILITY_HALF_LIFE_HOURS: float = 6.0  # Engram excitability decay half-life
    EXCITABILITY_BOOST: float = 0.5  # Excitability increase on slot activation
    WRITE_GATE_THRESHOLD: float = 0.0  # Store everything — no write gate filtering
    # v5.73.0: shadow-only threshold for auditing — memories below this WOULD be rejected
    # if the gate were active, but are STILL stored (WRITE_GATE_THRESHOLD stays 0.0).
    # Used to stamp would_reject=True on low-surprisal memories for later threshold tuning.
    # (I25 three-way registered: env YADGAR_WRITE_GATE_SHADOW_THRESHOLD, yaml, registry)
    WRITE_GATE_SHADOW_THRESHOLD: float = 0.15
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
    # OTLP_INSECURE: reserved / no-op for the HTTP OTLPSpanExporter (C2 P3). For the
    # opentelemetry-exporter-otlp-proto-http exporter, transport security is decided
    # by the OTLP_ENDPOINT URL scheme (http:// vs https://), not by a flag — so this
    # knob has no effect on the http exporter. Kept (not removed) to avoid churning the
    # I25 three-way config sync (config.py + config_yaml.py + config_registry.py).
    OTLP_INSECURE: bool = True  # reserved/no-op for http exporter — scheme decides TLS.

    # viz-trace-replay (Car B): Tempo query API base URL for the Traces tab
    # (e.g. http://localhost:3200). Empty = disabled → /api/traces/* graceful-degrade
    # to a 200 empty payload. Distinct from OTLP_ENDPOINT (the EXPORT sink); this is
    # the READ endpoint the mesh pipeline fetches by-id + searches.
    TEMPO_QUERY_URL: str = ""

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
    WRRF_CANDIDATE_MULTIPLIER: int = 10  # Candidate pool = max_results * this
    WRRF_VECTOR_WEIGHT: float = 1.0
    WRRF_FTS_WEIGHT: float = 0.5
    WRRF_PPR_WEIGHT: float = 0.5
    WRRF_SPREADING_WEIGHT: float = 0.3
    RERANKER_ENABLED: bool = True
    RERANKER_TOP_K: int = 50
    RETRIEVAL_PROFILE: str = "balanced"  # fast | balanced | full
    # §45 fanout boost scope — controls when C4/postmortem boosts apply in fanout recall.
    # "scoped": apply only when profile is not None (profile-origin callers: hook=fast).
    # "global": apply to all fanout recall (any profile, including None).
    # "off": never apply boosts (useful for A/B or CPU-constrained deploys).
    # Default "scoped" preserves pre-forward-only prod parity: hook boosted, default did not.
    FANOUT_BOOST_SCOPE: str = "scoped"  # scoped | global | off

    # v7: Query routing settings
    QUERY_ROUTING_ENABLED: bool = True
    TEMPORAL_KEYWORDS: str = "yesterday,today,last week,last month,last session,recently,before,after,when,during,while,since,until,earlier,later,previous,next,morning,evening,night,ago,back then"
    CODE_KEYWORDS: str = "function,class,method,variable,import,error,bug,fix,refactor,implement,API,endpoint,database,schema,test,deploy"
    RELATIONAL_KEYWORDS: str = "relationship,connection,related,between,link,cause,effect,impact,influence,depend,lead to,result in"

    # v9: Temporal retrieval settings
    TEMPORAL_RETRIEVAL_ENABLED: bool = True

    # v10: Cross-encoder reranking settings.
    # NOT the live reranker — that is the GTE_RERANKER_* slot below (`GTE_*` kept
    # for env back-compat; the model is Ettin-32m since T4/ADR-0104). These fields
    # configure the CE rerank STAGE plus the degraded sentence-transformers
    # fallback tier that runs only when the Ettin primary is off or has failed.
    # CROSS_ENCODER_MODEL is deliberately not baked into Dockerfile.backend, so in
    # the offline container that fallback scores zeros (ADR-0192).
    CROSS_ENCODER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    # Two readers, two meanings (ADR-0192, flagged not fixed): gates the CE rerank
    # stage in retrieval/_reranking_cross_encoder.py, and gates the ST fallback
    # model load in ml_client/local_ml_client.py.
    CROSS_ENCODER_ENABLED: bool = True
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
    COMET_ENRICHMENT_ENABLED: bool = (
        False  # RETIRED/DORMANT per ADR-0004 (en2a ablation: net-negative recall)
    )
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
    # v5.68 fix #38: was missing → AttributeError swallowed silently by except Exception: pass
    # in retrieval/fusion.py → profiles never surfaced in recall results.
    # Mirror BELIEF_HIGH_CONFIDENCE_BOOST (sibling weight, same retrieval layer).
    PROFILE_SEARCH_WEIGHT: float = 1.0

    # v19 Derived Beliefs (Hindsight)
    DERIVED_BELIEFS_ENABLED: bool = True
    BELIEF_HIGH_CONFIDENCE_BOOST: float = 1.2

    # v20 Comparison Query Routing
    COMPARISON_DUAL_SEARCH_ENABLED: bool = True
    COMPARISON_TOP_K_PER_OPTION: int = 10

    # v21 Fusion Method
    FUSION_METHOD: str = "convex"

    # v22 Advanced Reranking — cross-encoder reranker (field name kept `GTE_*` for
    # env/back-compat; T4 flipped the default from GTE-ModernBERT to Ettin-32m).
    # T4 winner: `cross-encoder/ettin-reranker-32m-v1` (32.8M, ModernBERT-lineage,
    # Apache-2.0). LongMemEval memory-domain A/B (Q=20/type × 6 types, legacy
    # in-process path) gated the flip: recall@5/@10 parity-or-better on every type,
    # +0.108 recall@5 on multi-session (the hardest type), ~6.3× CE speedup. GTE
    # rollback: set YADGAR_GTE_RERANKER_MODEL=Alibaba-NLP/gte-reranker-modernbert-base
    # (baked into Dockerfile.backend one cycle). 68m fallback = ettin-reranker-68m-v1.
    GTE_RERANKER_ENABLED: bool = True
    GTE_RERANKER_MODEL: str = "cross-encoder/ettin-reranker-32m-v1"
    GTE_RERANKER_MAX_LENGTH: int = 512
    # Misleading name kept for env back-compat (YADGAR_GTE_RERANKER_FALLBACK_TO_
    # FLASHRANK is documented and already written into every operator's config.yaml;
    # renaming needs an alias + deprecation window — ADR-0192 revisit_trigger).
    # It is a FAILURE-MODE SELECTOR, not a FlashRank switch: when the reranker
    # fails, False => return zeros, True => fall through to the degraded
    # sentence-transformers tier. It has never selected FlashRank, and it is not
    # read at all when GTE_RERANKER_ENABLED=False.
    GTE_RERANKER_FALLBACK_TO_FLASHRANK: bool = True

    # v23 NLI. v5.6.6: default True→False (~55s/call CPU, marginal gain); YADGAR_NLI_RERANKING_ENABLED=true re-enables.
    NLI_RERANKING_ENABLED: bool = False
    NLI_MODEL: str = "cross-encoder/nli-deberta-v3-base"
    NLI_WEIGHT: float = 0.3
    NLI_ONLY_FOR_OPEN_DOMAIN: bool = True

    # v24 Multi-Passage Evidence Aggregation
    # T3 Car 1 (core 5.125.0): default True→False — drops a batched CE call on the CE-bound
    # path. Gated on LongMemEval recall@k parity (memory domain). Toggle preserved:
    # YADGAR_MULTI_PASSAGE_RERANKING_ENABLED=1 restores old behaviour.
    MULTI_PASSAGE_RERANKING_ENABLED: bool = False
    MULTI_PASSAGE_CLUSTER_OVERLAP_THRESHOLD: float = 0.3
    MULTI_PASSAGE_MAX_CLUSTER_SIZE: int = 3

    # v25 Dual-Vector Architecture (prep only — _dual_vector_search removed in v6 T3)
    # IMPLICIT_EMBEDDING_MODEL retained as CONFIG-ONLY pending future DualCSE implementation.
    IMPLICIT_EMBEDDING_MODEL: str = ""

    # v5.41.2: wiki write wait timeout (opt-in read-your-writes path)
    # Maximum seconds wiki_add(wait=True) may block before returning a timeout error.
    # Only applies when wait=True is passed explicitly; default async path is unaffected.
    # Bumped 5→15 (Car #26): post-deploy cold drain measured ~12s; 15s covers that
    # plus margin so wait=True actually observes convergence in normal conditions.
    WIKI_WRITE_WAIT_TIMEOUT_SECONDS: float = 15.0

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

    # v5.141.0 (Car 2 Part B): memorize soft-gate knobs. NON-BLOCKING near-duplicate
    # check for DURABLE writes only (tags ∩ {feedback,decision,_anchor} OR is_protected
    # OR any tier set). Returns near_duplicates WITHOUT blocking the store — the mirror
    # of the wiki 0.80 gate but advisory, not rejecting. Episodic writes bypass entirely.
    MEMORIZE_SIM_GATE_ENABLED: bool = True
    # Cosine-similarity threshold to flag a near-duplicate memory. 0.85 (stricter than
    # the wiki 0.80) — memories are shorter/noisier so a higher bar avoids false dups.
    # CONFIGURABLE knob; calibrate before relying on the surfaced dups downstream.
    MEMORIZE_SIM_THRESHOLD: float = 0.85
    # Max near-duplicate candidates returned in the (non-blocking) near_duplicates list.
    MEMORIZE_SIM_TOP_K: int = 3

    # C5 (0047 PR#40 §5): ``DIRECTORY_ENFORCEMENT`` is DELETED. ADR-0215 had
    # already removed its BRANCH_ENFORCEMENT sibling; ADR-0225 set this one's end
    # condition as "until the registry check is actually wired", and C6 wires it
    # in this same PR. A knob whose OFF position disables a scoping guarantee
    # cannot coexist with an identity contract that is fail-loud by construction.

    # v5.62.0: Recall quality floor — drop results whose cross-encoder score is
    # below this threshold.  Targets keyword-only co-occurrence noise that survives
    # retrieval with _cross_encoder_score≈0 / _rerank_score=0.
    #
    # Calibration (2026-06-15, 80-sample audit, production corpus):
    #   Co-occurrence junk CE: 0.0 – 0.157
    #   Genuine results CE:    0.289 – 0.843
    # Default 0.0 = DISABLED.  Short synthetic content (test env) overlaps the
    # lower junk band (CE 0.03–0.08), so a non-zero default would break tests.
    # Production tuning: raise to 0.15–0.20 once write-time backfill (plan §C)
    # removes mis-stamped system/global rows that currently bulk up the corpus.
    # Set to 0.0 to disable entirely; missing CE scores are always preserved.
    RECALL_QUALITY_FLOOR: float = 0.0

    # v6 T6 Step 4: cross-type fusion settings (I25 three-way registered).
    # Per-type quotas: max candidates from each source before CE rerank.
    # Prevents one source from starving the other in the pool.
    RECALL_MEMORY_QUOTA: int = 5
    RECALL_WIKI_QUOTA: int = 5
    # Additive prior weights: native_score contribution to final CE-boosted score.
    # Small (0.1) so CE relevance dominates but native signals tie-break.
    RECALL_MEMORY_PRIOR_WEIGHT: float = 0.1
    RECALL_WIKI_PRIOR_WEIGHT: float = 0.1

    # Car C7 (0047, absorbing C8 item 5) DELETED ``RECALL_DOWNWEIGHT_FACTOR``.
    # It tuned a penalty that never worked: both call sites MULTIPLIED a score
    # of the form ``ce + w * native``, and ``ce`` is a raw cross-encoder logit
    # that is commonly negative — so a factor below 1.0 RAISED the score and
    # promoted the pages it was meant to sink. Its only user (``task_list``) is
    # now ``recall_disposition="exclude"`` and no disposition resolves to
    # ``"downweight"`` any more, so there is nothing left to tune. A future soft
    # sink must SUBTRACT or CLAMP, never multiply — and must not reuse this name.

    # task:0085: recall() output-size bounds (presentation-only, applied in
    # core/server/tools/recall.py AFTER retrieval — ranking is untouched).
    # Per-row content cap. 1200 measured at -54.8% row bytes combined with the
    # denylist projection (~18 KB per 10 rows) while still carrying a full
    # anchor-sized memory, and ~2x the CE passage window (GTE_RERANKER_MAX_LENGTH).
    RECALL_MAX_CONTENT_CHARS: int = 1200
    # Total serialised budget; rows past it are dropped behind a _dropped marker.
    # UNCALIBRATED: nobody has measured where the harness tool-output cap actually
    # sits. 65536 is "comfortably under the observed 78 KB failure with headroom",
    # not a derived figure — it is a knob precisely so it can be retuned without
    # a code change once the real cap is known.
    RECALL_MAX_TOTAL_BYTES: int = 65536

    # v5.51.0: Hook recall latency budget (I25 three-way registered).
    # Maximum seconds asyncio.wait_for may wait for retriever.recall in hook handlers.
    # On timeout: WARN log + yadgar_hook_recall_timeout_total incremented + empty returned.
    # Same defensive class as v5.50.10 OTEL shutdown bound. Default 2.0s is conservative
    # (p99=10s; 2s cuts ~1% slowest calls). Raise to 5s if counter rate too high.
    HOOK_RECALL_TIMEOUT_S: float = 2.0

    # v5.95 (#81 residual): size of the dedicated hook-recall thread pool. Hook
    # recalls run bounded here so a slow uncancellable recall cannot cascade into
    # event-loop starvation. ADR-0077: default 1 -> 2 — post-#166 the hook recall
    # is a forwarded HTTP wait (idle thread, not a GIL-holding in-core recall);
    # pool=1 structurally starved the second of every concurrent session pair
    # (measured 32-52% hook timeout rate). Read once at import (restart to apply).
    HOOK_RECALL_POOL_WORKERS: int = 2

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

    # cold-memory retention DRY-RUN visibility (#29)
    # Identifies cold immortal user memories that have no existing retention gate.
    # SAFETY: by default this ONLY reports — it DELETES NOTHING.
    # Real delete requires BOTH COLD_MEMORY_PURGE_ENABLED=True AND COLD_MEMORY_PURGE_DRY_RUN=False.
    COLD_MEMORY_RETENTION_DAYS: int = 90  # age threshold for candidate detection
    COLD_MEMORY_PURGE_ENABLED: bool = False  # master gate — OFF by default (report only)
    COLD_MEMORY_PURGE_DRY_RUN: bool = True  # dry-run gate — ON by default (never deletes)

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

    # #74 fix #1 — readiness anti-flap. The /health READINESS probe (db+embed)
    # must NOT flip to 503 on a single transient miss (a busy backend that times
    # out the 2s probe once). It requires this many CONSECUTIVE misses before
    # degrading. A single success resets the counter. LIVENESS (/health/live) is
    # separate — it never probes the backend, so a busy dependency can't SIGKILL
    # the core via P0 (which watches liveness). Default 3.
    HEALTH_READINESS_FAIL_THRESHOLD: int = 3

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

    # Task 0027c: bounded wait-for-backend at CORE startup (core_init_engines).
    # StorageEngine.__init__ runs _init_schema() inline and issues HTTP at once,
    # so a core started while the backend is down used to crashloop on
    # Restart=on-failure. The gate polls the backend /health for this many
    # seconds before failing cleanly. MUST stay strictly below the core unit's
    # TimeoutStartSec (asserted by test_retry_budget_is_inside_core_unit_timeout)
    # — a gate that outlives the start timeout turns a slow start into a
    # crashloop. 0 disables the gate entirely (escape hatch).
    # NOT a cold-boot mechanism: After=yadgar-backend.service is.
    BACKEND_READY_WAIT_SEC: int = 60
    # Fixed interval between /health probes. Fixed, not exponential — the wait is
    # for another process to finish loading a model, so backoff only adds latency
    # after readiness. 2s matches daemon.py's existing health poll.
    BACKEND_READY_POLL_SEC: float = 2.0

    # task:0113 — self-heal deadline (seconds) for the maintenance write-gate the
    # vacuum engages around its count-capture → export → swap window.  The
    # release runs in a finally, which covers returns/exceptions/sys.exit but not
    # SIGKILL, OOM-kill or power loss; post-task:0111 the core no longer restarts
    # during a vacuum, so a clear-on-start reset would never fire.  This TTL is
    # the only backstop that does.  Default 2400s sits above
    # yadgar-vacuum.service's TimeoutStartSec=30min so a slow-but-alive vacuum is
    # never un-gated underneath itself.
    MAINTENANCE_TTL_SEC: int = 2400

    # vacuum settings
    # Number of pre-vacuum DB snapshots to retain; older ones are pruned on
    # EVERY vacuum exit path.  Lowered 3 -> 2 by task:0046: each
    # snapshot is a full-size DB copy, so three of them guarded a 224 MB live DB
    # with ~800 MB of insurance.  The 2026-07-10 recovery used ONE quiesced copy;
    # the second exists only for the case where the newest is itself torn, and
    # past that ADR-0090's `.surql` export path is the fallback.  A never-zero
    # floor is enforced in code (`max(1, keep_n)`), not by this default.
    VACUUM_SNAPSHOT_RETENTION: int = 2

    # Age backstop for those snapshots (task:0046), mirroring ADR-0076 D1's
    # VACUUM_OLD_MAX_AGE_DAYS.  The NEWEST snapshot is exempt unconditionally, so
    # this can never take the host below one rollback anchor.
    VACUUM_SNAPSHOT_MAX_AGE_DAYS: int = 14

    # Task 0107: which side-build launcher Phase 3 uses to obtain its throwaway
    # SurrealDB — "auto" (host binary first, container second, SKIP third,
    # today's behaviour), "host" (host binary only, fails loud rather than
    # falling through when unresolvable), or "container" (container only,
    # ignoring any resolvable host binary — ADR-0186's structurally
    # skew-proof branch). See yadgar/core/vacuum/launcher.py::_launcher_mode.
    VACUUM_SIDE_LAUNCHER: str = "auto"

    # v4.9: threshold-driven auto-trigger for vacuum (emergency backstop only from v5.7.0).
    #
    # Trigger precedence (v5.7.0+):
    #   1. PRIMARY   — the nightly cycle at 19:00 UTC (yadgar-nightly-cycle.timer, PR-1a/1b),
    #                  whose script (yadgar-nightly-cycle) runs vacuum unconditionally as
    #                  step 4 of backup → consolidation → vacuum → backup, regardless of DB
    #                  size. task:0042 corrected this: an earlier version of this comment
    #                  mislabeled yadgar-vacuum.timer as the nightly 19:00 UTC trigger —
    #                  that unit is actually a SEPARATE weekly timer (Sundays 04:00 local)
    #                  that also runs `yadgar vacuum` standalone (nix systemd timer, v4.8.0).
    #   2. BACKSTOP  — this threshold path (ConsolidationScheduler._maybe_auto_vacuum()).
    #                  Fires only when DB exceeds VACUUM_AUTO_THRESHOLD_BYTES AND the local
    #                  clock falls inside [VACUUM_AUTO_WINDOW_START, VACUUM_AUTO_WINDOW_END).
    #                  Role: catch runaway DB growth between nightly cycles — not the
    #                  normal workhorse.
    #   3. MANUAL    — vacuum_now() MCP tool (writes trigger file, see PR-4 / YADGAR_VACUUM_TRIGGER_PATH).
    #
    # The cooldown logic in _maybe_auto_vacuum() prevents double-fires within the same day,
    # so the nightly cycle and the backstop coexist safely.
    #
    # Emergency backstop threshold: fire when DB exceeds this size (default 2 GiB).
    VACUUM_AUTO_THRESHOLD_BYTES: int = 2_147_483_648
    # Local time window for the backstop trigger (HH:MM, 24-hour, end is exclusive).
    VACUUM_AUTO_WINDOW_START: str = "19:00"
    VACUUM_AUTO_WINDOW_END: str = "23:00"
    # Set to False to disable the backstop threshold trigger entirely.
    VACUUM_AUTO_ENABLED: bool = True

    # v5.69 P3: sensitive-job lock + signal drain.
    # A sensitive job (e.g. vacuum) writes a lock file under YADGAR_DATA_DIR so an
    # EXTERNAL shutdown signal cannot interrupt it mid-swap (06-16 data loss).
    # SENSITIVE_LOCK_TTL_SEC: a lock older than this is treated as STALE and reaped
    #   (alongside the dead-PID check) so a crashed job never deadlocks shutdown.
    #   Default is generous — ~2x the worst-case vacuum — to never reap a live job.
    SENSITIVE_LOCK_TTL_SEC: int = 7200  # YADGAR_SENSITIVE_LOCK_TTL_SEC
    # SENSITIVE_DRAIN_TIMEOUT_SEC: max seconds the signal handler waits for an
    #   in-process sensitive job to release the lock before REFUSING the shutdown
    #   (returns without shutting down — never interrupts mid-swap).
    SENSITIVE_DRAIN_TIMEOUT_SEC: float = 300.0  # YADGAR_SENSITIVE_DRAIN_TIMEOUT_SEC

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
    # Default 500 covers 2 soft actions + capture_adr / capture_agent_prompt /
    # use_agent_prompt_library nudges + suggested_call fields with headroom.
    # History: v5.85 #126 added capture_agent_prompt (~371 worst case, was 340);
    # v5.89 #69 added use_agent_prompt_library (~441 worst case, bumped to 500).
    # Raise if new action types push the payload above this ceiling.
    SIGNALS_TOKEN_BUDGET_SOFT: int = 500
    # v5.84.0 car #12: ADR nudge threshold.
    # Hours of inactivity on the ADR log (relative to active_work) before the
    # capture_adr recommended_action fires in signals mode.
    ADR_DUE_WARN_HOURS: float = 12.0
    # v5.89 #69: dispatch-prelude read-side nudge threshold.
    # Hours without agent_dispatch_prelude call (vs active_work) before use_agent_prompt_library fires.
    DISPATCH_PRELUDE_DUE_WARN_HOURS: float = 12.0

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
    # v5.158.0 (Car #85): stop-hook maintenance cadence — human messages between
    # anchor-audit maintenance injections in the Stop hook. Lower priority than the
    # checkpoint (INTERVAL=25); a due checkpoint preempts the audit, which then
    # fires on the next eligible stop.
    ANCHOR_AUDIT_STOP_INTERVAL: int = 100
    # Car D (#83, ADR-0162): stop-hook maintenance cadence — human messages between
    # code_graph-refresh maintenance injections. Slowest of the maintenance items
    # (checkpoint=25, anchor-audit=100); code-structure drift is rare unless an
    # active refactor is underway. Gated by the code_graph.enabled runtime-config
    # row (ADR-0163, dir-aware): inert when disabled. (Formerly shared this
    # priority-2 slot with repo-wiki's own refresh cadence, mutually exclusive;
    # repo_wiki was decommissioned — #33/ADR-0162 — so code_graph now owns it
    # outright.)
    CODE_GRAPH_REFRESH_STOP_INTERVAL: int = 200
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
    # Fix A O7: raised 1 → 8 in lockstep with TOOL_POOL_WORKERS. With core
    # tool-body offload ON, N workers issue N parallel /rerank requests; at
    # concurrency 1 the backend semaphore would 503-storm (2s acquire timeout) and
    # those 503s feed back into O2 pool exhaustion. NOTE: this default is read by
    # the BACKEND container — it needs a rebump/env to pick up the new value (the
    # core default change alone does not propagate to the running backend).
    RERANK_MAX_CONCURRENCY: int = 8
    # Seconds to wait for semaphore before returning 503.
    # Should be ≤ CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC so probes always fail fast.
    RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC: float = 2.0

    # Fix A (daemon-offload-A) — tool-body offload off the asyncio loop.
    # OFFLOAD_TOOLS: master kill-switch. Default OFF for the first release; flip ON
    #   after live soak (the proven loop-block trigger stays inline until then,
    #   covered by the deployed P0 health-kill backstop).
    OFFLOAD_TOOLS: bool = False
    # v5.95: dropped 8 → 2. On the --cpus-1 core, 8 offload threads competing
    # for one CPU causes event-loop starvation under MCP burst → P0 health-kill.
    # 2 keeps competition minimal; raise via config.yaml if tool serialization
    # is a bottleneck. Keep in lockstep with RECALL_HEAVY_CONCURRENCY (must be >).
    TOOL_POOL_WORKERS: int = 2
    # #74 fix #2 — heavy-rerank fan-out gate. Process-wide cap on concurrent
    # backend /rerank calls the core issues. The binding constraint is the
    # BACKEND's serving capacity (fewer cores than TOOL_POOL_WORKERS), NOT the
    # pool size. MUST be strictly < TOOL_POOL_WORKERS (else the gate is a no-op
    # and N workers saturate the backend → slow /health → core 503 → P0 kill) and
    # ≤ RERANK_MAX_CONCURRENCY.
    # T3 Car 3: default is now the sentinel 0 = AUTO — derive from available_cpus()
    # (recall_heavy_concurrency_default() = 1 at ncpu ≤ 2, the pre-Car-3 value;
    # scales above without a code change). Any explicit positive value overrides.
    # Resolved + clamped in offload._heavy_concurrency().
    RECALL_HEAVY_CONCURRENCY: int = 0
    # Seconds a worker waits for a heavy-rerank slot before degrading (skip rerank
    # → pre-rerank order). Bounded so a gated worker never holds its pool slot past
    # the tool timeout (which would leak it). Mirrors the breaker probe timeout.
    RERANK_GATE_ACQUIRE_TIMEOUT_SEC: float = 2.0
    # #74 fix #3 — timeout cascade. Per-tool offload timeout. MUST cover a
    # realistic worst-case recall INCLUDING the backend rerank, i.e.
    # >= RERANK_BACKEND_TIMEOUT_SEC (90). A smaller value cancels the coroutine
    # mid-rerank and leaks an uncancellable worker that completed legit work.
    # Ordering invariant: SATURATION_GRACE > TOOL_TIMEOUT >= RERANK_BACKEND_TIMEOUT.
    TOOL_TIMEOUT_SEC: float = 95.0
    # O2 saturation grace: idle seconds (no completion) while the pool is full
    # before /health degrades → 503. MUST be > TOOL_TIMEOUT_SEC so legit ops keep
    # resetting the clock and only leaked workers trip the signal.
    TOOL_SATURATION_GRACE_SEC: float = 120.0
    # T3 Car 2 — recall side-effect fork. Forks BOTH inline side-effect halves off
    # the recall response path: the core session half (SR transition writes) to a
    # single-FIFO worker, and the backend batched heat DB write (~407ms tail) to a
    # tracked asyncio task. Default ON; flip False to restore inline behavior.
    RECALL_SIDEEFFECT_FORK: bool = True
    # Max QUEUED core session side-effects before submit backpressures to inline
    # (bounds memory under recall storms; overflow runs inline — slower, never lost).
    RECALL_SIDEEFFECT_SESSION_MAX_PENDING: int = 64
    # Max in-flight backend DB-write tasks before schedule refuses (inline write).
    RECALL_SIDEEFFECT_DB_MAX_INFLIGHT: int = 64

    # T3 Car 3 — CPU-aware, parallel-ready recall pipeline.
    # RECALL_PARALLELISM: master parallelism knob for the recall provider gather.
    #   "auto" (default) = derive the gather + torch-thread budgets from
    #   available_cpus() (sequential at ncpu ≤ 2 = today's behavior; fans out
    #   above). "1" forces sequential regardless of core count (the no-thrash /
    #   ops escape hatch). Read live in _shared/runtime/cpu.py.
    RECALL_PARALLELISM: str = "auto"
    # AVAILABLE_CPUS: override the detected CPU budget (0 = auto-detect via cgroup
    #   quota → os.cpu_count()). Non-zero pins the effective core count every recall
    #   concurrency budget derives from — the container-CPU escape hatch when the
    #   cgroup read is unavailable or wrong. Never < 1 downstream.
    AVAILABLE_CPUS: int = 0

    # v5.95 config-integrity Phase 4 — hot-path literals promoted to knobs so ops
    # can tune them without a rebuild. Each is read via get_settings() at the
    # consumer, so config.yaml is authoritative (and the phantom-knob ratchet
    # covers them).
    # Reranker idle-unload: free ~500 MB after this many idle seconds of no recall.
    RERANKER_IDLE_UNLOAD_SEC: float = 600.0
    # Interval between reranker idle-unload checks (background thread sleep).
    RERANKER_IDLE_CHECK_INTERVAL_SEC: int = 60
    # Outer hard bound on the whole /health handler body (never exceed even if a
    # dependency probe hangs). Container healthcheck uses --health-timeout 5s.
    HEALTH_HANDLER_TIMEOUT_SEC: float = 3.0
    # Per-dependency (db/embed) probe HTTP client timeout inside /health.
    HEALTH_PROBE_TIMEOUT_SEC: float = 2.0
    # Auto-vacuum cooldown: hours since the last auto-fire before another may fire.
    VACUUM_AUTO_COOLDOWN_HOURS: float = 6.0

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
    # backend 5.17.0 (Car 0) — % of backend container RAM budgeted for the unified
    # backend Cache (byte-bounded LRU eviction across the ce/embed namespaces).
    # Split weighted across namespaces; supersedes the count-cap *_MAX_ENTRIES.
    BACKEND_CACHE_RAM_PCT: float = 10.0
    # core 5.112.0 (#49) — % of the CORE container RAM (--memory 1g) budgeted for
    # the unified core Cache (yadgar/cache.py): byte-bounded LRU eviction across the
    # core read-tool namespaces (project_brief / wiki_read / wiki_query /
    # agent_prompt_prelude), sharing ONE process budget (weighted split). Mirrors
    # BACKEND_CACHE_RAM_PCT but for the core process's own container + namespaces.
    CORE_CACHE_RAM_PCT: float = 10.0

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
    # v5.88 — Graph node caps (I25 three-way registered; 0 or -1 = unlimited)
    VIZ_MAX_MEMORIES: int = 0  # max memory nodes in /api/graph (0/-1 = all; 0 = unlimited default)
    VIZ_MAX_WIKI: int = 0  # max wiki nodes in /api/graph (0/-1 = all; 0 = unlimited default)
    VIZ_MAX_ENTITIES: int = 0  # max entity nodes in /api/graph (0/-1 = all; 0 = unlimited default)
    # Precomputed server-side graph layout (I25 three-way registered).
    # viz-render-perf (Car A): the nightly/full consolidation cycle always computes
    # 3D node positions + caches them; /api/graph attaches x/y/z so the viz renders
    # pre-laid-out instead of a slow cold client-side force settle. The enable knob
    # was removed — precompute is unconditional (supersedes ADR-0010's default-OFF).
    VIZ_LAYOUT_ITERATIONS: int = 50  # spring_layout iteration cap (lower=faster/looser)
    # finish-viz — Milky-Way galaxy layout (I25 three-way registered). When on
    # (default), the nightly precompute produces galaxy positions (loose→dense core
    # bulge, multi-member clusters→K log-spiral arms) instead of spring_layout; the
    # client freezes physics on a galaxy payload so the shape holds.
    VIZ_GALAXY_LAYOUT: bool = True  # galaxy layout default-on (False → spring_layout)
    VIZ_GALAXY_ARMS: int = 4  # number of spiral arms (K)
    VIZ_GALAXY_SPIRAL_PITCH: float = 0.30  # log-spiral tightness (smaller=tighter winding)
    VIZ_GALAXY_CORE_DENSITY: float = 1.0  # core bulge packing density (higher=tighter core)
    # viz-render-perf (Car A) — per-edge-type caps for the /api/graph edge scans
    # (I25 three-way registered). Default 0 = unlimited → behavior-preserving day one.
    # Applied at the graph call sites only (ORDER BY strongest-first under any LIMIT);
    # the nightly precompute lays out the full uncapped graph.
    VIZ_MAX_TRANSITIONS: int = 0  # max transition (co-recall) edges (0/-1 = all)
    VIZ_MAX_WIKI_CROSSREFS: int = 0  # max wiki cross-reference edges (0/-1 = all)
    VIZ_MAX_CAUSAL_EDGES: int = 0  # max PC-algorithm causal edges (0/-1 = all)
    VIZ_MAX_RELATIONSHIPS: int = 0  # max entity typed-relation edges (0/-1 = all)
    VIZ_MAX_SIMILARITY_LINKS: int = 0  # max memory_similarity_link edges (0/-1 = all)

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
    # Set to true in ~/.yadgar/config.yaml after reading docs/plans/archive/PLAN_V5_49_0.md § Rollout.
    UPDATE_INSTALL_ENABLED: bool = False
    # Maximum age in seconds for an upgrade lock before it's treated as stale.
    # Default 3600 = 1 hour.  Allows recovery if the upgrader process was killed mid-run.
    UPDATE_LOCK_MAX_AGE_SECONDS: int = 3600

    # v5.85 car #6 — Agent-prompt Tier-1 passive library kill-gate.
    # When True, agent-prompt pages are retrievable via recall(type="wiki",
    # tags=["agent-prompt"]) and the save/dispatch surface is active. When False,
    # the library is intended to be inert. Default True — the library is pull-only
    # (no auto-injection), so it is non-intrusive when on.
    # (I25 three-way registered: env YADGAR_AGENT_PROMPT_LIBRARY_ENABLED, yaml, registry)
    AGENT_PROMPT_LIBRARY_ENABLED: bool = True

    model_config = {"env_prefix": "YADGAR_"}

    @observe(
        exempt="pydantic field_validator — must raise ValidationError inline; an @observe wrapper alters the raise path pydantic hooks"
    )
    @field_validator("VACUUM_AUTO_WINDOW_START", "VACUUM_AUTO_WINDOW_END")
    @classmethod
    def _validate_hhmm(cls, v: str) -> str:
        if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", v):
            raise ValueError(f"must be HH:MM (00:00-23:59), got {v!r}")
        return v

    @observe(
        exempt="pydantic field_validator — must raise ValidationError inline; an @observe wrapper alters the raise path pydantic hooks"
    )
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


# Named tuple: `except _KNOB_PARSE_ERRORS:` survives py3.14 ruff-format (an inline
# paren-tuple is rewritten to the PEP-758 bare form the older AST hooks reject).
_KNOB_PARSE_ERRORS: tuple[type[Exception], ...] = (ValueError, TypeError)


@observe(tier="hot")
def resolve_knob[T](
    env_name: str,
    field_name: str,
    parse: Callable[[str], T],
    default: T,
) -> T:
    """Resolve a tunable knob: live env > get_settings().<FIELD> > literal default.

    The DRY implementation of the v5.95.0 config-integrity read pattern. Consumers
    that historically did `os.environ.get("YADGAR_X", ...)` (env-ONLY — the phantom
    knob bug: config.yaml/UI showed+wrote values the code never read) call this
    instead so config.yaml becomes authoritative WITHOUT losing the live-env
    override.

    Precedence + rationale:
      1. Live `os.environ[env_name]` FIRST — read directly (not via get_settings)
         so a test/container override applies immediately, bypassing the
         get_settings() lru_cache lag. `parse` wraps ONLY this raw string; a
         ValueError/TypeError from a malformed env value is swallowed (falls
         through), never crashes the consumer.
      2. `get_settings().<field_name>` — the yaml-aware Settings layer
         (YamlConfigSource, precedence env>yaml>default). This is what makes a
         config.yaml value actually take effect. Already typed — not re-parsed.
         A missing field (AttributeError) or any Settings error falls through.
      3. `default` — a safe literal so the consumer can never hard-fail on a
         broken config surface.

    Note: callers that additionally CLAMP a value (e.g. min(x, pool_workers))
    must apply the clamp AFTER resolve_knob — the clamp is consumer logic, not
    part of the three-way resolution.
    """
    raw = os.environ.get(env_name)
    if raw is not None:
        try:
            return parse(raw)
        # Malformed env value → fall through to Settings. Specific catch via the named
        # constant (see _KNOB_PARSE_ERRORS); keeps the v5.46.16 specific-catch convention.
        except _KNOB_PARSE_ERRORS:
            pass
    try:
        return getattr(get_settings(), field_name)
    except Exception:  # noqa: BLE001 -- never hard-fail a consumer on config surface
        return default
