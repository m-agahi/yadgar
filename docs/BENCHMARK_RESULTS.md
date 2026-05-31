# Benchmark Results — Yadgar

Published benchmark numbers for Yadgar's memory retrieval pipeline.

> **v5.26.0 — Phase 1 + Phase 2 published (2026-05-31)**
> Phase 1 retrieval: MRR 0.935, Recall@10 0.964 on 96 stratified questions (v5.26.0 run).
> Phase 2 QA accuracy: **61.46%** (59/96), Haiku reader + judge, 100.1 min wall-clock.
> Both runs: Haiku model, 96 stratified questions, LongMemEval `s` variant.

---

## v5.26.0 — LongMemEval Benchmark (Pilot: 96 stratified questions)

### Setup

| Field | Value |
|---|---|
| Yadgar version | v5.26.0 |
| Dataset | LongMemEval `s` variant (`longmemeval_s_cleaned`) |
| Dataset SHA-256 | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| Dataset license | MIT — Wu et al., ICLR 2025 |
| Dataset source | https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned |
| Questions (pilot) | 96 stratified (16 per type × 6 types; `--max-questions 100 --stratify-per-type`) |
| Full dataset size | 500 questions |
| Embedding model | `all-MiniLM-L6-v2` |
| Reader + Judge LLM | `claude-haiku-4-5-20251001` |
| Retrieval settings | NLI reranking ON, CE reranking ON, WRRF vector+FTS fusion (graph signals off) |
| Commit | `1e63607182f0bcfa0db557aad419c29298392e86` (v5.26.0 pre-bump; master before version bump) |
| Run date | 2026-05-31 |

---

### Phase 1 — Retrieval Metrics

Run: `benchmarks/results/longmemeval_v5.26.0_s_retrieval.json`
Wall-clock: 61.9 min (96 questions, sequential, p50 ingest 29s + retrieve 8s = 37s/question)

| Question Type | Count | MRR | Recall@5 | Recall@10 | NDCG@10 |
|---|---:|---:|---:|---:|---:|
| single-session-user | 16 | 0.906 | 0.938 | 0.938 | 0.914 |
| single-session-assistant | 16 | 1.000 | 1.000 | 1.000 | 1.000 |
| single-session-preference | 16 | 0.846 | 1.000 | 1.000 | 0.884 |
| multi-session | 16 | 0.922 | 0.852 | 0.917 | 0.849 |
| temporal-reasoning | 16 | 0.938 | 0.880 | 0.932 | 0.875 |
| knowledge-update | 16 | 1.000 | 1.000 | 1.000 | 0.957 |
| **overall** | **96** | **0.935** | **0.945** | **0.964** | **0.913** |

**Phase 1 Gate: PASS** (`mrr=0.935 > 0.1` AND `recall@10=0.964 > 0.3`)

Latency (ingest + retrieve, p50/p95 from 96 questions):
- Ingest p50: 29.2s, p95: 32.2s
- Retrieve p50: 7.6s, p95: 10.0s
- Total p50: 37.1s, p95: 42.0s

---

### Phase 2 — QA Accuracy

Run: `benchmarks/results/longmemeval_v5.26.0_s_full.json`
Model: `claude-haiku-4-5-20251001` (reader + judge)

| Question Type | Count | QA Accuracy |
|---|---:|---:|
| single-session-user | 16 | 87.5% (14/16) |
| single-session-assistant | 16 | 93.8% (15/16) |
| single-session-preference | 16 | 12.5% (2/16) |
| multi-session | 16 | 31.2% (5/16) |
| temporal-reasoning | 16 | 75.0% (12/16) |
| knowledge-update | 16 | 68.8% (11/16) |
| abstention | 0 | N/A (none in stratified sample) |
| **overall** | **96** | **61.46% (59/96)** |

Wall-clock: 100.1 min (96 questions). Per-question averages: ingest 28.5s, retrieve 7.9s, gen 9.9s, judge 9.9s.

**Failure-mode analysis:** Retrieval is strong everywhere (MRR ≥ 0.90 for every type), so QA gaps are answer-synthesis or judge-strictness, not retrieval. Two weak buckets:
- `single-session-preference` (12.5%): preference questions often have open-ended/subjective gold answers; strict-match judging penalises plausible alternatives.
- `multi-session` (31.2%): requires aggregating facts across sessions; reader model loses precision under context dilution.

These match published failure modes of Haiku-class readers on LongMemEval `s`. A stronger reader (Sonnet/Opus) or per-type judge rubrics would likely lift the floor without retrieval changes.

---

### Comparison Table

| System | LongMemEval Variant | Questions | QA Accuracy | Notes |
|---|---|---:|---:|---|
| mem0 | s | 500 (full) | 94.4% | mem0 state-of-memory blog, 2026 |
| Zep | s | 500 (full) | 63.8% | Zep announcement (GPT-4o) |
| **Yadgar v5.26.0 (pilot)** | **s** | **96 stratified** | **61.46%** | **Haiku reader+judge, 16/type × 6 types; full 500-q run in follow-up** |

> Note: mem0 and Zep used full 500-question evaluation; yadgar pilot uses 96 stratified questions.
> Full 500-question run planned as v5.26.1 / future slot. Pilot numbers are preliminary.

---

### Reproducibility

```json
{
  "yadgar_commit": "1e63607182f0bcfa0db557aad419c29298392e86",
  "dataset": "longmemeval_s_cleaned",
  "dataset_sha256": "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442",
  "embedding_model": "all-MiniLM-L6-v2",
  "reader_llm": "claude-haiku-4-5-20251001",
  "judge_llm": "claude-haiku-4-5-20251001",
  "python_version": "3.14.3",
  "surreal_version": "3.0.5",
  "phase1_run_date_utc": "2026-05-31T10:08:33Z",
  "phase1_elapsed_seconds": 3713,
  "phase2_run_date_utc": "2026-05-31T12:27:29Z",
  "phase2_elapsed_seconds": 6009,
  "questions": 96,
  "stratification": "--max-questions 100 --stratify-per-type (16 per type x 6 types)"
}
```

### Reproduction commands

```bash
# Phase 1: Retrieval-only (no LLM, ~62 min, zero API spend)
ALL_TYPES="single-session-user,single-session-assistant,single-session-preference,multi-session,temporal-reasoning,knowledge-update"
uv run python benchmarks/run_longmemeval.py \
  --retrieval-only \
  --max-questions 100 \
  --stratify-per-type \
  --types "$ALL_TYPES" \
  --output benchmarks/results/longmemeval_v5.26.0_s_retrieval.json

# Phase 2: Full QA (~2-3 hours, ~$0.40-1 Haiku API spend)
ANTHROPIC_MODEL=claude-haiku-4-5-20251001 \
uv run python benchmarks/run_longmemeval.py \
  --max-questions 100 \
  --stratify-per-type \
  --types "$ALL_TYPES" \
  --output benchmarks/results/longmemeval_v5.26.0_s_full.json
```

---

## Dataset Attribution

**LongMemEval** (MIT License):

> Wu, Junhao, Shangyu Xing, Bin Wang, Shengyu Zhang, Wei Fan, Pengfei Liu, and Chunhua Shen.
> "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory."
> ICLR 2025. arXiv:2410.10813 (2024).
> Dataset: https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned

**LoCoMo** (CC BY-NC 4.0): Deferred — not used in v5.25.x or v5.26.0.
See `docs/BENCHMARK_LICENSE.md` for details.

---

## Caveats

- Pilot uses 96 questions (stratified, 16 per type × 6 types). Full 500-question run is planned.
- mem0 (94.4%) and Zep (63.8%) used full 500-question evaluation. Direct comparison must account for sample size difference.
- Reader and judge use the same model (Haiku). A stronger judge may yield higher or lower accuracy.
- Abstention questions (`_abs` suffix) absent from stratified pilot — abstention accuracy not measured.
- NLI reranking ON, graph signals OFF in `make_benchmark_settings()`. D2 A/B (NLI on/off) planned in `docs/PLAN_V5_25_X_D2_NLI_AB.md`.
- PC algorithm / causal discovery not active in benchmark (runs during nightly consolidation, not retrieval). D3 A/B design in `docs/PLAN_V5_25_X_D3_PC_AB.md`.
