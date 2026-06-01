"""Retrieval pipeline stages — plug-in architecture (v5.31.0)."""

from yadgar.retrieval.stages.adversarial import AdversarialStage
from yadgar.retrieval.stages.base import RetrievalStage
from yadgar.retrieval.stages.ce_rerank import CEReRankStage
from yadgar.retrieval.stages.fts import FTSStage
from yadgar.retrieval.stages.fusion import FusionStage
from yadgar.retrieval.stages.knn import KNNStage
from yadgar.retrieval.stages.mmr import MMRStage
from yadgar.retrieval.stages.nli import NLIStage
from yadgar.retrieval.stages.ppr import PPRStage
from yadgar.retrieval.stages.query_analysis import QueryAnalysisStage
from yadgar.retrieval.stages.rules import RulesStage
from yadgar.retrieval.stages.spreading import SpreadingStage
from yadgar.retrieval.stages.temporal import TemporalStage

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
