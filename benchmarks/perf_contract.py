"""Perf-loadtest CONTRACT checker (#79) — pure, stdlib-only.

Design authority: ``docs/plans/perf-loadtest-contract-2026-06-30.md`` §2.3, §2.5
and ``docs/plans/obs-velocity-completion-2026-07-04.md`` PART B §3.2.

This module holds ONLY the comparison logic: given a baseline aggregate and a
current aggregate, compute per-metric ``delta_pct`` and decide whether each
metric exceeds ``baseline + tolerance``. It imports nothing from ``yadgar`` and
loads no model — so its unit test (``yadgar/tests/test_perf_contract.py``) runs
in the fast, model-free suite and the CI-runnable piece of #79 is this pure
checker against a committed baseline.

RECORD-ONLY (Phase 1): ``compare_to_baseline`` computes ``flagged`` booleans but
NEVER raises or exits nonzero. Gating (Phase 2) is a future PR that consults the
``any_flagged`` result to decide the process exit code once baselines are stable.

Incomparability guard (§2.5): if the current run's ``snapshot_id`` or
``embedding_model`` differs from the baseline's, the comparison is invalid — the
pin's vectors / dataset shape changed — so deltas are reported but NOTHING is
flagged as a regression.
"""

from __future__ import annotations

import re

# Metrics the contract tracks. recall_p95_ms is the user-POV read latency; the
# CE budget (ce_mean_ms) is the distinct CE-regression signal — CE is ~90% of
# recall latency (ADR-0035) but can regress independently of (or while masked
# by) total recall latency, so it MUST be tracked separately.
METRIC_KEYS: tuple[str, ...] = (
    "recall_p50_ms",
    "recall_p95_ms",
    "ce_mean_ms",
)


def pct_delta(baseline: float, current: float) -> float | None:
    """Percent change from ``baseline`` to ``current``.

    Positive = current is larger (slower/worse for latency metrics).
    Returns ``None`` when ``baseline`` is zero (undefined percentage).
    """
    if baseline == 0:
        return None
    return (current - baseline) / baseline * 100.0


def _incomparability_reason(baseline: dict, current: dict) -> str | None:
    """Return a human reason if the two runs are NOT comparable, else None."""
    reasons: list[str] = []
    for key in ("snapshot_id", "embedding_model"):
        b = baseline.get(key)
        c = current.get(key)
        if b is not None and c is not None and b != c:
            reasons.append(f"{key} mismatch (baseline={b!r} current={c!r})")
    return "; ".join(reasons) if reasons else None


def compare_to_baseline(
    baseline: dict,
    current: dict,
    *,
    tolerance_pct: float = 15.0,
) -> dict:
    """Compare a current perf run against a committed baseline.

    Args:
        baseline: parsed ``perf_baseline.json`` — must carry ``snapshot_id``,
            ``embedding_model`` and an ``aggregated`` dict of metric->value.
        current: the current run's report, same shape.
        tolerance_pct: a metric is flagged when its delta EXCEEDS this percent
            (per §2.3: ``baseline + max(15%, noise-band)``; the caller can raise
            this once a per-metric noise band is known).

    Returns a dict:
        {
          "comparable": bool,
          "incomparable_reason": str | None,
          "tolerance_pct": float,
          "diff": { metric: {"baseline", "current", "delta_pct", "flagged"} },
          "any_flagged": bool,
        }

    RECORD-ONLY: never raises on a regression; ``any_flagged`` is advisory.
    """
    reason = _incomparability_reason(baseline, current)
    comparable = reason is None

    base_agg = baseline.get("aggregated", {})
    cur_agg = current.get("aggregated", {})

    diff: dict[str, dict] = {}
    any_flagged = False

    for key in METRIC_KEYS:
        if key not in base_agg or key not in cur_agg:
            # No baseline (or no current value) for this metric — skip cleanly.
            continue
        if base_agg[key] is None or cur_agg[key] is None:
            # Metric not recorded this run (e.g. CE cache fully hit, delta=0) — skip.
            continue
        b = float(base_agg[key])
        c = float(cur_agg[key])
        delta = pct_delta(b, c)
        # Flag only when comparable, delta is defined, and it exceeds tolerance.
        # Improvements (delta <= 0) are never flagged.
        flagged = bool(comparable and delta is not None and delta > tolerance_pct)
        if flagged:
            any_flagged = True
        diff[key] = {
            "baseline": b,
            "current": c,
            "delta_pct": delta,
            "flagged": flagged,
        }

    return {
        "comparable": comparable,
        "incomparable_reason": reason,
        "tolerance_pct": tolerance_pct,
        "diff": diff,
        "any_flagged": any_flagged,
    }


# ── CE dead-metric detection ──────────────────────────────────────────────────

#: Explanation emitted when d_count==0 (embed-service histogram not fed by
#: recall's in-process CE path, ADR-0078 / T2 in-process move).
CE_DEAD_STATUS = (
    "unavailable — recall CE runs in-process, embed rerank histogram"
    " not fed (see issue #50 / ADR-0078)"
)


def ce_metric_status(
    before: tuple[float, float] | None,
    after: tuple[float, float] | None,
    metrics_url_configured: bool = True,
) -> tuple[float | None, int | None, str]:
    """Derive (ce_mean_ms, ce_calls, ce_metric_status) from before/after scrapes.

    Args:
        before: ``(sum_seconds, count)`` scraped before the run, or ``None``.
        after:  ``(sum_seconds, count)`` scraped after the run, or ``None``.
        metrics_url_configured: whether a backend metrics URL was provided.

    Returns a 3-tuple ``(ce_mean_ms, ce_calls, status_string)`` where
    ``ce_mean_ms`` is ``None`` when CE is unavailable or d_count==0.

    The three distinct status values:

    * ``"available"`` — d_count > 0, mean computed (embed-service /rerank path).
    * ``CE_DEAD_STATUS`` — both scraped but d_count==0 (in-process CE, ADR-0078).
    * ``"unavailable — backend /metrics scrape failed"`` — URL set but scrape failed.
    * ``"unavailable — YADGAR_BACKEND_METRICS_URL not configured"`` — no URL given.
    """
    if before is not None and after is not None:
        d_sum = after[0] - before[0]
        d_count = after[1] - before[1]
        if d_count > 0:
            mean_ms = round((d_sum / d_count) * 1000.0, 2)
            return mean_ms, int(d_count), "available"
        else:
            return None, 0, CE_DEAD_STATUS
    elif metrics_url_configured:
        return None, None, "unavailable — backend /metrics scrape failed"
    else:
        return None, None, "unavailable — YADGAR_BACKEND_METRICS_URL not configured"


# ── P-SB (#50): CE source selection — observe-stage primary, legacy fallback ──

#: Status strings for the re-pointed CE source (P-SB §3.5).
CE_OBSERVE_STAGE_STATUS = "observe-stage source (current)"
CE_LEGACY_STATUS = "embed-rerank source (legacy)"
CE_SOURCE_UNAVAILABLE_STATUS = "unavailable — no CE source fed (observe-stage + legacy both count==0)"


def _mean_ms_from_scrapes(
    scrapes: tuple[tuple[float, float] | None, tuple[float, float] | None] | None,
) -> tuple[float | None, int | None]:
    """Return (mean_ms, count) from a (before, after) pair of (sum_seconds, count)
    scrapes, or (None, None) when unusable (either scrape None, or d_count<=0)."""
    if scrapes is None:
        return None, None
    before, after = scrapes
    if before is None or after is None:
        return None, None
    d_sum = after[0] - before[0]
    d_count = after[1] - before[1]
    if d_count <= 0:
        return None, None
    return round((d_sum / d_count) * 1000.0, 2), int(d_count)


def ce_source_status(
    observe_stage: tuple[tuple[float, float] | None, tuple[float, float] | None] | None,
    legacy: tuple[tuple[float, float] | None, tuple[float, float] | None] | None,
) -> tuple[float | None, int | None, str]:
    """Pick the CE latency source and return (ce_mean_ms, ce_calls, status).

    Priority (P-SB §3.5 / #50):
      1. observe-stage histogram
         (yadgar_observe_stage_duration_seconds{stage="retrieval.ce.score_ce_cached"})
         — the current in-process CE source, bridged onto :8001. PRIMARY.
      2. legacy embed-rerank histogram
         (yadgar_embed_rerank_duration_seconds{mode="ce"}) — only fed by old
         RemoteMLClient /rerank daemons (ADR-0105 portability note). FALLBACK.
      3. neither fed → unavailable.

    Each arg is a ``(before, after)`` pair of ``(sum_seconds, count)`` scrapes (or
    None if that endpoint/family was absent). observe-stage WINS whenever its
    d_count>0, even if legacy is also fed.
    """
    obs_mean, obs_calls = _mean_ms_from_scrapes(observe_stage)
    if obs_mean is not None:
        return obs_mean, obs_calls, CE_OBSERVE_STAGE_STATUS

    legacy_mean, legacy_calls = _mean_ms_from_scrapes(legacy)
    if legacy_mean is not None:
        return legacy_mean, legacy_calls, CE_LEGACY_STATUS

    return None, None, CE_SOURCE_UNAVAILABLE_STATUS


# ── Prometheus text-format parsing (for CE-span capture from /metrics) ────────

_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_labels(label_blob: str) -> dict[str, str]:
    return dict(_LABEL_RE.findall(label_blob))


def parse_prom_metric(text: str, metric_name: str, labels: dict[str, str]) -> float | None:
    """Extract a single Prometheus sample value by name + label subset.

    Matches the first line whose metric name equals ``metric_name`` and whose
    labels are a superset of ``labels``. Returns the float value, or ``None`` if
    no matching sample is present.

    Used to read ``yadgar_embed_rerank_duration_seconds_{sum,count}{mode="ce"}``
    from the backend ``/metrics`` endpoint so the harness can delta CE latency
    across a run without standing up Tempo.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            name, rest = line.split("{", 1)
            label_blob, _, value_str = rest.partition("}")
            sample_labels = _parse_labels(label_blob)
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            name, value_str = parts[0], parts[1]
            sample_labels = {}
        if name != metric_name:
            continue
        if any(sample_labels.get(k) != v for k, v in labels.items()):
            continue
        value_str = value_str.strip().split()[0] if value_str.strip() else ""
        try:
            return float(value_str)
        except ValueError:
            return None
    return None
