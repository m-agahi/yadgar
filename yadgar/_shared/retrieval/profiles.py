"""Built-in retrieval profiles for the plugin pipeline (v5.31.0).

Profiles configure which stages run and in what order.  The "balanced" profile
reproduces the pre-v5.31.0 monolithic recall() behavior exactly.

Profile dicts carry two representations for backward compatibility:
- ``stages``: ordered list of stage names for the pipeline runner.
- Legacy keys (``signals``, ``cross_encoder``, ``nli``, ``multi_passage``):
  still consumed by ``_RerankingMixin._apply_rerank_pipeline`` and
  ``_FusionMixin._build_initial_results`` until those are fully ported.

ADR-0077 extension (fast profile is memory-only):
- ``wiki`` (bool, default True): when False, ``_fanout_recall`` skips the
  WikiProvider arm entirely on the default ``type_filter="all"`` path
  (an explicit ``type="wiki"`` still honors caller intent). Measured: the
  wiki arm cost ~450ms per hook recall.
- ``engram_links`` (bool, default True): when False,
  ``_apply_rerank_pipeline`` skips ``_rerank_engram_links`` (one
  ``get_temporally_linked`` DB query per result row — measured 250-560ms).

So ``fast`` = memory-only BM25+HNSW+fusion: no CE/NLI/MP, no wiki fanout,
no engram-link enrichment — the hook-latency-budget profile (~0.8s target).
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe

# ---------------------------------------------------------------------------
# Canonical profile definitions
# ---------------------------------------------------------------------------

PROFILES: dict[str, dict] = {
    "fast": {
        # Stage names in execution order
        "stages": [
            "query_analysis",
            "fts",
            "knn",
            "fusion",
        ],
        # Legacy keys — consumed by existing mixin code
        "signals": ["vector", "fts"],
        "cross_encoder": False,
        "nli": False,
        "multi_passage": False,
        # ADR-0077 hotfix: fast must actually be fast — memory-only fanout
        # (skip WikiProvider, ~450ms) + no engram-link rerank (250-560ms).
        "wiki": False,
        "engram_links": False,
    },
    "balanced": {
        # Current default — reproduces legacy behavior exactly
        "stages": [
            "query_analysis",
            "fts",
            "knn",
            "ppr",
            "spreading",
            "temporal",
            "fusion",
            "ce_rerank",
            "nli",
            "mmr",
            "adversarial",
            "rules",
        ],
        "signals": ["vector", "fts", "ppr", "spreading"],
        "cross_encoder": True,
        "nli": False,
        "multi_passage": True,
    },
    "full": {
        # Same as balanced today; reserved for future heavy-stage additions.
        "stages": [
            "query_analysis",
            "fts",
            "knn",
            "ppr",
            "spreading",
            "temporal",
            "fusion",
            "ce_rerank",
            "nli",
            "mmr",
            "adversarial",
            "rules",
        ],
        "signals": ["vector", "fts", "ppr", "spreading"],
        "cross_encoder": True,
        "nli": True,
        "multi_passage": True,
    },
    "debug": {
        # All stages; diagnostic emit via stage_stats populated for every stage.
        "stages": [
            "query_analysis",
            "fts",
            "knn",
            "ppr",
            "spreading",
            "temporal",
            "fusion",
            "ce_rerank",
            "nli",
            "mmr",
            "adversarial",
            "rules",
        ],
        "signals": ["vector", "fts", "ppr", "spreading"],
        "cross_encoder": True,
        "nli": True,
        "multi_passage": True,
        "_debug": True,
    },
}

# Validate all profile names at import time
_VALID_PROFILES = frozenset(PROFILES.keys())


@observe(tier="hot", metric="retrieval.get_profile")
def get_profile(name: str) -> dict:
    """Return profile dict for *name*, raising ValueError on unknown profile."""
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"Unknown retrieval profile {name!r}. Valid profiles: {sorted(_VALID_PROFILES)}"
        ) from None
