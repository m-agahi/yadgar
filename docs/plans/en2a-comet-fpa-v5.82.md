# v5.82 — BC-EN2a measured: COMET-FPA exemption + ablation → flip or retire

**Task:** #82 (queued; own train, starts after #115 / v5.81 merges)
**Status:** queued — execution gated on #115 merge
**Last ❌ in BEHAVIOR_CONTRACT.md.** This is the final un-satisfied SHALL.

## Goal

Decide COMET's fate by measurement, not assertion. PLAN_V6 §1.3.1 bans an
un-measured contract flip: flip `BC-EN2a ✅` **only if** an ablation shows
`recall@k ↑`; otherwise retire COMET (`🗑`).

The nominal fix is ~5 lines: exempt COMET inferences from the FPA cosine gate
(`enrichment/__init__.py:101`, `_apply_fpa`, threshold `FPA_SIMILARITY_THRESHOLD=0.25`
in `config.py:227`). Precedent: logic-expansion is already FPA-exempt. But the fix
cannot be validated until the benchmark harness actually runs enrichment.

## Prerequisite (verified 2026-06-23 — ablation is INVALID without it)

The LongMemEval harness does not exercise enrichment at all today:

- `benchmarks/run_longmemeval.py:~477` calls `storage.insert_memory({...})` **without**
  `settings=` / `embeddings_engine=`. The enrichment guard at
  `storage/memory.py:212` short-circuits when those are absent → **no enrichment runs**.
- `make_benchmark_settings` (`run_longmemeval.py:~365`) sets
  `COMET_ENRICHMENT_ENABLED=False`.

Consequence: the frozen **0.868** baseline (recall@5 0.868 / MRR 0.917 / qa 0.686,
v5.80) had **zero enrichment**. An ablation run today would be no-op-vs-no-op.

Two more facts that shape the test:

- Enrichment affects **vector recall only** — enriched content is re-embedded; FTS
  is unchanged. So any signal shows up only in the vector arm / fused score, never
  in pure keyword retrieval.
- The COMET model (`mismayil/comet-bart-ai2`) needs `torch` + `transformers`
  (the `ml` extra). If they fail to load, `CometInferencer.infer` silently returns
  `[]`. Must confirm load, or the "COMET on" arm is secretly COMET-off.

## Revised plan

1. **Fix harness fidelity.**
   - Pass `embeddings_engine=embeddings` + `settings=settings` to the
     `run_longmemeval.py` ingest call (~line 477).
   - Enable COMET in `make_benchmark_settings` (+ ConceptNet / Logic so the
     benchmark mirrors prod enrichment, not a COMET-only subset).
   - **Positive control:** after seeding, assert at least one sample memory has a
     non-empty `enrichment_comet` field. If empty → ablation declared invalid,
     abort (don't report numbers from a silent no-op).
   - This step is worth landing on its own merit: the benchmark has never reflected
     prod enrichment, a real fidelity gap.

2. **Apply the COMET-FPA exemption** — the actual EN2a code change
   (`enrichment/__init__.py:101`).

3. **Ablation.** Two arms, both with enrichment running:
   - A: COMET FPA-**exempt** (the fix)
   - B: COMET FPA-**gated** (current behavior)
   - Compare `recall@k` against a freshly re-run **enrichment-on** baseline
     (the 0.868 baseline is enrichment-off and cannot be the comparand).
   - Cost note: ~2× full LongMemEval runs (~8h each). Budget accordingly.

4. **Decide.**
   - `recall@k ↑` → flip `BC-EN2a ✅`, ship the exemption.
   - flat / `↓` → **retire COMET** (`🗑`): drop the dependency + config + contract row.

## Files in scope

- `benchmarks/run_longmemeval.py` — ingest plumbing (`settings`/`embeddings_engine`),
  `make_benchmark_settings` COMET flag, positive-control assertion.
- `yadgar/enrichment/__init__.py:101` — `_apply_fpa` COMET exemption.
- `yadgar/config.py` — `FPA_SIMILARITY_THRESHOLD` (227), `COMET_ENRICHMENT_ENABLED` (216).
- `docs/BEHAVIOR_CONTRACT.md` — flip BC-EN2a ✅ or mark 🗑 (+ tally).
- `docs/CAPABILITY_REGISTRY.md` — COMET capability row LIVE or DEAD per outcome.

## Done =

Ablation valid (positive control passes) **and** BC-EN2a resolved one way — flipped
✅ with measured `recall@k ↑`, or COMET retired 🗑 with the measured flat/↓ result
recorded. One PR at end of train.
