# Benchmarks

Reproducibility scripts for Yadgar's published performance numbers.

## Suites

| File | Suite | Notes |
|---|---|---|
| run_locomo_jscore.py | LoCoMo | Jaccard scoring; primary regression suite |
| run_locomo_ablation.py | LoCoMo | Ablation: signal-by-signal contribution |
| run_longmemeval.py | LongMemEval | Long-context memory eval |
| run_benchmark_gpu.py | LoCoMo (GPU) | GPU-accelerated reranker path |
| test_e_locomo.py | LoCoMo | End-to-end smoke test |

## Run

```bash
# requires LoCoMo / LongMemEval datasets (gated by license; see
# https://huggingface.co/datasets/snap-stanford/locomo etc.)
.venv/bin/python -m benchmarks.run_locomo_jscore --episodes 200
```

## When to run

- Every major release (vX.0 → vY.0)
- After retrieval-pipeline changes
- Compare against baseline in `benchmarks/results/<version>.json`

Status: revived 2026-05-16 alongside v5.0.1 release.
