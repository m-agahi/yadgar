"""v5.101 P0 — histogram bucket p95-clamp fix.

Real cold recalls reach ~75s; the top finite ms-bucket was 10000 (10s), so
histogram_quantile clamped p95 at 10s. The ms-scale recall/mcp/stage histograms
must extend to 300000ms; the seconds-scale pipeline-stage histogram must extend
to 300s.
"""

from __future__ import annotations


def _bucket_upper_bounds(hist) -> list[float]:
    """Extract the finite bucket upper bounds from a prometheus Histogram.

    Reads the declared `_upper_bounds` (works for labelled histograms with zero
    observations — those emit no bucket samples via collect()).
    """
    bounds = [b for b in hist._upper_bounds if b != float("inf")]
    return sorted(bounds)


_NEW_MS_TAIL = [15000.0, 20000.0, 30000.0, 60000.0, 120000.0, 300000.0]


def test_recall_duration_ms_has_extended_buckets():
    from yadgar.metrics import yadgar_recall_duration_ms

    bounds = _bucket_upper_bounds(yadgar_recall_duration_ms)
    for edge in _NEW_MS_TAIL:
        assert edge in bounds, f"{edge} missing from yadgar_recall_duration_ms buckets: {bounds}"
    assert max(bounds) == 300000.0


def test_recall_stage_ms_has_extended_buckets():
    from yadgar.metrics import yadgar_recall_stage_ms

    bounds = _bucket_upper_bounds(yadgar_recall_stage_ms)
    for edge in _NEW_MS_TAIL:
        assert edge in bounds, f"{edge} missing from yadgar_recall_stage_ms buckets: {bounds}"


def test_mcp_request_duration_ms_has_extended_buckets():
    from yadgar.metrics import yadgar_mcp_request_duration_ms

    bounds = _bucket_upper_bounds(yadgar_mcp_request_duration_ms)
    for edge in _NEW_MS_TAIL:
        assert edge in bounds, (
            f"{edge} missing from yadgar_mcp_request_duration_ms buckets: {bounds}"
        )


def test_recall_stage_duration_seconds_has_extended_buckets():
    from yadgar.metrics import yadgar_recall_stage_duration_seconds

    bounds = _bucket_upper_bounds(yadgar_recall_stage_duration_seconds)
    for edge in (15.0, 30.0, 60.0, 120.0, 300.0):
        assert edge in bounds, (
            f"{edge}s missing from yadgar_recall_stage_duration_seconds buckets: {bounds}"
        )
    assert max(bounds) == 300.0
