# PLAN — v5.31.0: Recall pipeline plugin architecture (R2 from 2026-05-30 audit)

**Renumbered:** v5.20.0 → v5.31.0 on 2026-05-30. Reason: skip-1 minor convention adopted 2026-05-30 — odd-only minors for sequential features, even slots reserved for hotfix patches between them.

**Status:** drafted 2026-05-30. Plan-first per I27. Implements Refactor-2 (ADOPT) from `docs/competitor-audit-2026-05-30.md` and `docs/DECISIONS.md` R2 entry.

**Master at draft time:** core v5.10.3 shipped; v5.10.4 in-flight; later v5.10.x train + v5.11.0 + v5.13.0 + v5.15.0 + v5.17.0 + v5.27.0 + v5.29.0 drafted.

**Sequencing:** v5.31.0. Must ship AFTER v5.25.0 (Adopt-1 benchmarks) — the plugin architecture is the substrate for A/B testing individual stages, which is only meaningful once a baseline accuracy number exists. Independent of v5.27.0 (DuckDB) and v5.29.0 (bi-temporal).

---

## Why

`recall()` today is a tightly-coupled pipeline of stages (FTS / KNN / PPR / spreading / temporal → WRRF fusion → CE rerank → NLI diversity → MMR → adversarial → rules engine). Each stage was added empirically. There is no clean way to:

1. **A/B test a stage** — to answer "does NLI actually help?" (Ditch D2) or "does PC causal discovery improve retrieval?" (Ditch D3), we need to run recall WITH and WITHOUT that stage on the same query set and compare quality. Today that requires hand-editing `recall()`.
2. **Tune per-call profiles** — different consumers want different latency/quality tradeoffs. SessionStart hook = fast (low-latency). Conscious recall = balanced. Curator dispatch = full (max quality). Today one recall to rule them all.
3. **Observe per-stage cost** — which stage contributes most to recall p95? Today the duration metric is aggregated; per-stage histograms exist only ad-hoc.
4. **Swap a stage** — try a different rerank model, an alternative graph traversal algorithm, etc. — without rewriting `recall()`.

Audit recommendation: each stage = a registered plugin. Pipeline = ordered list of plugins. Per-call config picks which plugins run.

Patterns from competitors:
- **mem0** — swappable vector backends + retrieval strategies (`mode="basic"|"advanced"`).
- **Letta** — pluggable archival stores.
- **LangChain retrievers** — common interface, swappable.

---

## Goals

1. **`RetrievalStage` interface** — each stage implements `name`, `apply(state) -> state`, `enabled`, optional `config`.
2. **`RetrievalPipeline` orchestrator** — ordered list of stages; iterates with timing; collects per-stage stats; supports per-call enable/disable.
3. **Pre-configured profiles** — `fast` / `balanced` (= current default) / `full` / `debug` selectable via `recall(profile=...)` kwarg or env knob.
4. **Per-stage Prometheus metrics** — `yadgar_recall_stage_duration_seconds{stage="ce_rerank",profile="balanced"}` histograms.
5. **A/B test harness** — `recall_compare(query, profiles=["balanced","balanced_no_nli"])` returns both result sets side-by-side for benchmark scripts to evaluate.
6. **Backward compatible** — existing `recall(query, max_results, min_heat)` callers unaffected; `profile="balanced"` is default and matches current behavior.
7. **No silent regression** — plugin extraction must preserve current retrieval quality. Validated against v5.25.0 LongMemEval baseline before merge.

---

## Non-goals

- **Per-stage model swapping** in v5.31.0 (e.g. trying a different CE model) — that's v5.31.x patch territory once the interface is stable.
- **External plugin loading** (e.g. user-supplied plugins via entrypoints) — internal-only for v5.31.0.
- **Pipeline visualization in viz** — could be a v5.31.x viz add-on later.
- **Replacing WRRF fusion algorithm** — that's a research project; here we just preserve current behavior in plugin form.

---

## Approach

### File layout

```
yadgar/retrieval/
├── __init__.py
├── pipeline.py           # RetrievalPipeline class
├── state.py              # RetrievalState dataclass
├── profiles.py           # built-in profiles (fast/balanced/full/debug)
└── stages/
    ├── __init__.py
    ├── base.py           # RetrievalStage abstract base
    ├── fts.py            # full-text search stage
    ├── knn.py            # vector KNN stage
    ├── ppr.py            # personalized PageRank stage
    ├── spreading.py      # spreading activation stage
    ├── temporal.py       # temporal decay stage
    ├── fusion.py         # WRRF fusion stage
    ├── ce_rerank.py      # cross-encoder rerank stage
    ├── nli.py            # NLI diversity stage
    ├── mmr.py            # maximal marginal relevance stage
    ├── adversarial.py    # adversarial filter stage
    └── rules.py          # rules engine stage
```

Migrate stage-by-stage; each extracted stage gets a unit test plus a regression test (output before/after extraction must match on a fixture query set).

### Interface

```python
# yadgar/retrieval/state.py
@dataclass
class RetrievalState:
    query: str
    query_embedding: list[float] | None
    candidates: dict[int, RetrievalCandidate]  # memory_id → candidate
    stage_stats: dict[str, dict]  # stage_name → {duration_ms, count_in, count_out, ...}
    profile: str
    config: dict  # per-call overrides (e.g. max_results, min_heat)

# yadgar/retrieval/stages/base.py
class RetrievalStage(ABC):
    name: str  # e.g. "fts", "knn", "ce_rerank"

    @abstractmethod
    def apply(self, state: RetrievalState) -> RetrievalState: ...

    def is_enabled(self, profile: str, config: dict) -> bool:
        """Default: enabled in all profiles. Subclasses override for opt-in stages."""
        return True
```

### Profiles

```python
# yadgar/retrieval/profiles.py
PROFILES = {
    "fast": [
        "fts",      # cheap text match
        "knn",      # vector lookup
        "fusion",   # WRRF
        # skip PPR, spreading, temporal, CE rerank, NLI, MMR, adversarial, rules
    ],
    "balanced": [  # current default behavior
        "fts", "knn", "ppr", "spreading", "temporal",
        "fusion", "ce_rerank", "nli", "mmr", "adversarial", "rules",
    ],
    "full": [
        # same as balanced today; reserved for future heavy-stage additions
    ],
    "debug": [
        # all stages PLUS per-stage diagnostic emit
    ],
}
```

### Pipeline orchestrator

```python
# yadgar/retrieval/pipeline.py
class RetrievalPipeline:
    def __init__(self, stages: list[RetrievalStage]):
        self.stages = {s.name: s for s in stages}

    def run(self, state: RetrievalState) -> RetrievalState:
        stage_names = PROFILES[state.profile]
        for name in stage_names:
            stage = self.stages[name]
            if not stage.is_enabled(state.profile, state.config):
                continue
            t0 = time.perf_counter()
            state = stage.apply(state)
            dt_ms = (time.perf_counter() - t0) * 1000
            state.stage_stats[name] = {"duration_ms": dt_ms, ...}
            _stage_histogram.labels(stage=name, profile=state.profile).observe(dt_ms / 1000)
        return state
```

### Public API change

```python
# yadgar/server/tools/recall.py
@_tool()
def recall(
    query: str,
    max_results: int = 10,
    min_heat: float = 0.0,
    profile: str = "balanced",  # NEW
    stage_overrides: dict | None = None,  # NEW — {"nli": False} disables NLI for this call
) -> dict:
    ...
```

Existing callers unaffected — `profile="balanced"` reproduces today's behavior.

### A/B comparison harness

```python
# yadgar/retrieval/compare.py
def recall_compare(query: str, profiles: list[str], max_results: int = 10) -> dict:
    """Run the same query under multiple profiles; return side-by-side results
    + per-stage timing for each profile. Used by benchmark scripts to A/B test."""
```

### Per-stage Prometheus metrics

New metric series:
- `yadgar_recall_stage_duration_seconds{stage,profile}` — histogram per stage per profile
- `yadgar_recall_stage_candidates_in{stage,profile}` — gauge
- `yadgar_recall_stage_candidates_out{stage,profile}` — gauge
- `yadgar_recall_profile_invocations_total{profile}` — counter

I23 invariant satisfied (each metric has a writer in the plugin).

---

## Tests (red-first per TDD)

### Phase 0 — Interface
1. `test_retrieval_state_dataclass` — state has all expected fields
2. `test_pipeline_iterates_stages_in_order` — fixture stages with known side-effects, assert order preserved
3. `test_pipeline_skips_disabled_stages` — stage with `is_enabled=False` not called
4. `test_pipeline_collects_per_stage_stats` — stats dict populated after each stage
5. `test_profile_balanced_matches_legacy_recall` — golden output comparison against pre-extraction recall on 10 fixture queries

### Phase 1 — Per-stage extraction (one test set per stage)
6. `test_fts_stage_returns_expected_candidates_on_fixture`
7. `test_knn_stage_returns_expected_candidates_on_fixture`
...11 stages × ~2 tests each = ~22 tests

### Phase 2 — Profile selection
8. `test_profile_fast_skips_heavy_stages` — fast profile runs only fts+knn+fusion
9. `test_profile_full_runs_all_stages`
10. `test_profile_debug_emits_diagnostic_logs`
11. `test_invalid_profile_raises` — ValueError on unknown profile

### Phase 3 — Per-call overrides
12. `test_stage_override_disables_nli_for_one_call`
13. `test_stage_override_does_not_persist_to_next_call`

### Phase 4 — Metrics
14. `test_per_stage_duration_metric_observed`
15. `test_profile_invocation_counter_increments`

### Phase 5 — A/B harness
16. `test_recall_compare_returns_both_profiles`
17. `test_recall_compare_timing_breakdown_consistent`

### Phase 6 — Regression (CRITICAL)
18. `test_full_pipeline_output_matches_pre_extraction_on_fixture_set` — bit-for-bit same results on 50 fixture queries (where "bit-for-bit" allows for floating-point tolerance)
19. `test_balanced_profile_p95_latency_within_5pct_of_legacy` — measured against benchmark fixture

---

## Acceptance

- All ~25 tests green
- Existing recall test suite still passes (no behavioral regression)
- `recall(query, profile="balanced")` produces identical (or equivalent within float tolerance) output to v5.25.x `recall(query)`
- New per-stage Prometheus metrics visible in Grafana
- v5.25.0 LongMemEval benchmark numbers reproduce within 0.5 percentage points after this refactor (no quality regression)
- CHANGELOG + MIGRATION_NOTES updated for v5.31.0
- Pre-commit hooks pass (I13 / I23 / I24 / I25 / I26 / I27)

---

## Open questions

1. **Where does `query_embedding` get computed?** — outside the pipeline (in caller) or as a pre-stage? Lean pre-stage so different profiles could swap embedding model.
2. **Stage-level retry / circuit breaker?** — current code has CB-1 on CE rerank backend. Should that be in the CE stage's `apply()` or external? Lean external; CE stage just calls the existing `MLClient.rerank_ce()`.
3. **Async stages?** — could run FTS + KNN in parallel. Current code is serial. Lean: keep serial in v5.31.0; parallel = v5.31.x optimization.
4. **Per-stage config schema?** — e.g. NLI threshold, MMR lambda. Inline kwargs vs typed config classes? Lean: typed config classes (pydantic) — easier to validate, documents the API.
5. **Backward-compat shim duration?** — keep `recall(query, max_results)` signature working forever. Profile + stage_overrides are additive — old callers don't break.

---

## Dependencies + non-deps

**Hard prereq:** v5.25.0 ships first. Need a baseline benchmark number to validate "no quality regression after extraction."

**Independent of:** v5.10.x train, v5.21.0, v5.23.0, v5.27.0, v5.29.0.

**Unblocks:**
- **D2 NLI default decision** — once plugin arch exists + benchmarks exist, can A/B `balanced` vs `balanced_no_nli` and decide
- **D3 PC causal discovery validation** — same; `balanced_no_pc_causal` profile lets us measure
- **v5.31.x optimizations** — per-stage caching, async stage execution, alternative model swaps

---

## Risk + rollback

| Risk | Mitigation |
|---|---|
| Refactor introduces silent retrieval-quality regression | Phase 6 regression tests on 50 fixture queries + LongMemEval baseline comparison |
| Per-stage metric cardinality explodes | profile dimension capped to 4 known values; stage dimension capped to ~12 |
| Latency regression from plugin overhead | benchmark p95 within 5% of legacy gate in Phase 6 |
| Breaking existing recall callers | profile defaults to "balanced" = legacy behavior; all kwargs additive |
| Stage extraction misses subtle state mutations | RetrievalState carries all state; stages can only mutate via return value |

**Rollback:** revert the v5.31.0 commits. Recall returns to monolithic implementation. No data migration involved.

---

## Files to add / modify

### New
- `yadgar/retrieval/__init__.py`, `pipeline.py`, `state.py`, `profiles.py`
- `yadgar/retrieval/stages/` (11 stage files + base.py)
- `yadgar/retrieval/compare.py` (A/B harness)
- `yadgar/tests/test_retrieval_pipeline.py` (~25 tests)
- `yadgar/tests/test_retrieval_stages_*.py` (per-stage test files)
- `yadgar/tests/fixtures/recall_golden_queries.jsonl` (50-query regression set)

### Modify
- `yadgar/server/tools/recall.py` — replace monolithic body with pipeline.run() call
- `yadgar/metrics.py` — register new per-stage metric series
- `CHANGELOG.md` + `MIGRATION_NOTES.md` — v5.31.0 sections
- `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock` — version bump

---

## Estimated effort

| Phase | Days |
|---|---|
| 0 Interface + pipeline skeleton + Profile selection | 1 |
| 1 Extract 11 stages (one at a time, regression-tested) | 4-5 |
| 2 Metrics + A/B harness | 1 |
| 3 Regression validation against LongMemEval baseline | 1 |
| Release artifacts + CHANGELOG + MIGRATION_NOTES | 0.5 |
| **Total** | **7-8 days** |

Larger than most v5.x patches because it touches the hottest code path. Worth the investment per audit rationale + AUDIT_DECISIONS.md R2 = ADOPT.

---

## Cross-references

- `docs/competitor-audit-2026-05-30.md` Refactor R2 — audit recommendation source
- `docs/DECISIONS.md` R2 entry — formal decision: ADOPT
- `docs/PLAN_V5_25_0_BENCHMARK_PUBLICATION.md` — hard prereq (provides regression baseline)
- D2 + D3 entries in `docs/DECISIONS.md` — unblocked by this plan + Adopt-1 ship
