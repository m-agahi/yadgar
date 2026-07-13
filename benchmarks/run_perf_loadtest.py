#!/usr/bin/env python3
"""#79 recall-latency load-test harness (record-only).

Design authority: ``docs/plans/perf-loadtest-contract-2026-06-30.md`` +
``docs/plans/obs-velocity-completion-2026-07-04.md`` PART B §3.2.

CANONICAL RECALL-WALL METRIC (cross-version):
  ``time.perf_counter()`` wall over the ``/mcp`` JSON-RPC envelope → recall
  p50/p95 (ms).  This is the ONLY version-portable recall-wall metric: it
  requires no backend Prometheus metric to exist and measures the same path
  regardless of architecture version.  Use it for ALL cross-version comparisons,
  including the upcoming GTE-vs-Ettin sweep.

  The Prometheus histogram ``yadgar_recall_duration_ms`` on :8765 is an
  equivalent server-side signal (present since v5.96+, MCP transport overhead
  ≈ 0 per RCA 2026-07-13) — prefer it for single-version characterisation where
  scraping is convenient.

CE METRIC STATUS (P-SB re-point, #50):
  The PRIMARY CE source is now the ``@observe`` stage histogram
  ``yadgar_observe_stage_duration_seconds{stage="retrieval.ce.score_ce_cached"}``
  on the backend :8001 — this family IS fed by the in-process CE path
  (``LocalMLClient.score_cross_encoder`` → ``score_ce_cached``) once P-SB promoted
  that fn to ``tier="stage"`` and bridged the shared ``@observe`` registry onto
  :8001. ``ce_mean_ms`` = d_sum/d_count × 1000 (per CE pass);
  ``ce_wall_ms_per_recall`` = d_sum/N × 1000 (per-recall CE wall — the number the
  pre-P-SB harness pretended to give).

  The legacy ``yadgar_embed_rerank_duration_seconds{mode="ce"}`` histogram (fed
  ONLY by the embed-service ``POST /rerank`` HTTP endpoint, i.e. old
  ``RemoteMLClient`` daemons) is kept as a labelled FALLBACK for cross-version
  portability (ADR-0105). The pure ``perf_contract.ce_source_status`` picks
  observe-stage whenever fed, else legacy, else unavailable; ``ce_metric_status``
  records which source was used.

  The prior "CE = 70-90% / 7-8.5s of recall wall" claim came from the
  split-container era (``RemoteMLClient`` → ``/rerank`` HTTP). Per the RCA
  (2026-07-13), CE ≈ 25% of one cold recall wall (6.2s); signal-gather (~45%)
  and DB hydration (~23%) are the larger cold terms.

WHAT IT MEASURES:
  * Fires N representative ``recall`` calls at a running daemon over the same
    stateless JSON-RPC ``/mcp`` envelope the MCP tool uses (reused verbatim from
    ``yadgar/tests/e2e/test_offload_e2e.py``), timing each with
    ``time.perf_counter()`` → recall p50/p95 (ms).
  * Captures per-recall CE latency via the ``@observe`` CE stage histogram delta
    (P-SB #50), falling back to the legacy embed-rerank histogram for old daemons;
    emits ``ce_mean_ms: null`` with an explicit status field only when NEITHER
    source is fed.
  * Emits a JSON report mirroring the run_longmemeval schema, and runs the pure
    ``perf_contract`` checker against the committed baseline (RECORD-ONLY: it
    prints the diff + any flags but ALWAYS exits 0; Phase-2 gating is a later PR).

SCOPE (honest): this is the MINIMAL record-only skeleton — sequential recall
latency + CE-span capture + checker + report. The plan's full workload contract
(§2.1: W0 warm-up / A sequential / B 8-concurrent backpressure / C wiki_query /
D memorize→drain / E /health/live-under-load) and the quiesced-snapshot-pin
``cp -r`` machinery (§2.4) are NOT built here — they are documented follow-ons.
Phase B (concurrency) is the next increment; the driver already supports it via
``_call_tool`` threads.

INVOCATION (drives an ALREADY-RUNNING daemon; does NOT spawn one — keeps the
harness out of the surrealkv-lock / snapshot-copy business for v1):

    export YADGAR_DAEMON_URL=http://127.0.0.1:8765          # core daemon /mcp
    export YADGAR_BACKEND_METRICS_URL=http://127.0.0.1:8001/metrics  # CE source
    export YADGAR_PERF_SNAPSHOT_ID=prod-live-2026-07-04      # identity stamp
    make perf            # or: python benchmarks/run_perf_loadtest.py

If ``YADGAR_DAEMON_URL`` is unset the harness SKIPS with a clear reason (exit 0)
— so ``make perf`` / the CI ``workflow_dispatch`` are safe no-ops without a live
daemon. This module imports nothing from ``yadgar`` and loads no model.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# perf_contract is a sibling pure module (stdlib only).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from benchmarks.perf_contract import (  # noqa: E402
    ce_source_status,
    compare_to_baseline,
    parse_prom_metric,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _REPO_ROOT / "benchmarks" / "reports" / "perf_baseline.json"
_REPORTS_DIR = _REPO_ROOT / "benchmarks" / "reports"
_QUERIES_PATH = _REPO_ROOT / "benchmarks" / "golden" / "perf_queries.jsonl"

# P-SB (#50): primary CE source is the @observe stage histogram (in-process CE,
# bridged onto :8001). Legacy embed-rerank probe kept as a fallback for old
# RemoteMLClient daemons (ADR-0105 portability note).
_CE_STAGE_METRIC = "yadgar_observe_stage_duration_seconds"
_CE_STAGE_LABELS = {"stage": "retrieval.ce.score_ce_cached"}
_CE_LEGACY_METRIC = "yadgar_embed_rerank_duration_seconds"
_CE_LEGACY_LABELS = {"mode": "ce"}

# Fallback query mix if the committed .jsonl is absent (kept tiny + realistic).
_DEFAULT_QUERIES = [
    "recall latency CE bottleneck spreading activation",
    "offload freeze fix daemon health",
    "observability standard I33 tri-signal coverage",
    "fusion cross-encoder rerank batch N+1",
    "memorize write gate similarity drain",
]


def _load_queries() -> list[str]:
    if _QUERIES_PATH.exists():
        out: list[str] = []
        for line in _QUERIES_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                q = obj.get("query") if isinstance(obj, dict) else obj
            except json.JSONDecodeError:
                q = line
            if isinstance(q, str) and q:
                out.append(q)
        if out:
            return out
    return list(_DEFAULT_QUERIES)


def _call_recall(daemon_url: str, query: str, *, req_id: int, timeout: float) -> bool:
    """POST a stateless JSON-RPC recall. Return True on a non-error result.

    Envelope reused from yadgar/tests/e2e/test_offload_e2e.py::_call_tool.
    """
    args: dict = {"query": query, "max_results": 5}
    recall_dir = os.environ.get("YADGAR_RECALL_DIRECTORY")
    if recall_dir:
        args["directory"] = recall_dir
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {
                "name": "recall",
                "arguments": args,
            },
        }
    ).encode()
    hdrs: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    auth_token = os.environ.get("YADGAR_MCP_AUTH_TOKEN")
    if auth_token:
        hdrs["Authorization"] = f"Bearer {auth_token}"
    req = urllib.request.Request(
        daemon_url.rstrip("/") + "/mcp"
        if not daemon_url.rstrip("/").endswith("/mcp")
        else daemon_url,
        data=body,
        headers=hdrs,
    )
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode()
    except Exception:
        return False
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                payload = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                return False
            return "error" not in payload
    return False


def _scrape_metrics_text(metrics_url: str) -> str | None:
    """Fetch the backend /metrics exposition text, or None on failure."""
    try:
        return urllib.request.urlopen(metrics_url, timeout=5.0).read().decode()
    except Exception:
        return None


def _totals_from_text(text: str, metric: str, labels: dict[str, str]) -> tuple[float, float] | None:
    """Return (sum_seconds, count) for a histogram family+labels, or None if absent."""
    m_sum = parse_prom_metric(text, metric + "_sum", labels)
    m_count = parse_prom_metric(text, metric + "_count", labels)
    if m_sum is None or m_count is None:
        return None
    return m_sum, m_count


def _scrape_ce_sources(metrics_url: str) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Scrape both CE sources from one /metrics text.

    Returns ``(observe_stage_totals, legacy_totals)`` — each ``(sum, count)`` or
    None. One scrape covers both families (the observe-stage is bridged onto the
    same backend endpoint by P-SB).
    """
    text = _scrape_metrics_text(metrics_url)
    if text is None:
        return None, None
    observe_stage = _totals_from_text(text, _CE_STAGE_METRIC, _CE_STAGE_LABELS)
    legacy = _totals_from_text(text, _CE_LEGACY_METRIC, _CE_LEGACY_LABELS)
    return observe_stage, legacy


def _percentiles(latencies_ms: list[float]) -> dict[str, float]:
    lat = sorted(latencies_ms)
    p50 = statistics.median(lat)
    if len(lat) >= 20:
        p95 = statistics.quantiles(lat, n=20)[18]
    else:
        p95 = max(lat)
    return {
        "recall_p50_ms": round(p50, 2),
        "recall_p95_ms": round(p95, 2),
        "recall_mean_ms": round(statistics.mean(lat), 2),
    }


def _load_baseline() -> dict | None:
    if _BASELINE_PATH.exists():
        try:
            return json.loads(_BASELINE_PATH.read_text())
        except json.JSONDecodeError:
            return None
    return None


def run(
    daemon_url: str,
    metrics_url: str | None,
    *,
    n_recalls: int,
    n_warmup: int,
    snapshot_id: str,
    embedding_model: str,
    tolerance_pct: float,
) -> dict:
    queries = _load_queries()
    timeout = float(os.environ.get("YADGAR_PERF_TIMEOUT", "60"))

    # Warm-up (discarded from stats): JIT / cache warm.
    for i in range(n_warmup):
        _call_recall(daemon_url, queries[i % len(queries)], req_id=i, timeout=timeout)

    # P-SB (#50): scrape BOTH CE sources (observe-stage primary, legacy fallback).
    ce_before = _scrape_ce_sources(metrics_url) if metrics_url else (None, None)

    latencies_ms: list[float] = []
    errors = 0
    for i in range(n_recalls):
        q = queries[i % len(queries)]
        t0 = time.perf_counter()
        ok = _call_recall(daemon_url, q, req_id=1000 + i, timeout=timeout)
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)
        if not ok:
            errors += 1

    ce_after = _scrape_ce_sources(metrics_url) if metrics_url else (None, None)

    agg = _percentiles(latencies_ms)
    agg["n_recalls"] = n_recalls
    agg["error_rate"] = round(errors / n_recalls, 4) if n_recalls else 0.0

    # CE-span budget: derived via the pure ce_source_status helper in perf_contract.
    #
    # P-SB (#50) re-point: the PRIMARY source is now the @observe stage histogram
    # yadgar_observe_stage_duration_seconds{stage="retrieval.ce.score_ce_cached"}
    # (bridged onto :8001), which the in-process CE path DOES feed. The legacy
    # yadgar_embed_rerank_duration_seconds{mode="ce"} probe is kept as a fallback
    # for old RemoteMLClient daemons (ADR-0105 portability). ce_source_status picks
    # observe-stage whenever fed, else legacy, else unavailable.
    obs_before, legacy_before = ce_before
    obs_after, legacy_after = ce_after
    ce_mean, ce_calls, ce_status = ce_source_status(
        (obs_before, obs_after) if metrics_url else None,
        (legacy_before, legacy_after) if metrics_url else None,
    )
    if not metrics_url:
        ce_status = "unavailable — YADGAR_BACKEND_METRICS_URL not configured"
    agg["ce_mean_ms"] = ce_mean
    agg["ce_calls"] = ce_calls
    agg["ce_metric_status"] = ce_status
    # ce_wall_ms_per_recall: total CE wall attributed per recall (the number the old
    # harness pretended to give). d_sum / N × 1000 from whichever source was used.
    if ce_mean is not None and n_recalls:
        _src = (obs_after, obs_before) if "observe-stage" in ce_status else (legacy_after, legacy_before)
        _after, _before = _src
        if _after is not None and _before is not None:
            agg["ce_wall_ms_per_recall"] = round((_after[0] - _before[0]) / n_recalls * 1000.0, 2)
    if ce_mean is None and metrics_url:
        print(
            "WARNING: ce_mean_ms is null — neither the @observe CE stage histogram"
            " (yadgar_observe_stage_duration_seconds{stage='retrieval.ce.score_ce_cached'})"
            " nor the legacy embed-rerank histogram was fed (both d_count==0). Confirm"
            " the P-SB bridge is deployed (backend 5.44.0+) and at least one recall ran."
            " Falling back to yadgar_recall_duration_ms (:8765) for the recall wall.",
            file=sys.stderr,
        )

    report = {
        "benchmark": "perf-loadtest",
        "timestamp": datetime.now(UTC).isoformat(),
        "workload": {
            "n_warmup": n_warmup,
            "n_recalls": n_recalls,
            "concurrency": 1,
            "note": "v1 record-only: sequential recall + CE-span capture only",
        },
        "aggregated": agg,
        "snapshot_id": snapshot_id,
        "embedding_model": embedding_model,
        "reproducibility": {
            "snapshot_id": snapshot_id,
            "embedding_model": embedding_model,
            "daemon_url": daemon_url,
            "backend_metrics_url": metrics_url,
            "run_date_utc": datetime.now(UTC).isoformat(),
            "python_version": sys.version.split()[0],
        },
    }

    baseline = _load_baseline()
    if baseline is not None:
        report["baseline_diff"] = compare_to_baseline(baseline, report, tolerance_pct=tolerance_pct)
    else:
        report["baseline_diff"] = {
            "comparable": False,
            "incomparable_reason": "no committed baseline yet (record-only bootstrap)",
            "diff": {},
            "any_flagged": False,
        }
    return report


def main() -> int:
    daemon_url = os.environ.get("YADGAR_DAEMON_URL")
    if not daemon_url:
        print(
            "SKIP: YADGAR_DAEMON_URL unset — perf loadtest needs a running "
            "daemon.\n  export YADGAR_DAEMON_URL=http://127.0.0.1:8765\n"
            "  export YADGAR_BACKEND_METRICS_URL=http://127.0.0.1:8001/metrics"
        )
        return 0  # record-only: a missing daemon is a skip, not a failure

    metrics_url = os.environ.get("YADGAR_BACKEND_METRICS_URL")
    if not metrics_url:
        print(
            "WARN: YADGAR_BACKEND_METRICS_URL unset — CE-span budget will be "
            "recorded as null (no CE regression signal this run)."
        )

    report = run(
        daemon_url,
        metrics_url,
        n_recalls=int(os.environ.get("YADGAR_PERF_N", "30")),
        n_warmup=int(os.environ.get("YADGAR_PERF_WARMUP", "5")),
        snapshot_id=os.environ.get("YADGAR_PERF_SNAPSHOT_ID", "unpinned-live"),
        embedding_model=os.environ.get("YADGAR_PERF_EMBED_MODEL", "gte-modernbert"),
        tolerance_pct=float(os.environ.get("YADGAR_PERF_TOLERANCE_PCT", "15")),
    )

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_path = _REPORTS_DIR / f"perf_loadtest_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str))

    agg = report["aggregated"]
    print(f"\n=== perf-loadtest (record-only) → {out_path.name} ===")
    print(f"  recall p50 = {agg['recall_p50_ms']} ms")
    print(f"  recall p95 = {agg['recall_p95_ms']} ms")
    ce_status = agg.get("ce_metric_status", "unknown")
    ce_val = agg.get("ce_mean_ms")
    if ce_val is not None:
        print(f"  CE mean    = {ce_val} ms (over {agg.get('ce_calls')} CE calls)")
    else:
        print(f"  CE mean    = null [{ce_status}]")
    print(f"  error rate = {agg['error_rate']}")
    diff = report["baseline_diff"]
    if diff.get("comparable"):
        for metric, d in diff["diff"].items():
            flag = " ⚠ FLAGGED" if d["flagged"] else ""
            dp = d["delta_pct"]
            dp_str = f"{dp:+.1f}%" if dp is not None else "n/a (zero baseline)"
            print(f"  Δ {metric}: {dp_str} vs baseline{flag}")
        if diff["any_flagged"]:
            print("  NOTE: regression(s) flagged — RECORD-ONLY, not gating (Phase 1).")
    else:
        print(f"  baseline_diff: not comparable — {diff.get('incomparable_reason')}")
    return 0  # RECORD-ONLY: never gate the process exit.


if __name__ == "__main__":
    raise SystemExit(main())
