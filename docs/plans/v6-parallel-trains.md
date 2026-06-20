# v6 Parallel Trains — execution plan (2026-06-20)

Composed from the plan audit + file-collision map. Goal: maximise simultaneous,
collision-free work. Each train = its own branch → **one PR at the end**.

## Hard ordering constraints (only these — everything else is free)

1. **recall.py chain is sequential:** `#47 cog-map decide` → `#30 recall rebuild` → `#67 consensus landscape`. All rewrite the same function. The long pole.
2. **#41 lands before any retrieval/config edit:** pure deletion (24 dead knobs + dual-vector + confidence-gating + `remember`). Clears the base for #30 and frees `memorize.py` for #32/#35.
3. **Doc contention is a merge discipline, not a blocker:** `docs/BEHAVIOR_CONTRACT.md` (lint-enforced tally) + `docs/CAPABILITY_REGISTRY.md` (I32). Rule: each branch rebases on master before merge; only one BC-tally-touching PR merges at a time; registry edits stay section-local.

## Per-train task lists

### T1 · Harness (v6 Phase 0) ⭐ keystone
Branch `feat/v6-t1-eval-harness`. Code: `benchmarks/`, `metrics.py`, config knobs.
- [ ] 0.1a `make eval` target + adapter scoring recall@k / MRR / nDCG@k / latency p50/p95 over a golden set, reusing `benchmarks/run_longmemeval.py` + `isolated_surreal()` + existing `compute_recall`/`compute_ndcg`.
- [ ] 0.1b bootstrap golden set: auto-draft ~50–100 `(query, relevant_memory_ids[])` from the live store, written to `benchmarks/golden/` flagged `# CURATE` (human refines later).
- [ ] 0.1c committed baseline report (`benchmarks/reports/baseline-v5.74.md`).
- [ ] 0.1d wire `make eval` into CI **non-gating** first.
- [ ] 0.2 data-quality metrics: `%valid-embedding`, `duplicate-rate`, `zombie-rate`, `domain-coverage`, `surprise-distribution` → Prometheus + `yadgar stats`. Promote null-embedding to hard invariant.
- [ ] registry + contract entries; I32 + contract lint green.

### T2 · Viz fidelity (#33)
Branch `feat/v6-t2-viz-fidelity`. Code: `static/*`, `graph_api.py`, `http.py`, `_phase_post_write.py`.
- [ ] F1 `graph-detail.js` connection count from full edge-toggle set (not 4-of-11) — entity "0 connections" bug.
- [ ] F3 panel shows typed id (`entity:3103` not `3103`); route lookups by type.
- [ ] F4 `graph_api.py:195` weak edges (count<2) — render thin OR "N weak edges hidden" affordance.
- [ ] F2 heat staleness: emit `heat_updated` SSE on decay (or periodic re-sync / stale indicator).
- [ ] F5 single-source-of-truth fidelity test: known graph → mutate heat+edge → assert `/api/graph` payload + panel count == DB.
- [ ] registry entries for changed viz surface; I32 green.

### T3 · Dead-config/code cleanup (#41) — land FIRST, fast
Branch `feat/v6-t3-deadconfig`. Code: `config.py`, `config_yaml.py`, `retrieval/core+fusion`, `memorize.py`.
- [ ] delete dead Settings: `WRRF_K`, `CONFIDENCE_*` (5), `BELIEF_MIN_CONFIDENCE`, `BELIEF_SEARCH_PRIORITY_FOR_OPEN_DOMAIN`, `TEMPORAL_RETRIEVAL_ENABLED`+`TEMPORAL_BOOST_WEIGHT`+`TEMPORAL_DECAY_DAYS`+`TEMPORAL_EXACT_MATCH_BOOST`, `QUERY_PREFIX`, `EMBEDDING_CACHE_SIZE`, `PLASTICITY_*` (2), `STABILITY_INCREMENT`, `RECONSOLIDATION_*` (2), `CONSOLIDATION_COOLDOWN_SECONDS`, `IDLE_THRESHOLD_SECONDS`, `FRACTAL_LEVELS`, `COMPRESSION_GIST_AGE_HOURS`, `COMPRESSION_TAG_AGE_HOURS`. KEEP `BELIEF_HIGH_CONFIDENCE_BOOST` (wired).
- [ ] matching `config_yaml.py` schema deletions + I25 three-way-sync.
- [ ] delete `_dual_vector_search()` + `DUAL_VECTORS_ENABLED` guard (`retrieval/core.py`); delete `_apply_confidence_gating()` (`retrieval/fusion.py`).
- [ ] delete `remember` stub (`memorize.py`) — T3 OWNS this (not #32).
- [ ] update CAPABILITY_REGISTRY: retire ~15 entries to status removed / drop dead-knob refs; I32 STALE-clean.
- [ ] update tests that assert removed fields.

### T4 · Heat-decay single-writer (#59)
Branch `feat/v6-t4-heat-decay`. Code: `consolidation/*`.
- [ ] restructure `_HeatDecayMixin`: collect intents → reconcile → single `batch_writes`.
- [ ] orchestrator submits heat intents, no direct writes.
- [ ] single-write facade (`storage/heat_writer.py` or `client.py`).
- [ ] update `test_decay_recall_modulation` / `test_domain_decay`; new single-writer BC-C* test.
- [ ] rebase `consolidation/__init__.py` on T3 (CONSOLIDATION_COOLDOWN removal).

### T5 · e2e Phase 3 + cognitive-map decide (#47)
Branch `feat/v6-t5-e2e-cogmap`. Code: `tests/e2e/`, `cognitive_map.py`, `_state.py`, `lifecycle.py`, maybe `recall.py` (SR block).
- [ ] DECIDE cognitive_map fate (wire `compute_sr_matrix` to a real path OR delete). **Settle the SR-in-recall.py question before T6/#30 starts.**
- [ ] if delete: remove `cognitive_map.py`, `_state._cognitive_map`, lifecycle init, recall.py SR transition block.
- [ ] drive a batch of remaining ⏳ BC items → ≥1 real e2e each (Phase-1 critical paths first: BC-A1/2/3, BC-B1–4, BC-C1–3, BC-D2, BC-PCd2).
- [ ] coverage-lint extension: BC-* without a test → CI fail (or document gap honestly).
- [ ] contract tally updated; contract-coverage lint green.

## Wave 2 (after T3 + T5 land)
- **T6 · #30 recall rebuild** (blocked by T3, T5) — SourceProvider, DB DirectoryFilter, cross-type fusion, `recall type=all`, `wiki_query`→alias. Long pole.
- **T7 · #32 + #35** (blocked by T3) — tool-surface (reembed_all bug, bootstrap/seed) + fresh-memory-restore (recent_memories, restore freshness, memorize returns id).
- **T8 · #34 repo-wiki-native** — new `repo_wiki/` package (Option A); `wiki.py` touch waits on T6.

## Wave 3 (after T6 + T1)
- **#67 consensus landscape** (blocked by T6) — plugs into new recall interface; measured on T1 harness.
- **v6 Phase 1.1** surprise-gate ON (staged) · **1.3** enrichment/EN2a-FPA decision · **2.2/2.3/2.4** SR/embedding/reranker ablations · **3.x** brain-dynamics — all gated on T1 harness.

## Rate-limiter (brutal honesty)
T1 golden-set **curation** is human judgment, not tokens — the real bottleneck. #30 is the only true long pole (L + sequential with #67). 2 days ≈ all of Wave 1 + start Wave 2; Wave 3 needs T1's curated golden set.
