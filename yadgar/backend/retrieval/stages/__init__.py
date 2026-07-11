"""Retrieval pipeline stages — plug-in architecture (v5.31.0)."""

from yadgar.backend.retrieval.stages.adversarial import AdversarialStage
from yadgar.backend.retrieval.stages.base import RetrievalStage
from yadgar.backend.retrieval.stages.ce_rerank import CEReRankStage
from yadgar.backend.retrieval.stages.fts import FTSStage
from yadgar.backend.retrieval.stages.fusion import FusionStage
from yadgar.backend.retrieval.stages.knn import KNNStage
from yadgar.backend.retrieval.stages.mmr import MMRStage
from yadgar.backend.retrieval.stages.nli import NLIStage
from yadgar.backend.retrieval.stages.ppr import PPRStage
from yadgar.backend.retrieval.stages.query_analysis import QueryAnalysisStage
from yadgar.backend.retrieval.stages.rules import RulesStage
from yadgar.backend.retrieval.stages.spreading import SpreadingStage
from yadgar.backend.retrieval.stages.temporal import TemporalStage

__all__ = [
    "RetrievalStage",
    "QueryAnalysisStage",
    "FTSStage",
    "KNNStage",
    "PPRStage",
    "SpreadingStage",
    "TemporalStage",
    "FusionStage",
    "CEReRankStage",
    "NLIStage",
    "MMRStage",
    "AdversarialStage",
    "RulesStage",
]
