"""RetrievalPipeline — ordered stage orchestrator with per-stage telemetry.

v5.31.0 plugin architecture. Each stage is a ``RetrievalStage`` instance.
The pipeline iterates stage names from the profile, looks each one up in its
internal registry, and calls ``stage.apply(state)``, timing each call.

Post-fusion stages (ce_rerank, nli, mmr, adversarial, rules) are collapsed
into a single composite call via the CEReRankStage to preserve the exact
computation order pinned by characterization tests.  The pipeline detects
when it has reached the first post-fusion stage and dispatches to the
composite delegate; subsequent post-fusion stage names are skipped.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from yadgar._shared.observability.observe import observe
from yadgar._shared.retrieval.profiles import get_profile
from yadgar._shared.retrieval.stages.base import RetrievalStage
from yadgar._shared.retrieval.state import RetrievalState

logger = logging.getLogger(__name__)

# Stage names that are handled by the composite rerank pipeline delegate.
# When the pipeline encounters the first of these, it dispatches once and skips
# the rest.  This preserves characterization-test fixture integrity.
_POST_FUSION_STAGES = frozenset({"ce_rerank", "nli", "mmr", "adversarial", "rules"})


def _observe_stage_metric(stage: str, profile: str, elapsed_ms: float) -> None:
    """Emit per-stage Prometheus histogram. No-op on import error."""
    try:
        from yadgar._shared.metrics import (  # noqa: PLC0415
            yadgar_recall_stage_duration_seconds,
        )

        yadgar_recall_stage_duration_seconds.labels(stage=stage, profile=profile).observe(
            elapsed_ms / 1000.0
        )
    except Exception:  # noqa: BLE001
        pass


def _observe_candidates(stage: str, profile: str, count_in: int, count_out: int) -> None:
    """Emit per-stage candidate count gauges. No-op on import error."""
    try:
        from yadgar._shared.metrics import (  # noqa: PLC0415
            yadgar_recall_stage_candidates_in,
            yadgar_recall_stage_candidates_out,
        )

        yadgar_recall_stage_candidates_in.labels(stage=stage, profile=profile).set(count_in)
        yadgar_recall_stage_candidates_out.labels(stage=stage, profile=profile).set(count_out)
    except Exception:  # noqa: BLE001
        pass


def _observe_profile_invocation(profile: str) -> None:
    """Increment profile invocation counter. No-op on import error."""
    try:
        from yadgar._shared.metrics import (  # noqa: PLC0415
            yadgar_recall_profile_invocations_total,
        )

        yadgar_recall_profile_invocations_total.labels(profile=profile).inc()
    except Exception:  # noqa: BLE001
        pass


class RetrievalPipeline:
    """Orchestrate an ordered sequence of retrieval stages with timing + metrics.

    Usage::

        pipeline = RetrievalPipeline.from_retriever(retriever)
        state = RetrievalState(query="...", profile="balanced", ...)
        state = pipeline.run(state)
        results = state.result_memories

    Attributes:
        stages: Mapping of stage name → RetrievalStage instance.
    """

    def __init__(self, stages: list[RetrievalStage]) -> None:
        self.stages: dict[str, RetrievalStage] = {s.name: s for s in stages}

    @classmethod
    @observe(tier="hot", metric="retrieval.pipeline.from_retriever")
    def from_retriever(cls, retriever) -> RetrievalPipeline:
        """Build a fully wired pipeline from an existing ``Retriever`` instance.

        Creates one stage object per known stage name, wired to the retriever.
        """
        from yadgar._shared.retrieval.stages.adversarial import AdversarialStage  # noqa: PLC0415
        from yadgar._shared.retrieval.stages.ce_rerank import CEReRankStage  # noqa: PLC0415
        from yadgar._shared.retrieval.stages.fts import FTSStage  # noqa: PLC0415
        from yadgar._shared.retrieval.stages.fusion import FusionStage  # noqa: PLC0415
        from yadgar._shared.retrieval.stages.knn import KNNStage  # noqa: PLC0415
        from yadgar._shared.retrieval.stages.mmr import MMRStage  # noqa: PLC0415
        from yadgar._shared.retrieval.stages.nli import NLIStage  # noqa: PLC0415
        from yadgar._shared.retrieval.stages.ppr import PPRStage  # noqa: PLC0415
        from yadgar._shared.retrieval.stages.query_analysis import (
            QueryAnalysisStage,  # noqa: PLC0415
        )
        from yadgar._shared.retrieval.stages.rules import RulesStage  # noqa: PLC0415
        from yadgar._shared.retrieval.stages.spreading import SpreadingStage  # noqa: PLC0415
        from yadgar._shared.retrieval.stages.temporal import TemporalStage  # noqa: PLC0415

        stage_list: list[RetrievalStage] = [
            QueryAnalysisStage(retriever),
            FTSStage(retriever),
            KNNStage(retriever),
            PPRStage(retriever),
            SpreadingStage(retriever),
            TemporalStage(retriever),
            FusionStage(retriever),
            CEReRankStage(retriever),
            NLIStage(retriever),
            MMRStage(retriever),
            AdversarialStage(retriever),
            RulesStage(retriever),
        ]
        return cls(stage_list)

    @observe(tier="boundary", metric="retrieval.pipeline.run")
    def run(self, state: RetrievalState) -> RetrievalState:
        """Execute the pipeline for *state.profile*, collecting per-stage stats.

        Per-call ``stage_overrides`` (in state) can disable individual stages.
        Post-fusion stages (ce_rerank, nli, mmr, adversarial, rules) share a
        single composite execution; after the first one runs, the rest are no-ops.
        """
        profile_dict = get_profile(state.profile)
        stage_names: list[str] = profile_dict["stages"]
        overrides: dict = state.stage_overrides or {}

        _observe_profile_invocation(state.profile)

        # Set up default scores dict early so all stages see the same structure
        if not state.scores:
            state.scores = defaultdict(
                lambda: {
                    "vector": 0.0,
                    "fts": 0.0,
                    "ppr": 0.0,
                    "spread": 0.0,
                    "temporal": 0.0,
                }
            )

        _post_fusion_dispatched = False

        for name in stage_names:
            # Per-call override check: explicit False disables stage
            if overrides.get(name) is False:
                logger.debug("pipeline: stage %r disabled by stage_override", name)
                continue

            stage = self.stages.get(name)
            if stage is None:
                logger.warning("pipeline: unknown stage %r in profile %r", name, state.profile)
                continue

            if not stage.is_enabled(state.profile, {"stage_overrides": overrides}):
                logger.debug("pipeline: stage %r disabled by is_enabled()", name)
                continue

            # Composite post-fusion handling: dispatch once, skip the rest
            if name in _POST_FUSION_STAGES:
                if _post_fusion_dispatched:
                    # Record zero-duration no-op so stats are complete
                    state.stage_stats[name] = {"duration_ms": 0.0, "skipped": True}
                    continue
                _post_fusion_dispatched = True

            count_in = len(state.result_memories)
            t0 = time.perf_counter()
            state = stage.apply(state)
            dt_ms = (time.perf_counter() - t0) * 1000
            count_out = len(state.result_memories)

            state.stage_stats[name] = {
                "duration_ms": dt_ms,
                "count_in": count_in,
                "count_out": count_out,
            }

            _observe_stage_metric(name, state.profile, dt_ms)
            _observe_candidates(name, state.profile, count_in, count_out)

            logger.debug(
                "pipeline: stage=%r profile=%r dt=%.1fms in=%d out=%d",
                name,
                state.profile,
                dt_ms,
                count_in,
                count_out,
            )

        return state
