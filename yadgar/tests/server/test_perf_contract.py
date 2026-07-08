"""Fast, model-free unit tests for the #79 perf-loadtest CONTRACT checker.

Covers the pure comparison logic in ``benchmarks.perf_contract`` — delta_pct
math, over/under-tolerance flagging, and the incomparability guard (mismatched
``snapshot_id`` / ``embedding_model`` must NOT flag deltas as regressions,
per perf-loadtest-contract-2026-06-30.md §2.5).

No daemon, no ML, no model load — imports a stdlib-only module. Runs in the
default fast suite.
"""

from __future__ import annotations

import math

from benchmarks.perf_contract import (
    METRIC_KEYS,
    compare_to_baseline,
    parse_prom_metric,
    pct_delta,
)

# ── pct_delta ────────────────────────────────────────────────────────────────


def test_pct_delta_increase() -> None:
    # 100 -> 115 = +15%
    assert pct_delta(100.0, 115.0) == 15.0


def test_pct_delta_decrease() -> None:
    # 200 -> 150 = -25% (improvement; negative delta)
    assert pct_delta(200.0, 150.0) == -25.0


def test_pct_delta_zero_baseline_is_none() -> None:
    # Cannot compute a percentage against a zero baseline.
    assert pct_delta(0.0, 5.0) is None


# ── compare_to_baseline: flagging ────────────────────────────────────────────


def _baseline(snapshot_id: str = "pin-abc", embedding_model: str = "gte") -> dict:
    return {
        "snapshot_id": snapshot_id,
        "embedding_model": embedding_model,
        "aggregated": {
            "recall_p95_ms": 1000.0,
            "ce_mean_ms": 8000.0,
        },
    }


def _current(
    recall_p95_ms: float,
    ce_mean_ms: float,
    snapshot_id: str = "pin-abc",
    embedding_model: str = "gte",
) -> dict:
    return {
        "snapshot_id": snapshot_id,
        "embedding_model": embedding_model,
        "aggregated": {
            "recall_p95_ms": recall_p95_ms,
            "ce_mean_ms": ce_mean_ms,
        },
    }


def test_flag_true_when_recall_over_tolerance() -> None:
    # +20% recall p95 with 15% tolerance -> flagged
    result = compare_to_baseline(_baseline(), _current(1200.0, 8000.0), tolerance_pct=15.0)
    assert result["comparable"] is True
    assert result["diff"]["recall_p95_ms"]["flagged"] is True
    assert math.isclose(result["diff"]["recall_p95_ms"]["delta_pct"], 20.0)


def test_flag_false_when_recall_under_tolerance() -> None:
    # +10% recall p95 with 15% tolerance -> NOT flagged
    result = compare_to_baseline(_baseline(), _current(1100.0, 8000.0), tolerance_pct=15.0)
    assert result["diff"]["recall_p95_ms"]["flagged"] is False


def test_flag_true_on_ce_regression_even_when_recall_flat() -> None:
    # The whole point: CE budget catches a CE regression that recall p95
    # (if it happened to be masked) would not. +25% CE, flat recall.
    result = compare_to_baseline(_baseline(), _current(1000.0, 10000.0), tolerance_pct=15.0)
    assert result["diff"]["ce_mean_ms"]["flagged"] is True
    assert result["diff"]["recall_p95_ms"]["flagged"] is False
    assert result["any_flagged"] is True


def test_improvement_not_flagged() -> None:
    # Faster than baseline -> negative delta -> never flagged.
    result = compare_to_baseline(_baseline(), _current(800.0, 6000.0), tolerance_pct=15.0)
    assert result["any_flagged"] is False
    assert result["diff"]["recall_p95_ms"]["delta_pct"] < 0


# ── compare_to_baseline: incomparability guard (§2.5) ────────────────────────


def test_snapshot_mismatch_suppresses_flags() -> None:
    result = compare_to_baseline(
        _baseline(snapshot_id="pin-abc"),
        _current(2000.0, 20000.0, snapshot_id="pin-XYZ"),
        tolerance_pct=15.0,
    )
    assert result["comparable"] is False
    assert result["any_flagged"] is False
    # deltas may still be reported, but nothing is flagged as a regression
    for entry in result["diff"].values():
        assert entry["flagged"] is False
    assert "snapshot_id" in result["incomparable_reason"]


def test_embedding_model_mismatch_suppresses_flags() -> None:
    result = compare_to_baseline(
        _baseline(embedding_model="gte"),
        _current(2000.0, 20000.0, embedding_model="minilm"),
        tolerance_pct=15.0,
    )
    assert result["comparable"] is False
    assert result["any_flagged"] is False
    assert "embedding_model" in result["incomparable_reason"]


def test_missing_baseline_metric_is_skipped_not_crash() -> None:
    base = _baseline()
    del base["aggregated"]["ce_mean_ms"]
    result = compare_to_baseline(base, _current(1000.0, 9999.0), tolerance_pct=15.0)
    # recall still compared; ce absent from diff (no baseline to compare)
    assert "recall_p95_ms" in result["diff"]
    assert "ce_mean_ms" not in result["diff"]


def test_metric_keys_include_recall_and_ce() -> None:
    assert "recall_p95_ms" in METRIC_KEYS
    assert "ce_mean_ms" in METRIC_KEYS


# ── parse_prom_metric ────────────────────────────────────────────────────────

_PROM_SAMPLE = """\
# HELP yadgar_embed_rerank_duration_seconds Rerank inference latency
# TYPE yadgar_embed_rerank_duration_seconds histogram
yadgar_embed_rerank_duration_seconds_bucket{mode="ce",le="0.5"} 3
yadgar_embed_rerank_duration_seconds_bucket{mode="ce",le="+Inf"} 12
yadgar_embed_rerank_duration_seconds_sum{mode="ce"} 96.5
yadgar_embed_rerank_duration_seconds_count{mode="ce"} 12
yadgar_embed_rerank_duration_seconds_sum{mode="nli"} 1.0
yadgar_embed_rerank_duration_seconds_count{mode="nli"} 2
"""


def test_parse_prom_metric_sum_and_count() -> None:
    got_sum = parse_prom_metric(
        _PROM_SAMPLE, "yadgar_embed_rerank_duration_seconds_sum", {"mode": "ce"}
    )
    got_count = parse_prom_metric(
        _PROM_SAMPLE, "yadgar_embed_rerank_duration_seconds_count", {"mode": "ce"}
    )
    assert got_sum == 96.5
    assert got_count == 12.0


def test_parse_prom_metric_label_selectivity() -> None:
    # Must select the ce series, not nli.
    got = parse_prom_metric(
        _PROM_SAMPLE, "yadgar_embed_rerank_duration_seconds_sum", {"mode": "nli"}
    )
    assert got == 1.0


def test_parse_prom_metric_absent_returns_none() -> None:
    assert parse_prom_metric(_PROM_SAMPLE, "nonexistent_metric_total", {}) is None
