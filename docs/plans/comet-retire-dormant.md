# PLAN — Retire COMET to DORMANT (keep code, flip the flag off)

Status: **PLANNED 2026-06-24.** Decision: ADR-0004 (RETIRE COMET). User directive: **disable via the flag, KEEP the code dormant — do NOT delete** ("niche feature, worked hard on it; rather dormant than gone").

theme: enrichment / config / decommission-to-dormant
trigger: en2a ablation verdict (benchmarks/reports/en2a_comet_ablation_2026-06-24.md) — COMET net-negative recall (multi-session R@5 −4.2pt), ~17h/10-core cost.

## KEY CORRECTION (found 2026-06-24)
`COMET_ENRICHMENT_ENABLED` default is **`True`** (`config.py:221`), NOT False. Prior notes (ADR-0004 first draft, report, memory) wrongly said "already the default False" — that was the BENCHMARK harness override (`make_benchmark_settings`). In PRODUCTION COMET has been ENABLED by default (running on drained memories, or silently failing to load `comet-bart` weights and being swallowed by the enrichment try/except). So retiring COMET is a REAL config flip, not a no-op — and may remove hidden production load.

## Cost of "keep dormant" = ~zero (verified)
- `transformers`/`torch` are SHARED by `embeddings.py` + `doc2query.py` + `_seq2seq.py` — NOT COMET-only. Keeping COMET code adds NO extra dependency.
- COMET model is lazy-loaded (`comet.py::_ensure_model` → `from_pretrained` only on first `infer()`). Disabled → never loaded → no weights fetched, no runtime cost.
- So dormant COMET = dead-weight-free: the code sits behind an `if settings.COMET_ENRICHMENT_ENABLED:` gate that is now False.

## Steps

### S1 — Flip the flag off (the core change) — code, TDD, I25 three-way
- `yadgar/config.py:221`: `COMET_ENRICHMENT_ENABLED: bool = True` → `False`.
- I25 three-way sync: update the matching default in `config_registry.py` (the `YADGAR_COMET_ENRICHMENT_ENABLED` ConfigEntry default string) and `config_yaml.py` FIELD_META if it carries a default. Confirm all three agree on `false`.
- Test: assert the default is False (a config test) + that the enrichment pipeline skips the COMET branch when disabled (likely already covered — extend if not).
- Leave `COMET_FPA_EXEMPT` as-is (dormant sub-knob; harmless, default False).

### S2 — Mark the code DORMANT (so no one deletes or silently re-enables it)
- Banner comment at the top of `yadgar/enrichment/comet.py` + at the COMET branch in `yadgar/enrichment/__init__.py`: "RETIRED / DORMANT per ADR-0004 + benchmarks/reports/en2a_comet_ablation_2026-06-24.md (net-negative recall, prohibitive cost). Intentionally retained, NOT dead code. Do not enable without re-validating against the ablation."
- **Re-enable guard (recommended):** if `COMET_ENRICHMENT_ENABLED` is True at startup, log a WARNING ("COMET enrichment is retired/dormant per ADR-0004 — re-validate before relying on it"). Cheap insurance against silent re-enable. [DECISION NEEDED — see Open questions.]

### S3 — Update all COMET docs to DORMANT (not LIVE, not deleted)
- `docs/CAPABILITY_REGISTRY.md`: the COMET capability entry → status **DORMANT/RETIRED** (was LIVE/dormant-by-empty-endpoint) + cite the verdict + "intentionally retained dormant."
- `docs/architecture.md` + any enrichment/features doc: COMET section → "retired to dormant 2026-06-24 (ADR-0004); code retained, flag off by default."
- `docs/CHANGELOG.md`: entry — COMET enrichment retired to dormant (flag default True→False), code kept.
- Keep COMET tests RUNNING (they guard the dormant code so it still works if ever re-validated) — but confirm they're hermetic (mock/skip the real `comet-bart` download; don't pull weights in CI). [VERIFY — see Open questions.]

### S3b — Behavior contracts (BEHAVIOR_CONTRACT.md §Enrichment)
The retire resolves the two COMET contracts (neither is currently CI-red — BC-EN2a is xfail + model-skip-guarded):
- **BC-EN2a** ("COMET adds inferred commonsense triples", ❌ #64, xfail): was parked on "v6 enrichment-tuning decides whether un-FPA'd COMET helps recall." The ablation DECIDED it (no). Re-mark BC-EN2a as **WON'T-IMPLEMENT — COMET retired per ADR-0004** (stays xfail/skip; not green, intentional, non-blocking). Update the contract note + the test's xfail reason.
- **BC-EN2b** ("if COMET disabled → config reports disabled + emits exactly ONE startup warning", ⏳ #39, unimplemented): the retire makes this the live contract. The S2 re-enable/disabled-state startup WARNING satisfies it. Implement it → BC-EN2b goes **GREEN**. (This is why the S2 guard is recommended, not optional — it closes a real ⏳ contract.)
- BC-EN3a (doc2query) + other enrichers: SEPARATE, untouched.

### S4 — ADR + memory + en2a branch
- ADR-0004 (wiki yadgar-adr-log): correct decision/consequences to "disable via flag (default True→False) + KEEP dormant, do NOT delete; retain shared deps." (done in this pass).
- Correct the verdict memory (it said "optionally remove code/dep" — now "keep dormant").
- `feat/en2a-comet-fpa-exempt` branch: it adds `COMET_FPA_EXEMPT` (dormant COMET sub-knob) + the benchmark flags (`--comet`/`--comet-fpa-exempt`/`--enrich`) + tests. Disposition: **merge it** (the knob is dormant COMET surface, the flags are reusable eval infra) rather than cherry-pick-and-drop. [DECISION NEEDED.]

## Open questions for the user (the "did I miss anything")
1. **The default was TRUE** — confirm: flip to False (yes, per retire). Worth knowing COMET may have been running/failing in prod; the flip makes dormant explicit + may relieve hidden load.
2. **Re-enable guard** (S2): add the startup WARNING when enabled? (recommend yes — cheap.)
3. **en2a branch** (S4): merge the whole branch (COMET_FPA_EXEMPT knob + benchmark flags + tests), or cherry-pick only the benchmark flags and drop the toggle? (recommend merge — keeps dormant surface + eval infra.)
4. **COMET unit-test cost**: confirm the COMET tests are hermetic (no `comet-bart` weight download in CI). If they pull the real model, decide skip-by-default vs keep.
5. Single PR or split? Recommend ONE small PR ("retire COMET to dormant") = flag flip (I25) + code banners + doc updates, separate from the en2a-branch merge.

## Non-goals
- Do NOT delete COMET code, tests, or the shared `transformers`/`torch` deps.
- ConceptNet / Doc2Query / Logic enrichers are SEPARATE and untouched.
