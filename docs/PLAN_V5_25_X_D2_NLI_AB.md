# PLAN — D2: NLI Diversity Stage A/B Run

**Status:** draft — awaiting v5.26.0 pilot numbers to confirm whether A/B is warranted.

**Triggered by:** Adopt-1 benchmark (v5.26.0) producing baseline QA accuracy.
**References:** `docs/DECISIONS.md` D2 entry (2026-05-30); `docs/PLAN_V5_26_0_BENCHMARK_QA_PUBLICATION.md`.

---

## Decision to make (from D2 DEFER)

> NLI diversity stage (`NLI_RERANKING_ENABLED`) is currently default-ON.
> Decision rule: if NLI contributes < 5 percentage points accuracy gain vs NLI-OFF, flip default to opt-in.
> If > 5pp gain, keep default-on and close revisit.

---

## Prerequisites

- [ ] v5.26.0 ships with Phase 1 + Phase 2 baseline numbers (NLI-ON).
- [ ] Refactor-2 recall plugin arch (v5.31.0) shipped — enables per-stage A/B without code surgery.
  - If v5.31.0 has not shipped: A/B can still be done by env var toggle (`NLI_RERANKING_ENABLED=False`),
    but per-stage metrics won't be available. Decision can be made from QA accuracy alone.

---

## Scope

This plan covers a single A/B comparison:

| Run | Config | Questions |
|---|---|---|
| A (baseline) | `NLI_RERANKING_ENABLED=True` (v5.26.0 numbers — already have these) | 100 stratified |
| B (ablation) | `NLI_RERANKING_ENABLED=False` | 100 stratified |

Cost: ~$0.40–1 additional Haiku spend (same budget as v5.26.0 pilot).
Wall-clock: ~2–3 hours for Run B.

---

## Steps

1. **Pre-flight:** confirm v5.26.0 baseline numbers in `benchmarks/results/longmemeval_v5.26.0_s_full.json`.
2. **Run B (NLI-OFF):**
   ```bash
   NLI_RERANKING_ENABLED=False ANTHROPIC_MODEL=claude-haiku-4-5-20251001 \
     uv run python benchmarks/run_longmemeval.py \
     --max-questions 100 --stratify-per-type \
     --types single-session-user,single-session-assistant,single-session-preference,multi-session,temporal-reasoning,knowledge-update \
     --output benchmarks/results/longmemeval_v5.26.0_d2_nli_off_100q.json
   ```
3. **Compare:**
   - Compute `delta = accuracy_NLI_ON - accuracy_NLI_OFF`.
   - Apply decision rule: `delta < 0.05` → flip default OFF; `delta >= 0.05` → keep default ON.
4. **Record in DECISIONS.md** under a new dated section.
5. **If flip:** bump `NLI_RERANKING_ENABLED` default from `True` to `False` in `make_benchmark_settings()` and in `Settings` default. Minor version bump.

---

## Decision rule (from D2 entry)

> A/B run shows NLI contributes less than 5 percentage points accuracy gain → flip default OFF.
> A/B run shows more than 5pp gain → keep default ON and close revisit.

---

## Non-goals

- No full 500-question run in this plan (100q pilot is sufficient for the flip/keep decision).
- No changes to the NLI model itself (cross-encoder/nli-deberta-v3-small stays).
- No changes to cross-encoder (CE) reranking (separate setting).

---

## Effort estimate

| Phase | Time |
|---|---:|
| Run B (wall-clock) | 2–3 hours |
| Analysis + DECISIONS.md update | 0.5 hours |
| Code change (if flip) | 0.5 hours |
| **Total** | **3–4 hours** |

---

## Status

- [ ] Run B not yet executed.
- [ ] Decision pending v5.26.0 pilot numbers.
