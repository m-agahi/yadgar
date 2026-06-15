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

import yadgar.paths as _paths

FIELD_META: dict[str, dict[str, str]] = {
    # core
    "db_path": {"desc": "SurrealDB storage path", "section": "core"},
    "port": {"desc": "HTTP server port (daemon mode, default 8742)", "section": "core"},
    "embedding_model": {
        "desc": "Sentence-transformer model (e.g. all-MiniLM-L6-v2, all-mpnet-base-v2)",
        "section": "core",
    },
    "max_episode_tokens": {"desc": "Maximum tokens per episode chunk", "section": "core"},
    "overlap_tokens": {"desc": "Token overlap between episode chunks", "section": "core"},
    # daemon
    "daemon_check_interval": {
        "desc": "Seconds between astrocyte background loop wakeups (lower = more responsive, higher = less CPU)",
        "section": "daemon",
    },
    "idle_threshold_seconds": {
        "desc": "Idle seconds before triggering consolidation (e.g. 3600 to only consolidate after 1h of no Claude sessions)",
        "section": "daemon",
    },
    "num_astrocyte_processes": {
        "desc": "Number of domain-aware background worker processes",
        "section": "daemon",
    },
    "narrative_interval_hours": {
        "desc": "Hours between autobiographical narrative updates",
        "section": "daemon",
    },
    # memory_lifecycle
    "write_gate_threshold": {
        "desc": "Minimum score to store a memory (0.0 = store everything)",
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
    "compression_gist_age_hours": {
        "desc": "Hours before gist-compressing old memories (default 168 = 7 days)",
        "section": "memory_lifecycle",
    },
    "compression_tag_age_hours": {
        "desc": "Hours before tag-compressing very old memories (default 720 = 30 days)",
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
    "wrrf_k": {
        "desc": "RRF constant k (higher = smoother rank blending)",
        "section": "retrieval_fusion",
    },
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
    # confidence_gating
    "confidence_gating_enabled": {
        "desc": "Reject low-confidence result sets and trigger fallback",
        "section": "confidence_gating",
    },
    "confidence_min_results": {
        "desc": "Minimum results required before gating is applied",
        "section": "confidence_gating",
    },
    "confidence_score_spread_threshold": {
        "desc": "Minimum spread between top scores to be confident",
        "section": "confidence_gating",
    },
    "confidence_top_score_threshold": {
        "desc": "Minimum top score to pass confidence gate",
        "section": "confidence_gating",
    },
    "confidence_fallback_strategy": {
        "desc": "Strategy when gate fails: expand or relax",
        "section": "confidence_gating",
    },
    # temporal_retrieval
    "temporal_retrieval_enabled": {
        "desc": "Boost memories that match temporal expressions in query",
        "section": "temporal_retrieval",
    },
    "temporal_boost_weight": {
        "desc": "Weight of temporal boost signal",
        "section": "temporal_retrieval",
    },
    "temporal_decay_days": {
        "desc": "Days over which temporal relevance decays",
        "section": "temporal_retrieval",
    },
    "temporal_exact_match_boost": {
        "desc": "Extra boost multiplier for exact date matches",
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
    "embedding_cache_size": {
        "desc": "LRU cache size for embedding results",
        "section": "embedding_enhancement",
    },
    "query_prefix": {
        "desc": "Optional prefix prepended to all queries before embedding",
        "section": "embedding_enhancement",
    },
    "dual_vectors_enabled": {
        "desc": "Enable dual-vector architecture (explicit + implicit)",
        "section": "embedding_enhancement",
    },
    "implicit_embedding_model": {
        "desc": "Model for implicit/latent embedding channel",
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
    "reconsolidation_low_threshold": {
        "desc": "Below this heat: no modification on recall",
        "section": "neuromorphic",
    },
    "reconsolidation_high_threshold": {
        "desc": "Above this heat: archive old version and create updated memory",
        "section": "neuromorphic",
    },
    "plasticity_spike": {
        "desc": "How much plasticity increases on each memory access",
        "section": "neuromorphic",
    },
    "plasticity_half_life_hours": {
        "desc": "Plasticity decay half-life in hours",
        "section": "neuromorphic",
    },
    "stability_increment": {
        "desc": "Stability increase per successful retrieval",
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
    "belief_min_confidence": {
        "desc": "Minimum confidence to store a derived belief",
        "section": "profiles_beliefs",
    },
    "belief_high_confidence_boost": {
        "desc": "Score multiplier for high-confidence beliefs",
        "section": "profiles_beliefs",
    },
    "belief_search_priority_for_open_domain": {
        "desc": "Prioritize beliefs for open-domain queries",
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
    },
    "backend_log_level": {
        "desc": "Log level for the backend container (embed service + SurrealDB)",
        "section": "logging",
    },
    # misc
    "contextual_prefix_enabled": {
        "desc": "Prepend contextual prefix to improve embedding quality",
        "section": "misc",
    },
    "curation_similarity_threshold": {
        "desc": "Minimum similarity to trigger memory curation/merging",
        "section": "misc",
    },
    "crdt_agent_id": {"desc": "Agent identifier for multi-agent CRDT sync", "section": "misc"},
    "replay_max_restore_memories": {
        "desc": "Maximum memories included in context restoration",
        "section": "misc",
    },
    "replay_anchor_heat": {
        "desc": "Heat assigned to anchored (protected) memories",
        "section": "misc",
    },
    "replay_checkpoint_auto_interval": {
        "desc": "Auto-checkpoint every N tool calls",
        "section": "misc",
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
        "desc": "Token-budget upper bound for signals mode payload (default 350; raise if new action types added)",
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
}


SECTION_TITLES: dict[str, str] = {
    "core": "Core",
    "daemon": "Daemon / Background Processing",
    "memory_lifecycle": "Memory Lifecycle",
    "thermodynamics": "Memory Thermodynamics (Decay)",
    "retrieval_fusion": "Retrieval & Fusion (WRRF)",
    "reranking": "Reranking",
    "query_routing": "Query Routing",
    "confidence_gating": "Confidence Gating",
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
    "backend_model_preload": "Backend Model Preload Warm-Up (v5.5.0)",
    "hooks": "Hook Recall Latency Budget (v5.51.0)",
    "stats_cache": "Stats Cache (v5.51.0)",
    "recall_quality": "Recall Quality Floor (v5.62.0)",
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


def load_yaml(path: Path) -> dict:
    """Load YAML file with ruamel.yaml, return dict (empty if file missing)."""
    if not path.exists():
        return {}
    y = YAML()
    with open(path) as f:
        data = y.load(f)
    return data if isinstance(data, dict) else {}


def save_yaml(path: Path, data) -> None:
    """Save ruamel.yaml CommentedMap back to file (preserves comments)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    y = YAML()
    y.default_flow_style = False
    y.width = 4096  # prevent line wrapping
    with open(path, "w") as f:
        y.dump(data, f)


def get_field_section(field_name: str) -> str:
    """Return section name for a field, or 'misc' if not found."""
    meta = FIELD_META.get(field_name.lower())
    return meta["section"] if meta else "misc"


def coerce_value(field_name: str, raw: str) -> Any:
    """Coerce a string CLI value to the right Python type based on Settings field annotation.

    Handles bool (true/false/yes/no/1/0), int, float, str, lists (comma-separated).
    """
    from yadgar.config import Settings

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


def cmd_config_init(args) -> None:
    """Write a fully-commented default config.yaml.

    Uses ruamel.yaml to build a CommentedMap with section comments and per-field
    comments. Does NOT overwrite if file already exists (unless --force flag).
    """
    from yadgar.config import Settings

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


def cmd_config_list(args) -> None:
    """Print all settings in table format: KEY  VALUE  SOURCE

    Source is one of: default, yaml, env.
    If --section is given, filter to that section.
    """
    from yadgar.config import Settings

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


def cmd_config_get(args) -> None:
    """Print the current value and source of a single key."""
    from yadgar.config import Settings

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


def cmd_config_set(args) -> None:
    """Update a key in ~/.config/yadgar/config.yaml.

    Loads the existing file with ruamel.yaml (preserving comments),
    sets the value with proper type coercion, saves back.
    """
    from yadgar.config import Settings

    key = args.key.lower()
    key_upper = key.upper()
    raw_value = args.value

    if key_upper not in Settings.model_fields:
        print(f"Unknown setting: {key!r}", file=sys.stderr)
        print("Run 'yadgar config list' to see all settings.", file=sys.stderr)
        sys.exit(1)

    try:
        value = coerce_value(key, raw_value)
    except (ValueError, TypeError) as e:
        print(f"Invalid value for {key!r}: {e}", file=sys.stderr)
        sys.exit(1)

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

    print(f"Set {key} = {value!r}")
    print(f"Config: {path}")


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
