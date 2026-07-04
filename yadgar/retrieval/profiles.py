"""Built-in retrieval profiles for the plugin pipeline (v5.31.0).

Profiles configure which stages run and in what order.  The "balanced" profile
reproduces the pre-v5.31.0 monolithic recall() behavior exactly.

Profile dicts carry two representations for backward compatibility:
- ``stages``: ordered list of stage names for the pipeline runner.
- Legacy keys (``signals``, ``cross_encoder``, ``nli``, ``multi_passage``):
  still consumed by ``_RerankingMixin._apply_rerank_pipeline`` and
  ``_FusionMixin._build_initial_results`` until those are fully ported.
"""

from __future__ import annotations

from yadgar.observability.observe import observe

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


@observe(tier="hot", name="retrieval.get_profile")
def get_profile(name: str) -> dict:
    """Return profile dict for *name*, raising ValueError on unknown profile."""
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"Unknown retrieval profile {name!r}. Valid profiles: {sorted(_VALID_PROFILES)}"
        ) from None
