# Yadgar Eval Baseline — v5.74.0

**Generated:** 2026-06-20
**Branch:** feat/v6-t1-eval-harness
**Harness:** benchmarks/run_eval.py (v6 Phase 0)
**Golden set:** benchmarks/golden/golden_set.jsonl

---

## ⚠️ BOOTSTRAP WARNING

**This baseline was generated on a BOOTSTRAP golden set that REQUIRES HUMAN CURATION.**

The golden set (`benchmarks/golden/golden_set.jsonl`) was auto-drafted by
`benchmarks/build_golden_bootstrap.py` using paraphrase templates applied to
manually-crafted representative memories. All 40 pairs are marked
`bootstrap=true, needs_curation=true`.

**These numbers are NOT a trusted quality signal.** They serve only as:
1. A structural proof that the harness runs end-to-end
2. A committed reference point so regressions in harness output format are detectable
3. A starting baseline to compare against once the golden set is curated

The golden set uses synthetic memory IDs (1–40) that correspond to placeholder
content — not live yadgar memories. Running `make eval` against a fresh empty DB
will produce 0.0 on all retrieval metrics because none of these IDs exist.

---

## Harness Structural Validation

**Status:** harness pipeline validated (dry-run mode — no live DB)

```
python benchmarks/run_eval.py --dry-run
```

Output:
```
Yadgar Eval Harness — Phase 0
Golden set: benchmarks/golden/golden_set.jsonl
  Loaded 40 pairs (0 curated, 40 bootstrap/uncurated)
  WARNING: golden set is a BOOTSTRAP — auto-drafted, REQUIRES HUMAN CURATION.
  Results are informational only until the set is reviewed.
Dry-run: 40 pairs loaded. Exiting without scoring.
```

All 40 pairs loaded successfully. Harness exits cleanly in dry-run mode.

---

## Expected Metrics (on real yadgar corpus after curation)

When run against a populated yadgar corpus with a curated golden set, target metrics
(from LongMemEval v5.26.0 results as a reference point):

| Metric | LongMemEval v5.26 reference | Target on curated set |
|--------|-----------------------------|-----------------------|
| MRR    | 0.928                       | ≥ 0.80               |
| R@1    | —                           | ≥ 0.50               |
| R@5    | —                           | ≥ 0.70               |
| R@10   | 0.906                       | ≥ 0.80               |
| nDCG@10| 0.863                       | ≥ 0.75               |
| p50 latency | —                      | < 500 ms             |
| p95 latency | —                      | < 2000 ms (hook budget) |

These targets are placeholders. Actual baselines must come from running `make eval`
against a live corpus with a curated golden set.

---

## Data Quality Metrics (v6 Phase 0.2)

New Prometheus gauges added (from `yadgar/metrics.py`, written by `_collect_data_quality()`):

| Metric | Description |
|--------|-------------|
| `yadgar_data_quality_embedding_valid_ratio` | % memories with non-null embedding (target: 1.0) |
| `yadgar_data_quality_null_embedding_count` | Absolute null-embedding count (target: 0) |
| `yadgar_data_quality_duplicate_rate` | sim-links / active memories |
| `yadgar_data_quality_zombie_rate` | stale / total memories |
| `yadgar_data_quality_domain_coverage` | % memories with domain assigned |
| `yadgar_data_quality_surprise_p50` | Median surprise_score |
| `yadgar_data_quality_surprise_p95` | 95th percentile surprise_score |

`yadgar stats` CLI output now includes a `DATA QUALITY (v6 Phase 0.2)` section.

---

## Files Added / Changed

| File | Description |
|------|-------------|
| `benchmarks/run_eval.py` | Main eval harness adapter |
| `benchmarks/build_golden_bootstrap.py` | Bootstrap golden-set generator |
| `benchmarks/golden/golden_set.jsonl` | 40-pair bootstrap golden set (REQUIRES CURATION) |
| `benchmarks/reports/baseline-v5.74.md` | This file |
| `yadgar/metrics.py` | 7 data-quality gauges + `_collect_data_quality()` writer |
| `yadgar/cli/stats.py` | `_query_data_quality()` + `StatsData.dq_*` fields + table output |
| `yadgar/tests/test_v6_data_quality_stats.py` | 15 tests for data-quality functions |
| `Makefile` | `make eval` target |
| `.forgejo/workflows/eval.yaml` | Non-gating `workflow_dispatch` CI job |
| `docs/CAPABILITY_REGISTRY.md` | CAP-EVAL-001 + CAP-EVAL-002 entries |

---

## How to Run

```bash
# Local dry-run (no DB needed):
python benchmarks/run_eval.py --dry-run

# Full eval against running daemon:
YADGAR_DB_URL=http://127.0.0.1:8765 make eval

# Full eval with isolated SurrealDB (spawned automatically):
# Requires `surreal` on PATH
make eval

# Generate fresh bootstrap candidates from live corpus:
YADGAR_DB_URL=http://127.0.0.1:8765 python benchmarks/build_golden_bootstrap.py
```

---

## Next Steps (Human Curation Required)

1. Run `make eval` against a populated yadgar corpus
2. Review each pair in `benchmarks/golden/golden_set.jsonl`:
   - Verify the query actually retrieves the listed memory at recall
   - Add multi-hop pairs (one query relevant to 2+ memories)
   - Add cross-domain pairs
   - Set `needs_curation: false` after validating each pair
3. Re-run `make eval` with the curated set — commit the resulting JSON report as the new baseline
4. Once MRR/R@10 baseline is trusted, graduate CI job to a PR trigger with a regression gate:
   `recall@10 must not drop below baseline_recall@10 - 0.05`
