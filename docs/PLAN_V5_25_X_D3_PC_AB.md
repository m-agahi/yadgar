# PLAN — D3: PC Algorithm Causal Discovery A/B Run

**Status:** draft — awaiting v5.26.0 pilot numbers to confirm whether A/B is warranted.

**Triggered by:** Adopt-1 benchmark (v5.26.0) producing baseline QA accuracy.
**References:** `docs/DECISIONS.md` D3 entry (2026-05-30); `docs/PLAN_V5_26_0_BENCHMARK_QA_PUBLICATION.md`.

---

## Decision to make (from D3 DEFER)

> Validate that causal discovery (PC algorithm) improves recall accuracy.
> If not, retire or gate behind an opt-in flag.
> Unique-moat feature; removing without measurement also removes architectural distinction.

---

## Important architectural note

The PC algorithm runs during **consolidation** (`yadgar/consolidation/causal.py`), not during
retrieval. It builds a causal DAG from relationship edges, which would influence retrieval only
if `WRRF_PPR_WEIGHT > 0` (graph signal weight). In `make_benchmark_settings()`, `WRRF_PPR_WEIGHT = 0.0`
and `WRRF_SPREADING_WEIGHT = 0.0` — graph signals are disabled.

This means:
1. The v5.26.0 LongMemEval benchmark implicitly tests **causal-off** performance (PC algorithm
   was not running during benchmark ingestion).
2. D3 A/B is not about LongMemEval accuracy. It is about:
   (a) **Nightly cycle CPU cost** — does PC algorithm phase consume > 30s on typical state?
   (b) **Live retrieval quality** — in production (graph signals ON), does causal DAG improve
       recall? (Not testable with LongMemEval in current config.)
3. The D3 revisit trigger "Adopt-1 benchmarks produce causal-on vs causal-off accuracy numbers"
   was predicated on graph signals being enabled in the benchmark — they are not.

**Revised assessment for D3:**
- The v5.26.0 numbers do NOT provide causal-on vs causal-off A/B data.
- D3 remains DEFER pending: (a) LongMemEval run with `WRRF_PPR_WEIGHT > 0` + `WRRF_SPREADING_WEIGHT > 0`,
  or (b) a live-retrieval A/B with causal DAG enabled vs disabled.
- The CPU cost question (trigger: PC > 30s on typical state) can be answered from
  nightly cycle phase timing logs without a new benchmark run.

## Prerequisites

- [ ] v5.26.0 ships with Phase 1 + Phase 2 baseline numbers.
- [ ] Check nightly cycle phase timing logs for PC algorithm duration on real production state.
  If consistently < 5s: low cost, defer; if > 30s: high priority to gate.
- [ ] Decide whether to run LongMemEval with graph signals enabled (WRRF_PPR_WEIGHT > 0)
  to get a true causal-on vs causal-off QA accuracy comparison. This costs ~$0.40-1 more.
- [ ] Refactor-2 recall plugin arch (v5.31.0) optionally — enables per-stage metrics.

---

## Scope

Single A/B comparison:

| Run | Config | Questions |
|---|---|---|
| A (baseline) | PC algorithm default config (v5.26.0 numbers) | 100 stratified |
| B (ablation) | PC algorithm disabled | 100 stratified |

Cost: ~$0.40–1 additional Haiku spend.
Wall-clock: ~2–3 hours for Run B.

**Note:** If PC algorithm is already disabled by default in `make_benchmark_settings()`,
Run A and Run B are identical — the A/B is a no-op and this plan closes immediately
with "D3 already implicitly A/B'd". Check `make_benchmark_settings()` overrides before
dispatching Run B.

---

## Steps

1. **Pre-flight:** confirm v5.26.0 baseline numbers and identify PC algorithm config knob.
2. **Check `make_benchmark_settings()`:** if `pc_algorithm` or causal discovery is already
   off in the benchmark config, document that D3 is implicitly answered by v5.26.0 baseline
   (causal-off performance) and close.
3. **If PC is ON in baseline (Run A):** execute Run B:
   ```bash
   # Replace PC_ALGORITHM_ENABLED with actual env knob name from Settings
   PC_ALGORITHM_ENABLED=False ANTHROPIC_MODEL=claude-haiku-4-5-20251001 \
     uv run python benchmarks/run_longmemeval.py \
     --max-questions 100 --stratify-per-type \
     --types single-session-user,single-session-assistant,single-session-preference,multi-session,temporal-reasoning,knowledge-update \
     --output benchmarks/results/longmemeval_v5.26.0_d3_pc_off_100q.json
   ```
4. **Compare:** `delta = accuracy_PC_ON - accuracy_PC_OFF`.
   - `delta < 0` (PC hurts): disable by default. Investigate why.
   - `delta < 0.05` (PC neutral): gate as opt-in.
   - `delta >= 0.05` (PC helps): keep enabled and close revisit.
5. **Also consider:** CPU cost of PC algorithm phase.
   If PC adds more than 30s to nightly cycle AND delta < 0.05, gate behind opt-in.
6. **Record in DECISIONS.md** under new dated section.

---

## Decision rule (from D3 entry)

> Adopt-1 benchmarks produce causal-on vs causal-off accuracy numbers.
> CPU bursts traced to PC algorithm phase → retire regardless of accuracy.
> PC algorithm completion duration more than 30s on typical state → gate as opt-in.

---

## Non-goals

- No full 500-question run in this plan.
- No changes to the PC algorithm implementation itself (meek.py, independence.py, etc.).

---

## Effort estimate

| Phase | Time |
|---|---:|
| Pre-flight + config check | 0.5 hours |
| Run B if needed (wall-clock) | 2–3 hours |
| Analysis + DECISIONS.md update | 0.5 hours |
| Code change (if gate/retire) | 0.5–1 hours |
| **Total** | **3.5–5 hours** |

---

## Status

- [ ] Config knob identification pending.
- [ ] Run B not yet executed.
- [ ] Decision pending v5.26.0 pilot numbers.
