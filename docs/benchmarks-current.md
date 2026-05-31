# Benchmarks — current numbers

## Status (2026-05-31, v5.25.0)

**v5.25.0** ships Phase 1 LongMemEval retrieval infrastructure: dataset downloaded + sha256-pinned,
reproducibility metadata wired into output JSON, license attribution documented.
Phase 1 retrieval numbers PENDING deployment run (requires live SurrealDB server).
Phase 2 QA accuracy (comparison vs mem0 94.4 / Zep 63.8) ships in v5.26.0.

| Suite | Script | Dataset required | Status |
|---|---|---|---|
| LoCoMo F1 + J-Score | `run_locomo_jscore.py` | LoCoMo (CC BY-NC 4.0, HuggingFace) | scripts ready; numbers not published yet |
| LoCoMo ablation | `run_locomo_ablation.py` | LoCoMo (CC BY-NC 4.0) | scripts ready; numbers not published yet |
| **LongMemEval** | `run_longmemeval.py` | **MIT — downloaded + pinned (v5.25.0)** | **Phase 1 infra shipped; run pending deployment** |
| LoCoMo GPU | `run_benchmark_gpu.py` | LoCoMo (CC BY-NC 4.0) | scripts ready; GPU path untested |
| LoCoMo smoke test | `test_e_locomo.py` | LoCoMo JSON at `LOCOMO_JSON_PATH` | 6 tests collected, 6 skipped (dataset absent) |

The smoke test (`test_e_locomo.py`) runs correctly in the venv — all 6 tests skip cleanly with `pytest.skip` when the dataset file is absent. No import errors.

## How to generate numbers

```bash
# 1. Obtain datasets
#    LoCoMo: https://huggingface.co/datasets/snap-stanford/locomo
#    LongMemEval: obtain per project instructions

# 2. Run LoCoMo F1 + J-Score (single conversation, quick check)
LOCOMO_JSON_PATH=/path/to/locomo.json \
  .venv/bin/python -m benchmarks.run_locomo_jscore --conversation-indexes 0

# 3. Full LoCoMo run (matches published paper protocol)
OPENAI_API_KEY=sk-... \
LOCOMO_JSON_PATH=/path/to/locomo.json \
  .venv/bin/python -m benchmarks.run_locomo_jscore --provider openai

# 4. F1-only (no LLM judge needed)
LOCOMO_JSON_PATH=/path/to/locomo.json \
  .venv/bin/python -m benchmarks.run_locomo_jscore --f1-only

# 5. Smoke test
LOCOMO_JSON_PATH=/path/to/locomo.json \
  .venv/bin/python -m pytest benchmarks/test_e_locomo.py -v -p no:xdist -o "addopts="
```

## Baseline goal

Compare against Zikkaron's published LoCoMo numbers (see https://github.com/amanhij/Zikkaron) as the starting baseline. Yadgar's branch-aware retrieval (1.5× boost on current-branch matches) and CLS promotion are expected to shift LoCoMo scoring; quantify the delta in a future release.

## Per-release results

| Version | LoCoMo F1 | LoCoMo J-Score | LongMemEval retrieval recall@10 | LongMemEval QA | Notes |
|---|---:|---:|---:|---:|---|
| 5.0.1 | TBD | TBD | TBD | TBD | benchmarks revived this release (`b97ac35`) |
| **5.25.0** | — | — | **PENDING run** | — | Phase 1 infra shipped; dataset pinned (sha256: `d6f21ea…`); run pending deployment |
| **5.26.0** | — | — | (inherit 5.25.0) | **PENDING QA run** | Phase 2 QA + comparison vs mem0/Zep |
