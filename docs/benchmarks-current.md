# Benchmarks — current numbers

## Status (2026-05-16, v5.0.1)

Benchmark scripts revived in `b97ac35`. Import paths verified clean with `.venv/bin/python`. Full runs require external datasets gated by license.

| Suite | Script | Dataset required | Status |
|---|---|---|---|
| LoCoMo F1 + J-Score | `run_locomo_jscore.py` | LoCoMo (license-gated, HuggingFace) | scripts ready; numbers not published yet |
| LoCoMo ablation | `run_locomo_ablation.py` | LoCoMo (license-gated) | scripts ready; numbers not published yet |
| LongMemEval | `run_longmemeval.py` | LongMemEval (license-gated, HuggingFace) | scripts ready; numbers not published yet |
| LoCoMo GPU | `run_benchmark_gpu.py` | LoCoMo (license-gated) | scripts ready; GPU path untested |
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

| Version | LoCoMo F1 | LoCoMo J-Score | LongMemEval | Notes |
|---|---:|---:|---:|---|
| 5.0.1 | TBD | TBD | TBD | benchmarks revived this release (`b97ac35`) |
