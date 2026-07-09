"""YAML config file management for Yadgar.

Config file location: ~/.config/yadgar/config.yaml
Priority: env vars > YAML file > defaults
"""
# Module size justified: single-responsibility config loader — large LoC is schema data (FIELD_META), not logic.

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

import yadgar._shared.paths as _paths
from yadgar._shared.observability.observe import observe

FIELD_META: dict[str, dict[str, object]] = {
    # core
    "db_path": {"desc": "SurrealDB storage path", "section": "core"},
    "port": {"desc": "HTTP server port (daemon mode, default 8742)", "section": "core"},
    "embedding_model": {
        "desc": "Sentence-transformer model (e.g. all-MiniLM-L6-v2, all-mpnet-base-v2)",
        "section": "core",
    },
    "model_idle_eviction_seconds": {
        "desc": (
            "Seconds a loaded model may sit idle in the backend before eviction "
            "(default 0 = never evict). Read by the backend RemoteMLClient. "
            "v5.95: promoted from env-only to a config.yaml-authoritative knob."
        ),
        "section": "core",
    },
    "max_episode_tokens": {"desc": "Maximum tokens per episode chunk", "section": "core"},
    "overlap_tokens": {"desc": "Token overlap between episode chunks", "section": "core"},
    # daemon
    "daemon_check_interval": {
        "desc": "Seconds between astrocyte background loop wakeups (lower = more responsive, higher = less CPU)",
        "section": "daemon",
    },
    "num_astrocyte_processes": {
        "desc": "Number of domain-aware background worker processes",
        "section": "daemon",
    },
    "astrocyte_pool_enabled": {
        "desc": "Enable domain-aware astrocyte pool consolidation (set False to disable)",
        "section": "daemon",
    },
    "narrative_interval_hours": {
        "desc": "Hours between autobiographical narrative updates",
        "section": "daemon",
    },
    "similarity_linking_incremental_enabled": {
        "desc": (
            "v5.86 (OT-C4): link only memories created since the last run "
            "(probe×corpus), with a periodic full reconcile. Default False — "
            "the full N×N pass runs every cycle until enabled."
        ),
        "section": "daemon",
    },
    "similarity_linking_reconcile_interval_days": {
        "desc": (
            "Days between mandatory full similarity-link reconcile passes "
            "(safety net for re-embedding that mutates old↔old similarity)."
        ),
        "section": "daemon",
    },
    # memory_lifecycle
    "write_gate_threshold": {
        "desc": "Minimum score to store a memory (0.0 = store everything)",
        "section": "memory_lifecycle",
    },
    "write_gate_shadow_threshold": {
        "desc": (
            "Shadow-mode threshold (v5.73.0): memories below this score are stamped "
            "would_reject=True but are STILL stored (WRITE_GATE_THRESHOLD stays 0.0). "
            "Used to audit which memories would be dropped at a candidate threshold."
        ),
        "section": "memory_lifecycle",
    },
    "write_gate_continuity_discount": {
        "desc": "Threshold reduction for task-continuous content",
        "section": "memory_lifecycle",
    },
    "write_gate_continuity_window": {
        "desc": "Number of recent stores to track for continuity detection",
        "section": "memory_lifecycle",
    },
    "decision_auto_protect": {
        "desc": "Automatically protect detected decisions from decay",
        "section": "memory_lifecycle",
    },
    "action_stream_enabled": {
        "desc": "Capture tool actions in sensory buffer for later consolidation",
        "section": "memory_lifecycle",
    },
    "micro_checkpoint_enabled": {
        "desc": "Auto-checkpoint on significant events",
        "section": "memory_lifecycle",
    },
    "micro_checkpoint_cooldown": {
        "desc": "Minimum tool calls between micro-checkpoints",
        "section": "memory_lifecycle",
    },
    "session_coherence_bonus": {
        "desc": "Heat bonus applied to memories from the current session",
        "section": "memory_lifecycle",
    },
    "session_coherence_window_hours": {
        "desc": "How long the session coherence bonus lasts",
        "section": "memory_lifecycle",
    },
    "reinjection_enabled": {
        "desc": "Auto-surface related context when storing a new memory",
        "section": "memory_lifecycle",
    },
    "reinjection_max_results": {
        "desc": "Max related memories to reinject on store",
        "section": "memory_lifecycle",
    },
    # thermodynamics
    "decay_factor": {
        "desc": "Per-hour heat decay multiplier for memories (0.95 = 5% decay/hour)",
        "section": "thermodynamics",
    },
    "importance_decay_factor": {
        "desc": "Per-cycle importance decay (0.998 = very slow decay)",
        "section": "thermodynamics",
    },
    "cold_threshold": {
        "desc": "Heat below which a memory is archived (0.02 = ~6 months no-access floor)",
        "section": "thermodynamics",
    },
    "action_stream_cold_threshold": {
        "desc": "Heat below which an auto-captured action-stream memory is archived (higher than cold_threshold so they expire faster)",
        "section": "thermodynamics",
    },
    "hot_threshold": {
        "desc": "Minimum heat for hot-memory retrieval (0.0 = include all)",
        "section": "thermodynamics",
    },
    "project_context_min_heat": {
        "desc": "Minimum heat for project context injection (0.0 = include all)",
        "section": "thermodynamics",
    },
    "surprise_boost": {
        "desc": "Heat boost applied to surprising/novel memories",
        "section": "thermodynamics",
    },
    "emotional_decay_resistance": {
        "desc": "How much emotional salience slows decay (0-1)",
        "section": "thermodynamics",
    },
    "synaptic_window_minutes": {
        "desc": "Time window for synaptic boost propagation",
        "section": "thermodynamics",
    },
    "synaptic_boost": {
        "desc": "Heat boost propagated from high-importance nearby memories",
        "section": "thermodynamics",
    },
    # retrieval_fusion
    "wrrf_candidate_multiplier": {
        "desc": "Candidate pool size = max_results * this",
        "section": "retrieval_fusion",
    },
    "wrrf_vector_weight": {
        "desc": "Weight of vector similarity signal in WRRF fusion",
        "section": "retrieval_fusion",
    },
    "wrrf_fts_weight": {
        "desc": "Weight of full-text search signal in WRRF fusion",
        "section": "retrieval_fusion",
    },
    "wrrf_ppr_weight": {
        "desc": "Weight of personalized PageRank signal",
        "section": "retrieval_fusion",
    },
    "wrrf_spreading_weight": {
        "desc": "Weight of spreading activation signal",
        "section": "retrieval_fusion",
    },
    "wrrf_graph_prior_weight": {
        "desc": (
            "Weight of precomputed entity-graph prior in fusion (v5.54.1). "
            "Additive boost applied in ALL profiles including fast — O(1) field read, "
            "no per-query traversal. Set 0.0 to disable. Default 0.2."
        ),
        "section": "retrieval_fusion",
    },
    "wrrf_cofire_prior_weight": {
        "desc": (
            "Weight of precomputed co-recall (transition-edge) prior in fusion (v5.54.2). "
            "Additive boost applied in ALL profiles including fast — O(1) field read, "
            "no transition-table traversal on request path. 'Recalled together before' = "
            "learned association. Set 0.0 to disable. Default 0.15."
        ),
        "section": "retrieval_fusion",
    },
    "fusion_method": {"desc": "Fusion method: convex or other", "section": "retrieval_fusion"},
    "fusion_norm": {
        "desc": "Score normalization before fusion: zscore, minmax, or raw",
        "section": "retrieval_fusion",
    },
    "combmnz_enabled": {
        "desc": "Multiply fused score by the number of signals that contributed",
        "section": "retrieval_fusion",
    },
    # reranking
    "reranker_enabled": {"desc": "Enable cross-encoder reranking stage", "section": "reranking"},
    "reranker_top_k": {"desc": "Number of candidates passed to reranker", "section": "reranking"},
    "cross_encoder_enabled": {
        "desc": "Enable FlashRank ONNX cross-encoder reranking",
        "section": "reranking",
    },
    "cross_encoder_model": {"desc": "Cross-encoder model name", "section": "reranking"},
    "cross_encoder_top_k": {"desc": "Top-k passed to cross-encoder", "section": "reranking"},
    "cross_encoder_weight": {
        "desc": "Cross-encoder score weight in blend (retrieval gets 1-this)",
        "section": "reranking",
    },
    "gte_reranker_enabled": {
        "desc": "Enable GTE-Reranker (ModernBERT-based)",
        "section": "reranking",
    },
    "gte_reranker_model": {"desc": "GTE reranker model name", "section": "reranking"},
    "gte_reranker_max_length": {
        "desc": "Max token length for GTE reranker",
        "section": "reranking",
    },
    "gte_reranker_fallback_to_flashrank": {
        "desc": "Fall back to FlashRank if GTE reranker fails",
        "section": "reranking",
    },
    "gte_reranker_backend": {
        "desc": "GTE reranker backend: torch (fp32) or onnx-int8 (quantized, ~1.8x faster)",
        "section": "reranking",
    },
    "gte_reranker_onnx_file": {
        "desc": "ONNX artifact loaded when gte_reranker_backend=onnx-int8",
        "section": "reranking",
    },
    "nli_reranking_enabled": {
        "desc": "Enable NLI entailment scoring stage",
        "section": "reranking",
    },
    "nli_model": {"desc": "NLI model name", "section": "reranking"},
    "nli_weight": {"desc": "NLI signal weight in final blend", "section": "reranking"},
    "nli_only_for_open_domain": {
        "desc": "Only apply NLI reranking for open-domain queries",
        "section": "reranking",
    },
    "multi_passage_reranking_enabled": {
        "desc": "Enable multi-passage evidence aggregation",
        "section": "reranking",
    },
    "multi_passage_cluster_overlap_threshold": {
        "desc": "Overlap threshold for passage clustering",
        "section": "reranking",
    },
    "multi_passage_max_cluster_size": {
        "desc": "Maximum passages per evidence cluster",
        "section": "reranking",
    },
    # query_routing
    "query_routing_enabled": {
        "desc": "Enable automatic query routing to specialized retrievers",
        "section": "query_routing",
    },
    "query_expansion_enabled": {
        "desc": "Enable query expansion (pseudo-HyDE)",
        "section": "query_routing",
    },
    "comparison_dual_search_enabled": {
        "desc": "Run dual search for comparison queries",
        "section": "query_routing",
    },
    "comparison_top_k_per_option": {
        "desc": "Top-k results per option in comparison search",
        "section": "query_routing",
    },
    "temporal_keywords": {
        "desc": "Comma-separated keywords that trigger temporal routing",
        "section": "query_routing",
    },
    "code_keywords": {
        "desc": "Comma-separated keywords that trigger code-aware routing",
        "section": "query_routing",
    },
    "relational_keywords": {
        "desc": "Comma-separated keywords that trigger relational routing",
        "section": "query_routing",
    },
    # temporal_retrieval
    "temporal_retrieval_enabled": {
        "desc": "Boost memories that match temporal expressions in query",
        "section": "temporal_retrieval",
    },
    # embedding_enhancement
    "candidate_pool_multiplier": {
        "desc": "Total candidate pool = max_results * this before reranking",
        "section": "embedding_enhancement",
    },
    "fast_profile_candidate_multiplier": {
        "desc": "Candidate pool multiplier used only for profile='fast' (default 3; overrides global candidate_pool_multiplier on fast path)",
        "section": "embedding_enhancement",
    },
    # graph_knowledge
    "graph_max_hops": {
        "desc": "Maximum graph traversal hops for spreading activation",
        "section": "graph_knowledge",
    },
    "graph_min_edge_weight": {
        "desc": "Minimum edge weight to traverse in graph signals",
        "section": "graph_knowledge",
    },
    "graph_spreading_decay": {
        "desc": "Activation decay factor per hop",
        "section": "graph_knowledge",
    },
    "graph_spreading_max_depth": {
        "desc": "Maximum depth for spreading activation",
        "section": "graph_knowledge",
    },
    "graph_entity_min_length": {
        "desc": "Minimum character length for extracted entities",
        "section": "graph_knowledge",
    },
    "causal_threshold": {
        "desc": "Minimum co-occurrence count before inferring causality",
        "section": "graph_knowledge",
    },
    "ppr_damping": {"desc": "Personalized PageRank damping factor", "section": "graph_knowledge"},
    "ppr_iterations": {"desc": "Number of PageRank iterations", "section": "graph_knowledge"},
    "cluster_similarity_threshold": {
        "desc": "Minimum similarity to assign a memory to a cluster",
        "section": "graph_knowledge",
    },
    # neuromorphic
    "cognitive_load_limit": {
        "desc": "Max chunks in active context (Cowan's 4±1 rule)",
        "section": "neuromorphic",
    },
    "excitability_half_life_hours": {
        "desc": "Engram excitability decay half-life in hours",
        "section": "neuromorphic",
    },
    "excitability_boost": {
        "desc": "Excitability increase on engram slot activation",
        "section": "neuromorphic",
    },
    "dream_replay_pairs": {
        "desc": "Random memory pairs examined per dream replay cycle",
        "section": "neuromorphic",
    },
    # enrichment
    "index_enrichment_enabled": {
        "desc": "Enable index-time memory enrichment pipeline",
        "section": "enrichment",
    },
    "enrichment_min_content_length": {
        "desc": "Minimum content length to run enrichment",
        "section": "enrichment",
    },
    "conceptnet_enrichment_enabled": {
        "desc": "Expand memories with ConceptNet relations",
        "section": "enrichment",
    },
    "conceptnet_min_edge_weight": {
        "desc": "Minimum ConceptNet edge weight to include",
        "section": "enrichment",
    },
    "conceptnet_max_terms": {
        "desc": "Maximum ConceptNet terms to add per memory",
        "section": "enrichment",
    },
    "conceptnet_relations": {
        "desc": "Comma-separated ConceptNet relations to use",
        "section": "enrichment",
    },
    "comet_enrichment_enabled": {
        "desc": "Expand memories with COMET commonsense inference",
        "section": "enrichment",
    },
    "comet_model": {"desc": "COMET model name", "section": "enrichment"},
    "comet_num_beams": {"desc": "Beam search width for COMET generation", "section": "enrichment"},
    "comet_top_k_per_relation": {
        "desc": "Top-k inferences per COMET relation",
        "section": "enrichment",
    },
    "comet_min_confidence": {
        "desc": "Minimum COMET inference confidence to include",
        "section": "enrichment",
    },
    "comet_relations": {"desc": "Comma-separated COMET relations to use", "section": "enrichment"},
    "comet_query_expansion_enabled": {
        "desc": "Apply COMET expansion at query time too",
        "section": "enrichment",
    },
    "doc2query_enrichment_enabled": {
        "desc": "Generate synthetic queries for each memory (doc2query)",
        "section": "enrichment",
    },
    "doc2query_model": {"desc": "Doc2query model name", "section": "enrichment"},
    "doc2query_num_queries": {
        "desc": "Number of synthetic queries to generate per memory",
        "section": "enrichment",
    },
    "logic_enrichment_enabled": {
        "desc": "Enable formal logic pattern enrichment",
        "section": "enrichment",
    },
    "fpa_similarity_threshold": {
        "desc": "Similarity threshold for first-principles analysis",
        "section": "enrichment",
    },
    # profiles_beliefs
    "profile_extraction_enabled": {
        "desc": "Extract and maintain structured user profiles",
        "section": "profiles_beliefs",
    },
    "profile_confidence_direct": {
        "desc": "Confidence for directly stated profile attributes",
        "section": "profiles_beliefs",
    },
    "profile_confidence_inferred": {
        "desc": "Confidence for inferred profile attributes",
        "section": "profiles_beliefs",
    },
    "profile_summary_enabled": {
        "desc": "Generate profile summaries",
        "section": "profiles_beliefs",
    },
    "derived_beliefs_enabled": {
        "desc": "Derive and store higher-order beliefs from episodic memories",
        "section": "profiles_beliefs",
    },
    "belief_high_confidence_boost": {
        "desc": "Score multiplier for high-confidence beliefs",
        "section": "profiles_beliefs",
    },
    # adversarial
    "adversarial_detection_enabled": {
        "desc": "Detect and suppress adversarially-crafted memory injection",
        "section": "adversarial",
    },
    "adversarial_score_gap_threshold": {
        "desc": "Maximum acceptable score gap between top results",
        "section": "adversarial",
    },
    "adversarial_diversity_enforcement": {
        "desc": "Enforce result diversity to prevent manipulation",
        "section": "adversarial",
    },
    "adversarial_min_confidence": {
        "desc": "Minimum confidence required to surface a memory",
        "section": "adversarial",
    },
    # observability (OTLP exporter)
    "otlp_endpoint": {
        "desc": "OTLP/HTTP endpoint for Tempo trace export (e.g. http://tempo:4318/v1/traces). Empty = disabled.",
        "section": "observability",
    },
    "otlp_headers": {
        "desc": "Comma-separated k=v auth/tenant headers for OTLP exporter (e.g. x-tenant=foo,authorization=Bearer tok)",
        "section": "observability",
    },
    "otlp_timeout_sec": {
        "desc": "OTLP exporter HTTP timeout in seconds (default 10)",
        "section": "observability",
    },
    "otlp_insecure": {
        "desc": "Use plain HTTP for OTLP export (true = HTTP, false = TLS; default true)",
        "section": "observability",
    },
    # backend_cache
    "dbsize_cache_ttl_sec": {
        "desc": "/admin/dbsize response cache TTL in seconds (0 = disabled, default 60)",
        "section": "backend_cache",
    },
    # logging
    "core_log_level": {
        "desc": "Log level for the core yadgar MCP server (DEBUG/INFO/WARNING/ERROR)",
        "section": "logging",
        "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    },
    "backend_log_level": {
        "desc": "Log level for the backend container (embed service + SurrealDB)",
        "section": "logging",
        "choices": ["debug", "info", "warn", "error"],
    },
    "log_format": {
        "desc": "Log output format (json | text | human)",
        "section": "logging",
        "choices": ["json", "text", "human"],
    },
    # misc
    "sensitive_lock_ttl_sec": {
        "desc": "Seconds before a sensitive-job lock (vacuum) is treated as stale and reaped",
        "section": "core",
    },
    "sensitive_drain_timeout_sec": {
        "desc": "Max seconds the signal handler drains an in-process sensitive job before refusing shutdown",
        "section": "core",
    },
    "contextual_prefix_enabled": {
        "desc": "Prepend contextual prefix to improve embedding quality",
        "section": "embedding_enhancement",
    },
    "curation_similarity_threshold": {
        "desc": "Minimum similarity to trigger memory curation/merging",
        "section": "memory_lifecycle",
    },
    "crdt_agent_id": {"desc": "Agent identifier for multi-agent CRDT sync", "section": "core"},
    "replay_max_restore_memories": {
        "desc": "Maximum memories included in context restoration",
        "section": "session_end_capture",
    },
    "replay_anchor_heat": {
        "desc": "Heat assigned to anchored (protected) memories",
        "section": "session_end_capture",
    },
    "replay_checkpoint_auto_interval": {
        "desc": "Auto-checkpoint every N tool calls",
        "section": "session_end_capture",
    },
    # project_brief (v5.7.12)
    "active_work_stale_hours": {
        "desc": "Hours before active_work is considered stale (triggers refresh_active_work in signals mode)",
        "section": "project_brief",
    },
    "checkpoint_stale_hours": {
        "desc": "Hours before checkpoint is considered stale (triggers refresh_checkpoint in signals mode)",
        "section": "project_brief",
    },
    "project_brief_max_anchors": {
        "desc": "Maximum anchors returned in restore mode top_anchors list (default 12)",
        "section": "project_brief",
    },
    # project_brief soft warning tier (v5.10.1)
    "active_work_warn_hours": {
        "desc": "Hours before consider_refresh_active_work soft action fires (default 12; must be < active_work_stale_hours)",
        "section": "project_brief",
    },
    "checkpoint_warn_hours": {
        "desc": "Hours before consider_refresh_checkpoint soft action fires (default 12; must be < checkpoint_stale_hours)",
        "section": "project_brief",
    },
    "auto_refresh_active_work": {
        "desc": "Watchdog opt-in: auto-write stub _active_work when stale (default false; enable via systemd unit env)",
        "section": "active_work_watchdog",
    },
    "signals_token_budget_soft": {
        "desc": "Token-budget upper bound for signals mode payload (default 400; raise if new action types added)",
        "section": "project_brief",
    },
    # v5.84.0 car #12: ADR nudge threshold
    "adr_due_warn_hours": {
        "desc": "Hours of ADR log inactivity (vs active_work) before capture_adr fires in signals mode (default 12)",
        "section": "project_brief",
    },
    # v5.89 #69: dispatch-prelude read-side nudge threshold
    "dispatch_prelude_due_warn_hours": {
        "desc": "Hours without agent_dispatch_prelude call (vs active_work) before use_agent_prompt_library fires in signals mode (default 12)",
        "section": "project_brief",
    },
    # anchor_hygiene (v5.8.0)
    "anchor_conditional_ttl_days": {
        "desc": "Default valid_until offset (days) for tier=conditional anchors (default 90)",
        "section": "anchor_hygiene",
    },
    "anchor_ephemeral_ttl_days": {
        "desc": "Default valid_until offset (days) for tier=ephemeral anchors (default 14)",
        "section": "anchor_hygiene",
    },
    "anchor_semantic_immortal_requires_reason": {
        "desc": "Require non-empty reason when anchor(tier='semantic_immortal') is called (default true)",
        "section": "anchor_hygiene",
    },
    # v5.8.0 PR-B: anchor hygiene signals + recommended_actions
    "anchor_redundancy_cosine": {
        "desc": "Minimum cosine similarity for anchor redundancy candidate pair (default 0.92)",
        "section": "anchor_hygiene",
    },
    "anchor_promote_words": {
        "desc": "Minimum word count for anchor promote-to-wiki candidate (default 500)",
        "section": "anchor_hygiene",
    },
    "anchor_promote_headers": {
        "desc": "Minimum markdown header count for anchor promote-to-wiki candidate (default 2)",
        "section": "anchor_hygiene",
    },
    "anchor_audit_threshold": {
        "desc": "anchor_count_project threshold above which audit_anchors action is emitted (default 15)",
        "section": "anchor_hygiene",
    },
    # v5.9.0: anchor audit pass knobs
    "anchor_audit_consolidation_enabled": {
        "desc": "Toggle anchor audit pass inside consolidate_now() (default true)",
        "section": "anchor_hygiene",
    },
    "anchor_audit_max_actions_per_run": {
        "desc": "Hard cap on actions returned per audit run — token budget (default 20)",
        "section": "anchor_hygiene",
    },
    "anchor_audit_history_retention_days": {
        "desc": "How long _audit_anchors sentinel snapshots are retained (days, default 30)",
        "section": "anchor_hygiene",
    },
    # v5.21.0: cross-project anchor dedup
    "anchor_cross_project_cosine": {
        "desc": "Minimum cosine for cross-project anchor dedup candidate (default 0.95, higher than within-project 0.92)",
        "section": "anchor_hygiene",
    },
    # session_end_capture (v5.10.6)
    "session_end_capture_enabled": {
        "desc": "Kill switch for session-end sentinel capture (default true)",
        "section": "session_end_capture",
    },
    "session_end_retention_days": {
        "desc": "Auto-prune sentinel memory rows older than this many days (default 30)",
        "section": "session_end_capture",
    },
    "session_end_snippet_turns": {
        "desc": "Last N human turns embedded in sentinel for rotation resilience (default 5)",
        "section": "session_end_capture",
    },
    "session_end_min_messages": {
        "desc": "Skip sentinel if session had fewer than N human messages (default 2)",
        "section": "session_end_capture",
    },
    # backend_hot_path_cache (backend v5.4.0)
    "ce_cache_enabled": {
        "desc": "Enable CE score LRU cache (false/0 = disabled, pre-v5.4.0 behaviour)",
        "section": "backend_hot_path_cache",
    },
    "embed_cache_enabled": {
        "desc": "Enable embedding vector LRU cache (false/0 = disabled)",
        "section": "backend_hot_path_cache",
    },
    "ce_cache_max_entries": {
        "desc": "Maximum entries in CE score LRU cache (0 = disabled, default 100000)",
        "section": "backend_hot_path_cache",
    },
    "embed_cache_max_entries": {
        "desc": "Maximum entries in embedding vector LRU cache (0 = disabled, default 100000)",
        "section": "backend_hot_path_cache",
    },
    "cache_snapshot_interval_sec": {
        "desc": "Interval in seconds between periodic cache snapshots to disk (default 600)",
        "section": "backend_hot_path_cache",
    },
    "cache_snapshot_dir": {
        "desc": "Directory for cache snapshot files ce.snap + embed.snap (default /data/cache)",
        "section": "backend_hot_path_cache",
    },
    "backend_cache_ram_pct": {
        "desc": (
            "Percent of backend container RAM budgeted for the unified backend "
            "Cache; byte-bounded LRU eviction across ce/embed namespaces (default 10)"
        ),
        "section": "backend_hot_path_cache",
    },
    "core_cache_ram_pct": {
        "desc": (
            "Percent of the core container RAM budgeted for the unified core Cache; "
            "byte-bounded LRU eviction across the core read-tool namespaces "
            "project_brief/wiki_read/wiki_query/agent_prompt_prelude (default 10)"
        ),
        "section": "backend_hot_path_cache",
    },
    # v5.5.0 — model preload warm-up
    "model_preload": {
        "desc": (
            "Preload rerank models (ce/nli/pair) in background after startup (default true). "
            "Set to false to disable warm-up and keep lazy-load behaviour."
        ),
        "section": "backend_model_preload",
    },
    "model_preload_delay_sec": {
        "desc": (
            "Seconds to wait after startup before loading rerank models in background (default 10). "
            "Gives the app time to start serving before the warm-up begins."
        ),
        "section": "backend_model_preload",
    },
    # viz_config — v5.11.0
    "viz_node_size_3d": {
        "desc": "3D node sphere radius (nodeRelSize, default 8)",
        "section": "viz_config",
    },
    "viz_node_size_2d": {"desc": "2D canvas node base radius (default 4)", "section": "viz_config"},
    "viz_heat_hue_start": {
        "desc": "Heat colour hue at h=0 (cool end, default 240 = blue)",
        "section": "viz_config",
    },
    "viz_heat_hue_end": {
        "desc": "Heat colour hue at h=1 (hot end, default 0 = red)",
        "section": "viz_config",
    },
    "viz_heat_sat_base": {
        "desc": "Heat colour saturation base % (default 60)",
        "section": "viz_config",
    },
    "viz_heat_sat_gain": {
        "desc": "Heat colour saturation gain % (default 30)",
        "section": "viz_config",
    },
    "viz_heat_light_base": {
        "desc": "Heat colour lightness base % (default 40)",
        "section": "viz_config",
    },
    "viz_heat_light_gain": {
        "desc": "Heat colour lightness gain % (default 20)",
        "section": "viz_config",
    },
    "viz_cat_color_architecture": {
        "desc": "Wiki category colour: architecture (default #58a6ff)",
        "section": "viz_config",
    },
    "viz_cat_color_decision": {
        "desc": "Wiki category colour: decision (default #ffa657)",
        "section": "viz_config",
    },
    "viz_cat_color_pattern": {
        "desc": "Wiki category colour: pattern (default #3fb950)",
        "section": "viz_config",
    },
    "viz_cat_color_debugging": {
        "desc": "Wiki category colour: debugging (default #f85149)",
        "section": "viz_config",
    },
    "viz_cat_color_reference": {
        "desc": "Wiki category colour: reference (default #8b949e)",
        "section": "viz_config",
    },
    "viz_cat_color_convention": {
        "desc": "Wiki category colour: convention (default #d2a8ff)",
        "section": "viz_config",
    },
    "viz_cat_color_fact": {
        "desc": "Wiki category colour: fact (default #a5d6ff)",
        "section": "viz_config",
    },
    "viz_cat_color_analysis": {
        "desc": "Wiki category colour: analysis (default #d29922)",
        "section": "viz_config",
    },
    "viz_edge_color_semantic": {
        "desc": "Edge colour: semantic (default #1f6feb)",
        "section": "viz_config",
    },
    "viz_edge_color_temporal": {
        "desc": "Edge colour: temporal (default #6e40c9)",
        "section": "viz_config",
    },
    "viz_edge_color_transition": {
        "desc": "Edge colour: transition (default #3fb950)",
        "section": "viz_config",
    },
    "viz_edge_color_wiki_crossref": {
        "desc": "Edge colour: wiki_crossref (default #d2a8ff)",
        "section": "viz_config",
    },
    "viz_edge_color_memory_wiki": {
        "desc": "Edge colour: memory_wiki (default #ffa657)",
        "section": "viz_config",
    },
    "viz_edge_width_3d_multiplier": {
        "desc": "3D edge width multiplier over 2D base (default 1.8 — Variant C)",
        "section": "viz_config",
    },
    "viz_edge_arrow_len": {
        "desc": "Arrow length for directional edge types (default 5)",
        "section": "viz_config",
    },
    "viz_edge_opacity": {
        "desc": "Link opacity for all edges (default 0.9 — Variant C)",
        "section": "viz_config",
    },
    "viz_edge_variant": {
        "desc": "Informational: edge style variant in use (default C)",
        "section": "viz_config",
    },
    "viz_wiki_shape": {
        "desc": "Desired shape for wiki nodes — config only; renderer not wired pending v5.10.7.3 (default octahedron)",
        "section": "viz_config",
    },
    "viz_physics_charge_strength": {
        "desc": "D3 charge (repulsion) strength (default -18.0 — v5.50.0)",
        "section": "viz_config",
    },
    "viz_physics_link_distance_2d": {
        "desc": "D3 link distance in 2D mode (default 30)",
        "section": "viz_config",
    },
    "viz_physics_link_distance_3d": {
        "desc": "D3 link distance in 3D mode (default 36)",
        "section": "viz_config",
    },
    "viz_layout_zoom_fit_tick": {
        "desc": "Engine tick threshold to trigger auto-zoom-fit (default 80)",
        "section": "viz_config",
    },
    "viz_layout_zoom_fit_padding": {
        "desc": "Padding px passed to zoomToFit() (default 50)",
        "section": "viz_config",
    },
    "viz_layout_zoom_fit_transition_ms": {
        "desc": "Transition duration ms for zoomToFit() (default 800)",
        "section": "viz_config",
    },
    "viz_search_match_color": {
        "desc": "Stroke colour for search-matched nodes (default #ffffff)",
        "section": "viz_config",
    },
    "viz_search_pinned_color": {
        "desc": "Stroke colour for pinned nodes (default #ffd700)",
        "section": "viz_config",
    },
    "viz_search_dim_opacity": {
        "desc": "Opacity for non-matched dimmed nodes (default 0.18)",
        "section": "viz_config",
    },
    "viz_max_memories": {
        "desc": "Max memory nodes in the /api/graph payload (default 500; 0 or -1 = unlimited)",
        "section": "viz_config",
    },
    "viz_max_wiki": {
        "desc": "Max wiki nodes in the /api/graph payload (default 200; 0 or -1 = unlimited)",
        "section": "viz_config",
    },
    "viz_max_entities": {
        "desc": "Max entity nodes in the /api/graph payload (default 2000; 0 or -1 = unlimited)",
        "section": "viz_config",
    },
    "viz_precomputed_layout_enabled": {
        "desc": (
            "Precompute + cache 3D graph layout server-side during consolidation "
            "so /api/graph serves x/y/z for near-instant viz render (default OFF)"
        ),
        "section": "viz_config",
    },
    "viz_layout_iterations": {
        "desc": "spring_layout iteration cap for the precomputed layout (default 50; lower=faster)",
        "section": "viz_config",
    },
    # memory_blocks (v5.35.1)
    "memory_block_max_per_scope": {
        "desc": "Maximum blocks per (scope, directory) tuple (default 10)",
        "section": "memory_blocks",
    },
    "memory_block_default_char_limit": {
        "desc": "Default per-block character limit when none specified (default 2000)",
        "section": "memory_blocks",
    },
    "memory_block_hard_char_limit": {
        "desc": "Absolute maximum per-block character limit — hard cap (default 8000)",
        "section": "memory_blocks",
    },
    "memory_block_total_budget_chars": {
        "desc": "Total character budget across all blocks at restore-time (default 12000)",
        "section": "memory_blocks",
    },
    # cpu_burst_detection (v5.15.0 D1)
    "phase_duration_warn_ms": {
        "desc": (
            "Consolidation phase duration warn threshold in milliseconds (default 60000 = 1 min). "
            "When any _consolidation_cycle() phase exceeds this, a CRITICAL log is emitted. "
            "Set to 0 to disable."
        ),
        "section": "cpu_burst_detection",
    },
    # wiki_write_wait (v5.41.2)
    "wiki_write_wait_timeout_seconds": {
        "desc": (
            "Maximum seconds wiki_add(wait=True) may block before returning a timeout error "
            "(default 5.0). Only applies to the opt-in wait=True path — the default async "
            "path is completely unaffected. Increase for slow storage; decrease for faster "
            "failure in interactive callers."
        ),
        "section": "wiki_write_wait",
    },
    # wiki_similarity_gate (v5.39.0)
    "wiki_sim_gate_enabled": {
        "desc": "Enable wiki_add similarity gate (default true). Set to false to disable entirely.",
        "section": "wiki_similarity_gate",
    },
    "wiki_sim_content_threshold": {
        "desc": (
            "Minimum cosine similarity on combined (title+content) embedding to flag a duplicate "
            "(default 0.80). Calibrated on all-MiniLM-L6-v2: near-clones ~0.91-0.95, "
            "distinct pages ~0.50-0.65. Reduce to 0.70 for stricter gate; raise toward 0.90 "
            "to reduce false positives."
        ),
        "section": "wiki_similarity_gate",
    },
    "wiki_sim_title_threshold": {
        "desc": (
            "Reserved: minimum cosine similarity on title-only embedding (default 0.85). "
            "Currently unused — single combined embedding stored. Future schema upgrade will "
            "add title_embedding column and activate this threshold."
        ),
        "section": "wiki_similarity_gate",
    },
    "wiki_sim_mode": {
        "desc": (
            "Gate enforcement mode: 'hard' (default) rejects duplicate creates; "
            "'soft' logs a WARNING but allows the write. Use soft mode to audit "
            "without blocking existing agents."
        ),
        "section": "wiki_similarity_gate",
    },
    "wiki_sim_top_k": {
        "desc": "Max candidate duplicate pages returned in rejection response (default 5).",
        "section": "wiki_similarity_gate",
    },
    # v5.42.1: embed failure knob
    "wiki_embed_failure_blocks_write": {
        "desc": (
            "When True, wiki_add fails if _compute_embedding returns None or raises. "
            "Default False: emit WARN log + metric, proceed with NULL embedding (backward compat). "
            "Flip to True once embed service is confirmed reliable."
        ),
        "section": "wiki_similarity_gate",
    },
    # v5.42.6: enforcement knobs
    "directory_enforcement": {
        "desc": (
            "When True (default), wiki_add rejects payloads missing directory_context. "
            "Set to False as a migration escape hatch. "
            "Emits WARN log + yadgar_writes_with_enforcement_relaxed metric when off."
        ),
        "section": "wiki_similarity_gate",
    },
    "branch_enforcement": {
        "desc": (
            "When True (default), wiki_add and memorize reject payloads missing branch. "
            "Set to False as a migration escape hatch. "
            "Emits WARN log + yadgar_writes_with_enforcement_relaxed metric when off."
        ),
        "section": "wiki_similarity_gate",
    },
    # v5.51.0 — hook recall latency budget
    "hook_recall_timeout_s": {
        "desc": (
            "Maximum seconds asyncio.wait_for waits for retriever.recall in hook handlers "
            "(prompt-recall, instructions-loaded, subagent-start). On timeout: WARN log + "
            "yadgar_hook_recall_timeout_total incremented + empty result returned. "
            "Default 2.0. Raise to 5.0 if counter rate too high in prod."
        ),
        "section": "hooks",
    },
    # v5.95 (#81 residual) — dedicated hook-recall pool size
    "hook_recall_pool_workers": {
        "desc": (
            "SEPARATE pool just for hook auto-recalls (SessionStart/UserPrompt), isolated so "
            "hook bursts cannot starve MCP tool calls (ADR-0025). Default 2 (ADR-0077): "
            "post-#166 the hook recall is a forwarded HTTP wait (idle thread, not a "
            "GIL-holding in-core recall); pool=1 starved the second of every concurrent "
            "session pair. Lower back to 1 only if loop-lag returns on the --cpus-1 core "
            "(check yadgar_event_loop_lag_max + yadgar_hook_recall_timeout_total). "
            "Independent of the tool-offload pool (TOOL_POOL_WORKERS). Restart to apply."
        ),
        "section": "hooks",
    },
    # v5.51.0 — /api/stats TTL cache
    "stats_cache_ttl_s": {
        "desc": (
            "/api/stats response TTL in seconds. Within the TTL, the same compute result is "
            "returned without calling get_memory_stats. 0 = disabled (recompute every request). "
            "Default 5. Does not affect /api/system (already sampled by background thread)."
        ),
        "section": "stats_cache",
    },
    # v5.53.1 — stale wiki count cache TTL
    "stale_count_cache_ttl_s": {
        "desc": (
            "Seconds before stale_wiki_count is recomputed from disk scan. "
            "0 = disabled (scan on every signals call, not recommended). "
            "Default 300 (5 minutes) — keeps signals hot path fast while staying fresh."
        ),
        "section": "wiki_staleness",
    },
    # v5.49.0 — memory_archive retention
    "memory_archive_retention_days": {
        "desc": (
            "Purge memory_archive rows whose archived_at exceeds this age (days, default 90). "
            "Set to 0 to disable permanent deletion entirely."
        ),
        "section": "memory_archive_retention",
    },
    "memory_archive_retention_circuit_breaker": {
        "desc": (
            "Maximum rows deleted in a single purge_expired_archives() call (default 500). "
            "Fires a CRITICAL log when the cap is hit."
        ),
        "section": "memory_archive_retention",
    },
    "memory_archive_retention_thrash_guard_days": {
        "desc": (
            "Skip archives whose created_at is more recent than this many days ago (default 7). "
            "Prevents thrash-purging recently-created archives that carry an old archived_at."
        ),
        "section": "memory_archive_retention",
    },
    # cold-memory retention DRY-RUN visibility (#29)
    "cold_memory_retention_days": {
        "desc": (
            "Age threshold (days) for cold user-memory retention candidates (default 90). "
            "Memories older than this with heat<COLD_THRESHOLD and access_count=0 are "
            "surfaced in the nightly report. Set to 0 to disable candidate detection entirely."
        ),
        "section": "cold_memory_retention",
    },
    "cold_memory_purge_enabled": {
        "desc": (
            "Master gate for cold-memory hard deletes (default false). "
            "When false the pass only logs candidates and emits a metric — deletes nothing. "
            "Set true only after reviewing the yadgar_cold_purge_candidates gauge trend."
        ),
        "section": "cold_memory_retention",
    },
    "cold_memory_purge_dry_run": {
        "desc": (
            "Dry-run gate (default true). When true no memory is deleted even if "
            "COLD_MEMORY_PURGE_ENABLED=true. Both this AND COLD_MEMORY_PURGE_ENABLED must "
            "be set (enabled=true, dry_run=false) to trigger real deletes."
        ),
        "section": "cold_memory_retention",
    },
    # v5.48.0 — update mechanism
    "update_check_on_start": {
        "desc": (
            "Opt-in: probe PyPI for a newer yadgar version on daemon start (default false). "
            "Anonymous version-only check. No user-ID, no telemetry. "
            "Respects HTTPS_PROXY env for corporate firewalls."
        ),
        "section": "update",
    },
    "update_check_timeout_seconds": {
        "desc": "HTTP timeout in seconds for the PyPI version probe (default 5).",
        "section": "update",
    },
    "update_pypi_url": {
        "desc": (
            "PyPI JSON API endpoint used for version probes "
            "(default https://pypi.org/pypi/yadgar/json). "
            "Override for air-gapped environments with a local PyPI mirror."
        ),
        "section": "update",
    },
    "update_user_agent_template": {
        "desc": (
            "User-Agent header template for version probe requests. "
            "{version} is replaced with the running yadgar version (default 'yadgar/{version}')."
        ),
        "section": "update",
    },
    "update_debug_apis_enabled": {
        "desc": (
            "Enable /api/control/update endpoint (default 'off'). "
            "Set to 'on' for Control-tab integration (v5.50) or power-user CLI use. "
            "Also requires bearer token auth (YADGAR_REQUIRE_AUTH + YADGAR_MCP_AUTH_TOKEN)."
        ),
        "section": "update",
    },
    # v5.50.2 — umbrella control API gate
    "debug_apis_enabled": {
        "desc": (
            "Enable /api/control/{config,action,restart}/* endpoints (default false). "
            "Bearer token alone is insufficient — this gate must also be true. "
            "Set to true to enable the Control tab config editor, action triggers, and restart endpoints. "
            "YADGAR_UPDATE_DEBUG_APIS_ENABLED remains the narrower gate for /api/control/update."
        ),
        "section": "update",
    },
    # v5.49.0 — upgrade snapshot retention
    "update_snapshot_retention": {
        "desc": (
            "Number of upgrade snapshots to retain after each upgrade (default 3). "
            "Older snapshots are pruned by the orchestrator. "
            "Set to 0 to keep all snapshots (no pruning)."
        ),
        "section": "update",
    },
    # v5.49.0 Phase 9 — orchestrator knobs
    "update_install_enabled": {
        "desc": (
            "Enable the yadgar update --install routine-upgrade orchestrator (default false). "
            "Set to true after reading docs/plans/archive/PLAN_V5_49_0.md § Rollout. "
            "When false, run_install() refuses immediately with a clear message."
        ),
        "section": "update",
    },
    "update_lock_max_age_seconds": {
        "desc": (
            "Maximum age in seconds for an upgrade lock before it is treated as stale (default 3600). "
            "If the upgrader process was killed mid-run, the lock will be recycled after this period."
        ),
        "section": "update",
    },
    # v5.85 car #6 — Agent-prompt Tier-1 passive library (Phase 1) kill-gate
    "agent_prompt_library_enabled": {
        "desc": (
            "Enable the agent-prompt Tier-1 passive library (default true). "
            "When true, agent-prompt pages are retrievable via recall(type='wiki', "
            "tags=['agent-prompt']) and the save/dispatch surface is active. "
            "When false, the library is intended to be inert."
        ),
        "section": "agent_prompt_library",
    },
    # v5.62.0 — recall quality floor
    "recall_quality_floor": {
        "desc": (
            "Minimum cross-encoder score for a recall result to be returned (default 0.0 = disabled). "
            "Rows without a _cross_encoder_score always pass through regardless of this setting. "
            "Calibration (2026-06-15): co-occurrence junk CE 0.0–0.157, genuine results CE 0.289–0.843. "
            "Production tuning: raise to 0.15–0.20 after write-time backfill (plan §C) completes."
        ),
        "section": "recall_quality",
    },
    "recall_memory_quota": {
        "desc": (
            "v6 T6 Step 4: Max memory candidates in the fusion pool before CE rerank (default 5). "
            "Prevents memory candidates from starving wiki candidates."
        ),
        "section": "unified_recall",
    },
    "recall_wiki_quota": {
        "desc": (
            "v6 T6 Step 4: Max wiki candidates in the fusion pool before CE rerank (default 5). "
            "Prevents wiki candidates from being starved by memory candidates."
        ),
        "section": "unified_recall",
    },
    "recall_memory_prior_weight": {
        "desc": (
            "v6 T6 Step 4: Additive prior weight for memory native_score in CE fusion (default 0.1). "
            "CE score is primary; prior is a tie-shaper."
        ),
        "section": "unified_recall",
    },
    "recall_wiki_prior_weight": {
        "desc": (
            "v6 T6 Step 4: Additive prior weight for wiki native_score in CE fusion (default 0.1). "
            "CE score is primary; prior is a tie-shaper."
        ),
        "section": "unified_recall",
    },
    # ── v5.89 #67: FIELD_META backfill — drain the I25 Tier-2 grandfathered ───
    # backlog. Descriptions sourced from docs/configuration.md + config.py
    # inline comments. Each entry also gets a _REGISTRY ConfigEntry so the I25
    # three-way ratchet (test_config_three_way_sync) covers it.
    # core / server
    "host": {
        "desc": (
            "Bind address for the MCP HTTP server (default 127.0.0.1, loopback-only). "
            "Set to 0.0.0.0 only with auth + TLS — LAN exposure is not recommended otherwise."
        ),
        "section": "security",
    },
    "require_auth": {
        "desc": (
            "Enforce bearer-token auth on /api/* and /hooks/* routes (default true). "
            "When false the middleware is a no-op and logs a startup WARN. "
            "Flip to true after minting YADGAR_MCP_AUTH_TOKEN."
        ),
        "section": "security",
    },
    "allowed_origins": {
        "desc": (
            "Comma-separated CORS allowed origins for the MCP HTTP transport "
            "(default loopback only). Wildcard '*' is never allowed."
        ),
        "section": "security",
    },
    "max_hash_bytes": {
        "desc": (
            "Maximum file size in bytes for path-based memorize hashing (default 10485760 = 10 MiB). "
            "Files larger than this are skipped entirely."
        ),
        "section": "security",
    },
    "auto_capture_rate_limit": {
        "desc": (
            "Max /hooks/auto-capture requests per directory key per minute (default 30). "
            "Prevents log-flooding from a misbehaving hook."
        ),
        "section": "security",
    },
    "asgi_shutdown_timeout_sec": {
        "desc": (
            "Cap (seconds) on uvicorn's wait for in-flight requests to drain on SIGTERM "
            "(default 5). 0 = unlimited (uvicorn default); >=1 abandons after that many seconds."
        ),
        "section": "core",
    },
    "max_batch_statements": {
        "desc": (
            "Per-transaction cap on SQL statements in batch_writes (default 500). "
            "Bounds SurrealDB's recursive serialiser stack on large batches (e.g. full-table decay)."
        ),
        "section": "core",
    },
    "max_batch_bytes": {
        "desc": (
            "Per-transaction serialised-byte cap in batch_writes (default 1000000 = 1 MB). "
            "Prevents HTTP 413 from SurrealDB. Whichever limit fires first (statements or bytes) "
            "starts a new chunk."
        ),
        "section": "core",
    },
    "max_caused_by_rows": {
        "desc": (
            "Ceiling on caused_by relationship rows; oldest are pruned past this limit "
            "(default 100000). Set to 0 to disable the ceiling check."
        ),
        "section": "core",
    },
    "db_size_warning_bytes": {
        "desc": (
            "Warn when total surreal_db/ size exceeds this threshold (default 1073741824 = 1 GiB). "
            "The warning fires at most once per hour."
        ),
        "section": "core",
    },
    "check_invariants_query_timeout_seconds": {
        "desc": (
            "Per-table query timeout (seconds) for check_invariants (default 60). On timeout the "
            "table is skipped at WARN and the remaining tables still run."
        ),
        "section": "core",
    },
    "wiki_slug_prefix": {
        "desc": (
            "Optional prefix for wiki .md archive filenames "
            "(e.g. 'myproject' -> 'myproject-overview.md'). Empty = no prefix."
        ),
        "section": "core",
    },
    # daemon / consolidation similarity-linking
    "similarity_link_threshold": {
        "desc": "Minimum cosine similarity to create a memory_similarity_link (default 0.78).",
        "section": "daemon",
    },
    "max_similarity_links_per_memory": {
        "desc": (
            "Degree cap per memory on memory_similarity_link rows (default 15) — "
            "bounds the similarity-link table size."
        ),
        "section": "daemon",
    },
    "similarity_matrix_max_candidates": {
        "desc": (
            "Cap on memories (most-recently-accessed first) used to build the N×N similarity "
            "matrix in _link_similar_memories / _merge_duplicates (default 4000). Prevents OOM at scale."
        ),
        "section": "daemon",
    },
    "cls_pattern_max_candidates": {
        "desc": (
            "Cap on the most-recently-accessed episodic memories scanned by "
            "find_recurring_patterns (default 2000)."
        ),
        "section": "daemon",
    },
    # memory_lifecycle
    "predictive_coding_entity_ttl_seconds": {
        "desc": (
            "TTL (seconds) for the entity-set cache in WriteGate novelty computation (default 300). "
            "Avoids a get_all_entities() DB call on every write-gate evaluation; invalidated on "
            "entity add/delete. Set to 0 to disable caching."
        ),
        "section": "memory_lifecycle",
    },
    "reinject_on_write": {
        "desc": (
            "Write-time reinjection gate (default false). When off, the retriever.recall() block in "
            "memorize() is skipped, saving 30-50ms of sync vector search per write. Enable only if "
            "write-time related-context surfacing is needed."
        ),
        "section": "memory_lifecycle",
    },
    # thermodynamics / retrieval boosts
    "recall_boost": {
        "desc": (
            "Per-access heat boost added during each decay cycle: "
            "new_heat = min(decay(mem) + access_count_since_decay * RECALL_BOOST, 1.0) (default 0.05). "
            "Frequently-accessed memories decay slower (MemoryBank parity). 0.0 = pure exponential decay."
        ),
        "section": "thermodynamics",
    },
    "branch_boost_weight": {
        "desc": (
            "Convex-combination boost for current-branch memories: "
            "boosted = score + (1 - score) * BRANCH_BOOST_WEIGHT (default 0.2). Keeps scores in [0, 1]."
        ),
        "section": "retrieval_fusion",
    },
    "postmortem_boost_factor": {
        "desc": (
            "Convex boost applied to _postmortem/_incident-tagged memories when the query contains an "
            "action verb (default 0.3): boosted = score + (1 - score) * POSTMORTEM_BOOST_FACTOR. "
            "Set to 0.0 to disable."
        ),
        "section": "retrieval_fusion",
    },
    "postmortem_boost_keywords": {
        "desc": (
            "Comma-separated action verbs that trigger the postmortem/incident boost "
            "(default deploy,push,merge,restart,vacuum,rollback,upgrade,migrate,bump,release)."
        ),
        "section": "retrieval_fusion",
    },
    "retrieval_profile": {
        "desc": (
            "Default retrieval preset: fast, balanced, or full (default balanced). "
            "fast = memory-only BM25+HNSW+fusion: no CE/NLI/MP, no wiki fanout, "
            "no engram-link enrichment (ADR-0077 — the hook-latency-budget profile)."
        ),
        "section": "retrieval_fusion",
        "choices": ["fast", "balanced", "full"],
    },
    "fanout_boost_scope": {
        "desc": (
            "Controls when C4 branch and postmortem/incident boosts apply in fanout recall "
            "(default scoped). "
            "'scoped': apply only when profile is not None (profile-origin callers, e.g. hook=fast). "
            "'global': apply to all fanout recalls regardless of profile. "
            "'off': never apply boosts (A/B or CPU-constrained deploys)."
        ),
        "section": "retrieval_fusion",
        "choices": ["scoped", "global", "off"],
    },
    "heavy_rerank_enabled": {
        "desc": (
            "Heavy-rerank kill switch for CPU-only hosts (default true). When false, all CE/NLI/MP "
            "reranking is skipped and retrieval falls back to BM25+HNSW fusion only."
        ),
        "section": "reranking",
    },
    # neuromorphic
    "hopfield_beta": {
        "desc": "Hopfield associative-recall sharpness (default 8.0; low = blended, high = precise).",
        "section": "neuromorphic",
    },
    "hopfield_max_patterns": {
        "desc": "Maximum patterns retained in the Hopfield energy store (default 5000).",
        "section": "neuromorphic",
    },
    "sr_discount": {
        "desc": "Successor-representation discount factor γ (default 0.9).",
        "section": "neuromorphic",
    },
    "sr_update_rate": {
        "desc": "Incremental successor-representation update learning rate (default 0.1).",
        "section": "neuromorphic",
    },
    # embedding / profiles
    "implicit_embedding_model": {
        "desc": (
            "Model for the implicit/latent embedding channel (default empty = disabled). "
            "Config-only — retained pending a future DualCSE implementation."
        ),
        "section": "embedding_enhancement",
    },
    "profile_search_weight": {
        "desc": "Weight of structured-profile results in the retrieval blend (default 1.0).",
        "section": "profiles_beliefs",
    },
    # observability
    "metrics_enabled": {
        "desc": (
            "Expose the /metrics Prometheus endpoint (default true). When false, /metrics returns 404. "
            "The endpoint is always unauthenticated — bind Yadgar to loopback so only local scrapers reach it."
        ),
        "section": "observability",
    },
    # project_brief
    "brief_mode_default": {
        "desc": "Default mode for project_brief: catalog or full (default catalog).",
        "section": "project_brief",
        "choices": ["catalog", "full"],
    },
    "project_init_cap_chars": {
        "desc": (
            "Hard character cap for _project_init memory content (default 2000). "
            "bootstrap_project raises ValueError on overflow — no silent truncation."
        ),
        "section": "project_brief",
    },
    # backend timeouts
    "backend_http_timeout_sec": {
        "desc": (
            "Short HTTP timeout (seconds) for non-import backend calls — health, /sql, /admin/dbsize "
            "(default 5)."
        ),
        "section": "backend_timeouts",
    },
    "backend_import_timeout_sec": {
        "desc": "Long HTTP timeout (seconds) for the vacuum /import POST and /export GET (default 300).",
        "section": "backend_timeouts",
    },
    "migration_http_timeout_sec": {
        "desc": (
            "HTTP timeout (seconds) for schema-migration calls during StorageEngine.__init__ "
            "(default 30). Migrations can be slower than operational reads."
        ),
        "section": "backend_timeouts",
    },
    "rerank_backend_timeout_sec": {
        "desc": (
            "Dedicated HTTP timeout (seconds) for /rerank backend calls (default 90). CE inference can "
            "take 8-46s on CPU; the general backend timeout caused spurious circuit-breaker opens. "
            "0 falls back to BACKEND_HTTP_TIMEOUT_SEC."
        ),
        "section": "backend_timeouts",
    },
    # circuit breaker + rerank concurrency
    "circuit_breaker_enabled": {
        "desc": (
            "Backend ML circuit breaker (default true). Opens when /rerank repeatedly times out or "
            "errors, to stop saturating the backend."
        ),
        "section": "circuit_breaker",
    },
    "circuit_breaker_failure_threshold": {
        "desc": "Consecutive per-endpoint failures before the breaker trips OPEN (default 3).",
        "section": "circuit_breaker",
    },
    "circuit_breaker_open_duration_sec": {
        "desc": "Seconds the breaker stays OPEN before allowing a single probe attempt (default 60).",
        "section": "circuit_breaker",
    },
    "circuit_breaker_probe_timeout_sec": {
        "desc": "Short HTTP timeout (seconds) for HALF_OPEN probe calls — fast-fail when saturated (default 2.0).",
        "section": "circuit_breaker",
    },
    "circuit_breaker_max_open_duration_sec": {
        "desc": "Cooldown ceiling (seconds) for exponential backoff on repeated probe failures (default 600.0).",
        "section": "circuit_breaker",
    },
    "circuit_breaker_backoff_factor": {
        "desc": "Backoff multiplier — each failed probe multiplies the OPEN cooldown by this factor (default 2.0).",
        "section": "circuit_breaker",
    },
    "rerank_max_concurrency": {
        "desc": (
            "BACKEND cross-encoder cap: max concurrent /rerank inference threads the backend serves at "
            "once (default 8). Independent of the core tool pool — this lives in the backend container "
            "and requires a backend restart/env change to take effect. The #74 backend-saturation guard: "
            "raised from 1 in lockstep with tool_pool_workers (Fix A O7) so N-parallel core offload "
            "does not cause rerank 503-storms. Effective recall concurrency = "
            "min(TOOL_POOL_WORKERS, RECALL_HEAVY_CONCURRENCY, RERANK_MAX_CONCURRENCY) — bumping only "
            "one gate does nothing if the others are lower."
        ),
        "section": "circuit_breaker",
    },
    "rerank_semaphore_acquire_timeout_sec": {
        "desc": (
            "Seconds to wait for the /rerank concurrency semaphore before returning 503 (default 2.0). "
            "Should be <= circuit_breaker_probe_timeout_sec so probes always fail fast."
        ),
        "section": "circuit_breaker",
    },
    # ── Fix A (daemon-offload-A): tool-body offload off the asyncio loop ─────────
    "offload_tools": {
        "desc": (
            "Master kill-switch for running sync MCP tool bodies off the asyncio loop on a bounded "
            "worker pool (default false). Enable after live soak; OFF keeps the proven inline behaviour "
            "with the deployed P0 health-kill backstop. Requires remote engines (YADGAR_EMBED_URL)."
        ),
        "section": "circuit_breaker",
    },
    "tool_pool_workers": {
        "desc": (
            "Size of the offload ThreadPoolExecutor — max MCP tool bodies (recall/memorize/wiki/…) "
            "running OFF the --cpus-1 event loop at once (default 2). Offload threads compete with "
            "the event loop for the single core; fewer threads = less loop-starvation risk. v5.95: "
            "dropped 8→2 for this reason. Raise only if tool serialization is measurably a bottleneck. "
            "Must be strictly > recall_heavy_concurrency (else the rerank sub-gate is a no-op). "
            "Effective recall concurrency = min(TOOL_POOL_WORKERS, RECALL_HEAVY_CONCURRENCY, "
            "RERANK_MAX_CONCURRENCY) — bumping this alone does nothing if recall_heavy_concurrency "
            "or rerank_max_concurrency are lower. Restart to apply."
        ),
        "section": "circuit_breaker",
    },
    "recall_heavy_concurrency": {
        "desc": (
            "Sub-gate INSIDE the tool pool: max concurrent HEAVY recalls (the rerank fan-out) "
            "the core issues at once (default 1). Clamped at runtime to ≤ TOOL_POOL_WORKERS. "
            "Protects the backend from too many simultaneous rerank waves (#74 fix). v5.95: "
            "dropped 3→1 in lockstep with tool_pool_workers dropping 8→2. Must be strictly < "
            "tool_pool_workers or this gate is a no-op (all pool workers can do heavy recalls "
            "simultaneously). Sized to the BACKEND's real serving capacity. MUST be ≤ "
            "rerank_max_concurrency. Effective recall concurrency = min(TOOL_POOL_WORKERS, "
            "RECALL_HEAVY_CONCURRENCY, RERANK_MAX_CONCURRENCY)."
        ),
        "section": "circuit_breaker",
    },
    "rerank_gate_acquire_timeout_sec": {
        "desc": (
            "Seconds a worker waits for a heavy-rerank slot before degrading (skip rerank → "
            "pre-rerank order) (default 2.0). Bounded so a gated worker never holds its pool slot "
            "past tool_timeout_sec (which would leak it)."
        ),
        "section": "circuit_breaker",
    },
    "tool_timeout_sec": {
        "desc": (
            "Per-tool offload timeout in seconds (default 95.0) — frees the loop on a wedged op. "
            "MUST cover a worst-case recall incl. rerank (>= rerank_backend_timeout_sec=90) so a "
            "legit rerank is not cancelled mid-flight (leaking the worker). Ordering invariant: "
            "tool_saturation_grace_sec > tool_timeout_sec >= rerank_backend_timeout_sec."
        ),
        "section": "circuit_breaker",
    },
    "tool_saturation_grace_sec": {
        "desc": (
            "O2: idle seconds (no pool completion) while the pool is full before /health degrades to 503 "
            "(default 120.0). MUST be > tool_timeout_sec so only leaked workers trip the signal."
        ),
        "section": "circuit_breaker",
    },
    # v5.95 config-integrity Phase 4 — hot-path literals promoted to knobs.
    "reranker_idle_unload_sec": {
        "desc": (
            "Idle seconds of no recall activity before rerank models are unloaded to free "
            "~500 MB (default 600.0). Read by the lifecycle reranker-idle background thread."
        ),
        "section": "circuit_breaker",
    },
    "reranker_idle_check_interval_sec": {
        "desc": (
            "Seconds between reranker idle-unload checks — the background thread's sleep interval "
            "(default 60). Lower = more responsive unload, more wakeups."
        ),
        "section": "circuit_breaker",
    },
    "health_handler_timeout_sec": {
        "desc": (
            "Outer hard bound (seconds) on the whole /health handler body so it can never exceed "
            "this even if a dependency probe hangs (default 3.0). Container healthcheck uses "
            "--health-timeout 5s; keep this below that."
        ),
        "section": "circuit_breaker",
    },
    "health_probe_timeout_sec": {
        "desc": (
            "Per-dependency (db/embed) probe HTTP client timeout inside /health, seconds "
            "(default 2.0). Probes run concurrently; keep < health_handler_timeout_sec."
        ),
        "section": "circuit_breaker",
    },
    "vacuum_auto_cooldown_hours": {
        "desc": (
            "Auto-vacuum cooldown: hours since the last auto-fire before another auto-vacuum "
            "may trigger (default 6.0). In-memory, resets on restart."
        ),
        "section": "daemon",
    },
    "health_readiness_fail_threshold": {
        "desc": (
            "#74 fix: consecutive /health READINESS probe misses (db+embed) before degrading to 503 "
            "(default 3). Anti-flap — a single transient miss (busy backend) does not 503. LIVENESS "
            "(/health/live) is separate and never probes the backend, so a busy dependency can't "
            "SIGKILL the core via the P0 healthcheck."
        ),
        "section": "circuit_breaker",
    },
    # write queue / DLQ
    "queue_drain_interval": {
        "desc": "Drain interval (seconds) for the async write queue — how long entries stay visible before flush (default 30).",
        "section": "write_queue",
    },
    "queue_max_permanent_attempts": {
        "desc": "4xx (permanent) failures send a queue entry to the DLQ after this many tries (default 3).",
        "section": "write_queue",
    },
    "queue_max_transient_attempts": {
        "desc": "5xx / network (transient) failures send a queue entry to the DLQ after this many tries (default 20).",
        "section": "write_queue",
    },
    "queue_backoff_base_s": {
        "desc": "Initial retry delay (seconds) for queue retries (default 30).",
        "section": "write_queue",
    },
    "queue_backoff_max_s": {
        "desc": "Maximum retry delay cap (seconds) for queue retries (default 3600).",
        "section": "write_queue",
    },
    "queue_dlq_retention_days": {
        "desc": "Prune DLQ entries older than this many days (default 90).",
        "section": "write_queue",
    },
    # table / memory retention windows
    "action_log_retention_days": {
        "desc": "Prune processed action_log rows older than this each consolidation cycle (default 7). 0 = disable.",
        "section": "table_retention",
    },
    "episode_retention_days": {
        "desc": "Prune episode rows older than this each consolidation cycle (default 14). 0 = disable.",
        "section": "table_retention",
    },
    "astrocyte_process_retention_days": {
        "desc": "Prune astrocyte_process rows older than this each consolidation cycle (default 7). 0 = disable.",
        "section": "table_retention",
    },
    "memory_cluster_retention_days": {
        "desc": "Prune memory_cluster rows older than this each consolidation cycle (default 30). 0 = disable.",
        "section": "table_retention",
    },
    "derived_belief_retention_days": {
        "desc": "Prune derived_belief rows older than this each consolidation cycle (default 30). 0 = disable.",
        "section": "table_retention",
    },
    "prospective_memory_retention_days": {
        "desc": "Prune prospective_memory rows older than this each consolidation cycle (default 30). 0 = disable.",
        "section": "table_retention",
    },
    "narrative_entry_retention_days": {
        "desc": "Prune narrative_entry rows older than this each consolidation cycle (default 90). 0 = disable.",
        "section": "table_retention",
    },
    "action_stream_max_age_days": {
        "desc": (
            "Age cap (days) for _action_stream-tagged memories deleted by _memify_prune Pass 5 "
            "(default 14). These start at heat=0.4, too warm for Pass 1. 0 = disable the age cap."
        ),
        "section": "table_retention",
    },
    "auto_generated_memory_max_age_days": {
        "desc": "Age cap (days) for cold unaccessed 'auto-generated'-tagged memories deleted by _memify_prune (default 30). 0 = disable.",
        "section": "table_retention",
    },
    "auto_abstracted_memory_max_age_days": {
        "desc": "Age cap (days) for cold unaccessed 'auto-abstracted'-tagged memories deleted by _memify_prune (default 30). 0 = disable.",
        "section": "table_retention",
    },
    "dream_insight_max_age_days": {
        "desc": "Age cap (days) for unaccessed dream memories deleted by _memify_prune regardless of heat (default 21). 0 = disable.",
        "section": "table_retention",
    },
    # vacuum
    "vacuum_old_max_age_days": {
        "desc": "Age backstop for surreal_db.old-* rollback dirs (ADR-0076 D1): reap any .old dir older than this many days on each vacuum finalize. Default 7. The current-run .old is always exempted.",
        "section": "vacuum",
    },
    "vacuum_snapshot_retention": {
        "desc": "Number of pre-vacuum DB snapshots to retain; older ones are pruned after a successful vacuum (default 3).",
        "section": "vacuum",
    },
    "vacuum_auto_enabled": {
        "desc": "Enable the threshold-driven emergency-backstop vacuum trigger (default true). Set false to disable the backstop entirely.",
        "section": "vacuum",
    },
    "vacuum_auto_threshold_bytes": {
        "desc": "Emergency-backstop threshold: fire the auto-vacuum when the DB exceeds this size (default 2147483648 = 2 GiB).",
        "section": "vacuum",
    },
    "vacuum_auto_window_start": {
        "desc": "Local-time window start (HH:MM, 24-hour) for the backstop auto-vacuum trigger (default 19:00).",
        "section": "vacuum",
    },
    "vacuum_auto_window_end": {
        "desc": "Local-time window end (HH:MM, 24-hour, exclusive) for the backstop auto-vacuum trigger (default 23:00).",
        "section": "vacuum",
    },
}


SECTION_TITLES: dict[str, str] = {
    "core": "Core",
    "daemon": "Daemon / Background Processing",
    "memory_lifecycle": "Memory Lifecycle",
    "thermodynamics": "Memory Thermodynamics (Decay)",
    "retrieval_fusion": "Retrieval & Fusion (WRRF)",
    "reranking": "Reranking",
    "query_routing": "Query Routing",
    "temporal_retrieval": "Temporal Retrieval",
    "embedding_enhancement": "Embedding Enhancement",
    "graph_knowledge": "Graph & Knowledge",
    "neuromorphic": "Neuromorphic (Hopfield / HDC / SR)",
    "enrichment": "Index-Time Enrichment",
    "profiles_beliefs": "Profiles & Beliefs",
    "adversarial": "Adversarial Protection",
    "observability": "Observability (OTLP / Tracing)",
    "backend_cache": "Backend Cache",
    "backend_hot_path_cache": "Backend Hot-Path Cache (CE + Embed LRU)",
    "logging": "Logging",
    "misc": "Miscellaneous",
    "project_brief": "Project Brief",
    "anchor_hygiene": "Anchor Hygiene (TTL)",
    "viz_config": "Visualization Knobs (v5.11.0)",
    "cpu_burst_detection": "CPU Burst Detection (v5.15.0)",
    "memory_blocks": "Memory Blocks (v5.35.1)",
    "wiki_similarity_gate": "Wiki Similarity Gate (v5.39.0)",
    "wiki_write_wait": "Wiki Write Wait / Read-Your-Writes (v5.41.2)",
    "update": "Update Mechanism (v5.48.0)",
    "memory_archive_retention": "Memory Archive Retention (v5.49.0)",
    "cold_memory_retention": "Cold-Memory Retention DRY-RUN Visibility (#29)",
    "backend_model_preload": "Backend Model Preload Warm-Up (v5.5.0)",
    "hooks": "Hook Recall Latency Budget (v5.51.0)",
    "stats_cache": "Stats Cache (v5.51.0)",
    "recall_quality": "Recall Quality Floor (v5.62.0)",
    "unified_recall": "Unified Scoped Recall (v6 T6)",
    "agent_prompt_library": "Agent-Prompt Passive Library (v5.85)",
    "security": "Security (Bind / Auth / CORS)",
    "backend_timeouts": "Backend HTTP Timeouts",
    "circuit_breaker": "Backend ML Circuit Breaker",
    "write_queue": "Async Write Queue / DLQ",
    "table_retention": "Table & Memory Retention Windows",
    "vacuum": "Vacuum",
}

# Ordered list of sections for deterministic output
_SECTION_ORDER = list(SECTION_TITLES.keys())


def get_config_path() -> Path:
    """Return config file path.

    Resolution order:
      1. ``YADGAR_CONFIG_FILE`` env var (container bind-mount override).
      2. Default ``~/.config/yadgar/config.yaml`` (XDG_CONFIG_HOME).

    The env override lets the container image pass ``-e YADGAR_CONFIG_FILE=/data/config.yaml``
    so the yaml file is read from the bind-mounted ``/data`` volume rather than
    inside the container's home directory (which may not exist with ``--user root``).
    """
    return _paths.CONFIG_YAML_PATH


@observe(tier="stage")
def load_yaml(path: Path) -> dict:
    """Load YAML file with ruamel.yaml, return dict (empty if file missing)."""
    if not path.exists():
        return {}
    y = YAML()
    with open(path) as f:
        data = y.load(f)
    return data if isinstance(data, dict) else {}


@observe(tier="stage")
def save_yaml(path: Path, data) -> None:
    """Save ruamel.yaml CommentedMap back to file (preserves comments)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    y = YAML()
    y.default_flow_style = False
    y.width = 4096  # prevent line wrapping
    with open(path, "w") as f:
        y.dump(data, f)


@observe(tier="hot")
def get_field_section(field_name: str) -> str:
    """Return section name for a field, or 'misc' if not found."""
    meta = FIELD_META.get(field_name.lower())
    return meta["section"] if meta else "misc"


@observe(tier="hot")
def coerce_value(field_name: str, raw: str) -> Any:
    """Coerce a string CLI value to the right Python type based on Settings field annotation.

    Handles bool (true/false/yes/no/1/0), int, float, str, lists (comma-separated).
    """
    from yadgar._shared.config import Settings

    fields = Settings.model_fields
    key_upper = field_name.upper()
    field_info = fields.get(key_upper)
    if field_info is None:
        # Return as-is if unknown
        return raw

    annotation = field_info.annotation

    # Unwrap Optional[X] → X
    import typing

    origin = getattr(annotation, "__origin__", None)
    if origin is typing.Union:
        args = [a for a in annotation.__args__ if a is not type(None)]
        annotation = args[0] if args else str

    if annotation is bool:
        if raw.lower() in ("true", "yes", "1"):
            return True
        if raw.lower() in ("false", "no", "0"):
            return False
        raise ValueError(f"Invalid bool value: {raw!r}. Use true/false/yes/no/1/0.")

    if annotation is int:
        return int(raw)

    if annotation is float:
        return float(raw)

    if annotation is list or (origin is list):
        return [item.strip() for item in raw.split(",")]

    return raw


@observe(tier="boundary")
def cmd_config_init(args) -> None:
    """Write a fully-commented default config.yaml.

    Uses ruamel.yaml to build a CommentedMap with section comments and per-field
    comments. Does NOT overwrite if file already exists (unless --force flag).
    """
    from yadgar._shared.config import Settings

    path = get_config_path()
    force = getattr(args, "force", False)

    if path.exists() and not force:
        print(f"Config file already exists: {path}", file=sys.stderr)
        print("Use --force to overwrite.", file=sys.stderr)
        sys.exit(1)

    path.parent.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    # Build a mapping: section → [(field_name_lower, value)]
    section_fields: dict[str, list[tuple[str, Any]]] = {s: [] for s in _SECTION_ORDER}
    section_fields["misc"] = section_fields.get("misc", [])

    for field_upper, _field_info in Settings.model_fields.items():
        field_lower = field_upper.lower()
        value = getattr(settings, field_upper)
        section = get_field_section(field_lower)
        if section not in section_fields:
            section_fields[section] = []
        section_fields[section].append((field_lower, value))

    # Build top-level CommentedMap grouped by section
    cm = CommentedMap()

    y = YAML()
    y.default_flow_style = False
    y.width = 4096

    separator = "─" * 60

    # ruamel.yaml prepends "# " to each line of before-comments automatically.
    # So we must NOT include leading "# " in our strings.

    first_section = True
    for section in _SECTION_ORDER:
        fields_in_section = section_fields.get(section, [])
        if not fields_in_section:
            continue

        title = SECTION_TITLES.get(section, section)

        for i, (field_lower, value) in enumerate(fields_in_section):
            meta = FIELD_META.get(field_lower, {})
            desc = meta.get("desc", "")

            cm[field_lower] = value

            # Before the first field of each section, add the section header
            if i == 0:
                if first_section:
                    # Also prepend the file header before the very first field
                    cm.yaml_set_comment_before_after_key(
                        field_lower,
                        before=(
                            f"Yadgar configuration file\n"
                            f" Location: ~/.config/yadgar/config.yaml\n"
                            f" Priority: environment variables (YADGAR_*) > this file > defaults\n"
                            f" Edit values below or use: yadgar config set <key> <value>\n"
                            f"\n"
                            f" {separator}\n"
                            f" {title}\n"
                            f" {separator}\n"
                            f" {desc}"
                        ),
                    )
                    first_section = False
                else:
                    cm.yaml_set_comment_before_after_key(
                        field_lower,
                        before=(f"\n {separator}\n {title}\n {separator}\n {desc}"),
                    )
            else:
                cm.yaml_set_comment_before_after_key(
                    field_lower,
                    before=f" {desc}",
                )

    save_yaml(path, cm)
    print(f"Config written to: {path}")


@observe(tier="boundary")
def cmd_config_list(args) -> None:
    """Print all settings in table format: KEY  VALUE  SOURCE

    Source is one of: default, yaml, env.
    If --section is given, filter to that section.
    """
    from yadgar._shared.config import Settings

    section_filter = getattr(args, "section", None)
    path = get_config_path()
    yaml_data = load_yaml(path)
    settings = Settings()

    rows = []
    for field_upper in Settings.model_fields:
        field_lower = field_upper.lower()
        section = get_field_section(field_lower)

        if section_filter and section != section_filter:
            continue

        value = getattr(settings, field_upper)

        env_key = f"YADGAR_{field_upper}"
        if env_key in os.environ:
            source = "env"
        elif field_lower in yaml_data and yaml_data[field_lower] is not None:
            source = "yaml"
        else:
            source = "default"

        rows.append((field_lower, str(value), source))

    if not rows:
        print(f"No settings found for section: {section_filter!r}", file=sys.stderr)
        return

    # Column widths
    max_key = max(len(r[0]) for r in rows)
    max_val = min(max(len(r[1]) for r in rows), 60)

    header = f"{'KEY':<{max_key}}  {'VALUE':<{max_val}}  SOURCE"
    print(header)
    print("-" * len(header))
    for key, val, source in rows:
        display_val = val if len(val) <= 60 else val[:57] + "..."
        print(f"{key:<{max_key}}  {display_val:<{max_val}}  {source}")


@observe(tier="boundary")
def cmd_config_get(args) -> None:
    """Print the current value and source of a single key."""
    from yadgar._shared.config import Settings

    key = args.key.lower()
    key_upper = key.upper()

    if key_upper not in Settings.model_fields:
        print(f"Unknown setting: {key!r}", file=sys.stderr)
        print("Run 'yadgar config list' to see all settings.", file=sys.stderr)
        sys.exit(1)

    path = get_config_path()
    yaml_data = load_yaml(path)
    settings = Settings()

    value = getattr(settings, key_upper)
    env_key = f"YADGAR_{key_upper}"

    if env_key in os.environ:
        source = "env"
    elif key in yaml_data and yaml_data[key] is not None:
        source = "yaml"
    else:
        source = "default"

    meta = FIELD_META.get(key, {})
    desc = meta.get("desc", "")
    section = meta.get("section", "misc")

    print(f"Key:     {key}")
    print(f"Value:   {value}")
    print(f"Source:  {source}")
    print(f"Section: {section}")
    if desc:
        print(f"Desc:    {desc}")


@observe(tier="stage")
def set_config_value(key: str, raw: object) -> object:
    """Sanctioned single writer for one config knob — shared by CLI + Control API.

    Coerces ``raw`` to the right Python type via ``coerce_value`` (which reads
    ``Settings.model_fields[KEY].annotation`` — the authoritative type source,
    handling Optional[...] / int / float / bool / list uniformly), then
    load/mutate/dumps ~/.config/yadgar/config.yaml with ruamel (comment-preserving)
    and chmod 0o600.

    ``raw`` may be a string (CLI args) or an already-typed JSON value
    (int/float/bool from the API) — non-string inputs are stringified first so
    BOTH callers run the identical annotation-driven coercion path. This is the
    only sanctioned yaml write path; never hand-write yaml bypassing this fn.

    Returns the coerced value. Raises ValueError/TypeError on coercion failure
    (CLI maps to exit(1); API maps to HTTP 422). Raises KeyError when ``key`` is
    not a known Settings field.
    """
    from yadgar._shared.config import Settings

    key = key.lower()
    if key.upper() not in Settings.model_fields:
        raise KeyError(key)

    raw_str = raw if isinstance(raw, str) else str(raw)
    value = coerce_value(key, raw_str)

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing file to preserve comments
    y = YAML()
    y.default_flow_style = False
    y.width = 4096

    if path.exists():
        with open(path) as f:
            data = y.load(f)
        if not isinstance(data, CommentedMap):
            data = CommentedMap(data or {})
    else:
        data = CommentedMap()

    data[key] = value

    with open(path, "w") as f:
        y.dump(data, f)
    # S2 (H-9): restrict config.yaml to owner read/write only — it may contain credentials.
    os.chmod(path, 0o600)
    return value


@observe(tier="boundary")
def cmd_config_set(args) -> None:
    """Update a key in ~/.config/yadgar/config.yaml (CLI front-end).

    Delegates the coercion + comment-preserving yaml write to the shared
    :func:`set_config_value` so the CLI and the Control-tab API run one
    validation path.
    """
    key = args.key.lower()

    try:
        value = set_config_value(key, args.value)
    except KeyError:
        print(f"Unknown setting: {key!r}", file=sys.stderr)
        print("Run 'yadgar config list' to see all settings.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, TypeError) as e:
        print(f"Invalid value for {key!r}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Set {key} = {value!r}")
    print(f"Config: {get_config_path()}")


@observe(tier="boundary")
def cmd_config_edit(args) -> None:
    """Open config.yaml in $EDITOR (fallback: nano, then vi)."""
    path = get_config_path()

    if not path.exists():
        # Create with init first
        class _FakeArgs:
            force = False

        cmd_config_init(_FakeArgs())

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        # Try fallbacks
        for candidate in ("nano", "vi"):
            result = subprocess.run(["which", candidate], capture_output=True)
            if result.returncode == 0:
                editor = candidate
                break

    if not editor:
        print("No editor found. Set $EDITOR environment variable.", file=sys.stderr)
        sys.exit(1)

    os.execvp(editor, [editor, str(path)])
