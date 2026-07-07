"""Retrieval pipeline stages — plug-in architecture (v5.31.0)."""

from yadgar._shared.retrieval.stages.adversarial import AdversarialStage
from yadgar._shared.retrieval.stages.base import RetrievalStage
from yadgar._shared.retrieval.stages.ce_rerank import CEReRankStage
from yadgar._shared.retrieval.stages.fts import FTSStage
from yadgar._shared.retrieval.stages.fusion import FusionStage
from yadgar._shared.retrieval.stages.knn import KNNStage
from yadgar._shared.retrieval.stages.mmr import MMRStage
from yadgar._shared.retrieval.stages.nli import NLIStage
from yadgar._shared.retrieval.stages.ppr import PPRStage
from yadgar._shared.retrieval.stages.query_analysis import QueryAnalysisStage
from yadgar._shared.retrieval.stages.rules import RulesStage
from yadgar._shared.retrieval.stages.spreading import SpreadingStage
from yadgar._shared.retrieval.stages.temporal import TemporalStage

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
