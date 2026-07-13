"""P-SB harness re-point (#50 / §3.5) — ce source-selection matrix.

The old harness read CE latency ONLY from
``yadgar_embed_rerank_duration_seconds{mode="ce"}``, which recall's in-process CE
never feeds (count=0 → ce_mean_ms silently None since T2, #50). Post-P-SB the
primary source is
``yadgar_observe_stage_duration_seconds{stage="retrieval.ce.score_ce_cached"}``
(bridged onto :8001). The legacy embed-rerank probe stays as a labelled fallback
for old daemons (ADR-0105 portability note).

``ce_source_status`` (pure) picks the source and returns
``(ce_mean_ms, ce_calls, status)`` where status distinguishes:
  * observe-stage source (current)
  * embed-rerank source (legacy)
  * unavailable

All three legs are exercised here WITHOUT a live daemon — the point of keeping the
decision in the pure ``perf_contract`` module.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BENCH_DIR = str(Path(__file__).resolve().parents[3] / "benchmarks")
if _BENCH_DIR not in sys.path:
    sys.path.insert(0, _BENCH_DIR)

import perf_contract  # noqa: E402


def test_observe_stage_source_is_primary_when_fed():
    """When the observe-stage histogram has count>0, it is the source (even if the
    legacy embed-rerank probe is ALSO fed — observe-stage wins)."""
    observe = ((0.0, 0.0), (1.5, 3.0))  # d_sum=1.5s over d_count=3 → 500ms/call
    legacy = ((0.0, 0.0), (9.9, 9.0))  # legacy fed too, but must be ignored
    mean_ms, calls, status = perf_contract.ce_source_status(observe, legacy)
    assert mean_ms == 500.0, mean_ms
    assert calls == 3
    assert "observe-stage" in status, status


def test_legacy_source_used_when_observe_stage_absent():
    """observe-stage absent/count==0 but legacy embed-rerank fed → legacy source."""
    observe = ((0.0, 0.0), (0.0, 0.0))  # d_count=0 → observe-stage unavailable
    legacy = ((0.0, 0.0), (2.0, 4.0))  # d_sum=2.0s over 4 → 500ms/call
    mean_ms, calls, status = perf_contract.ce_source_status(observe, legacy)
    assert mean_ms == 500.0, mean_ms
    assert calls == 4
    assert "legacy" in status.lower() or "embed-rerank" in status.lower(), status


def test_observe_stage_none_falls_back_to_legacy():
    """observe-stage scrape returned None (family absent) → legacy used if fed."""
    observe = None
    legacy = ((0.0, 0.0), (1.0, 2.0))
    mean_ms, calls, status = perf_contract.ce_source_status(observe, legacy)
    assert mean_ms == 500.0
    assert calls == 2
    assert "legacy" in status.lower() or "embed-rerank" in status.lower(), status


def test_unavailable_when_neither_source_fed():
    """Both sources count==0 or None → ce_mean_ms None, status unavailable."""
    observe = ((0.0, 0.0), (0.0, 0.0))
    legacy = ((0.0, 0.0), (0.0, 0.0))
    mean_ms, calls, status = perf_contract.ce_source_status(observe, legacy)
    assert mean_ms is None
    assert "unavailable" in status.lower(), status


def test_unavailable_when_both_none():
    mean_ms, calls, status = perf_contract.ce_source_status(None, None)
    assert mean_ms is None
    assert "unavailable" in status.lower(), status
