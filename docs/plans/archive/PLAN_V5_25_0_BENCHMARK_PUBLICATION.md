# PLAN — v5.25.0: Benchmark Infrastructure + Phase 1 Retrieval-Only

**Renumbered:** v5.17.0 → v5.25.0 on 2026-05-30. Reason: skip-1 minor convention adopted 2026-05-30 — odd-only minors for sequential features, even slots reserved for hotfix patches between them.

**Split:** 2026-05-30. Original v5.25.0 plan covered both retrieval and full QA publication.
Split into two ships to bound risk per deploy:
- **v5.25.0 (this plan):** benchmark infrastructure + Phase 1 retrieval-only. Zero API spend.
- **v5.26.0:** Phase 2 QA + sanity checks + publication. See `docs/PLAN_V5_26_0_BENCHMARK_QA_PUBLICATION.md`.

**Status:** drafted 2026-05-30. Plan-first per I27.

**Audit lineage:** Adopt-1 in `docs/competitor-audit-2026-05-30.md` ("Formal benchmarking — High impact, medium effort"). Audit text: *"This is the single highest-ROI missing piece for Yadgar's credibility."*

**Effort estimate:** 1–2 calendar days.

**Downstream:** v5.26.0 (`PLAN_V5_26_0_BENCHMARK_QA_PUBLICATION.md`) is blocked until this plan ships.

See also `docs/DECISIONS.md` — 2026-05-30 Plan-derived deferrals.

---

## ⚠️ Dataset License Notice (from LICENSE_COMPLIANCE_AUDIT_2026-05-30)

| Dataset | License | Use constraint |
|---|---|---|
| **LongMemEval** | MIT | Free use; cite per academic standards |
| **LoCoMo** | **CC BY-NC 4.0** | **NON-COMMERCIAL ONLY.** Yadgar OSS use is fine. Commercial use (product marketing, investor materials, paid services advertising the numbers) requires **written permission from SNAP Research** |

**Citations (required when publishing numbers):**
- LongMemEval: Wu et al., "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory", arXiv:2410.10813 (2024)
- LoCoMo: Maharana et al., "Evaluating Very Long-Term Conversational Memory of LLM Agents", arXiv:2402.17753 (2024)

---

## Goal — validated infrastructure + intermediate retrieval metric

Ship a validated code path and a first citeable retrieval number (`retrieval_recall@K`, `nDCG@K`, `MRR` on `longmemeval_s`) committed to `docs/BENCHMARK_RESULTS.md` as a v0 draft. No Claude API calls. No API spend.

The headline QA accuracy number (what mem0 94.4 and Zep 63.8 are measured on) is v5.26.0's job. v5.25.0 gates Phase 2: if retrieval is catastrophically broken, it is better to discover this for free (no LLM budget burn) before committing to the full run.

Why LongMemEval first (not LoCoMo):
- It is the metric mem0 (94.4) and Zep (63.8) actively market. The audit gap: *"Yadgar has no LongMemEval score to point to."*
- Script (`benchmarks/run_longmemeval.py`) is 848 LOC and complete — `--retrieval-only` flag runs natively with zero LLM calls.
- Phase 1 retrieval recall is itself a citable intermediate metric (mem0's state-of-memory blog also publishes retrieval recall separately from QA).

---

## Non-goals (explicit)

- **No Claude QA calls.** v5.26.0 handles Phase 2.
- **No publication of QA headline number.** `docs/BENCHMARK_RESULTS.md` ships as a v0 draft with retrieval section only; QA section is a placeholder.
- **No D2 / D3 decisions or RECONSIDER.** D2 and D3 revisit triggers require QA accuracy data. Phase 1 retrieval numbers are not sufficient to fire them. D2/D3 fire in v5.26.0.
- **No LoCoMo numbers.** LoCoMo follows in a separate slot after LongMemEval QA headline exists.
- **No GPU benchmark, no ablation study.**
- **No new test infra.** Existing pytest + standalone scripts are sufficient.

---

## Current state (verified from code, 2026-05-30)

| Asset | Path | Status |
|---|---|---|
| Main script | `benchmarks/run_longmemeval.py` | 848 LOC, working, retrieval + full QA modes |
| `--retrieval-only` flag | embedded in CLI | Runs ingestion + retrieval eval; skips all LLM calls |
| Dataset download | `download_dataset()` in script | HuggingFace URL hardcoded: `https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned` |
| Variants supported | `oracle`, `s` (default), `m` | `s` = ~500 questions, ~40 sessions per question |
| Settings template | `make_benchmark_settings()` | Retrieval config with NLI + CE on — correct for benchmark but CPU-intensive |
| Output JSON | `benchmarks/data/longmemeval/longmemeval_<variant>_<mode>_<ts>.json` | Per-query + aggregated metrics |
| Reproducibility metadata | **PARTIAL.** Script captures variant, timestamp, settings_overrides, per_query. Does NOT capture commit SHA, dataset sha256, embedding model name, or `claude` version. Step 5 must ADD these. | Gap to fix in this plan. |
| Smoke test | `benchmarks/test_e_locomo.py` (LoCoMo only) | 6 tests, all skip without `LOCOMO_JSON_PATH` |
| Results doc | `docs/benchmarks-current.md` | Has empty "TBD" row |

---

## Open questions (must resolve during implementation)

1. **Which LongMemEval variant did mem0 and Zep publish against?** Script defaults to `s` (~500 questions, standard published variant per the LongMemEval paper, ICLR 2025). The audit cites 94.4 (mem0) and 63.8 (Zep) without naming the variant. **Action:** verify by reading mem0's [state-of-memory blog](https://mem0.ai/blog/state-of-ai-agent-memory-2026) and Zep's announcement before run. Default assumption: `s`. This is Open Question 9 in the roadmap wiki.

2. **Dataset license gate.** `benchmarks-current.md` claims "license-gated". The HuggingFace card for `longmemeval-cleaned` is MIT — programmatic download is fine. Document the sha256 of the downloaded file in the run output. Do NOT commit the dataset to the repo.

---

## Plan steps (concrete, executable)

### Step 0 — Pre-flight (≤ 0.5 day)

- Confirm mem0/Zep variant (Open Question 1 above). Read mem0 state-of-memory blog. Document the variant in this plan or in `docs/BENCHMARK_RESULTS.md` preflight section.
- Confirm `.venv/bin/python -m benchmarks.run_longmemeval --help` runs clean on current master (no import errors). Smoke test only — no dataset download.
- Verify `benchmarks/run_longmemeval.py` is importable with no import errors on current master.
- Note: "retrieval-only is free" means free in API spend. It is NOT free in compute — NLI cross-encoder and CE reranker are local ML models (CPU/GPU). Expect up to 2 hrs wall-clock depending on hardware.

### Step 1 — Phase 1: retrieval-only run (≤ 1 day)

- Download dataset: `.venv/bin/python -m benchmarks.run_longmemeval --variant s --retrieval-only --max-questions 0`
  (script calls `download_dataset()` before run).
- Dataset download step caches to `benchmarks/data/longmemeval/` — no re-download on subsequent runs.
- Expected wall-clock: 30 min – 2 hrs for 500 questions × (ingest + retrieve), depending on hardware and NLI/CE model load time.
- Output JSON: `benchmarks/data/longmemeval/longmemeval_s_retrieval_<ts>.json`

**Gate to Phase 2 (i.e., gating v5.26.0):** numbers must be plausible — `mrr > 0.1` and `recall@10 > 0.3`. If catastrophically bad (mrr ≈ 0), STOP, investigate `make_benchmark_settings()` config before any LLM budget burn in v5.26.0.

### Step 2 — Sanity-check retrieval (≤ 0.25 day)

- Rerun on a 20-question subset (`--max-questions 20 --retrieval-only`). Aggregated metrics must be broadly consistent with full-run sub-aggregates.
- Inspect 3-5 worst-performing questions manually (hit_rank is None or > 50). Is retrieval failing on a specific question type? Document findings.

### Step 3 — Smoke-test import on current master (pre-existing, verify still passes)

```
.venv/bin/python -c "import benchmarks.run_longmemeval; print('OK')"
```

No new tests added in this plan. Scope: infrastructure validation only.

### Step 4 — Dataset attribution (≤ 0.25 day)

- Read LongMemEval HuggingFace dataset card. Record sha256 of downloaded file.
- Add LongMemEval citation to `benchmarks/README.md` (required by MIT license): Wu et al. arXiv:2410.10813, ICLR 2025.
- Do NOT add LoCoMo citations to this plan's scope (separate plan).

### Step 5 — Reproducibility metadata scaffolding (CRITICAL — add before v5.26.0 runs)

The existing result JSON captures: variant, timestamp, settings_overrides, per_query. It does NOT capture:
- Yadgar commit SHA (`git rev-parse HEAD`)
- Dataset filename + sha256
- Embedding model name (used inside the script but not written to output)
- `claude` version (not needed in v5.25.0 since no LLM calls, but the schema must be ready for v5.26.0)
- Python version + key library versions

**Action:** Patch `run_benchmark()` to append a `reproducibility` dict to the output JSON before saving. Fields:
```python
results["reproducibility"] = {
    "yadgar_commit": subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
    "dataset_sha256": sha256_of(dataset_path),
    "embedding_model": settings.EMBEDDING_MODEL,
    "reader_llm": None,   # placeholder; Phase 2 fills this in
    "judge_llm": None,    # placeholder; Phase 2 fills this in
    "python_version": sys.version,
    "run_date_utc": datetime.now(UTC).isoformat(),
}
```
This scaffolding ships in v5.25.0. Phase 2 (v5.26.0) fills in `reader_llm` and `judge_llm` after those calls complete.

### Step 6 — `docs/BENCHMARK_RESULTS.md` v0 draft (≤ 0.5 day)

Create `docs/BENCHMARK_RESULTS.md` as a v0 draft. Sections:

1. **Setup** — Yadgar version, hardware class, settings overrides, dataset variant + sha256, variant choice rationale (answered in Step 0).
2. **Phase 1 — LongMemEval retrieval metrics** — recall@k, nDCG@k, MRR per question type + overall. Populated from Step 1 run.
3. **Phase 2 — LongMemEval QA accuracy** — PLACEHOLDER. To be filled in v5.26.0.
4. **Comparison table** — mem0 (94.4), Zep (63.8), Yadgar (PENDING v5.26.0).
5. **Caveats** — Phase 1 only; QA accuracy pending.
6. **Reproduction** — exact command line for `--retrieval-only` run.

Also update `docs/benchmarks-current.md`: replace "TBD" row with a row noting v5.25.0 retrieval metrics are published, QA pending v5.26.0.

---

## Acceptance criteria

v5.25.0 ships when ALL of the following are true:

- [ ] `.venv/bin/python -m benchmarks.run_longmemeval --help` runs clean on current master (no import errors).
- [ ] `docs/benchmarks-current.md` row for v5.25.0 exists with retrieval metrics (recall@10, nDCG@10, MRR). Row for v5.26.0 is "PENDING QA run".
- [ ] `docs/BENCHMARK_RESULTS.md` exists with: Phase 1 retrieval metrics, placeholder Phase 2 section, reproducibility metadata block.
- [ ] `benchmarks/run_longmemeval.py` `run_benchmark()` output JSON includes `reproducibility` dict (commit SHA, dataset sha256, embedding model, run_date_utc).
- [ ] Result JSON committed at `benchmarks/results/longmemeval_v5.25.0_s_retrieval.json`.
- [ ] LongMemEval citation added to `benchmarks/README.md`.
- [ ] Phase 1 gate condition documented: `mrr > 0.1` AND `recall@10 > 0.3`. If gate fails, v5.26.0 is blocked pending investigation.
- [ ] `CHANGELOG.md` has v5.25.0 entry: benchmark infra + Phase 1 retrieval-only + link to `docs/BENCHMARK_RESULTS.md`.

**NOT in scope for v5.25.0 ship:** QA accuracy, D2/D3 RECONSIDER, DECISIONS.md updates, README banner with headline number.

---

## Effort estimate (calendar days)

| Phase | Days |
|---|---:|
| Step 0 pre-flight + variant confirmation | 0.25 – 0.5 |
| Step 1 Phase 1 retrieval run (wall-clock) | 0.5 – 1 |
| Step 2 sanity + manual spot-check | 0.25 |
| Step 3 smoke-test | 0.1 |
| Step 4 dataset attribution | 0.25 |
| Step 5 reproducibility scaffolding patch | 0.25 – 0.5 |
| Step 6 `docs/BENCHMARK_RESULTS.md` v0 draft | 0.25 – 0.5 |
| **Total** | **1 – 2 calendar days** |

---

## Risks

- **Retrieval pipeline catastrophically bad.** Mitigated: Step 1 gate catches this before v5.26.0 LLM spend. If gate fails, file a debug issue and hold v5.26.0.
- **Script bit-rot.** Plan confirmed script imports clean as of 2026-05-30 (Step 0 pre-flight). Confirm again on day of run.
- **Local ML compute cost.** NLI cross-encoder + CE rerank are CPU-intensive. "Free" means zero API spend; compute time on developer hardware is non-zero. Budget 30–120 min for Phase 1 run.
- **Dataset download failure.** HuggingFace URL is hardcoded. If URL changes, script's `download_dataset()` will 404. Verify URL in Step 0.

---

## Dependencies & blockers

- **None blocking start.** Scripts exist on master. No file overlap with active v5.10.x train.
- **v5.26.0 is gated on this plan shipping.** Phase 2 must not start until Phase 1 gate condition is confirmed.
- **D2 / D3 revisit triggers are NOT fired by v5.25.0.** They require QA accuracy data (v5.26.0).
- **v5.31.x (R2 plugin arch) is gated on v5.26.0,** not v5.25.0 directly.

---

## Coordination notes for main thread

- Plan-only doc → direct to master per workflow rule set 2026-05-30 (wiki slug `yadgar-workflow-plan-commits-direct-to-master`).
- Implementation work requires a feature branch — `feat/v5.25.0-benchmark-infra` is the obvious name. Branch from latest master after this plan commits.
- Related plan: `docs/PLAN_V5_26_0_BENCHMARK_QA_PUBLICATION.md` (depends on this plan).
- Implementer must read `docs/AUDIT_DECISIONS.md` per protocol before re-recommending or re-scoping.
