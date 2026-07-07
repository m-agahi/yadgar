"""Metacognition — Yadgar knowing what it knows and doesn't know.

Implements three capabilities from MetaRAG (Zhou et al., ACM Web 2024)
and Cognitive Workspace (arXiv:2508.13171):

1. Coverage assessment — "Do I have enough knowledge about this topic?"
2. Gap detection — "What don't I know about this project?"
3. Cognitive load management — optimal 4±1 chunk context packing
   with primacy-recency positioning.

MetaCognition is assembled from three mixin classes:
  _CoverageMixin      (coverage.py)      — assess_coverage
  _GapDetectionMixin  (gap_detection.py) — detect_gaps
  _CognitiveLoadMixin (cognitive_load.py) — manage_context, chunk_memories, summarize_overflow
"""

import logging

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.metacognition.cognitive_load import _CognitiveLoadMixin
from yadgar._shared.metacognition.coverage import _CoverageMixin
from yadgar._shared.metacognition.gap_detection import _GapDetectionMixin
from yadgar._shared.storage import StorageEngine

logger = logging.getLogger(__name__)


class MetaCognition(_CoverageMixin, _GapDetectionMixin, _CognitiveLoadMixin):
    """Metacognitive layer: coverage assessment, gap detection,
    and cognitive load management (Cowan's 4±1 chunk limit)."""

    def __init__(
        self,
        storage: StorageEngine,
        embeddings: EmbeddingEngine,
        knowledge_graph: KnowledgeGraph,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._embeddings = embeddings
        self._graph = knowledge_graph
        self._settings = settings
        self._chunk_limit = settings.COGNITIVE_LOAD_LIMIT


__all__ = ["MetaCognition"]
