# PLAN — v5.25.0: Benchmark Publication (LongMemEval headline number)

**Renumbered:** v5.17.0 → v5.25.0 on 2026-05-30. Reason: skip-1 minor convention adopted 2026-05-30 — odd-only minors for sequential features, even slots reserved for hotfix patches between them.

**Status:** drafted 2026-05-30. Plan-first per I27. Fires the revisit trigger on `DECISIONS.md` D2 (NLI default-on) and D3 (PC causal discovery). Lean MVP scope: **publish one comparable number against mem0 (94.4) and Zep (63.8) on LongMemEval** so Yadgar has a benchmark figure to cite.

**Audit lineage:** Adopt-1 in `docs/competitor-audit-2026-05-30.md` ("Formal benchmarking — High impact, medium effort"). Audit text: *"This is the single highest-ROI missing piece for Yadgar's credibility."*

**Master at draft time:** core v5.10.3 shipped; v5.10.4 in-flight on branch `feat/v5.10.4-consolidate-now-mode-hook-schema`.

**Proposed slot:** **v5.25.0**. (Previously confirmed as v5.13.0; renumbered to v5.25.0 under skip-1 convention.)

---

## ⚠️ Dataset License Notice (from LICENSE_COMPLIANCE_AUDIT_2026-05-30)

| Dataset | License | Use constraint |
|---|---|---|
| **LongMemEval** | MIT | Free use; cite per academic standards |
| **LoCoMo** | **CC BY-NC 4.0** | **NON-COMMERCIAL ONLY.** Yadgar OSS use is fine. Commercial use (product marketing, investor materials, paid services advertising the numbers) requires **written permission from SNAP Research** |

**Citations (required when publishing numbers):**
- LongMemEval: Wu et al., "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory", arXiv:2410.10813 (2024)
- LoCoMo: Maharana et al., "Evaluating Very Long-Term Conversational Memory of LLM Agents", arXiv:2402.17753 (2024)

**Publication checklist** (gate before any commit citing LoCoMo numbers):
- [ ] Confirm context is non-commercial (OSS docs, README, conference talk in academic context) OR obtain SNAP Research permission in writing
- [ ] LoCoMo attribution + CC BY-NC 4.0 notice included
- [ ] LongMemEval cited if used alongside

---

## Why v5.25.0 (proposed slot)

Slot reasoning — present this as a proposal; main thread synthesizes against the rest of the roadmap.

| Slot | Status | Why this isn't where benchmarks go |
|---|---|---|
| v5.10.4 → v5.10.9 | Active micro-fix train; surgical scope (single-bug fixes, hook fixes, secret-gate, etc.) | Benchmark publication is a multi-day cross-cutting effort, not a micro-fix. Doesn't belong in a patch train. |
| v5.21.0 | Anchor cross-project (`PLAN_V5_21_0_ANCHOR_CROSS_PROJECT.md`) | Independent feature, already planned. Don't conflate. |
| v5.23.0 | Wiki Bookmarks page (`PLAN_V5_23_0_WIKI_BOOKMARKS.md`) | Independent UI feature. Don't conflate. |
| **v5.25.0** | **Benchmark publication** | **Odd-minor slot under skip-1 convention. Downstream blockers waiting on it (R2 plugin arch, D2 NLI, D3 PC causal) — getting the number out earlier unblocks more later work.** |
| v5.31.x | R2 retrieval-pipeline plugin architecture (per DECISIONS R2) | Pre-slotted AS the slot that follows benchmark landing. |
| v5.99.0 | Roadmap freshness | Unrelated. |

**Sequencing constraint:** v5.25.0 must ship **before** v5.31.x (R2 plugin arch). The decisions log already pins R2 to after benchmarks land. Inverting the order would mean designing the plugin architecture without benchmark data to justify the per-stage toggles it enables.

**Calendar ordering is independent of version ordering.** v5.25.0 can run in parallel with v5.10.x train (different agent / different files). Both can land in any chronological order — but no v5.31+ work touches the retrieval pipeline until v5.25.0 numbers exist.

---

## Goal — one headline number

**Ship a single citeable number:** Yadgar's LongMemEval QA accuracy, on the same variant mem0 and Zep published against, with full reproducibility metadata.

That's it. Not five benchmarks. Not the full LoCoMo + LongMemEval + GPU + ablation matrix. **One number, properly documented, properly comparable.**

Why LongMemEval first (not LoCoMo):
- It is the metric mem0 (94.4) and Zep (63.8) actively market. Yadgar's audit explicitly calls this gap out: *"Yadgar has no LongMemEval score to point to."*
- Script (`benchmarks/run_longmemeval.py`) is already 848 LOC and complete — dataset download is built-in (HuggingFace URL hardcoded, no license gate at download level).
- Retrieval-only mode is free (no LLM, no API key) → produces partial signal even if budget kills Phase 2.

LoCoMo + ablation can ship in a follow-up minor (v5.25.x or later); they are not part of v5.25.0 acceptance criteria.

---

## Non-goals (explicit)

- **No LoCoMo numbers** in v5.25.0. LoCoMo follows in a separate slot once LongMemEval headline exists.
- **No GPU benchmark.** `run_benchmark_gpu.py` is the GPU rerank path; untested per `benchmarks-current.md`. Wait for separate hardware story.
- **No ablation study.** `run_locomo_ablation.py` (per-signal contribution) only makes sense after R2 plugin arch (v5.31.x) lands.
- **No D2 / D3 decisions in this plan.** This plan PRODUCES the data D2/D3 need; the actual revisit happens after ship.
- **No `flush_only()` MCP primitive, no pipeline refactor.** Pure benchmark publication.
- **No new test infra.** Existing pytest + standalone scripts are enough.

---

## Current state (verified from code, 2026-05-30)

| Asset | Path | Status |
|---|---|---|
| Main script | `benchmarks/run_longmemeval.py` | 848 LOC, working, retrieval + full QA modes |
| Dataset download | embedded in script (`download_dataset`) | HuggingFace URL hardcoded: `https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned` — **license check needed before automated download** |
| Variants supported | `oracle`, `s` (default, ~40 sessions), `m` (~500 sessions) | `s` is script default |
| Settings template | `make_benchmark_settings()` | Hardcoded retrieval config tuned for LongMemEval — full WRRF weights, CE on, NLI on, query expansion on |
| Reader/judge LLM | `claude -p --output-format json` via subprocess | Per-question: 2 invocations (reader + judge) |
| Output JSON | `benchmarks/data/longmemeval/longmemeval_<variant>_<mode>_<ts>.json` | Per-query + aggregated metrics |
| Smoke test | `benchmarks/test_e_locomo.py` (LoCoMo only) | 6 tests, all skip without `LOCOMO_JSON_PATH` |
| Results doc | `docs/benchmarks-current.md` | Has empty "TBD" row for v5.0.1 |
| Tracker | "scripts ready; numbers not published yet" | TBD across all three suites |

---

## Open questions (must resolve before / during implementation)

1. **Which LongMemEval variant did mem0 and Zep publish against?** Script defaults to `s` (~40 sessions, the standard published variant per the LongMemEval paper, ICLR 2025). The audit cites 94.4 (mem0) and 63.8 (Zep) without naming the variant. **Action:** verify by reading mem0's [state-of-memory blog](https://mem0.ai/blog/state-of-ai-agent-memory-2026) and Zep's announcement before run. If they used a different variant, match it. Default assumption: `s`. Open question for main thread to confirm or delegate verification.
2. **Dataset license.** `benchmarks-current.md` claims "license-gated". The HuggingFace dataset card needs to be read; some "license-gated" datasets gate human reading but allow programmatic download. **Action:** read the dataset card, document license terms, decide whether to commit to dataset or document the download step. (Likely the latter; benchmark datasets are typically not redistributed.)
3. **Cost ceiling for Phase 2.** ~500 questions × 2 `claude -p` calls = ~1000 invocations. At Opus pricing this is real money. Acceptable to use Sonnet for judge to halve cost? Acceptable to subsample to 100 questions if 500 is unaffordable? **Action:** main thread decides cost ceiling. If unset, default to: full 500 with whatever model the running session has, document model + cost in result file.
4. **Where exactly do the headline numbers get published?** Options:
   - `docs/benchmarks-current.md` "Per-release results" table (replace TBD with actual)
   - New `docs/BENCHMARK_RESULTS.md` — dedicated long-form doc with per-question breakdowns
   - `CHANGELOG.md` v5.13.0 entry
   - `README.md` — top-level mention if number is competitive

   **Default plan:** all three of the first three; README mention conditional on number being above Zep's 63.8 (genuinely competitive). Otherwise quietly publish without README banner and frame as "first published number, more to come".

---

## Plan steps (concrete, executable)

### Step 0 — Pre-flight (≤ 1 day)

- Read mem0 and Zep public docs to confirm LongMemEval variant — answer open question 1.
- Read LongMemEval HuggingFace dataset card — answer open question 2 (license).
- Confirm `.venv/bin/python -m benchmarks.run_longmemeval --help` runs clean on current master (no import errors). Smoke test alone — no dataset.
- Verify `claude -p --output-format json` is callable in the run environment.

### Step 1 — Phase 1: retrieval-only run (≤ 1 day)

- Download dataset to `benchmarks/data/longmemeval/` (script handles this).
- Run: `.venv/bin/python -m benchmarks.run_longmemeval --variant s --retrieval-only`.
- Cost: zero (no LLM calls).
- Output: `recall@5/10/50`, `nDCG@5/10/50`, `mrr` per question type and overall.
- Persist output JSON. Capture full reproducibility metadata (see Step 4).
- Expected wall-clock: ~30 min – 2 hours for 500 questions × (ingest + retrieve + per-question fresh DB), depending on hardware.

**Gate to Phase 2:** numbers look plausible (mrr > 0.1, recall@10 > 0.3). If numbers are catastrophically bad, STOP and investigate before burning LLM budget. Likely cause = retrieval pipeline misconfig in `make_benchmark_settings()`.

### Step 2 — Phase 2: full QA run (≤ 2 days, including troubleshoot)

- Run: `.venv/bin/python -m benchmarks.run_longmemeval --variant s`.
- Cost: ~1000 `claude -p` invocations. Document exact cost in result JSON.
- Output: full per-query + aggregated metrics including `qa_accuracy` per question type, plus overall accuracy = the headline number.
- Wall-clock: hours-to-overnight depending on Claude latency and concurrency. Script is sequential; if concurrency is needed, that's a separate spike (out of scope for v5.25.0).
- Persist output JSON.

### Step 3 — Sanity checks (≤ 0.5 day)

- Cross-check: rerun on a 10-question subset (`--max-questions 10`), expect aggregated metrics roughly consistent with full-run sub-aggregates.
- Spot-check 5 wrong answers manually — is the system actually wrong, or is the judge wrong? If judge is unreliable (>20% disagreement with manual check), document as caveat in published number.
- Run on a different seed or different question ordering (script needs to support this — if not, document as limitation).

### Step 4 — Publication (≤ 1 day)

Three publish targets, each with concrete content:

#### 4a. Update `docs/benchmarks-current.md`
Replace TBD row in "Per-release results" with actual numbers for v5.13.0. Add reproducibility footer: commit SHA, dataset filename + sha256, embedding model, settings dict path, embedding_model. Update top-of-doc status block to reflect that LongMemEval IS now published; LoCoMo remains TBD.

#### 4b. Create `docs/BENCHMARK_RESULTS.md`
Long-form per-question-type breakdown. Sections:
- Setup (Yadgar version, hardware class, settings overrides, dataset variant + sha256)
- LongMemEval headline number + per-question-type accuracy table
- LongMemEval retrieval metrics (recall@k, nDCG@k, MRR per type)
- Comparison table (mem0 94.4, Zep 63.8, Yadgar X.X) with cited sources for competitor numbers
- Caveats (judge model, subsampling if any, known failure modes)
- Reproduction instructions: exact command line + env

#### 4c. CHANGELOG.md entry for v5.25.0
One paragraph in the v5.25.0 section: "Published first LongMemEval QA accuracy: X.X% on `longmemeval_s` variant. See `docs/BENCHMARK_RESULTS.md` for breakdown and reproduction."

#### 4d. Conditional: README.md
If headline number is ≥ Zep's 63.8 (i.e. genuinely competitive), add a "Benchmarks" line to README with the number + link to BENCHMARK_RESULTS.md. If below 63.8, publish without README banner — frame as "first published number, retrieval pipeline tuning in progress."

### Step 5 — Reproducibility metadata (CRITICAL — without this, the number is uncitable)

Every published number MUST capture:

- **Yadgar commit SHA** at run time (`git rev-parse HEAD`)
- **Dataset filename + sha256** (`sha256sum benchmarks/data/longmemeval/longmemeval_s_cleaned.json`)
- **Embedding model name + version** (Settings.EMBEDDING_MODEL)
- **Reader LLM identity** (`claude` version reported by `--output-format json`)
- **Judge LLM identity** (same)
- **Settings dict** (the full overrides dict passed to `make_benchmark_settings`)
- **Python version + key library versions** (output of `uv pip freeze | grep -E "torch|sentence-transformers|transformers"`)
- **Wall-clock + cost** (Phase 2 cost in USD if known)
- **Run date + UTC timestamp**

This metadata block is appended to the result JSON automatically by the script (verify; if not, add it). It is also reproduced in `docs/BENCHMARK_RESULTS.md` so anyone reading the doc can reconstruct the run.

### Step 6 — Fire D2 + D3 revisit triggers (≤ 0.5 day, post-ship)

Both D2 (NLI as default) and D3 (PC algorithm causal discovery) are DEFER decisions whose `revisit_triggers` list "Adopt-1 (benchmarks) produces baseline numbers" as the first trigger.

**Required post-ship actions:**

1. Add entry to `docs/DECISIONS.md` under the 2026-05-30 audit section, OR open a new dated audit section if a fresh audit has run since:
   - Note that Adopt-1 has shipped (cite v5.25.0 commit SHA).
   - State that D2 and D3 are now in **RECONSIDER** posture per their own revisit triggers.
   - Either include the A/B numbers if Step 7 has been executed, or schedule Step 7 explicitly.

2. Draft (do not necessarily implement) two follow-up plans:
   - `docs/PLAN_V5_25_X_D2_NLI_AB.md` — A/B run of LongMemEval with `NLI_RERANKING_ENABLED=False` vs True. Decision rule: if NLI contributes <5pp, flip default OFF.
   - `docs/PLAN_V5_25_X_D3_PC_AB.md` — A/B run with `pc_algorithm` disabled in nightly cycle. Decision rule from D3 entry.

   These plans are draft-only on v5.25.0 ship; whether to implement them is a separate decision per their respective revisit triggers.

### Step 7 (optional, may slip to v5.25.x) — D2 + D3 A/B runs

The actual A/B experimentation. Costs: 2× full LongMemEval Phase 2 runs (one per ablation). Could land in v5.25.1 / v5.25.2 / v5.31.0 — slot determined by main thread after Step 6 plans are reviewed.

---

## Acceptance criteria

v5.25.0 ships when ALL of the following are true:

- [ ] `docs/benchmarks-current.md` "Per-release results" table has a populated row for v5.25.0 (no TBD).
- [ ] `docs/BENCHMARK_RESULTS.md` exists with: headline number, per-question-type breakdown, comparison-to-competitors table, reproducibility metadata, reproduction command.
- [ ] `CHANGELOG.md` has v5.25.0 entry citing the headline number and linking to `BENCHMARK_RESULTS.md`.
- [ ] Result JSON files committed at `benchmarks/results/longmemeval_v5.25.0_<variant>_full.json` (and `..._retrieval.json` for Phase 1).
- [ ] `docs/DECISIONS.md` updated with D2 + D3 RECONSIDER notes (Step 6.1).
- [ ] (Optional but recommended) D2 + D3 follow-up plan files drafted (Step 6.2).

**Headline number quality bar:** must include reproducibility metadata. **The number itself can be ANY number — including below Zep's 63.8.** A bad benchmark number is still a published benchmark number; that's the point. Iteration follows.

---

## Effort estimate (calendar days)

| Phase | Days |
|---|---:|
| Step 0 pre-flight | 0.5 – 1 |
| Step 1 Phase 1 (retrieval-only) | 0.5 – 1 |
| Step 2 Phase 2 (full QA) | 1 – 2 (including troubleshoot + rerun) |
| Step 3 sanity checks | 0.5 |
| Step 4 publication writeups | 1 |
| Step 5 reproducibility metadata (overlaps Step 4) | 0 (folded into 4) |
| Step 6 D2/D3 revisit triggers + draft plans | 0.5 |
| **Total** | **4 – 6 calendar days** |

Matches the audit estimate ("2-4 days to set up") with extra buffer for publication writeups + D2/D3 revisit chore. **Single-agent serial execution.** If multiple agents work in parallel (Phase 1 + plan drafts), compresses to ~3 days.

---

## Risks

- **Cost overrun on Phase 2.** Mitigate via Step 0 cost ceiling decision + Phase 1 going first (free, gates Phase 2).
- **Judge unreliability.** Mitigate via Step 3 spot-check. If judge is bad, document as caveat — don't suppress the number.
- **Dataset license blocks committing the data.** Mitigate by NOT committing dataset; document download step in `BENCHMARK_RESULTS.md`. Run inputs are reproducible from the download URL + sha256.
- **Retrieval pipeline misconfig produces catastrophically bad number.** Phase 1 catches this before LLM budget burn. If caught, debug `make_benchmark_settings()` and rerun.
- **Number is uncompetitive (below Zep's 63.8).** This is the *good* outcome of the benchmark: we now KNOW where we stand. Publish honestly, frame as baseline, use the data to drive R2 (plugin arch) prioritization in v5.14.x.
- **Script bit-rot.** `benchmarks-current.md` claims scripts work; commit `b97ac35` (revive). Confirm in Step 0 pre-flight that they STILL work on current master.

---

## Dependencies & blockers

- **None blocking start.** v5.10.4 active development on a separate branch — no file overlap. Scripts already exist on master.
- **Downstream:** v5.31.x (R2 plugin arch) and D2/D3 revisit BLOCKED until v5.25.0 ships.
- **Hardware:** no special hardware needed for Phase 1 or default Phase 2. If GPU benchmark is later added, that's a separate slot.

---

## What this plan ship enables (post-v5.25.0)

| Downstream item | What unlocks |
|---|---|
| DECISIONS.md D2 (NLI default) | RECONSIDER posture; A/B run becomes meaningful |
| DECISIONS.md D3 (PC causal) | RECONSIDER posture; ablation run becomes meaningful |
| v5.31.x R2 (recall pipeline plugin arch) | Per-stage A/B becomes routine, justifying the refactor cost |
| Yadgar README credibility | A real number to cite vs "no published benchmarks" gap audit calls out |
| Yadgar release marketing | CHANGELOG / blog post material |

---

## Coordination notes for main thread

- Plan-only doc → direct to master per workflow rule set 2026-05-30 (wiki slug `yadgar-workflow-plan-commits-direct-to-master`).
- Implementation work on this plan will require a feature branch — `feat/v5.25.0-longmemeval-publication` is the obvious name. Implementer should branch from latest master after this plan commits.
- v5.10.4 active development on `feat/v5.10.4-consolidate-now-mode-hook-schema` — file overlap NONE (this plan touches `benchmarks/`, `docs/benchmarks-current.md`, `docs/BENCHMARK_RESULTS.md` new, `CHANGELOG.md`, possibly `README.md`; v5.10.4 touches `yadgar/server/tools/admin_other.py`, hooks, `yadgar/tests/test_consolidate_now.py`).
- Implementer must read `docs/AUDIT_DECISIONS.md` per protocol before re-recommending or re-scoping.
- If main thread decides a different version slot, rename the file and update the header line + "Proposed slot" section. Body content remains valid for any v5.13.x or v5.1y.0 slot.
