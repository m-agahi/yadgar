"""RetrievalState dataclass — carries all pipeline state between stages."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RetrievalState:
    """Immutable-by-convention carrier for all data flowing through the retrieval pipeline.

    Stages receive a state, mutate fields they own, and return it (or a copy).
    No field should be mutated by two stages simultaneously.

    Attributes:
        query: The raw query string from the caller.
        max_results: Maximum results to return after the final trim.
        min_heat: Minimum heat threshold for memory candidates.
        profile: Profile name ("fast", "balanced", "full", "debug").
        stage_overrides: Per-call enable/disable map, e.g. {"nli": False}.

        scores: Per-memory, per-signal score map. Keys are memory IDs (int).
            Inner dict has keys: "vector", "fts", "ppr", "spread", "temporal".
        vector_memory_ids: Memory IDs returned from vector search (in order).
        query_embedding: Raw embedding bytes for the query; set by KNN stage.
        query_analysis: Output of analyze_query(); enriches downstream stages.
        w_temporal: Temporal signal weight (0.0 if temporal stage not active).
        fused: Sorted list of (memory_id, fused_score) after fusion.
        fused_scores: Dict version of fused for O(1) lookup.
        result_memories: Working list of memory dicts; manipulated by post-fusion stages.
        seen_ids: Set of memory IDs already in result_memories (dedup guard).
        use_cross_encoder: Whether the CE reranker should run (set by fusion stage).
        open_domain_mode: Whether the query is classified as open-domain.

        stage_stats: Per-stage timing and count stats populated by pipeline runner.
    """

    query: str
    max_results: int = 5
    min_heat: float = 0.0
    profile: str = "balanced"
    stage_overrides: dict = field(default_factory=dict)

    # Signal score accumulator: memory_id → {signal: score}
    scores: dict = field(default_factory=dict)
    # Vector search output — used as seeds for spreading activation
    vector_memory_ids: list[int] = field(default_factory=list)
    # Embedding bytes for the canonical query
    query_embedding: object = None
    # Structured query analysis from analyze_query()
    query_analysis: dict = field(default_factory=dict)
    # Temporal signal weight; 0.0 if temporal stage did not run
    w_temporal: float = 0.0

    # Post-fusion state
    fused: list = field(default_factory=list)
    fused_scores: dict = field(default_factory=dict)
    result_memories: list[dict] = field(default_factory=list)
    seen_ids: set = field(default_factory=set)
    use_cross_encoder: bool = False
    open_domain_mode: bool = False

    # Per-stage telemetry: stage_name → {duration_ms, count_in, count_out}
    stage_stats: dict = field(default_factory=dict)
