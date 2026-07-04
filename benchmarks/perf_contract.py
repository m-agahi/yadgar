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
