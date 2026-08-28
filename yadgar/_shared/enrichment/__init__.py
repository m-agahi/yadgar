"""Index-time enrichment pipeline for Yadgar memories.

Generates implied facts, commonsense inferences, and synthetic queries
at storage time to bridge the cue-trigger semantic disconnect.
"""

import logging
from dataclasses import dataclass, field

from yadgar._shared.config import Settings
from yadgar._shared.enrichment._seq2seq import _load_seq2seq_model as _load_seq2seq_model
from yadgar._shared.enrichment.comet import CometInferencer as CometInferencer
from yadgar._shared.enrichment.conceptnet import (
    HARDCODED_EXPANSIONS as HARDCODED_EXPANSIONS,
)
from yadgar._shared.enrichment.conceptnet import (
    ConceptNetExpander as ConceptNetExpander,
)
from yadgar._shared.enrichment.doc2query import Doc2QueryExpander as Doc2QueryExpander
from yadgar._shared.enrichment.fpa import FPAFilter as FPAFilter
from yadgar._shared.enrichment.logic import LogicExpander as LogicExpander
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentResult:
    concepts: list[str] = field(default_factory=list)
    comet_inferences: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    logic_expansions: list[str] = field(default_factory=list)
    enriched_content: str = ""
    model_versions: dict = field(default_factory=dict)


class EnrichmentPipeline:
    """Orchestrator for index-time enrichment techniques."""

    def __init__(self, settings: Settings, embedding_engine=None) -> None:
        self._settings = settings
        self._embedding_engine = embedding_engine
        self._fpa: FPAFilter | None = None
        self._conceptnet: ConceptNetExpander | None = None
        self._comet: CometInferencer | None = None
        self._doc2query: Doc2QueryExpander | None = None
        self._logic: LogicExpander | None = None

    @observe(tier="stage")
    def _get_fpa(self) -> FPAFilter | None:
        if self._fpa is None and self._embedding_engine is not None:
            self._fpa = FPAFilter(self._embedding_engine)
        return self._fpa

    @observe(tier="stage")
    def _get_conceptnet(self) -> ConceptNetExpander:
        if self._conceptnet is None:
            self._conceptnet = ConceptNetExpander()
        return self._conceptnet

    @observe(tier="stage")
    def _get_comet(self) -> CometInferencer:
        if self._comet is None:
            self._comet = CometInferencer()
        return self._comet

    @observe(tier="stage")
    def _get_doc2query(self) -> Doc2QueryExpander:
        if self._doc2query is None:
            self._doc2query = Doc2QueryExpander()
        return self._doc2query

    @observe(tier="stage")
    def _get_logic(self) -> LogicExpander:
        if self._logic is None:
            self._logic = LogicExpander()
        return self._logic

    @observe(tier="stage")
    def _apply_fpa(self, embedding: bytes, texts: list[str], threshold: float) -> list[str]:
        fpa = self._get_fpa()
        if fpa is None or embedding is None:
            return texts
        return fpa.filter(embedding, texts, threshold)

    @observe(tier="boundary")
    def enrich(self, content: str, embedding: bytes, settings: Settings) -> EnrichmentResult:
        result = EnrichmentResult()

        if len(content) < settings.ENRICHMENT_MIN_CONTENT_LENGTH:
            result.enriched_content = content
            return result

        threshold = settings.FPA_SIMILARITY_THRESHOLD

        # ConceptNet expansion
        if settings.CONCEPTNET_ENRICHMENT_ENABLED:
            try:
                concepts = self._get_conceptnet().expand(content, settings)
                concepts = self._apply_fpa(embedding, concepts, threshold)
                result.concepts = concepts
            except Exception as e:  # noqa: BLE001 — per-enricher isolation: ConceptNet reaches conceptnet_lite (SQLite) or the remote HTTP API, whose failures share no common base, and one dead enricher must not sink the other three
                logger.warning("ConceptNet enrichment failed: %s", e)

        # COMET commonsense inference
        # RETIRED / DORMANT per ADR-0004 + benchmarks/reports/en2a_comet_ablation_2026-06-24.md
        # (net-negative recall, prohibitive cost). Intentionally retained — NOT dead code.
        # Do not enable without re-validating against the ablation. Gate defaults False.
        if settings.COMET_ENRICHMENT_ENABLED:
            try:
                inferences = self._get_comet().infer(content, settings)
                inferences = self._apply_fpa(embedding, inferences, threshold)
                result.comet_inferences = inferences
                if self._get_comet()._model is not None:
                    result.model_versions["comet"] = settings.COMET_MODEL
            except Exception as e:  # noqa: BLE001 — per-enricher isolation: COMET loads a seq2seq model (torch/transformers), whose failures share no common base; ADR-0004 keeps this path dormant but reachable
                logger.warning("COMET enrichment failed: %s", e)

        # Doc2Query synthetic queries
        if settings.DOC2QUERY_ENRICHMENT_ENABLED:
            try:
                queries = self._get_doc2query().expand(content, settings)
                queries = self._apply_fpa(embedding, queries, threshold)
                result.queries = queries
                if self._get_doc2query()._model is not None:
                    result.model_versions["doc2query"] = settings.DOC2QUERY_MODEL
            except Exception as e:  # noqa: BLE001 — per-enricher isolation: Doc2Query loads a seq2seq model (torch/transformers), whose failures share no common base
                logger.warning("Doc2Query enrichment failed: %s", e)

        # Logic expansion (no external deps, no FPA needed — these are structural)
        if settings.LOGIC_ENRICHMENT_ENABLED:
            try:
                expansions = self._get_logic().expand(content)
                result.logic_expansions = expansions
            except (AttributeError, TypeError) as e:  # fmt: skip
                logger.warning("Logic enrichment failed: %s", e)

        # Build enriched content
        all_terms = (
            result.concepts + result.comet_inferences + result.queries + result.logic_expansions
        )

        if all_terms:
            result.enriched_content = content + "\n[enrichment] " + " | ".join(all_terms)
        else:
            result.enriched_content = content

        return result
