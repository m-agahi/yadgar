"""Yadgar native eval harness — Phase 0 measurement (v6 Quality Foundation).

Runs a golden set of (query, relevant_memory_ids[]) pairs through yadgar recall
against an isolated/local SurrealDB instance and computes:

  - recall@k   (k = 1, 5, 10, 20)
  - MRR        (mean reciprocal rank)
  - nDCG@k     (k = 1, 5, 10, 20)
  - latency    p50 / p95 per query

Output: a summary table printed to stdout + a machine-readable JSON report written
to benchmarks/reports/<name>.json.

# BOOTSTRAP NOTE: the default golden set is auto-drafted and REQUIRES HUMAN CURATION.
# Results on the bootstrap set are informational only — not a trusted quality signal
# until the golden set has been reviewed and curated.

Reuse policy (v6 plan §0.0):
  - Metric primitives: compute_recall() / compute_ndcg() from run_longmemeval
  - MRR loop: from run_longmemeval.evaluate_retrieval
  - Isolated SurrealDB: spawn_surreal_for_benchmark / YADGAR_DB_URL server-mode
  - Settings factory: make_benchmark_settings from run_longmemeval

Usage:
  # Against fresh isolated SurrealDB (requires `surreal` on PATH):
  python benchmarks/run_eval.py

  # Against existing server (skip spawn):
  YADGAR_DB_URL=http://127.0.0.1:8000 python benchmarks/run_eval.py

  # Custom golden set:
  python benchmarks/run_eval.py --golden benchmarks/golden/my_set.jsonl

  # Custom report output:
  python benchmarks/run_eval.py --output benchmarks/reports/my_run.json

  # Dry-run (print config, skip scoring):
  python benchmarks/run_eval.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Reused primitives from run_longmemeval ─────────────────────────────────────

from benchmarks.run_longmemeval import (
    compute_ndcg,
    compute_recall,
    get_surreal_version,
    get_yadgar_commit,
    make_benchmark_settings,
    spawn_surreal_for_benchmark,
)
from yadgar._surreal_runner import teardown_surreal_proc
from yadgar.config import Settings
from yadgar.curation import MemoryCurator
from yadgar.embeddings import EmbeddingEngine
from yadgar.knowledge_graph import KnowledgeGraph
from yadgar.retrieval import Retriever
from yadgar.storage import StorageEngine
from yadgar.thermodynamics import MemoryThermodynamics

# ── Constants ───────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent
_GOLDEN_DEFAULT = _REPO_ROOT / "benchmarks" / "golden" / "golden_set.jsonl"
_REPORTS_DIR = _REPO_ROOT / "benchmarks" / "reports"
_K_VALUES = [1, 5, 10, 20]

# ── Golden set I/O ─────────────────────────────────────────────────────────────


def load_golden_set(path: Path) -> tuple[list[dict], dict]:
    """Load golden pairs from a JSONL file.

    Returns (pairs, header) where header is the metadata sentinel line (query_id=HEADER).
    Filters out HEADER lines and pairs with no relevant IDs (neither memory nor wiki).

    v6-T6 schema additions:
      relevant_wiki_slugs[]  — wiki slugs relevant for this query (mixed-type cases)
      type                   — "memory" | "wiki" | "all" annotation (informational,
                               used to segment reporting; not used for routing)
    """
    pairs: list[dict] = []
    header: dict = {}
    skipped = 0

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("query_id") == "HEADER":
                header = obj
                continue
            mem_ids = obj.get("relevant_memory_ids", [])
            wiki_slugs = obj.get("relevant_wiki_slugs", [])
            if not mem_ids and not wiki_slugs:
                skipped += 1
                continue
            pairs.append(obj)

    if skipped:
        print(f"  Skipped {skipped} pairs with empty relevant_memory_ids and relevant_wiki_slugs")

    is_bootstrap = header.get("bootstrap", False)
    curated_pairs = [p for p in pairs if not p.get("needs_curation", False)]
    bootstrap_pairs = [p for p in pairs if p.get("needs_curation", False)]

    print(
        f"  Loaded {len(pairs)} pairs ({len(curated_pairs)} curated, {len(bootstrap_pairs)} bootstrap/uncurated)"
    )
    if is_bootstrap:
        print("  WARNING: golden set is a BOOTSTRAP — auto-drafted, REQUIRES HUMAN CURATION.")
        print("  Results are informational only until the set is reviewed.")

    return pairs, header


# ── Self-seeding ───────────────────────────────────────────────────────────────


def seed_pairs_into_storage(
    pairs: list[dict],
    storage: StorageEngine,
    embeddings: EmbeddingEngine,
    settings: Settings,
) -> list[dict]:
    """Ingest golden pair content into an isolated DB and remap relevant_memory_ids.

    Each pair's `memory_content_preview` (or `content` if present) is stored as a
    real memory row.  The returned list is a shallow copy of `pairs` with
    `relevant_memory_ids` replaced by the real integer IDs assigned by SurrealDB.

    This makes the harness self-contained: spawning an isolated SurrealDB
    and running `make eval` on a clean checkout produces real, non-zero metrics.
    """
    remapped: list[dict] = []
    for pair in pairs:
        content = pair.get("content") or pair.get("memory_content_preview") or pair.get("query")
        memory_payload = {
            "content": content,
            "directory_context": "eval-bootstrap",
            "tags": pair.get("tags", []),
            "heat": 1.0,
        }
        try:
            real_id = storage.insert_memory(
                memory_payload,
                embeddings_engine=embeddings,
                settings=settings,
            )
        except Exception as exc:
            print(f"  WARNING: seed failed for {pair['query_id']}: {exc}", file=sys.stderr)
            remapped.append(pair)
            continue
        new_pair = dict(pair)
        new_pair["relevant_memory_ids"] = [real_id]
        new_pair["_seeded_id"] = real_id
        remapped.append(new_pair)
    return remapped


# ── Engine factory ─────────────────────────────────────────────────────────────


def create_eval_engines(settings: Settings):
    """Create minimal engine set for eval (no data dir — server-mode only)."""
    db_path = os.environ.get("YADGAR_DB_PATH", settings.DB_PATH)
    storage = StorageEngine(db_path)
    embeddings = EmbeddingEngine(settings.EMBEDDING_MODEL)
    kg = KnowledgeGraph(storage, settings)
    thermo = MemoryThermodynamics(storage, embeddings, settings)
    retriever = Retriever(storage, embeddings, kg, settings)
    curator = MemoryCurator(storage, embeddings, thermo, settings)
    return storage, embeddings, retriever, curator


# ── Per-query eval ─────────────────────────────────────────────────────────────


def compute_diversity(results: list[dict]) -> int | None:
    """Count the number of UNIQUE astrocyte domains across the top-k results.

    Intended for landscape mode recall where results carry ``voting_domains``.
    Returns None (gracefully) when no result has ``voting_domains`` — this keeps
    standard eval runs unaffected (landscape mode is not in the default eval loop).

    Used as a future before/after metric for landscape vs balanced recall.

    Args:
        results: Per-query recall output (list of memory dicts).

    Returns:
        Count of unique astrocyte domain names present across all voting_domains
        fields, or None if no result carries voting_domains.
    """
    all_domains: set[str] = set()
    has_any = False
    for r in results:
        domains = r.get("voting_domains")
        if isinstance(domains, list):
            all_domains.update(domains)
            has_any = True
    return len(all_domains) if has_any else None


def _extract_retrieved_keys(results: list[dict]) -> list[str]:
    """Extract a unified namespace key list from retriever results.

    Keys use a prefixed namespace so memory IDs and wiki slugs can coexist in
    the same string list consumed by compute_recall / compute_ndcg / MRR:
      "mem:<int_id>"   — for memory rows (type=memory or no _source tag)
      "wiki:<slug>"    — for wiki rows (_source="wiki")

    v6-T6: used by evaluate_pair for both memory-only and mixed-type scoring.
    The same prefix scheme is used in gold_keys (built in evaluate_pair) so the
    primitives from run_longmemeval work unmodified.
    """
    keys: list[str] = []
    for m in results:
        if m.get("_source") == "wiki":
            slug = m.get("slug") or m.get("id", "")
            if slug:
                keys.append(f"wiki:{slug}")
        else:
            mid = m.get("id")
            if mid is None:
                continue
            try:
                raw = mid
                if isinstance(raw, str) and ":" in raw:
                    raw = raw.split(":", 1)[1]
                keys.append(f"mem:{int(raw)}")
            except TypeError, ValueError:
                pass
    return keys


def evaluate_pair(
    pair: dict,
    retriever: Retriever,
    k_values: list[int],
    max_results: int = 50,
) -> dict:
    """Run recall for one golden pair and compute metrics (legacy path).

    Uses memory-id granularity (not session granularity like LongMemEval).

    v6-T6: unified namespace scoring. Gold set may specify:
      - relevant_memory_ids[]   — integer memory IDs mapped to "mem:<id>" keys
      - relevant_wiki_slugs[]   — wiki slug strings mapped to "wiki:<slug>" keys
    Retrieved results are similarly mapped via _extract_retrieved_keys().
    This allows compute_recall / compute_ndcg / MRR from run_longmemeval to work
    across both memory and wiki results with zero primitive changes.

    NOTE: This function routes through retriever.recall() (legacy path) which is
    memory-only.  Wiki-gold pairs will show recall@k=0 on this path regardless
    of the golden set.  Use evaluate_pair_unified() for the fan-out path.

    Returns a metrics dict including latency_ms.
    """
    query = pair["query"]
    # Build unified gold key set (prefixed namespace)
    gold_keys: set[str] = set()
    for mid in pair.get("relevant_memory_ids", []):
        try:
            gold_keys.add(f"mem:{int(mid)}")
        except TypeError, ValueError:
            pass
    for slug in pair.get("relevant_wiki_slugs", []):
        if slug:
            gold_keys.add(f"wiki:{slug}")

    t0 = time.perf_counter()
    try:
        results = retriever.recall(query, max_results=max_results, min_heat=0.0)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "query_id": pair["query_id"],
            "error": str(exc),
            "latency_ms": elapsed_ms,
        }
    elapsed_ms = (time.perf_counter() - t0) * 1000

    retrieved_keys = _extract_retrieved_keys(results)

    metrics: dict = {
        "query_id": pair["query_id"],
        "derivation": pair.get("derivation", "unknown"),
        "type": pair.get("type", "memory"),
        "bootstrap": pair.get("bootstrap", True),
        "needs_curation": pair.get("needs_curation", True),
        "latency_ms": elapsed_ms,
        "retrieved_count": len(retrieved_keys),
        "gold_count": len(gold_keys),
    }

    # recall@k + nDCG@k — reuse primitives from run_longmemeval
    # Unified namespace: both gold and retrieved use prefixed string keys.
    retrieved_str = retrieved_keys
    gold_str = gold_keys

    for k in k_values:
        metrics[f"recall@{k}"] = compute_recall(retrieved_str, gold_str, k)
        metrics[f"ndcg@{k}"] = compute_ndcg(retrieved_str, gold_str, k)

    # MRR
    mrr = 0.0
    for rank, rid in enumerate(retrieved_str):
        if rid in gold_str:
            mrr = 1.0 / (rank + 1)
            break
    metrics["mrr"] = mrr

    # Diversity metric (landscape mode): unique astrocyte domains in top-k.
    # Absent from standard eval runs (voting_domains not present on legacy results).
    diversity = compute_diversity(results)
    if diversity is not None:
        metrics["diversity_domains"] = diversity

    return metrics


# ── Unified-recall eval pair (Step 0 — routes through MCP recall tool) ────────


def evaluate_pair_unified(
    pair: dict,
    directory: str,
    k_values: list[int],
    max_results: int = 50,
    type_filter: str = "all",
) -> dict:
    """Run recall for one golden pair via the MCP recall tool (fan-out path).

    Step 0 fix: unlike evaluate_pair() which calls retriever.recall() (memories
    only), this function calls yadgar.server.tools.recall.recall() — the same
    entry point MCP callers use — so fusion + directory-scoping + wiki results
    are exercised.

    Args:
        pair: Golden pair dict with query, relevant_memory_ids, relevant_wiki_slugs.
        directory: Caller directory passed to recall() (required by v5.65 Fix D).
        k_values: k values for recall@k and nDCG@k.
        max_results: Max results to fetch per query.
        type_filter: "all" | "memory" | "wiki" (Step 5; default "all").

    Returns:
        Metrics dict with recall@k, nDCG@k, mrr, latency_ms (same schema as
        evaluate_pair() so aggregate_metrics() works unchanged).
    """
    import sys as _sys

    # Get the MCP recall tool (registered by @_tool() — module-level attribute)
    # Must go through sys.modules because @_tool() replaces the local name.
    _recall_module = _sys.modules.get("yadgar.server.tools.recall")
    if _recall_module is None:
        import yadgar.server.tools.recall as _recall_module  # noqa: PLC0415

    recall_fn = getattr(_recall_module, "recall")

    query = pair["query"]

    # Build unified gold key set (prefixed namespace — same as evaluate_pair)
    gold_keys: set[str] = set()
    for mid in pair.get("relevant_memory_ids", []):
        try:
            gold_keys.add(f"mem:{int(mid)}")
        except TypeError, ValueError:
            pass
    for slug in pair.get("relevant_wiki_slugs", []):
        if slug:
            gold_keys.add(f"wiki:{slug}")

    t0 = time.perf_counter()
    try:
        # Build kwargs — type= only passed in Step 5 when supported
        kwargs: dict = {"max_results": max_results, "min_heat": 0.0, "directory": directory}
        if type_filter != "all":
            kwargs["type"] = type_filter

        results = recall_fn(query, **kwargs)
    except TypeError:
        # Fallback: older recall() signature without type= (Steps 0-4)
        results = recall_fn(query, max_results=max_results, min_heat=0.0, directory=directory)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "query_id": pair["query_id"],
            "error": str(exc),
            "latency_ms": elapsed_ms,
        }
    elapsed_ms = (time.perf_counter() - t0) * 1000

    retrieved_keys = _extract_retrieved_keys(results)

    metrics: dict = {
        "query_id": pair["query_id"],
        "derivation": pair.get("derivation", "unknown"),
        "type": pair.get("type", "memory"),
        "bootstrap": pair.get("bootstrap", True),
        "needs_curation": pair.get("needs_curation", True),
        "latency_ms": elapsed_ms,
        "retrieved_count": len(retrieved_keys),
        "gold_count": len(gold_keys),
    }

    retrieved_str = retrieved_keys
    gold_str = gold_keys

    for k in k_values:
        metrics[f"recall@{k}"] = compute_recall(retrieved_str, gold_str, k)
        metrics[f"ndcg@{k}"] = compute_ndcg(retrieved_str, gold_str, k)

    mrr = 0.0
    for rank, rid in enumerate(retrieved_str):
        if rid in gold_str:
            mrr = 1.0 / (rank + 1)
            break
    metrics["mrr"] = mrr

    # Diversity metric (landscape mode): unique astrocyte domains in top-k.
    # Absent from standard eval runs (voting_domains not present on unified results).
    diversity = compute_diversity(results)
    if diversity is not None:
        metrics["diversity_domains"] = diversity

    return metrics


# ── Aggregation ────────────────────────────────────────────────────────────────


def aggregate_metrics(per_query: list[dict], k_values: list[int]) -> dict:
    """Aggregate per-query metrics into mean values + latency percentiles."""
    valid = [m for m in per_query if "error" not in m]
    errors = [m for m in per_query if "error" in m]

    if not valid:
        return {"error": "all queries failed"}

    n = len(valid)
    agg: dict = {
        "n_pairs": len(per_query),
        "n_valid": n,
        "n_errors": len(errors),
    }

    # Metric means
    for k in k_values:
        agg[f"recall@{k}"] = sum(m[f"recall@{k}"] for m in valid) / n
        agg[f"ndcg@{k}"] = sum(m[f"ndcg@{k}"] for m in valid) / n
    agg["mrr"] = sum(m["mrr"] for m in valid) / n

    # Latency percentiles
    latencies = sorted(m["latency_ms"] for m in valid)
    agg["latency_p50_ms"] = statistics.median(latencies)
    # p95: use quantiles if enough points, else max
    if len(latencies) >= 20:
        agg["latency_p95_ms"] = statistics.quantiles(latencies, n=20)[18]  # 95th percentile
    else:
        agg["latency_p95_ms"] = max(latencies)
    agg["latency_mean_ms"] = statistics.mean(latencies)

    # By derivation type
    for derivation in ("paraphrase", "tag_lookup", "recency_anchor", "wiki_primary"):
        subset = [m for m in valid if m.get("derivation") == derivation]
        if subset:
            agg[f"mrr_{derivation}"] = sum(m["mrr"] for m in subset) / len(subset)
            agg[f"recall@10_{derivation}"] = sum(m["recall@10"] for m in subset) / len(subset)

    # v6-T6: by query type (memory / wiki / all)
    for qtype in ("memory", "wiki", "all"):
        subset = [m for m in valid if m.get("type") == qtype]
        if subset:
            agg[f"mrr_type_{qtype}"] = sum(m["mrr"] for m in subset) / len(subset)
            agg[f"recall@10_type_{qtype}"] = sum(m["recall@10"] for m in subset) / len(subset)

    # Diversity metric (landscape mode): avg unique domains per query.
    # Only aggregated when landscape results carry voting_domains — skip gracefully otherwise.
    _diversity_vals = [m["diversity_domains"] for m in valid if "diversity_domains" in m]
    if _diversity_vals:
        agg["avg_diversity_domains"] = sum(_diversity_vals) / len(_diversity_vals)

    # Bootstrap vs curated split
    curated = [m for m in valid if not m.get("needs_curation", True)]
    uncurated = [m for m in valid if m.get("needs_curation", True)]
    agg["n_curated"] = len(curated)
    agg["n_bootstrap_only"] = len(uncurated)
    if curated:
        agg["mrr_curated"] = sum(m["mrr"] for m in curated) / len(curated)
        agg["recall@10_curated"] = sum(m["recall@10"] for m in curated) / len(curated)

    return agg


# ── Report output ──────────────────────────────────────────────────────────────


def print_summary_table(agg: dict, golden_header: dict) -> None:
    """Print a human-readable summary table to stdout."""
    is_bootstrap = golden_header.get("bootstrap", False)
    print()
    print("=" * 60)
    print("  Yadgar Eval Harness — Phase 0 Baseline")
    if is_bootstrap:
        print("  *** BOOTSTRAP golden set — REQUIRES HUMAN CURATION ***")
    print("=" * 60)
    print(
        f"  Pairs evaluated : {agg.get('n_pairs', '?')}  (valid: {agg.get('n_valid', '?')}, errors: {agg.get('n_errors', 0)})"
    )
    print(
        f"  Curated pairs   : {agg.get('n_curated', 0)}  (bootstrap only: {agg.get('n_bootstrap_only', 0)})"
    )
    print()
    print("  ── Retrieval metrics (all pairs) ──────────────────────")
    for k in _K_VALUES:
        r = agg.get(f"recall@{k}", 0.0)
        n = agg.get(f"ndcg@{k}", 0.0)
        print(f"  recall@{k:<3}  = {r:.4f}    nDCG@{k:<3}  = {n:.4f}")
    print(f"  MRR       = {agg.get('mrr', 0.0):.4f}")
    print()
    print("  ── Latency ────────────────────────────────────────────")
    print(f"  p50  = {agg.get('latency_p50_ms', 0.0):.1f} ms")
    print(f"  p95  = {agg.get('latency_p95_ms', 0.0):.1f} ms")
    print(f"  mean = {agg.get('latency_mean_ms', 0.0):.1f} ms")
    print()
    print("  ── By derivation type ─────────────────────────────────")
    for deriv in ("paraphrase", "tag_lookup", "recency_anchor", "wiki_primary"):
        mrr_k = f"mrr_{deriv}"
        r10_k = f"recall@10_{deriv}"
        if mrr_k in agg:
            print(f"  {deriv:<18}  MRR={agg[mrr_k]:.4f}  R@10={agg[r10_k]:.4f}")
    print()
    print("  ── By query type (v6-T6 unified recall) ───────────────")
    for qtype in ("memory", "wiki", "all"):
        mrr_k = f"mrr_type_{qtype}"
        r10_k = f"recall@10_type_{qtype}"
        if mrr_k in agg:
            print(f"  type={qtype:<8}           MRR={agg[mrr_k]:.4f}  R@10={agg[r10_k]:.4f}")
    print("=" * 60)
    if is_bootstrap:
        print()
        print("  IMPORTANT: These numbers are from a MACHINE-GENERATED bootstrap")
        print("  golden set and should NOT be treated as ground-truth quality metrics.")
        print("  Human curation of golden_set.jsonl is required before using this")
        print("  as a regression gate. See benchmarks/golden/golden_set.jsonl.")
    print()


def write_json_report(
    path: Path,
    agg: dict,
    per_query: list[dict],
    golden_header: dict,
    reproducibility: dict,
    golden_path: str,
) -> None:
    """Write machine-readable JSON report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "benchmark": "yadgar-native-eval",
        "version": "v6-t6-step0",
        "timestamp": datetime.now(UTC).isoformat(),
        "golden_set": golden_path,
        "bootstrap_warning": golden_header.get("bootstrap", False),
        "note": (
            "BOOTSTRAP — auto-drafted, REQUIRES HUMAN CURATION. "
            "Numbers are informational only until golden_set.jsonl is curated."
            if golden_header.get("bootstrap", False)
            else "Golden set curated."
        ),
        "aggregated": agg,
        "per_query": per_query,
        "reproducibility": reproducibility,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"Report written to: {path}")


# ── Main runner ────────────────────────────────────────────────────────────────


def run_eval(
    golden_path: Path = _GOLDEN_DEFAULT,
    output_path: Path | None = None,
    max_results: int = 50,
    dry_run: bool = False,
    no_seed: bool = False,
    unified: bool = False,
    eval_directory: str = "eval-bootstrap",
    settings_overrides: dict | None = None,
) -> dict:
    """Run the full eval pipeline. Returns aggregated metrics dict.

    Args:
        unified: When True, routes through the MCP recall tool (fan-out path)
            instead of retriever.recall(). Requires UNIFIED_RECALL_ENABLED=True
            in the yadgar server for wiki results to appear.
        eval_directory: Directory context for unified recall calls. Defaults to
            "eval-bootstrap" (matching the seed_pairs_into_storage stamp).
    """
    print("Yadgar Eval Harness — Phase 0")
    print(f"Golden set: {golden_path}")

    # ── Load golden set ─────────────────────────────────────────────────
    pairs, golden_header = load_golden_set(golden_path)
    if not pairs:
        print("No pairs to evaluate — exiting.", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(f"Dry-run: {len(pairs)} pairs loaded. Exiting without scoring.")
        return {}

    # ── Settings ────────────────────────────────────────────────────────
    settings = make_benchmark_settings(**(settings_overrides or {}))

    # Build reproducibility metadata
    reproducibility = {
        "yadgar_commit": get_yadgar_commit(),
        "surreal_version": get_surreal_version(),
        "embedding_model": settings.EMBEDDING_MODEL,
        "python_version": sys.version,
        "run_date_utc": datetime.now(UTC).isoformat(),
        "golden_pairs": len(pairs),
        "self_seeded": not no_seed and not bool(os.environ.get("YADGAR_EVAL_NO_SEED")),
    }

    # ── Surreal server lifecycle ────────────────────────────────────────
    _spawned_proc = None
    _surreal_tmpdir = None
    _server_mode = bool(os.environ.get("YADGAR_DB_URL"))

    if not _server_mode:
        if not shutil.which("surreal"):
            print(
                "WARNING: `surreal` binary not on PATH. "
                "Falling back to embedded mode — FULLTEXT retrieval will fail. "
                "Set YADGAR_DB_URL=http://127.0.0.1:8000 or install SurrealDB.",
                file=sys.stderr,
            )
        else:
            _surreal_tmpdir = tempfile.mkdtemp(prefix="yadgar_eval_surreal_")
            print(f"Starting SurrealDB server (tmpdir: {_surreal_tmpdir}) ...")
            _spawned_proc, _port = spawn_surreal_for_benchmark(_surreal_tmpdir)
            os.environ["YADGAR_DB_URL"] = f"http://127.0.0.1:{_port}"
            print(f"SurrealDB started on port {_port}")

    try:
        # ── Engine init ───────────────────────────────────────────────
        print("Loading embedding model...")
        storage, embeddings, retriever, curator = create_eval_engines(settings)

        # ── Self-seeding (isolated DB only) ──────────────────────────
        # When using an isolated/fresh SurrealDB, seed the golden pair
        # content so there are real memories to retrieve against.
        # Skip seeding when YADGAR_DB_URL points at a live corpus (no_seed=True)
        # or when the caller explicitly opts out.
        if not no_seed and not bool(os.environ.get("YADGAR_EVAL_NO_SEED")):
            n_before_seed = len(pairs)
            print(f"Seeding {n_before_seed} pairs into isolated DB ...")
            pairs = seed_pairs_into_storage(pairs, storage, embeddings, settings)
            print(f"Seeded {len(pairs)} pairs. Eval will use remapped IDs.")
        else:
            print("Skipping self-seed (--no-seed or YADGAR_EVAL_NO_SEED set).")

        # ── Eval loop ────────────────────────────────────────────────
        if unified:
            print(
                f"Unified mode: routing through MCP recall tool (fan-out path), "
                f"directory={eval_directory!r}"
            )
            # Unified path: init the server stack so the MCP tool can access storage.
            from yadgar import server as _srv  # noqa: PLC0415

            _srv.init_engines(db_path=os.environ.get("YADGAR_DB_PATH", settings.DB_PATH))
            # Enable unified recall for this run
            os.environ["YADGAR_UNIFIED_RECALL_ENABLED"] = "true"
            import importlib  # noqa: PLC0415

            import yadgar.config as _ycfg  # noqa: PLC0415

            _ycfg.get_settings.cache_clear()

        per_query: list[dict] = []
        n = len(pairs)
        print(f"Evaluating {n} pairs ...")
        for i, pair in enumerate(pairs):
            if (i + 1) % 10 == 0 or i == 0:
                print(f"  [{i + 1}/{n}] {pair['query_id']}: {pair['query'][:60]}...")
            if unified:
                # v5.80: route each pair through the recall type that matches its
                # golden annotation so per-type MRR exercises the real code path
                # (e.g. type=memory pairs go through recall(type="memory") which
                # exercises the single-provider bypass introduced in v5.80).
                # Pairs without a type annotation default to "all".
                pair_type = pair.get("type", "all")
                if pair_type not in ("memory", "wiki", "all"):
                    pair_type = "all"
                metrics = evaluate_pair_unified(
                    pair,
                    eval_directory,
                    _K_VALUES,
                    max_results=max_results,
                    type_filter=pair_type,
                )
            else:
                metrics = evaluate_pair(pair, retriever, _K_VALUES, max_results=max_results)
            per_query.append(metrics)

        # ── Aggregate ─────────────────────────────────────────────────
        agg = aggregate_metrics(per_query, _K_VALUES)

        # ── Output ────────────────────────────────────────────────────
        print_summary_table(agg, golden_header)

        if output_path is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = _REPORTS_DIR / f"eval_{ts}.json"

        write_json_report(
            path=output_path,
            agg=agg,
            per_query=per_query,
            golden_header=golden_header,
            reproducibility=reproducibility,
            golden_path=str(golden_path),
        )

        return agg

    finally:
        if _spawned_proc is not None:
            teardown_surreal_proc(_spawned_proc)
        if _surreal_tmpdir:
            import shutil as _shutil

            _shutil.rmtree(_surreal_tmpdir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Yadgar native eval harness — Phase 0 quality measurement."
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=_GOLDEN_DEFAULT,
        help=f"Path to golden_set.jsonl (default: {_GOLDEN_DEFAULT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path for the JSON report (default: benchmarks/reports/eval_<timestamp>.json)",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=50,
        help="Max recall() results per query (default: 50)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load config and golden set but skip scoring",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help=(
            "Skip self-seeding golden pair content into isolated DB. "
            "Use when YADGAR_DB_URL points at a live corpus with real memories."
        ),
    )
    parser.add_argument(
        "--unified",
        choices=["on", "off"],
        default="off",
        help=(
            "Routing mode. 'on': route through MCP recall tool (fan-out path, "
            "exercises fusion + wiki + directory-scoping). "
            "'off' (default): legacy retriever.recall() path (memory-only). "
            "Requires UNIFIED_RECALL_ENABLED server setting when 'on'."
        ),
    )
    parser.add_argument(
        "--eval-directory",
        type=str,
        default="eval-bootstrap",
        help=(
            "Directory context for unified recall calls (default: 'eval-bootstrap'). "
            "Only used with --unified on."
        ),
    )
    args = parser.parse_args()

    run_eval(
        golden_path=args.golden,
        output_path=args.output,
        max_results=args.max_results,
        dry_run=args.dry_run,
        no_seed=args.no_seed,
        unified=(args.unified == "on"),
        eval_directory=args.eval_directory,
    )


if __name__ == "__main__":
    main()
