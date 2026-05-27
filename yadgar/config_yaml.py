"""YAML config file management for Yadgar.

Config file location: ~/.yadgar/config.yaml
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
    "logging": "Logging",
    "misc": "Miscellaneous",
    "project_brief": "Project Brief",
}

# Ordered list of sections for deterministic output
_SECTION_ORDER = list(SECTION_TITLES.keys())


def get_config_path() -> Path:
    """Return config file path.

    Resolution order:
      1. ``YADGAR_CONFIG_FILE`` env var (container bind-mount override).
      2. Default ``~/.yadgar/config.yaml``.

    The env override lets the container image pass ``-e YADGAR_CONFIG_FILE=/data/config.yaml``
    so the yaml file is read from the bind-mounted ``/data`` volume rather than
    ``/root/.yadgar/`` (which doesn't exist inside ``--user root`` containers).
    """
    override = os.environ.get("YADGAR_CONFIG_FILE", "").strip()
    if override:
        return Path(override)
    return Path("~/.yadgar/config.yaml").expanduser()


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
                            f" Location: ~/.yadgar/config.yaml\n"
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
    """Update a key in ~/.yadgar/config.yaml.

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
