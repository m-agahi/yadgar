"""A/B comparison harness for the plugin pipeline (v5.31.0).

Runs the same query under multiple profiles side-by-side and returns
a comparison dict with results and per-stage timing for each profile.
Used by benchmark scripts to A/B test stage contributions.
"""

from __future__ import annotations

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def recall_compare(
    retriever,
    query: str,
    profiles: list[str],
    max_results: int = 10,
    min_heat: float = 0.0,
    current_branch: str | None = None,
    default_branch: str | None = None,
) -> dict:
    """Run the same query under multiple profiles; return side-by-side results.

    Args:
        retriever: A ``Retriever`` instance (or any object with a ``recall``
            method and a ``_pipeline`` attribute).
        query: The query string.
        profiles: List of profile names to compare, e.g. ["balanced", "full"].
        max_results: Maximum results per profile.
        min_heat: Minimum heat threshold.
        current_branch: Active git branch for branch filtering.
        default_branch: Repository default branch for branch filtering.

    Returns:
        Dict with keys:
        - ``query``: the query string
        - ``profiles``: dict keyed by profile name, each value being:
          - ``results``: list of result memory dicts (same as recall())
          - ``stage_stats``: per-stage timing dict {stage: {duration_ms, ...}}
          - ``error``: error string if the run failed (results will be empty)
    """
    from yadgar.retrieval.pipeline import RetrievalPipeline  # noqa: PLC0415
    from yadgar.retrieval.state import RetrievalState  # noqa: PLC0415

    output: dict = {
        "query": query,
        "profiles": {},
    }

    # Build or reuse a pipeline
    if hasattr(retriever, "_pipeline") and retriever._pipeline is not None:
        pipeline: RetrievalPipeline = retriever._pipeline
    else:
        pipeline = RetrievalPipeline.from_retriever(retriever)

    for profile_name in profiles:
        state = RetrievalState(
            query=query,
            max_results=max_results,
            min_heat=min_heat,
            profile=profile_name,
            current_branch=current_branch,
            default_branch=default_branch,
            scores=defaultdict(
                lambda: {
                    "vector": 0.0,
                    "fts": 0.0,
                    "ppr": 0.0,
                    "spread": 0.0,
                    "temporal": 0.0,
                }
            ),
        )
        try:
            state = pipeline.run(state)
            output["profiles"][profile_name] = {
                "results": state.result_memories,
                "stage_stats": state.stage_stats,
            }
        except Exception as exc:
            logger.warning(
                "recall_compare: profile=%r query=%r failed: %s",
                profile_name,
                query[:60],
                exc,
            )
            output["profiles"][profile_name] = {
                "results": [],
                "stage_stats": {},
                "error": str(exc),
            }

    return output
