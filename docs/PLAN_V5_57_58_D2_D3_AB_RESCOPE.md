# PLAN — D2/D3 A/B benchmark runs (re-scoped to use v5.31 plugin arch)

**Status:** drafted 2026-06-01. Supersedes `docs/PLAN_V5_25_X_D2_NLI_AB.md` + `docs/PLAN_V5_25_X_D3_PC_AB.md` (both drafted pre-v5.31 plugin arch).

**Why re-scope:** v5.31.0 shipped recall pipeline plugin architecture with profiles + `stage_overrides=` + `recall_compare()` A/B harness. The original D2/D3 plans assumed full benchmark re-runs (~470 min wall-clock each). The plugin arch makes A/B trivial: run one benchmark with `profile="balanced"`, then re-run with `stage_overrides={"nli": {"enabled": False}}` (D2) or `WRRF_PPR_WEIGHT > 0` (D3). Same gold context, single config diff.

**Effort estimate:**
- D2 NLI A/B: 0.5-1d (benchmark re-run + analysis)
- D3 PC A/B: 1-1.5d (config + new edges + benchmark re-run + analysis)

**Slots:** v5.57.0 + v5.58.0 (v5.57 odd, v5.58 even; A/B work is data/analysis, not user-facing feature). Or fold into the v5.41-ish slot. Discuss.

---

## D2 — NLI diversity opt-in vs default-on

### Background

NLI (Natural Language Inference) reranker filters contradictory candidates from recall results. Default-ON since v5.x. **No NLI-OFF arm has ever run.** Decision deferred 2026-05-30 audit.

Question: does NLI improve QA accuracy, or does it remove useful candidates?

### Method

- Baseline (already exists): v5.26.0 LongMemEval-s 500q Sonnet 4.6, **69.4% QA accuracy, MRR=0.928, R@10=0.906**. NLI ON.
- Treatment: same 500q, same reader, same retrieval pipeline EXCEPT `stage_overrides={"nli": {"enabled": False}}` passed into MCP `recall()` (new v5.31.1 capability).
- No new code needed beyond the benchmark harness invocation.

### Acceptance criteria

1. Treatment run completes (470 min wall-clock + cost similar to baseline).
2. Results saved to `benchmarks/results/longmemeval_v5.XX_s_full_nli_off.json` + `_hypotheses.jsonl`.
3. Per-type comparison table in `docs/BENCHMARK_RESULTS.md`:
   - Overall QA delta
   - Per-category delta (ssu, ssa, ku, tr, ms, ssp, abstention)
   - Phase 1 metrics (MRR, R@10, NDCG@10) — should be IDENTICAL since NLI is post-retrieval
4. Decision committed: NLI stays default-on / flips default-off / becomes opt-in based on result.
5. `docs/DECISIONS.md` D2 entry status: DEFER → DECIDED.
6. Roadmap wiki updated.

### Risks

- 470 min wall-clock burns Max quota again. Should be acceptable per anchor 484470 (zero cash spend last run).
- Result inconclusive (delta within noise): doc the result + close the decision as "no measurable effect, keep status quo".

---

## D3 — PC algorithm causal discovery

### Background

v5.3.x shipped PC-algorithm-based causal edge discovery via `WRRF_PPR_WEIGHT`. Default-OFF (`WRRF_PPR_WEIGHT=0.0`) since shipped. v5.26.0 benchmark ran with PPR weight = 0 — no causal-on data exists.

Question: does PPR-weighted causal edge propagation improve recall efficacy?

### Method

- Run benchmark with `WRRF_PPR_WEIGHT=0.5` (treatment) vs `0.0` (baseline = v5.26.0 result).
- Same 500q LongMemEval-s, Sonnet 4.6.
- v5.31.1 capability: pass via `stage_overrides={"ppr": {"weight": 0.5}}` OR env var `YADGAR_WRRF_PPR_WEIGHT=0.5` before benchmark spawn.
- Confirm causal edges actually exist at benchmark time (pre-flight check: `recall(query="anything")` and verify causal stage emits non-zero candidates).

### Pre-flight risks

- LongMemEval haystack is synthetic. Causal edges may not form during ingest (insufficient cause-effect signals). If pre-flight shows 0 causal edges, ABORT this arm — there's nothing to weight.
- Mitigation: pre-flight first with 20q stratified sample. Only run full 500q if causal edges materialize.

### Acceptance criteria

1. Pre-flight 20q confirms causal edges form OR plan aborted with documented reason.
2. Treatment run completes (similar cost to baseline).
3. Results saved.
4. Per-type comparison in `docs/BENCHMARK_RESULTS.md`.
5. `docs/DECISIONS.md` D3 entry status: DEFER → DECIDED.

---

## Both A/B runs — shared infrastructure

### Order of execution

1. Pre-flight D3 first (cheap; aborts if no causal edges).
2. If D3 pre-flight passes → run D3 full first (it's the more expensive arm to skip if D2 takes priority).
3. Then D2.

Alternative: parallel. Both benchmarks can run on separate yadgar instances via the existing spawn-server pattern (`yadgar/_surreal_runner.py`). Two concurrent runs ~1.5x serial wall-clock (CPU-bound).

### Comparison framework

`benchmarks/compare_runs.py` (new helper if not present):
- Input: 2 result JSONs
- Output: per-type delta table + statistical significance flag (e.g., binomial test on QA correctness, p<0.05)
- Print AND write to `docs/BENCHMARK_RESULTS.md` as a new section

### Acceptance criteria (both)

1. Both results JSONs land.
2. Comparison tables in BENCHMARK_RESULTS.md.
3. DECISIONS.md D2 + D3 both closed with verdict + rationale.
4. Roadmap wiki "Deferred decisions" table: D2 + D3 rows moved out of OPEN.
5. v5.36.0 Adopt-7 plan (extract-on-ingest) gets a follow-up note: D2/D3 result may influence Adopt-7 architecture (e.g., if NLI default-off wins, fact extraction needs its own contradiction filter).

---

## Non-goals

- No changes to NLI model weights or PC algorithm.
- No code refactor — purely data/analysis work.
- No new benchmark variants (still longmemeval_s 500q).
- No paper/publication artifacts.

## Coordination notes

- Could run by main thread with helper agent for analysis, OR full dispatch to single agent (~1-2d total). Lean: dispatch.
- Wall-clock burns Max quota — schedule overnight ideally.
- Don't bundle with feature work — A/B runs need clean stable master.

## References

- v5.26.0 baseline: `benchmarks/results/longmemeval_v5.26.0_s_full.json`
- v5.31.0 plugin arch: `yadgar/retrieval/pipeline.py` + `profiles.py`
- v5.31.1 MCP kwargs: `yadgar/server/tools/recall.py` (profile + stage_overrides)
- Original drafts: `docs/PLAN_V5_25_X_D2_NLI_AB.md`, `docs/PLAN_V5_25_X_D3_PC_AB.md`
- Decision log: `docs/DECISIONS.md`
- Roadmap wiki: `yadgar-roadmap-future-improvements` (Deferred decisions table)
