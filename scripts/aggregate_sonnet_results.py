#!/usr/bin/env python3
"""Aggregate Sonnet 500q incremental JSONL into final benchmark JSON.

Run after the full 500q run completes (JSONL has 500 lines).

Usage:
    python3 scripts/aggregate_sonnet_results.py

Outputs:
    benchmarks/results/longmemeval_v5.26.0_s_full.json  (updated/final)
    Prints per-type accuracy table to stdout
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
JSONL = REPO / "benchmarks/results/longmemeval_v5.26.0_s_full_hypotheses.jsonl"
OUTPUT = REPO / "benchmarks/results/longmemeval_v5.26.0_s_full.json"

QUESTION_TYPES = [
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
]

RETRIEVAL_METRICS = ["recall@5", "recall@10", "recall@50", "ndcg@5", "ndcg@10", "ndcg@50", "mrr"]


def _parse_jsonl_line(line: str) -> dict | None:
    """Parse one JSONL line; return None on any error."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def load_jsonl(path: Path) -> list[dict]:
    """Load all valid JSON objects from a JSONL file."""
    results = []
    with open(path) as f:
        for line in f:
            entry = _parse_jsonl_line(line)
            if entry is not None:
                results.append(entry)
    return results


def _avg_metrics(queries: list[dict], metrics: list[str]) -> dict:
    """Compute mean of each metric across queries that have it."""
    out: dict = {}
    for metric in metrics:
        vals = [q.get(metric, 0) for q in queries if metric in q]
        if vals:
            out[metric] = round(sum(vals) / len(vals), 4)
    return out


def _type_agg(queries: list[dict]) -> dict:
    """Build aggregation dict for one question type."""
    retrieval_q = [q for q in queries if not q.get("is_abstention") and "mrr" in q]
    correct_count = sum(1 for q in queries if q.get("correct", False))
    agg: dict = {
        "count": len(queries),
        "qa_accuracy": round(correct_count / len(queries), 4) if queries else 0,
        "qa_correct": correct_count,
        "qa_total": len(queries),
    }
    if retrieval_q:
        agg.update(_avg_metrics(retrieval_q, RETRIEVAL_METRICS))
    return agg


def _overall_agg(per_query: list[dict]) -> dict:
    """Build overall aggregation dict across all questions."""
    all_retrieval = [q for q in per_query if not q.get("is_abstention") and "mrr" in q]
    all_correct = sum(1 for q in per_query if q.get("correct", False))
    total = len(per_query)
    agg: dict = {
        "count": total,
        "qa_accuracy": round(all_correct / total, 4) if total else 0,
        "qa_correct": all_correct,
        "qa_total": total,
    }
    if all_retrieval:
        agg.update(_avg_metrics(all_retrieval, RETRIEVAL_METRICS))
    return agg


def _abstention_agg(per_query: list[dict]) -> dict | None:
    """Build abstention-only aggregation, or None if no abstention questions."""
    abs_q = [q for q in per_query if q.get("is_abstention")]
    if not abs_q:
        return None
    abs_correct = sum(1 for q in abs_q if q.get("correct", False))
    return {
        "count": len(abs_q),
        "qa_accuracy": round(abs_correct / len(abs_q), 4),
        "qa_correct": abs_correct,
    }


def aggregate(per_query: list[dict]) -> dict:
    """Compute per-type and overall aggregation over per_query results."""
    by_type: dict[str, list[dict]] = {}
    for qr in per_query:
        qtype = qr.get("question_type", "unknown")
        by_type.setdefault(qtype, []).append(qr)

    agg = {qtype: _type_agg(queries) for qtype, queries in sorted(by_type.items())}
    agg["overall"] = _overall_agg(per_query)

    abs_agg = _abstention_agg(per_query)
    if abs_agg is not None:
        agg["abstention"] = abs_agg

    return agg


def print_table(agg: dict) -> None:
    """Print formatted accuracy table to stdout."""
    header = (
        f"{'Type':<30} {'Count':>5} {'MRR':>7} {'R@10':>7}"
        f" {'NDCG@10':>7} {'QA Acc':>7} {'Correct':>8}"
    )
    print("\n" + "=" * 80)
    print("LongMemEval v5.26.0 — Sonnet 4.6 — 500q Full Results")
    print("=" * 80)
    print(header)
    print("-" * len(header))

    for qtype in QUESTION_TYPES + ["overall"]:
        if qtype not in agg:
            continue
        a = agg[qtype]
        print(
            f"{qtype:<30} {a['count']:>5} "
            f"{a.get('mrr', 0):>7.3f} {a.get('recall@10', 0):>7.3f} "
            f"{a.get('ndcg@10', 0):>7.3f} {a.get('qa_accuracy', 0):>7.1%} "
            f"{a.get('qa_correct', 0):>4}/{a.get('qa_total', 0):<4}"
        )

    if "abstention" in agg:
        a = agg["abstention"]
        print(
            f"{'abstention':<30} {a['count']:>5} "
            f"{'N/A':>7} {'N/A':>7} {'N/A':>7} {a.get('qa_accuracy', 0):>7.1%} "
            f"{a.get('qa_correct', 0):>4}/{a['count']:<4}"
        )
    print()


def main() -> None:
    """Entry point."""
    if not JSONL.exists():
        print(f"ERROR: JSONL not found: {JSONL}")
        sys.exit(1)

    per_query = load_jsonl(JSONL)
    print(f"Loaded {len(per_query)} results from JSONL")

    if len(per_query) < 500:
        print(f"WARNING: only {len(per_query)}/500 questions complete — run may not be finished")
        ans = input("Continue anyway? [y/N] ").strip().lower()
        if ans != "y":
            sys.exit(1)

    agg = aggregate(per_query)
    print_table(agg)

    existing: dict = {}
    if OUTPUT.exists():
        with open(OUTPUT) as f:
            existing = json.load(f)

    existing["per_query"] = per_query
    existing["aggregated"] = agg
    existing["total_questions"] = len(per_query)

    with open(OUTPUT, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    print(f"Saved: {OUTPUT}")

    overall = agg.get("overall", {})
    acc = overall.get("qa_accuracy", 0)
    correct = overall.get("qa_correct", 0)
    total = overall.get("qa_total", 0)
    print(f"\nHeadline: {acc:.1%} ({correct}/{total}) overall accuracy")
    print("\nCopy above table into commit message for Step 10.")


if __name__ == "__main__":
    main()
