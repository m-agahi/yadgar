# PLAN — v5.26.0: Benchmark Phase 2 QA + Publication

**Status:** drafted 2026-05-30.

**Depends on:** `docs/PLAN_V5_25_0_BENCHMARK_PUBLICATION.md` SHIPPED. v5.26.0 MUST NOT start until:
1. v5.25.0 ships with validated Phase 1 retrieval metrics in `docs/BENCHMARK_RESULTS.md`.
2. Phase 1 gate condition is confirmed: `mrr > 0.1` AND `recall@10 > 0.3`. If gate fails, v5.26.0 is blocked pending retrieval pipeline investigation.

**Goal:** ship the one citeable QA accuracy number against mem0 (94.4) and Zep (63.8) on LongMemEval `s` variant. Full reproducibility metadata. Real API spend.

**Effort estimate:** 2–3 calendar days wall-clock (including API run time).

**Split rationale:** Split from original v5.25.0 plan 2026-05-30 to isolate API spend and QA pipeline risk from infrastructure validation. v5.25.0 validates the code path for free; v5.26.0 spends budget only after that validation passes.

See also `docs/DECISIONS.md` — 2026-05-30 Plan-derived deferrals.

---

## ⚠️ Dataset License Notice

| Dataset | License | Use constraint |
|---|---|---|
| **LongMemEval** | MIT | Free use; cite per academic standards |
| **LoCoMo** | **CC BY-NC 4.0** | **NON-COMMERCIAL ONLY.** LoCoMo is explicitly out of scope for v5.26.0. |

**LoCoMo is NOT part of v5.26.0.** LoCoMo benchmark numbers ship in a separate slot, after LongMemEval headline exists.

---

## Non-goals (explicit)

- **No LoCoMo numbers** in v5.26.0.
- **No GPU benchmark, no ablation study.**
- **No Adopt-5 JS SDK, no DuckDB export** — separate planned slots.
- **No retrieval pipeline refactor.** v5.31.0 (R2 plugin arch) handles that.

---

## Risk callouts

| Risk | Severity | Mitigation |
|---|---|---|
| **API spend overrun** | High | Set kill-ceiling before run. Fermi: 500 questions × 2 `claude -p` calls = ~1000 invocations. At Haiku: ~$2–5. Sonnet: ~$15–50. Opus: ~$100–200. Default: Sonnet for both reader + judge (balance cost vs quality). Document model in result JSON. Main thread must decide kill ceiling before Phase 2 starts. |
| **Claude rate limits / timeouts** | Medium | Sequential run at ~120s timeout/call. With 1000 calls, 5% timeout rate = 50 retries adds ~1 hr. Script logs failures + continues; retry logic is basic (single attempt). If rate-limited hard, add `time.sleep(1)` between calls or use `--max-questions 50` subset first. |
| **Sanity-eval (judge) flakiness** | Medium | Step 3 spot-check: rerun on 20 questions, manually verify 5 wrong answers. If judge disagrees with manual check >20% of the time, document as caveat in published number. Do not suppress number — publish with caveat. |
| **Sequential execution wall-clock** | Medium | 500 questions × (ingest ~15s + retrieve ~5s + generate ~10s + judge ~10s) ≈ ~700s per question worst case; more realistically ~40s/question with fast NLI = ~5.5 hrs. Concurrency is out of scope; if total exceeds 12 hrs, subsample to 200 questions + document. |
| **v5.25.0 not shipped** | Blocking | v5.26.0 cannot start until Phase 1 gate passes. Hard dependency, no workaround. |
| **Variant mismatch (mem0/Zep used different variant)** | Medium | Confirmed in v5.25.0 Step 0. If they used `m` (500 sessions) not `s`, rerun Phase 1 on `m` first, then Phase 2 on `m`. Do not publish comparison numbers against mismatched variants. |

---

## Plan steps

### Step 1 — Pre-flight (inherits from v5.25.0, verify still holds)

- Confirm v5.25.0 Phase 1 gate passed: `mrr > 0.1`, `recall@10 > 0.3` in `benchmarks/results/longmemeval_v5.25.0_s_retrieval.json`.
- Confirm `claude -p --output-format json` is callable in the run environment (required for `call_claude_pipe()`).
- Decide API cost ceiling and model identity (main thread decision, MUST happen before Phase 2 starts).
- Confirm the LongMemEval variant mem0/Zep published against (from v5.25.0 Step 0 research). Document in this plan or in `docs/BENCHMARK_RESULTS.md`.

### Step 2 — Phase 2: full QA run (≤ 2 days, including troubleshoot + rerun)

Run the full QA benchmark:
```
.venv/bin/python -m benchmarks.run_longmemeval --variant s
```

- Cost: ~1000 `claude -p` invocations. Log exact cost estimate in result JSON (`reproducibility.estimated_cost_usd`).
- Wall-clock: realistically 4–8 hours (40s/question × 500 questions, sequential). Overnight acceptable.
- Output: `benchmarks/data/longmemeval/longmemeval_s_full_<ts>.json` with `per_query[*].correct` and `aggregated.overall.qa_accuracy`.
- The script already writes JSONL hypotheses file alongside the main JSON — keep it.

If kill-ceiling is hit during run:
1. Stop the run. Note how many questions completed.
2. Compute partial accuracy on completed questions.
3. Document as "partial run (N/500 questions)" in published output. Do not extrapolate to full-run accuracy.
4. File a follow-up ticket to resume with rate-limited or cheaper model.

### Step 3 — Sanity checks (≤ 0.5 day)

- Rerun on 20-question subset (`--max-questions 20`). Aggregated accuracy must be broadly consistent with full-run sub-aggregates (within ±15 percentage points).
- Spot-check 5 wrong answers manually. Is the system actually wrong, or is the judge wrong?
  - If judge is unreliable (>20% disagreement with manual on spot-check), document as caveat in the published number. Do NOT suppress the number.
- Cross-check abstention questions separately (LongMemEval has `_abs` variants). Abstention accuracy is tracked separately by the script — include in published output.

### Step 4 — Publication (≤ 0.5 day)

#### 4a. Fill `docs/BENCHMARK_RESULTS.md` Phase 2 section

The v0 draft from v5.25.0 has a placeholder Phase 2 section. Replace it with:
- LongMemEval QA accuracy headline: overall + per question type table.
- Comparison table: mem0 (94.4), Zep (63.8), Yadgar (X.X) with cited sources for competitor numbers.
- Reproducibility block: Yadgar commit SHA, dataset sha256, embedding model, reader LLM, judge LLM, Python version, run date UTC, wall-clock, estimated cost USD.
- Caveats: judge model identity, any subsampling, any known failure modes.
- Reproduction instructions: exact command line + env.

#### 4b. Update `docs/benchmarks-current.md`

Replace v5.25.0 "PENDING QA run" row with actual QA accuracy for v5.26.0. Update top-of-doc status block: LongMemEval QA IS now published; LoCoMo remains TBD.

#### 4c. CHANGELOG.md entry for v5.26.0

One paragraph: "Published first LongMemEval QA accuracy: X.X% on `longmemeval_s` variant (Phase 1 retrieval shipped in v5.25.0). See `docs/BENCHMARK_RESULTS.md` for breakdown, comparison to mem0/Zep, and reproduction instructions."

#### 4d. Conditional: README.md

If headline number is ≥ Zep's 63.8 (genuinely competitive), add a "Benchmarks" section to README with the number + link to `BENCHMARK_RESULTS.md`.
If below 63.8: publish without README banner. Frame as "first published number, pipeline tuning in progress" (see v5.31.0 R2 plugin arch for data-driven tuning path).

### Step 5 — Fire D2 + D3 revisit triggers (≤ 0.5 day, post-ship)

Both D2 (NLI as default) and D3 (PC algorithm causal discovery) are DEFER decisions whose `revisit_triggers` list "Adopt-1 (benchmarks) produces baseline numbers" as the first trigger.

**Required post-ship actions:**

1. Add entry to `docs/DECISIONS.md` under a new 2026-05 audit section (or extend 2026-05-30 section):
   - Note that Adopt-1 has shipped (cite v5.26.0 commit SHA).
   - State that D2 and D3 are now in **RECONSIDER** posture per their own revisit triggers.
   - Include the headline QA number. Note whether NLI-on settings were used (they were, per `make_benchmark_settings()`).

2. Draft (do not implement) two follow-up plans:
   - `docs/PLAN_V5_25_X_D2_NLI_AB.md` — A/B run of LongMemEval with `NLI_RERANKING_ENABLED=False` vs True. Decision rule: if NLI contributes < 5pp, flip default OFF.
   - `docs/PLAN_V5_25_X_D3_PC_AB.md` — A/B run with `pc_algorithm` disabled in nightly cycle. Decision rule from D3 entry in `docs/DECISIONS.md`.

   These are draft-only at v5.26.0 ship; whether to run them is a separate decision.

### Step 6 (optional, may slip to v5.27.x) — D2 + D3 A/B runs

The actual A/B experimentation. Costs: 2× full Phase 2 QA runs (one per ablation). Each ~$20–100 depending on model. Slot determined by main thread after Step 5 plans are reviewed. Slips cleanly to v5.27.x without blocking v5.31.0 (R2 plugin arch just needs the baseline number from v5.26.0).

---

## Acceptance criteria

v5.26.0 ships when ALL of the following are true:

- [ ] `docs/BENCHMARK_RESULTS.md` Phase 2 section populated: headline QA accuracy, per-question-type breakdown, comparison table (mem0 / Zep / Yadgar), reproducibility metadata block.
- [ ] Overall `qa_accuracy` is a REAL number (any value — uncompetitive is still citeable). No placeholder.
- [ ] `docs/benchmarks-current.md` v5.26.0 row populated (no TBD).
- [ ] `CHANGELOG.md` has v5.26.0 entry citing headline number + link to `BENCHMARK_RESULTS.md`.
- [ ] Result JSON committed at `benchmarks/results/longmemeval_v5.26.0_s_full.json` with `reproducibility` block populated (commit SHA, dataset sha256, embedding model, reader LLM, judge LLM, run date, cost).
- [ ] `docs/DECISIONS.md` updated with D2 + D3 RECONSIDER posture notes (Step 5.1).
- [ ] D2 + D3 follow-up plan files drafted (Step 5.2): `docs/PLAN_V5_25_X_D2_NLI_AB.md` and `docs/PLAN_V5_25_X_D3_PC_AB.md`.
- [ ] (Conditional) README.md updated if headline ≥ 63.8.

**Headline number quality bar:** must include full reproducibility metadata. The number itself can be ANY value — including below Zep's 63.8. A bad number is still a published number; that's the point. Iteration follows via v5.31.0 R2 plugin arch.

---

## Effort estimate (calendar days)

| Phase | Days |
|---|---:|
| Step 1 pre-flight (inherit v5.25.0) | 0.25 |
| Step 2 Phase 2 QA run (wall-clock) | 0.5 – 1 (4–8 hrs, run overnight if needed) |
| Step 2 troubleshoot / rerun | 0.5 (buffer; may not be needed) |
| Step 3 sanity checks | 0.5 |
| Step 4 publication writeups | 0.5 – 1 |
| Step 5 D2/D3 triggers + draft plans | 0.5 |
| **Total** | **2 – 3 calendar days** |

---

## Dependencies & blockers

- **Hard dependency:** v5.25.0 shipped with Phase 1 gate passing. No workaround.
- **Cost ceiling decision:** main thread must approve API spend before Phase 2 starts. Default recommendation: Sonnet for both reader + judge (best cost/quality tradeoff).
- **Downstream:** v5.31.0 (R2 plugin arch) is blocked until v5.26.0 ships (needs baseline QA accuracy).
- **D2 / D3 A/B:** blocked until v5.26.0 ships. May run as v5.27.x depending on scope.

---

## What this plan ship enables (post-v5.26.0)

| Downstream item | What unlocks |
|---|---|
| DECISIONS.md D2 (NLI default) | RECONSIDER posture; A/B run becomes meaningful |
| DECISIONS.md D3 (PC causal) | RECONSIDER posture; ablation run becomes meaningful |
| v5.31.0 R2 (recall pipeline plugin arch) | Per-stage A/B becomes routine; cost justified by data |
| Yadgar README credibility | Real number to cite vs "no published benchmarks" gap |
| Yadgar release notes / blog | CHANGELOG / blog post material |

---

## Coordination notes for main thread

- Plan-only doc → direct to master per workflow rule set 2026-05-30.
- Implementation work requires a feature branch — `feat/v5.26.0-benchmark-qa-publication` is the obvious name. Branch from latest master after v5.25.0 ships.
- v5.25.0 and v5.26.0 CANNOT run in parallel on the same branch. v5.26.0 needs v5.25.0's `reproducibility` scaffolding in `run_longmemeval.py` to be present before Phase 2 run.
- Implementer must read `docs/AUDIT_DECISIONS.md` per protocol before re-recommending or re-scoping.
