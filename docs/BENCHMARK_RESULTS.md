# Benchmark Results — Yadgar

Published benchmark numbers for Yadgar's memory retrieval pipeline.

> **v0 draft — Phase 1 retrieval scaffold (2026-05-31)**
> Phase 1 infrastructure and reproducibility metadata shipped in v5.25.0.
> Full Phase 1 retrieval run pending deployment (requires live SurrealDB — see §Reproduction).
> Phase 2 QA accuracy (the headline number vs mem0/Zep) ships in v5.26.0.

---

## v5.25.0 — Phase 1: LongMemEval Retrieval-Only

### Setup

| Field | Value |
|---|---|
| Yadgar version | v5.25.0 |
| Dataset | LongMemEval `s` variant (`longmemeval_s_cleaned`) |
| Dataset SHA-256 | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| Dataset license | MIT — Wu et al., ICLR 2025 |
| Dataset source | https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned |
| Total questions | 500 |
| Embedding model | `all-MiniLM-L6-v2` |
| Retrieval settings | NLI reranking ON, CE reranking ON, WRRF vector+FTS fusion |
| Phase | 1 (retrieval-only, no LLM calls) |
| Commit | `357b8b177f580fb5112953304d105bc7bea071ce` (plan-split) |

### Phase 1 Retrieval Metrics — PENDING

> Full run pending v5.25.0 deployment.
> Infrastructure validated: dataset downloaded and pinned (sha256 above).
> Reproducibility metadata wired into output JSON.
> Run requires a live SurrealDB server — embedded SurrealDB does not support FULLTEXT ANALYZER (pre-existing upstream limitation).

To populate this table, run after deployment:
```bash
.venv/bin/python -m benchmarks.run_longmemeval \
  --variant s --retrieval-only \
  --output benchmarks/results/longmemeval_v5.25.0_s_retrieval.json
```

| Question Type | Count | MRR | Recall@5 | Recall@10 | NDCG@10 |
|---|---:|---:|---:|---:|---:|
| single-session-user | 70 | PENDING | PENDING | PENDING | PENDING |
| single-session-assistant | 56 | PENDING | PENDING | PENDING | PENDING |
| single-session-preference | 30 | PENDING | PENDING | PENDING | PENDING |
| multi-session | 133 | PENDING | PENDING | PENDING | PENDING |
| temporal-reasoning | 133 | PENDING | PENDING | PENDING | PENDING |
| knowledge-update | 78 | PENDING | PENDING | PENDING | PENDING |
| **overall** | **500** | **PENDING** | **PENDING** | **PENDING** | **PENDING** |

### Phase 1 Gate Condition

v5.26.0 QA run is BLOCKED until Phase 1 gate passes:
- `mrr > 0.1` AND `recall@10 > 0.3`

If either condition fails: investigate retrieval pipeline before spending LLM budget.

---

## v5.26.0 — Phase 2: LongMemEval QA Accuracy (PENDING)

> QA accuracy (the headline number comparable to mem0 / Zep) ships in v5.26.0.
> This section will be populated after Phase 1 gate passes.

### Comparison Table (PENDING)

| System | LongMemEval QA Accuracy | Variant | Notes |
|---|---:|---|---|
| mem0 | 94.4% | s | mem0 state-of-memory blog, 2026 |
| Zep | 63.8% | s | Zep announcement |
| **Yadgar** | **PENDING v5.26.0** | s | Phase 2 not yet run |

---

## Dataset Attribution

**LongMemEval** (MIT License):

> Wu, Junhao, Shangyu Xing, Bin Wang, Shengyu Zhang, Wei Fan, Pengfei Liu, and Chunhua Shen.
> "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory."
> ICLR 2025. arXiv:2410.10813 (2024).
> Dataset: https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned

**LoCoMo** (CC BY-NC 4.0): Deferred — not used in v5.25.0 or v5.26.0.
See `docs/BENCHMARK_LICENSE.md` for details.

---

## Caveats

- v5.25.0 ships infrastructure and dataset pin only. Phase 1 numbers PENDING deployment run.
- Phase 1 metrics are retrieval-only (no LLM calls, zero API spend).
- Phase 2 QA accuracy is not yet available. The comparison table above contains placeholder values.
- When Phase 2 runs: both reader and judge are Claude. Model identity will be recorded in the reproducibility block.

---

## Reproduction

Phase 1 run (retrieval-only, ~30–120 min CPU wall-clock, zero API spend):
```bash
# Requires: live SurrealDB server OR fix for embedded FULLTEXT ANALYZER syntax.
# See benchmarks/README.md for setup.
.venv/bin/python -m benchmarks.run_longmemeval \
  --variant s \
  --retrieval-only \
  --output benchmarks/results/longmemeval_v5.25.0_s_retrieval.json
```

Phase 2 run (full QA, ~$2–50 API spend depending on model, v5.26.0 only):
```bash
# NOT for v5.25.0. Requires Phase 1 gate to pass first.
.venv/bin/python -m benchmarks.run_longmemeval \
  --variant s \
  --output benchmarks/results/longmemeval_v5.26.0_s_full.json
```

Output JSON includes a `reproducibility` block:
```json
{
  "reproducibility": {
    "yadgar_commit": "<git sha>",
    "dataset_sha256": "<sha256 of longmemeval_s_cleaned.json>",
    "embedding_model": "all-MiniLM-L6-v2",
    "reader_llm": null,
    "judge_llm": null,
    "python_version": "...",
    "run_date_utc": "..."
  }
}
```
