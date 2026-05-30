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

## Dataset Licenses & Citations

### LoCoMo
**License:** CC BY-NC 4.0 (non-commercial)

Maharana et al., "Evaluating Very Long-Term Conversational Memory of LLM Agents," arXiv preprint arXiv:2402.17753 (2024). Available: https://huggingface.co/datasets/snap-stanford/locomo

**Commercial Use Notice:** Yadgar's use is open-source non-commercial. Commercial use of LoCoMo (including marketing materials, investor presentations, or proprietary deployments) requires written permission from SNAP Research. Contact the dataset maintainers before commercial deployment.

### LongMemEval
**License:** MIT

Wu et al., "LongMemEval: Benchmarking Long-Context Large Language Models," arXiv preprint arXiv:2410.10813 (2024). Available: https://huggingface.co/datasets/mtvu/LongMemEval
