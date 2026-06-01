# PLAN — v6.3.0: Adopt-7 LLM extract-on-ingest (SKELETON)

**Status:** SKELETON — 2026-06-01. Placeholder for future drafting. Not ready for impl.

**Re-slotted 2026-06-01 night:** was v5.36.0 standalone; moved to v6.3.0 sub-release inside v6 LLM curator framework. Rationale: extract-on-ingest IS LLM inference on memory pipeline → v6 territory by definition. v5.36 standalone would have built parallel LLM infra; v6.0 ships the scaffolding (model routing, scheduler, safety, knobs) and v6.3 plugs in as one of several curator jobs. v6 sub-release order: v6.0 scaffolding → v6.1 dedup+cleanup → v6.2 synthesis → **v6.3 Adopt-7 extract-on-ingest** → v6.4 contradiction+staleness → v6.5 correlation.

**Realistic eval target re-scoped:** original skeleton aimed at closing the −25pp gap to mem0 (cloud GPU + GPT-4o-class). Unrealistic on laptop 8B local. Revised target: **+5pp on multi-session + temporal-reasoning categories** (the two synthesis-heavy types). Anything more = bonus.

**Knob-gated (ALL v6 jobs share this surface):**
```
YADGAR_V6_ENABLED=0                # master switch, default OFF
YADGAR_V6_DRY_RUN=1                # default ON, log proposed ops
YADGAR_V6_WINDOW_START=19:00
YADGAR_V6_WINDOW_END=23:00
YADGAR_V6_RATE_LIMIT_PER_HOUR=50
YADGAR_V6_MAX_OPS_PER_NIGHT=20     # circuit breaker
YADGAR_V6_SCOPE_HEAT_MAX=0.2
YADGAR_V6_SCOPE_AGE_MIN_DAYS=30
YADGAR_V6_PROTECTED_SKIP=1

# Per-job (Adopt-7 specific):
YADGAR_V6_JOB_EXTRACT_ENABLED=0           # default OFF even when v6 ON
YADGAR_V6_JOB_EXTRACT_MODEL_TIER=reasoning # fast|reasoning
YADGAR_V6_JOB_EXTRACT_TAGS_ALLOWLIST=*
YADGAR_V6_JOB_EXTRACT_BATCH_SIZE=10
YADGAR_V6_JOB_EXTRACT_EVAL_GATE_PCT=5     # +5pp target on synthesis-heavy categories
```

**Origin:** v5.26.0 LongMemEval-s 500q result. yadgar 69.4% vs mem0 94.4%
(−25pp). Reader-attributable failures = 76/153 errors (≈50%). Retrieval already
strong (R@10=0.906, MRR=0.928). The gap is reader synthesis from raw chunks.

**Hypothesis:** mem0's lead is LLM-extract-at-write-time → reader gets
pre-distilled atomic facts instead of raw conversation chunks. Adopting this
pattern should close most of the per-type gaps where reader fails despite
gold context in top-10 (single-session-preference 33%, multi-session 56%,
temporal-reasoning 64%).

---

## 1. Problem

Yadgar stores: raw chunks + summaries. Reader at recall time must synthesize
across N retrieved chunks. On synthesis-heavy categories the reader picks one
snippet and ignores the rest, or fails to aggregate facts across sessions.

mem0 stores: LLM-extracted atomic facts ("user graduated Business Admin
in 2018", "user commutes 45 min each way"). Reader gets dense pre-distilled
rows. Synthesis already done at ingest.

Per-type gap (v5.26.0):
- single-session-preference: 33.3% — reader picks wrong preference fragment
- multi-session: 55.6% — reader fails to aggregate across sessions
- temporal-reasoning: 63.9% — reader can't reason "which is latest"
- knowledge-update: 75.6% — reader returns superseded fact

## 2. Constraints

- **I1 (request path thin):** NO LLM call in MCP handler. Extraction lives in
  drainer or ConsolidationScheduler.
- **I2 (drainer single catch-up):** drainer already does encode + vector + entities.
  Adding LLM extract here = scope creep. Probably ConsolidationScheduler.
- **I4 (ML via to_thread):** if extract runs in async context, must to_thread.
- **I6 (no double-pay):** extraction runs once per source memory. Use
  `consolidation_state` field or new `extracted=True` marker.
- **I9 (write-path budget ≤5ms p50):** zero impact on memorize/recall hot paths.

## 3. Open design questions

1. **Trigger:** per-memory at consolidation cycle vs batched (every N memories
   or every M minutes)?
2. **Model:** reuse v6 curator tier (deepseek-r1:8b nightly) or sync small
   model (qwen3:8b in drainer)? Cost: nightly is cheap but stale; sync is
   fresher but blocks drainer throughput.
3. **Storage:** extracted facts as new memory rows linked via
   `extracted_from` → source memory? Or new `extracted_fact` table? Or
   `compression_level=N` on existing row? Lean: new rows + `extracted_from`
   edge — reuses recall/heat/decay infra.
4. **Retrieval interplay:** at recall time, return both raw chunks AND
   extracted facts? Or prefer extracted when present? Risk: double-counting
   in fusion ranker.
5. **Schema for extracted fact:** atomic statement + provenance + entities +
   timestamps? Mirror mem0's schema or yadgar-native (richer with our
   bi-temporal + branch fields)?
6. **Backward compat:** ~520k existing memories — extract retroactively
   (one-time backfill night) or only forward? Lean: forward + opportunistic
   backfill during nightly curator.
7. **Eval:** re-run LongMemEval-s 500q with extracted facts in haystack.
   Target: close to mem0 94.4%. Stretch: beat by leveraging bi-temporal.
8. **Cost model:** estimate LLM tokens/memory. At 520k memories × ~500 prompt
   tokens × N extracted facts → total inference budget for backfill +
   ongoing.
9. **Failure mode:** extract returns garbage / wrong facts. Quality gate?
   Confidence threshold? Re-extract on next curator pass?
10. **Interaction with NLI contradiction (Adopt-2 v5.17.0):** extracted facts
    may contradict each other across sessions. Same contradiction handler
    applies?

## 4. References

- `docs/competitor-audit-2026-05-30.md` §1 — mem0 architecture
- `benchmarks/results/longmemeval_v5.26.0_s_full.json` — per-type breakdown
- v6 curator anchor (mem 484431) — nightly LLM tier already exists, may host
- `docs/ARCHITECTURE_INVARIANTS.md` — I1/I2/I4/I6/I9 constraints
- `yadgar/consolidation/scheduler.py` — likely host module

## 5. Effort estimate

UNKNOWN. Skeleton stage. Probable range: 5-10 days impl + 1-2 days eval.
Refine when drafting.

## 6. Dependencies

- v6 LLM curator infrastructure should exist (model routing, scheduler,
  scope-limit safety, soft-delete recovery). If v6 not shipped yet, this
  plan blocks on v6.
- Bi-temporal schema (v5.29.0 Adopt-3) helpful but not required.
- Plugin recall pipeline (v5.31.0 R2) makes the "extracted-fact-aware
  fusion" easier to slot.

## 7. Risk register

- LLM extraction quality: garbage in → garbage facts → worse than raw chunks.
- Cost: 520k backfill may be expensive.
- Storage bloat: each memory → N extracted facts → table grows N×.
- Reader interaction: fusion ranker may double-count if both raw + extracted
  surface.
- Invariant violation: easy to accidentally call LLM in request path.

## 8. Decision points to resolve before impl

- DP-A: trigger model (drainer-sync vs scheduler-batch vs nightly)
- DP-B: model selection (sync small vs nightly reasoning)
- DP-C: storage shape (new rows vs new table vs field)
- DP-D: backfill policy (forward-only vs nightly-opportunistic vs full)
- DP-E: eval gate (must hit X% on LongMemEval-s before shipping)

---

## Next steps when picking this up

1. Re-read this skeleton.
2. Pull current mem0 V3 algorithm details — has it changed?
3. Confirm v6 curator landed; if so, prototype extraction as new curator job.
4. Resolve DP-A through DP-E in a discussion turn with user.
5. Convert skeleton → full plan with §Implementation, §Test plan, §Rollout.
6. Add to architecture invariants if any new I-rules needed (e.g. "extracted
   facts MUST link to source via `extracted_from` edge").
