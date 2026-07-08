"""Sleep-time compute system — offline processing for creative connections and maintenance."""

from __future__ import annotations

import logging

from yadgar._shared.tracing import trace_span
from yadgar.backend.sleep_compute.community import _CommunityMixin
from yadgar.backend.sleep_compute.dream import _DreamMixin
from yadgar.backend.sleep_compute.embed_compress import _EmbedCompressMixin

logger = logging.getLogger(__name__)

__all__ = ["SleepComputeEngine"]


class SleepComputeEngine(_DreamMixin, _CommunityMixin, _EmbedCompressMixin):
    """Offline sleep-time processing engine.

    Runs during extended idle periods to:
    - Dream replay: discover unexpected cross-domain connections
    - Community detection: find clusters of related entities
    - Cluster summarization: generate summaries for memory groups
    - Re-embedding: update stale embeddings to current model
    - Compression: compress old verbose memories
    """

    def __init__(
        self,
        storage,
        embeddings,
        knowledge_graph,
        curation,
        thermodynamics,
        settings,
    ) -> None:
        from yadgar.backend.narrative import NarrativeEngine

        self._storage = storage
        self._embeddings = embeddings
        self._graph = knowledge_graph
        self._curator = curation
        self._thermo = thermodynamics
        self._settings = settings
        self._narrative = NarrativeEngine(storage, knowledge_graph, settings)

    @trace_span()
    def run_sleep_cycle(self) -> dict:
        """Orchestrate all sleep-time operations in order."""
        stats: dict = {}

        logger.info("Sleep cycle phase 1: dream replay")
        stats["dream_replay"] = self.dream_replay()

        logger.info("Sleep cycle phase 2: community detection")
        stats["communities"] = self.detect_communities()

        logger.info("Sleep cycle phase 3: cluster summarization")
        self.generate_cluster_summaries()
        stats["cluster_summaries_generated"] = True

        logger.info("Sleep cycle phase 4: re-embedding")
        stats["reembedded"] = self.reembed_stale()

        logger.info("Sleep cycle phase 5: compression")
        stats["compressed"] = self.compress_old_memories()

        logger.info("Sleep cycle phase 6: auto-narrate")
        stats["narrative"] = self._narrative.auto_narrate()

        logger.info("Sleep cycle complete: %s", stats)
        return stats
