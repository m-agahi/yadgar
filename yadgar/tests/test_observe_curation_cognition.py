"""I33 STEP 2 — @observe sentinel checks for curation/enrichment/metacognition."""

from __future__ import annotations


def _has_span(fn) -> bool:
    return bool(getattr(fn, "_yadgar_observe_has_span", False))


def test_curation_ingestion_instrumented():
    from yadgar._shared.curation import ingestion

    assert _has_span(ingestion.merge_memory)


def test_enrichment_pipeline_instrumented():
    from yadgar._shared.enrichment import EnrichmentPipeline

    assert _has_span(EnrichmentPipeline.enrich)


def test_metacognition_coverage_instrumented():
    from yadgar._shared.metacognition.coverage import _CoverageMixin

    assert _has_span(_CoverageMixin.assess_coverage)


def test_curation_contradiction_instrumented():
    from yadgar._shared.curation.contradiction import detect_contradictions

    assert _has_span(detect_contradictions)


def test_metacognition_gap_detection_instrumented():
    from yadgar._shared.metacognition.gap_detection import _GapDetectionMixin

    assert _has_span(_GapDetectionMixin.detect_gaps)
