# Benchmark Dataset Licenses

This document records the license status and required attributions for datasets
used in Yadgar's published benchmark numbers.

---

## LongMemEval — MIT License

**Status:** GREEN — free use, commercial and non-commercial, with attribution.

**License:** MIT
**Source:** https://github.com/xiaowu0162/LongMemEval/blob/main/LICENSE
**HuggingFace:** https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned

**Required citation (per MIT attribution clause and academic standards):**

> Wu, Junhao, Shangyu Xing, Bin Wang, Shengyu Zhang, Wei Fan, Pengfei Liu, and Chunhua Shen.
> "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory."
> ICLR 2025. arXiv:2410.10813 (2024).

**Use in Yadgar:** Yadgar runs `benchmarks/run_longmemeval.py` against the
`longmemeval_s_cleaned` variant (~500 questions). The dataset is downloaded at
runtime and NOT committed to the Yadgar repository. Published benchmark scores
(retrieval recall@K, QA accuracy) cite this dataset per the above attribution.

---

## LoCoMo — CC BY-NC 4.0 (NOT used in v5.25.0)

**Status:** YELLOW — non-commercial only, attribution required.

**License:** Creative Commons Attribution-NonCommercial 4.0 International
**Source:** https://github.com/snap-research/locomo/blob/main/LICENSE.txt
**HuggingFace:** https://huggingface.co/datasets/snap-stanford/locomo

**Note for v5.25.0 / v5.26.0:** LoCoMo benchmark numbers are **deferred**.
LongMemEval is the primary benchmark for v5.25.0–v5.26.0. LoCoMo will be
benchmarked in a separate release slot after the LongMemEval headline number exists.

**Commercial use restriction:** Publishing LoCoMo-based benchmark numbers in
commercial materials (paid product marketing, investor presentations, proprietary
deployments) requires written permission from SNAP Research before use.
Non-commercial (open-source, academic) publication is permitted with attribution.

**Required citation when LoCoMo numbers are eventually published:**

> Maharana, Adyasha, Dong-Ho Lee, Sergey Tulyakov, Mohit Bansal, Francesco Barbieri,
> and Yuwei Fang. "Evaluating Very Long-Term Conversational Memory of LLM Agents."
> ACL 2024. arXiv:2402.17753 (2024).

---

## Audit lineage

License compliance for both datasets was audited in:
`docs/LICENSE_COMPLIANCE_AUDIT_2026-05-30.md`

See §6 (LongMemEval) and §7 (LoCoMo) for full analysis.
