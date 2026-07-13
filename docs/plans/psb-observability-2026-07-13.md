# P-SB Recall-Observability Car — expose per-stage recall latency on /metrics (tasks #6 + #50)

**Status:** AUDITED — ready (build GO). Must-fixes folded in below (stale paths → `yadgar/backend/retrieval/` + `yadgar/_shared/observability/`; except-narrowing → else-block form; ADR-0078→ADR-0105 attribution; core 5.132→5.133 bump is manual/unenforced; sweep scope-bounded). ONE non-blocking user-decision item remains: §8 q2 (accept 10s histogram buckets — recommended).
**Date:** 2026-07-13 (audited 2026-07-13 by opus adversarial auditor)
**Author:** agent (bot), feasibility-design dispatch (fable).
**Train:** `feat/obs-quickwins-train` — ONE car; observability instrumentation only.
**Tasks:** #6 (I33 v2 span-budget refine + hot-loop sweep, ADR-0085) + #50 (harness `ce_mean_ms` silently None, ADR-0105).
**Motivating RCA:** `docs/testing/recall-span-attribution-2026-07-13.md`.

---

## BLUF

Recall-wall attribution is blind: the CE histogram the harness reads died at
ADR-0078 (count=0 live), and the `@observe` stage histograms that WOULD attribute
the wall are invisible on every scraped endpoint. Feasibility work for this plan
found out **why** — and it is worse than the dispatch assumed: **the entire
`@observe` metric arm (boundary RED + stage histograms) is silently dead in every
process** due to a circular import (`observe.py` → `metrics.py` → `config.py` →
`observe.py`) swallowed by a bare `except Exception` at
`yadgar/_shared/observability/observe.py:56-64`. Verified live (zero
`yadgar_observe_*` families on :8765 AND :8001) and reproduced locally
(`_PROM_AVAILABLE=False` after import, ImportError: "cannot import name 'observe'
from partially initialized module").

The car therefore has four pieces, in dependency order:

1. **P0 — break the circular import** (leaf registry module) so `@observe`
   metrics emit at all. This alone lights up :8765 (core already serves the
   shared registry).
2. **Expose the `yadgar_observe_*` families on backend :8001 /metrics** via a
   read-only bridge collector (recall runs in the backend process; its shared
   registry is currently never scraped — :8001 serves only the isolated embed
   registry).
3. **Two hot→stage tier promotions** (`retrieval.vector.encode_query`,
   `retrieval.ce.score_ce_cached`) so query-embed and total-CE become their own
   stages — everything else in the live recall path is already
   `@observe(tier="stage")`-covered. These land in the P0 half (criterion 1 +
   #50 need them), NOT gated behind the sweep. Plus the ADR-0085 I33 v2 lint
   refinement + a SCOPE-BOUNDED hot-loop flip (only `_cosine_similarity` this
   car; broader sweep deferred — §3.4).
4. **Re-point the harness `ce_mean_ms`** at
   `yadgar_observe_stage_duration_seconds{stage="retrieval.ce.score_ce_cached"}`
   (fixes #50).

After this ships, `curl :8001/metrics` gives per-stage recall latency including a
real CE-stage duration, and a clean WARM per-stage breakdown is obtainable by
histogram delta — no Tempo dependency.

---

## 0. AUDIT VERIFICATION TABLE (2026-07-13, opus adversarial auditor)

Every load-bearing claim independently re-verified against master (core 5.132.0,
backend 5.43.0). **P0 is real and confirmed both live and by local repro.**

### 0.1 CRITICAL CORRECTION — stale file paths (Reorg Round 2)

The plan was drafted against **pre-reorg** paths. Since PR #167–#170 the retrieval
package moved under `yadgar/backend/`. Build MUST use the corrected paths:

| plan says | actual (master) |
|---|---|
| `yadgar/retrieval/core.py` | `yadgar/backend/retrieval/core.py` |
| `yadgar/retrieval/scoring.py` | `yadgar/backend/retrieval/scoring.py` |
| `yadgar/retrieval/fusion.py` | `yadgar/backend/retrieval/fusion.py` |
| `yadgar/retrieval/reranking.py` | `yadgar/backend/retrieval/reranking.py` |
| `yadgar/retrieval/_reranking_cross_encoder.py` | `yadgar/backend/retrieval/_reranking_cross_encoder.py` |
| `yadgar/retrieval/recall_pipeline.py` | `yadgar/backend/retrieval/recall_pipeline.py` |
| `yadgar/retrieval/providers/*.py` | `yadgar/backend/retrieval/providers/*.py` |
| `observe.py` / `metrics.py` | `yadgar/_shared/observability/{observe,metrics}.py` (correct) |
| `config.py` | `yadgar/_shared/config/config.py` |
| `paths.py` | `yadgar/_shared/paths/paths.py` |
| `server_helpers.py` | `yadgar/_shared/server_helpers/server_helpers.py` (correct) |

Note also: the decorator kwarg is `metric=` (not `name=`) — every `@observe` in
the retrieval package reads `@observe(tier="...", metric="retrieval.*")`. And the
plan's cited line numbers point at the `@observe` decorator line; the `def` is one
line below (consistent off-by-one, correct functions).

### 0.2 P0 — VERIFIED (stronger than the plan states)

| claim | verdict | evidence |
|---|---|---|
| observe.py L56-64 bare `except Exception → _PROM_AVAILABLE=False` around prometheus + `_registry` import | **VERIFIED** | exact lines 56-64; `except Exception:  # pragma: no cover` at L63 |
| `metrics.py:41` imports `resolve_knob` before `_registry` at `:47` | **VERIFIED** | L41 `from yadgar._shared.config import resolve_knob`; L47 `_registry = CollectorRegistry()` |
| `config.py:13` imports `observe` | **VERIFIED** | `yadgar/_shared/config/config.py:13` |
| `paths.py:33` imports `observe` | **VERIFIED** | `yadgar/_shared/paths/paths.py:33` |
| cycle inevitable, both import orders | **VERIFIED** | `uv run python -c "…observe…; print(o._PROM_AVAILABLE)"` → `False`; metrics-first → `False`. sys.settrace pins the raise to `observe.py:60`, `ImportError: cannot import name '_registry'/'observe' from partially initialized module` |
| live: zero `yadgar_observe_*` on :8765 AND :8001 | **VERIFIED (stronger)** | curl :8765 → 0 matches / 1207 total lines; curl :8001 → 0 / 272. **The families are ABSENT, not present-with-`count 0`.** A registered Histogram emits `_count 0`/`_sum 0`/buckets on scrape even if never fired. Total absence proves *registration itself never ran* (`_get_or_create` short-circuited on `_PROM_AVAILABLE=False`). Dozens of other `yadgar_*` families ARE live → metrics enabled + populated → rules out the innocent "nothing emitted yet" reading. Airtight. |
| core :8765 serves the shared `_registry` | **VERIFIED** | `http.py:638-648` lazy-imports `metrics_handler`; `metrics.py:1178-1202` → `generate_latest(_registry)` (same object) |
| 6 importer sites of `from …metrics import _registry` (leaf re-export preserves identity) | **VERIFIED** | 6 sites; module identity is process-global — a leaf `registry.py` re-export is identity-safe |

### 0.3 Backend exposure / bridge — VERIFIED

| claim | verdict | evidence |
|---|---|---|
| :8001 serves ONLY the isolated embed `CollectorRegistry()` | **VERIFIED** | `embed_service_metrics.py:48 _registry = CollectorRegistry()`; handler `:492` → `generate_latest(_registry)` |
| 7 family names exist in BOTH registries (naive concat invalid) | **VERIFIED (static)** | shared `metrics.py` registers cache families L108-135, log families L718-733; embed `embed_service_metrics.py` registers log families L154-168 + cache families. All 7 present in both at code level. (Live: only 3 of 7 appear on both endpoints — the rest register lazily on first emission; the static collision is the real hazard, correctly flagged.) |
| strict `yadgar_observe_` prefix filter is collision-safe | **VERIFIED** | none of the 7 (`yadgar_cache_*`, `yadgar_log_*`) start with `yadgar_observe_`; prefix bridge cannot duplicate them |
| bridge cost is scrape-time only, ~1µs hot-path | **VERIFIED** | custom collector `.collect()` runs at scrape; `.observe()` already fires post-fix regardless of exposure |

### 0.4 Stage taxonomy + promotions — VERIFIED

| claim | verdict | evidence (actual `yadgar/backend/retrieval/…`) |
|---|---|---|
| promote A: `retrieval.vector.encode_query` tier=hot, `scoring.py:172` | **VERIFIED** | `@observe(tier="hot", metric="retrieval.vector.encode_query")` on `_encode_vector_query` (query-embed) |
| promote B: `retrieval.ce.score_ce_cached` tier=hot, `_reranking_cross_encoder.py:193` | **VERIFIED** | `@observe(tier="hot", metric="retrieval.ce.score_ce_cached")` |
| `score_ce_cached` is per-BATCH, not per-candidate (no ADR-0074 storm) | **VERIFIED** | signature `score_ce_cached(self, query, texts: list[str])` — one call scores the whole candidate list; internal loop is over `texts` but the decorated fn fires ONCE per CE pass. **This was the highest-risk claim; it holds.** |
| `score_ce_cached` = single funnel for all mode=ce passes | **VERIFIED** | 3 call sites route through it: `cross_encoder_rerank` (L132), `score_documents` (L181 via multi-passage), `providers/fusion.py:76`. Caveat: `score_single_pair` (mode=`pair`, uncached) bypasses it but is NOT on the hot ranking path. |
| 9 other RCA stages already `@observe(tier="stage")` or boundary | **VERIFIED** | all sampled stage rows in §3.3 confirmed at corrected paths (all off-by-one decorator/def) |
| `_cosine_similarity` `server_helpers.py:452` tier=hot still opens per-call spans | **VERIFIED** | `@observe(tier="hot", metric="tools.project._cosine_similarity")` |
| `recall_via_pipeline` (`core.py:382-383`) has NO callers | **PARTIAL** | ZERO production callers (correct — fanout uses `Retriever.recall`), BUT 7 live test call sites in `tests/_shared/test_retrieval_pipeline.py` + mocks. "Dead in production" is accurate; "no callers" overstated — the `retrieval.pipeline.*` spans still fire in CI. Exclude from dashboards/harness as planned. |

### 0.5 Perf of the two promotions — VERIFIED, negligible

`hot` tier emits NOTHING (span-open/close only); `stage` adds exactly one
`_STAGE_DURATION.labels(stage=…).observe(elapsed_s)`. Promotion cost = **+1
`histogram.observe()` per call**. Per full recall: +1 (encode_query) + ≤3
(score_ce_cached) = **≤4 histogram.observe()/recall ≈ 4–20µs** against a recall
that spends 100ms+ in CE. **Acceptable.** (The process-wide metric-arm revival in
§6 is the real behavior change; the A/B stays mandatory — see below.)

### 0.6 ADR / lint / version claims

| claim | verdict | evidence |
|---|---|---|
| ADR-0074 (boundary+stage always; hot-loop `span=False`) | **VERIFIED** | wiki `yadgar-adr-log`, accepted 2026-07-09, decision matches verbatim |
| ADR-0085 (two-commit: I33 v2 lint then sweep; `_span_budget`, ≥40-char, ADR-0041 rule, advisory loop report) | **VERIFIED** | accepted 2026-07-09, all elements present |
| ADR-0105 (`ce_mean_ms` None since T2; `yadgar_recall_duration_ms` portable) | **VERIFIED** | accepted 2026-07-13 |
| ADR-0041 (span decorators forbidden in logging-handler set) | **VERIFIED** | accepted 2026-07-04 |
| "**ADR-0078/T2** moved retrieval CE in-process" | **WRONG ATTRIBUTION** | ADR-0078 is a *DB-isolation* directive — says nothing about CE/LocalMLClient. The CE-in-process fact is stated by **ADR-0105** (which cites the T2 train as the historical event). Corrected in §1 below. |
| `.observe-allowlist.json` has NO `_span_budget` key | **VERIFIED** | 195 keys; structural keys `_comment` + `_exempt_globs` (11 globs incl. `observe.py`, `log_config.py`) + per-fn `"module:Class.method" → {category, rationale}`. `_span_budget` slots in as a new top-level section — feasible. |
| `_CE_METRIC = "yadgar_embed_rerank_duration_seconds"`, harness already emits `ce_mean_ms: null` + `ce_metric_status` | **VERIFIED** | `benchmarks/run_perf_loadtest.py:94` + module docstring L20-26 |

### 0.7 VERSION machinery — CORRECTION

The plan implies `check_backend_bump.py` fires on `_shared/` changes. **It does
NOT.** `scripts/check_backend_bump.py` watches only paths with a `backend` dir
component (`BACKEND_BUILD_DIRS = ("backend",)`) + `Dockerfile.backend` /
`entrypoint-backend.sh`; `_shared/` paths return `False` from
`_is_backend_build_input`. Consequences:

- The **backend bump 5.43.0 → 5.44.0** IS enforced — this car edits
  `yadgar/backend/embed_service/embed_service_metrics.py` (bridge) +
  `yadgar/backend/retrieval/*` (promotions), which are `backend` paths.
- The **core bump 5.132.0 → 5.133.0** (for `observe.py` / new `registry.py` /
  `config.py` / `metrics.py` under `_shared/`) is **NOT enforced by any gate** —
  it is a manual discipline item. Bump `version` in `server.json` +
  `pyproject.toml` + `flake.nix` (all three read 5.132.0 today; the
  pre-commit sync-version hook rewrites `flake.nix` — re-stage after).
- Train integration's "core 5.133.0 / backend 5.44.0" is the correct, sufficient
  version story. Criterion 8 updated to name both explicitly.

---

## 1. The observability gap (RCA + ADR-0105) — plus the new P0 finding

Per the RCA and ADR-0105:

- `yadgar_embed_rerank_duration_seconds{mode="ce"}` is fed ONLY by the
  embed-service `POST /rerank` endpoint (`RemoteMLClient` path). Since
  the T2 train moved retrieval CE in-process (`LocalMLClient.score_cross_encoder`;
  this fact is codified in **ADR-0105**, NOT ADR-0078 — ADR-0078 is a DB-isolation
  directive and does not mention CE), recall CE never touches it — count=0 live,
  harness `ce_mean_ms` silently None since T2 (#50).
- Per-stage timing today exists only as `span_end` structured logs, which are
  BatchSpanProcessor flush-truncated — only ONE complete cold trace was ever
  reconstructed from `podman logs`; warm attribution needs Tempo. The RCA's cold
  attribution (signal-gather ~45%, hydration ~23%, CE ~25%) rests on that single
  trace; the per-signal split of the ~2.8s cold head was never captured.
- The real optimization target — signal-gather (vector/FTS/PPR/spreading) — is
  therefore unmeasurable in steady state.

**New finding (this design pass): the `@observe` metric arm never emits.**
`observe.py:56-64` wraps its Prometheus + registry imports in `try/except
Exception → _PROM_AVAILABLE=False`. The import chain is circular:

```
yadgar/_shared/observability/observe.py:60  from ....metrics import _registry
yadgar/_shared/observability/metrics.py:41  from yadgar._shared.config import resolve_knob   # BEFORE _registry at :47
yadgar/_shared/config/config.py:13          from ...observability.observe import observe     # @observe-decorated YamlConfigSource._load
yadgar/_shared/paths/paths.py:33            from ...observability.observe import observe
```

Whichever module loads first, the re-entrant import hits a partially initialized
module and raises; the bare except converts that into `_PROM_AVAILABLE=False`
for the process lifetime. Confirmed by:

- Local repro: `uv run python -c "from yadgar._shared.observability import
  observe as o; print(o._PROM_AVAILABLE)"` → `False`;
  `yadgar_observe_stage_duration_seconds` absent from
  `metrics._registry._names_to_collectors`. Import-spy shows
  `ImportError: cannot import name 'observe' from partially initialized module`.
- Live: `curl :8765/metrics | grep -c yadgar_observe` → 0 (core serves the
  shared registry — the families would appear if registration succeeded);
  `curl :8001/metrics | grep -c yadgar_observe` → 0.

Consequence: every `@observe` boundary RED counter/histogram and every stage
histogram shipped since v5.101 has been metric-silent. Spans and logs work
(different code path — `trace_span` does not depend on the registry import);
only the metric arm is dead. This corrects the RCA's framing: the stage
histograms are not merely "not scraped" — they are never written.

---

## 2. Feasibility verdicts

### Q1 — Expose `@observe` stage histograms on /metrics: **BUILDABLE — but blocked-by the P0 circular import**

Evidence:

- `@observe` writes stage durations via
  `_STAGE_DURATION.labels(stage=...).observe(elapsed_s)`
  (`observe.py:162-163`) into families registered on the SHARED registry
  `yadgar._shared.observability.metrics._registry` (`observe.py:60`,
  `observe.py:78`) — when the import succeeds, which it currently never does
  (§1).
- Core :8765 `/metrics` serves that shared registry
  (`yadgar/core/server/http.py:638-648` → shared `metrics_handler`,
  `metrics.py:1178-1202`). Once the cycle is fixed, core-process `@observe`
  metrics appear on :8765 with NO further work.
- Backend :8001 `/metrics` serves ONLY the isolated embed registry
  (`yadgar/backend/embed_service/embed_service_metrics.py:49`
  `_registry = CollectorRegistry()`; handler at `:492-503`; route
  `embed_service.py:813-822`). Recall runs in the backend process
  (`recall_route` → `_fanout_recall` → `MemoryProvider.candidates` →
  `Retriever.recall`), so its stage samples land in the backend's shared
  registry — which nothing scrapes. This is the exposure gap.
- Naive concatenation of `generate_latest(embed_registry) +
  generate_latest(shared_registry)` is INVALID: 7 family names exist in both
  registries (`yadgar_cache_evictions_total`, `yadgar_cache_hit_total`,
  `yadgar_cache_miss_total`, `yadgar_cache_size_entries`,
  `yadgar_log_dropped_total`, `yadgar_log_file_rotations_total`,
  `yadgar_log_file_size_bytes`) — duplicate families break the exposition.
- A **bridge collector** registered on the embed registry that yields only
  `yadgar_observe_*` families from the shared registry at scrape time is
  supported by prometheus_client 0.25.0 (custom collector = any object with
  `collect()`). `CollectorRegistry.restricted_registry(names)` also exists
  (`prometheus_client/registry.py:101`, verified in the resolved 0.25.0) but
  filters by exact SAMPLE names (`metrics_core.py:58-65` — `_bucket`/`_sum`/
  `_count` must each be enumerated) and is marked Experimental — the explicit
  prefix-filter bridge is simpler and more robust.
- Perf: zero hot-path change. Histogram `.observe()` is ~1µs and (post-fix)
  happens at call time regardless of exposure; the bridge only runs at scrape
  time.

### Q2 — I33 v2 span-budget refine + hot-loop sweep (#6): **BUILDABLE — spec already accepted (ADR-0085), lint counterpart verified absent**

Evidence:

- ADR-0074 (accepted 2026-07-09) sets the policy: boundary + stage spans always;
  per-item hot-loop helpers `@observe(span=False)` — metrics survive, spans
  don't. ADR-0085 (accepted 2026-07-09) specifies the two-commit execution:
  Commit 1 = I33 v2 lint (`_span_budget` allowlist section, hard-fail on listed
  fn opening a per-call span, ≥40-char rationale, stale-entry governance;
  advisory loop-heuristic report; ADR-0041 logging-handler module hard rule;
  widen `span=False`/`tier="hot"` docs). Commit 2 = sweep.
  `docs/plans/full-observability-standard-2026-07-03.md` §5b carries the same
  spec and marks P-SB as the sole remaining phase, unblocked post-T4.
- Verified absent today: `.observe-allowlist.json` has NO `_span_budget` key
  (inspected); `scripts/check_observe_coverage.py` is the lint to extend.
- Stage coverage survey of the LIVE recall path (see §3.3 table): all nine
  RCA-named stages are already `@observe(tier="stage")` or boundary EXCEPT
  query-embed (`retrieval.vector.encode_query`, tier=hot, `scoring.py:172`) and
  a canonical all-passes CE stage (`retrieval.ce.score_ce_cached`, tier=hot,
  `_reranking_cross_encoder.py:193`). Both run ≤3 times per recall — not hot
  loops; stage promotion is ADR-0074-conformant.
- The `retrieval.pipeline.*` stage metrics (`stages/knn.py`, `stages/fts.py`,
  …) belong to `Retriever.recall_via_pipeline` (`core.py:383`) which has **no
  callers** (grepped) — a parallel taxonomy that must be excluded from
  dashboards/harness queries, not instrumented further.
- Remaining span-storm offender still opening per-call spans:
  `tools.project._cosine_similarity`
  (`yadgar/_shared/server_helpers/server_helpers.py:452`, tier=hot). ADR-0074's
  other named offenders (`_row_to_dict`, `_extract_id`) are already
  allowlist-exempt or undecorated. The Commit-1 advisory loop report enumerates
  any others.

### Q3 — Warm per-stage breakdown without Tempo: **BUILDABLE via Prometheus histogram deltas — recommended**

Evidence:

- Prometheus histograms are cumulative in-process counters — immune to the
  BatchSpanProcessor log-flush truncation that killed warm span reconstruction
  (RCA "CRITICAL MEASUREMENT CAVEAT").
- Procedure (post Q1 exposure): scrape :8001 `/metrics`; run N identical warm
  recalls; scrape again; per stage compute `d_sum/d_count` (mean per invocation)
  and `d_sum/N` (per-recall wall share). Stages nest
  (`retrieval.rerank` ⊃ `retrieval.cross_encoder_rerank` ⊃
  `retrieval.ce.score_ce_cached`) — the breakdown doc must state the stage tree
  so shares aren't double-counted (§3.6).
- Limitation accepted: aggregate distribution, not single-trace attribution.
  Tempo remains the optional tool for trace-shaped questions; it is NOT required
  for the wall breakdown, which is the goal.

### Q4 — Scope discipline: **HELD**

No recall-pipeline redesign. Changes: one leaf module + two import-line moves
(circular-import fix), one bridge collector + registration (backend metrics),
two one-line tier promotions, lint + allowlist (I33 v2), one harness scrape
function re-point. Backend files change → `BACKEND_VERSION` bump
(`yadgar/__init__.py:21`, currently 5.43.0; `scripts/check_backend_bump.py`
enforces). `_shared` changes ship in both images → core version bump too.

---

## 3. The design

### 3.1 P0 — break the observe↔metrics↔config import cycle

Extract the registry into a leaf module with zero yadgar imports:

- New `yadgar/_shared/observability/registry.py`:
  `_registry = CollectorRegistry()` (plus nothing else).
- `metrics.py` imports `_registry` from it and re-exports (back-compat for the
  many `from yadgar._shared.observability.metrics import _registry` sites).
- `observe.py:60` imports `_registry` from `registry.py` directly — the cycle
  through `config.py` is gone (observe no longer imports metrics at module
  load). NOTE: this alone kills the cycle, because `registry.py` is a genuine
  leaf. `config.py:13` and `paths.py:33` keep importing `observe` unchanged.
- **Rework the `except` at `observe.py:56-64` — the plan's original "catch
  ImportError of prometheus_client only" is a TRAP** and is REPLACED. The
  circular import raises an `ImportError`, so a naive `except ImportError →
  _PROM_AVAILABLE=False` would silently swallow the exact same bug class again.
  The two imports MUST be structurally separated so the external-dep guard cannot
  mask an internal structural failure:

  ```python
  try:
      from prometheus_client import Counter as _Counter
      from prometheus_client import Histogram as _Histogram
  except ImportError:
      # prometheus_client is a HARD dep (pyproject.toml:51, not optional-extras);
      # this branch is defensive-only and should never fire in a valid install.
      _PROM_AVAILABLE = False
  else:
      # NOT guarded — a failure here is a STRUCTURAL bug (the cycle). Let it raise
      # loud rather than zero the metric arm for the process lifetime.
      from yadgar._shared.observability.registry import _registry as _yadgar_registry
      _PROM_AVAILABLE = True
  ```

  Rationale: the whole P0 bug WAS silent degradation. Since prometheus_client is a
  hard dependency (agent-verified in `[project.dependencies]`), there is no
  legitimate no-prometheus install to protect — fail loud on the internal import,
  degrade only on the (never-fired) external-dep branch. This resolves open
  questions 3 and 4 (see §8).
- Regression tests: import `observe` first / `metrics` first in fresh
  interpreters (subprocess), assert `_PROM_AVAILABLE is True` and
  `yadgar_observe_stage_duration_seconds` present in
  `_registry._names_to_collectors` in both orders.

Alternative considered — lazy family creation on first emission: works
(late registration verified locally) but leaves the cycle in place and hides it
behind more laziness; the leaf module kills the cycle structurally (I34-clean:
registry is a genuine leaf).

### 3.2 Backend exposure — bridge collector on :8001

In `yadgar/backend/embed_service/embed_service_metrics.py`:

```python
class _SharedObserveBridge:
    """Yield yadgar_observe_* families from the shared registry at scrape time."""
    def collect(self):
        from yadgar._shared.observability.registry import _registry as shared
        for metric in shared.collect():
            if metric.name.startswith("yadgar_observe_"):
                yield metric

_registry.register(_SharedObserveBridge())
```

- Exposes exactly the four bounded families (`yadgar_observe_requests_total`,
  `yadgar_observe_request_duration_seconds`,
  `yadgar_observe_stage_duration_seconds`, `yadgar_observe_stage_errors_total`)
  on :8001 alongside the embed families. The strict prefix guarantees the 7
  colliding `yadgar_cache_*`/`yadgar_log_*` families are never duplicated.
- Read-only at scrape time; no hot-path cost; no dynamic-gauge refreshers run
  (those live in the shared `metrics_handler`, not in `collect()`).
- Core :8765 needs NOTHING — it already serves the shared registry.

### 3.3 Stage taxonomy of the live recall path + tier promotions

Live path (all files under `yadgar/backend/`): `recall_route`
(`backend/embed_service/embed_service.py:1287`) → `_fanout_recall`
(`backend/retrieval/recall_pipeline.py:458`) → `MemoryProvider.candidates`
(`backend/retrieval/providers/memory.py:55`) → `Retriever.recall`
(`backend/retrieval/core.py:524`).

NOTE (audit): the file:line entries in the table below are the pre-reorg paths
as drafted; prepend `yadgar/backend/` to every retrieval path (see §0.1). The
`@observe` kwarg is `metric=`, and the cited line is the decorator (def is +1).

| stage label (metric) | fn / file:line | tier today | action |
|---|---|---|---|
| `retrieval.recall` | `Retriever.recall` `core.py:524` | boundary | none |
| `tools.recall._fanout_recall` | `recall_pipeline.py:458` | stage (span=False) | none |
| `retrieval.provider.memory_candidates` | `providers/memory.py:55` | stage | none |
| `retrieval.resolve_query_and_candidate_k` | `core.py:439` | stage | none |
| `retrieval.fts` | `scoring.py:145` | stage | none |
| `retrieval.vector` | `scoring.py:187` | stage | none |
| `retrieval.vector.encode_query` | `scoring.py:172` | **hot** | **promote → stage** (splits query-embed from KNN) |
| `retrieval.ppr` | `scoring.py:243` | stage | none |
| `retrieval.spreading` | `scoring.py:264` | stage | none |
| `retrieval.temporal` | `scoring.py:302` | stage | none |
| `retrieval.fusion` | `fusion.py:201` | stage | none |
| `retrieval.build_results` | `fusion.py:290` | stage | none (≈ hydration: `get_memories_by_ids` is ~99% of it per RCA — 1439/1442ms) |
| `retrieval.rerank` | `reranking.py:293` | stage | none (whole rerank pipeline) |
| `retrieval.cross_encoder_rerank` | `_reranking_cross_encoder.py:95` | stage | none (CE pass #1) |
| `retrieval.score_documents` | `_reranking_cross_encoder.py:165` | stage | none (CE pass #2) |
| `retrieval.ce.score_ce_cached` | `_reranking_cross_encoder.py:193` | **hot** | **promote → stage** (canonical ALL-passes CE — the #50 source) |
| `retrieval.crossfuse.fuse_candidates` | `providers/fusion.py:178` | stage | none |
| `retrieval.crossfuse.score_candidates_ce` | `providers/fusion.py:55` | hot | leave hot (CE pass #3 already counted inside `score_ce_cached`) |

`score_ce_cached` is the single funnel all 2-3 CE passes go through (RCA span
tree: it appears under both `_rerank_cross_encoder` and the fanout
`fuse_candidates`). Promoting it gives ONE stage whose `d_sum` is total CE wall
per window — exactly what #50 needs. Both promotions are one-line
`tier="hot"` → `tier="stage"` edits; call frequency ≤3/recall — no ADR-0074
storm risk.

Excluded: `retrieval.pipeline.*` (dead `recall_via_pipeline` path, no callers).
Do not query these in dashboards or the harness.

### 3.4 I33 v2 lint + hot-loop sweep (ADR-0085 execution, #6)

Execute exactly as ADR-0085 / full-observability plan §5b — this car is its
vehicle, not a redesign:

**Commit A — lint refinement (`scripts/check_observe_coverage.py`):**
1. `_span_budget` section in `.observe-allowlist.json`: `fq → {rationale}`
   (≥40 chars), meaning "must NOT open a per-call span"; lint HARD-FAILS a
   listed fn carrying a span-opening decorator without `span=False`; stale-entry
   hard-fail (same governance as existing sections).
2. Advisory non-failing loop-heuristic: span-decorated fn called inside
   `For`/`While` in the same module → stdout report (ADR-0040 glob-audit
   channel pattern).
3. ADR-0041 hard rule: span-opening decorators forbidden in the
   logging-handler module set (explicit file list: `log_config.py`, the
   LogSpanProcessor module in `tracing.py`).
4. Widen `span=False` / `tier="hot"` docstrings in `observe.py` — the module
   docstring's "hot: span only" wording is corrected to name the hot-loop
   budget case as the second legitimate `span=False` reason (per ADR-0085 the
   intended semantics are "attributes on enclosing span, NO per-call span").

**Commit B — sweep (under the refined lint):**
- Seed `_span_budget` with **only** `tools.project._cosine_similarity`
  (`yadgar/_shared/server_helpers/server_helpers.py:452`) and flip it to
  `span=False`. **SCOPE-BOUND (audit):** do NOT sweep "whatever the advisory
  report surfaces" in this car — that set is unbounded, and a codebase-wide
  `@observe` sweep has broad blast radius (memory 531809 / v5.105: ~11
  decorator-contract bugs, ~5 wasted push cycles, 2h wedged CI last time). The
  advisory loop report still PRINTS (Commit A) so offenders are catalogued;
  flipping them is deferred to a dedicated follow-up sweep car so the must-ship
  P0 is not held hostage to open-ended fallout.
- The two §3.3 promotions belong with the P0 half (acceptance criterion 1
  requires their `count>0`, and #50 needs the CE stage) — land them in the same
  commit as the leaf-registry + bridge, NOT gated behind the sweep.
- Verify boundary spans return in Tempo (per-op span count for
  `audit_anchors`/recall drops from tens-of-thousands to tens).

### 3.5 Harness re-point (#50)

`benchmarks/run_perf_loadtest.py` (`_CE_METRIC` at `:94`, `_scrape_ce_totals`
at `:174`):

- Primary CE source becomes
  `yadgar_observe_stage_duration_seconds{stage="retrieval.ce.score_ce_cached"}`
  scraped from `YADGAR_BACKEND_METRICS_URL` (:8001). Report
  `ce_mean_ms = d_sum/d_count × 1000` (per CE pass) and
  `ce_wall_ms_per_recall = d_sum/N × 1000` (per-recall CE wall, the number the
  old harness pretended to give).
- Keep the `yadgar_embed_rerank_duration_seconds` probe as a legacy fallback
  for old daemons (ADR-0105 portability note); `ce_metric_status` gains a value
  distinguishing "observe-stage source (current)" from "embed-rerank source
  (legacy)" from "unavailable".
- While at it, optionally emit the full per-stage table (all
  `yadgar_observe_stage_duration_seconds` deltas for the §3.3 labels) in the
  harness JSON — it is one scrape away and is the warm breakdown (§3.6).

### 3.6 Warm per-stage breakdown procedure (documented output of this car)

1. `curl -s :8001/metrics` → snapshot A.
2. Drive N ≥ 6 identical warm recalls (MCP tool or harness).
3. `curl -s :8001/metrics` → snapshot B.
4. Per stage label: `mean_ms = (B.sum−A.sum)/(B.count−A.count) × 1000`;
   `per_recall_ms = (B.sum−A.sum)/N × 1000`.
5. Attribute the wall against the stage TREE (do not sum siblings with
   ancestors): `retrieval.recall` = `resolve_query_and_candidate_k` + `fts` +
   `vector` (⊃ `vector.encode_query`) + `ppr` + `spreading` + `temporal` +
   `fusion` + `build_results` + `rerank` (⊃ `cross_encoder_rerank`,
   `score_documents`, …); `retrieval.ce.score_ce_cached` cross-cuts (sums CE
   from both `rerank` and `crossfuse.fuse_candidates`).

Flush-immune, Tempo-free, portable to any deploy carrying this car.

---

## 4. Acceptance criteria

Each tagged `[unit]` (CI-gated), `[scrape-e2e]` (deployed stack), or
`[manual-diagnostic]` (informational, NOT a pass/fail gate).

1. `[scrape-e2e]` `curl :8001/metrics` after ≥1 recall shows
   `yadgar_observe_stage_duration_seconds{stage=...}` with `count > 0` for at
   minimum: `retrieval.fts`, `retrieval.vector`, `retrieval.vector.encode_query`,
   `retrieval.ppr`, `retrieval.spreading`, `retrieval.fusion`,
   `retrieval.build_results`, `retrieval.rerank`,
   `retrieval.cross_encoder_rerank`, `retrieval.ce.score_ce_cached`.
2. `[unit]` The :8001 exposition parses cleanly
   (`prometheus_client.parser.text_string_to_metric_families`) — no duplicate
   family names.
3. `[scrape-e2e]` `curl :8765/metrics` shows `yadgar_observe_*` families
   (core-process emissions) — proof the P0 circular-import fix landed.
4. `[scrape-e2e]` Harness run against the deployed stack reports non-null
   `ce_mean_ms` with `ce_metric_status` indicating the observe-stage source.
5. `[manual-diagnostic — NOT a gate]` Warm breakdown per §3.6 yields per-stage
   means whose tree-sum covers ≈90% of the `retrieval.recall` boundary
   duration (denominator = `yadgar_observe_request_duration_seconds{name=
   "retrieval.recall"}`, the boundary RED histogram — NOT a stage histogram),
   with no Tempo access. The 90% is a heuristic sanity check (un-instrumented
   glue accounts for the remainder); do NOT fail the car on the exact number.
6. `[unit]` I33 v2: lint hard-fails a `_span_budget`-listed fn that opens a
   per-call span; advisory loop report prints; existing I33 coverage stays at 0
   MISSING.
7. `[scrape-e2e]` Post-sweep trace check: recall / `audit_anchors` per-op span
   counts drop to tens; boundary spans findable by name.
8. `[unit]` `BACKEND_VERSION` bumped **5.43.0 → 5.44.0** (`check_backend_bump.py`
   green — enforced, since backend/ files change) AND core `version` bumped
   **5.132.0 → 5.133.0** in `server.json` + `pyproject.toml` + `flake.nix`
   (MANUAL — `check_backend_bump.py` does NOT gate `_shared/` changes; see §0.7).
   Pre-commit sync-version hook rewrites `flake.nix` — re-stage after commit.

---

## 5. Test plan

- **Import-order regression (P0):** two subprocess tests — `import
  yadgar._shared.observability.observe` first, and `...metrics` first — each
  asserting `_PROM_AVAILABLE is True` and the four family names present in the
  leaf registry. (Red today: both fail on master.)
- **Identity test (audit-added):** assert the leaf `registry.py._registry` IS
  (object identity, not just equal) the object returned by
  `from yadgar._shared.observability.metrics import _registry`, the object
  `observe.py` registers on, and the object core `metrics_handler` renders. One
  shared `CollectorRegistry` across leaf ← metrics (re-export) ← observe ←
  bridge ← core http — pin identity, not family presence.
- **Emission test:** call a `@observe(tier="stage")` fn, assert
  `yadgar_observe_stage_duration_seconds` sample count increments in the shared
  registry (extends the observe-decorator test — locate it under
  `yadgar/tests/` at the post-reorg path; it currently monkeypatches the
  families and so never caught the dead arm — add a no-monkeypatch variant).
- **Bridge test:** register bridge, render backend `metrics_handler`, assert
  (a) `yadgar_observe_*` families present, (b) exposition parses with
  `text_string_to_metric_families`, (c) no family appears twice.
- **Promotion tests:** `encode_query` / `score_ce_cached` still return correct
  values and emit one stage sample per call (decorator behavior, existing test
  patterns).
- **Lint tests:** `_span_budget` hard-fail path, stale-entry fail, rationale
  length, advisory-report emission (fixtures mirroring existing
  check_observe_coverage tests).
- **Harness unit:** `ce_metric_status` matrix — observe-stage present / legacy
  only / neither (extends `benchmarks/perf_contract.py` helpers).
- **E2E (manual, deploy):** acceptance criteria 1-5 + 7 against the running
  stack; A/B warm-recall floor per the §5b/ADR-0033 method (≥6 warm runs,
  median, same box) before/after, since the metric arm going live is the first
  time these histograms actually emit.

---

## 6. Risks

- **Metric arm going live is a real behavior change.** The v5.106 "no
  measurable slowdown" result was measured with the metric arm silently dead
  (§1) — spans+logs only. Post-fix, every boundary/stage fn process-wide starts
  doing a ~1µs histogram/counter emission. Bounded label sets keep cardinality
  at the §3.3-audited level, and recall does a few dozen stage emissions per
  call — expected noise-level, but the A/B in the test plan is mandatory, not
  optional.
- **Circular-import fix touches the config import graph.** `config.py:13` and
  `paths.py:33` keep importing `observe` (unchanged); only observe's own
  registry import moves to the leaf. Both import orders are covered by
  subprocess tests. Residual risk: some third module import-times against
  `metrics._registry` identity — mitigated by re-exporting the SAME object.
- **Registry-bridging complexity:** the bridge iterates all shared-registry
  collectors per scrape (~83 families) — negligible; strict
  `yadgar_observe_` prefix prevents the 7-family collision; a parse test pins
  it.
- **Nested-stage double counting** in dashboards/reports (rerank ⊃ CE stages;
  `score_ce_cached` cross-cuts two parents). Mitigated by documenting the stage
  tree (§3.6) and having the harness compute shares only from the tree's
  leaves.
- **Bucket ceiling:** the shared stage family uses prometheus default buckets
  (top 10s). Cold stages can exceed 10s → +Inf bucket. Means via `sum/count`
  are unaffected (the harness/breakdown path); only >10s quantiles are
  unresolvable. Changing buckets would touch ALL stages — deferred (open
  question 2).
- **Test cross-contamination:** embed-registry tests now see observe families
  via the bridge. Convention already is delta-assertions
  (`embed_service_metrics.py` module docstring); audit any absolute-value
  assertions in `tests/backend/`.

---

## 7. Scope boundary

IN: circular-import fix (leaf registry), backend bridge exposure, two tier
promotions, I33 v2 lint + `_span_budget` sweep, harness ce re-point, warm
breakdown doc, version bumps.

OUT: any recall-pipeline logic change; Tempo work; new metric families beyond
the existing four `yadgar_observe_*`; bucket retuning; the dead
`recall_via_pipeline` path; core :8765 endpoint changes; renaming the
`tools.recall.*` stage namespace.

---

## 8. Open questions — RESOLVED / REMAINING (audit 2026-07-13)

1. **`score_ce_cached` double parentage:** **RESOLVED — keep the label, document.**
   Promoting it emits CE samples ALSO inside `retrieval.cross_encoder_rerank` and
   `retrieval.crossfuse.fuse_candidates`. Fine for the harness (single cross-cut
   stage). The §3.6 breakdown doc must forbid adding it to tree sums (it's a
   cross-cut, not a tree leaf). No dedicated `retrieval.ce.total` metric — avoids
   a new family (OUT-of-scope per §7).
2. **Buckets:** **REMAINS — user decision (non-blocking).** Recommend ACCEPT the
   10s default-bucket ceiling for this car. Means via `sum/count` (the harness +
   breakdown path) are unaffected; only >10s quantiles are unresolvable, and
   widening touches ALL stages (blast radius). Defer bucket retuning to a
   follow-up. Building on the default buckets is safe.
3. **Leaf-registry blast radius:** **RESOLVED.** 6 importer sites of
   `from …metrics import _registry` (agent-enumerated); a leaf `registry.py`
   re-exporting the SAME object preserves identity (process-global module
   identity). prometheus_client is a HARD dep (`pyproject.toml:51`,
   `[project.dependencies]`), so there is no I3 no-deps mode to trip the guard —
   the else-block form (§3.1) degrades only on the never-fired external-dep
   branch.
4. **Hard-fail vs WARN on structural failure:** **RESOLVED — fail loud,
   everywhere, no dev/prod split.** The else-block form (§3.1) lets the internal
   registry import raise unguarded in ALL environments. The whole P0 was silent
   degradation; a dev/prod split adds complexity for no benefit (a hard dep
   cannot legitimately be absent in prod). REJECT the split.
5. **Harness legacy fallback:** **RESOLVED — keep it, minimal.** Retain the
   `yadgar_embed_rerank_duration_seconds` probe as a labelled legacy fallback
   (`ce_metric_status: "embed-rerank source (legacy)"`) for old daemons per
   ADR-0105's portability note — it is a few lines and costs nothing. The
   observe-stage source is primary; `yadgar_recall_duration_ms` remains the
   portable cross-version wall signal for sweeps (not a CE-specific substitute).
6. **`_fanout_recall` uses `tools.recall.*` namespace backend-side:** **REMAINS
   — cosmetic, out of scope.** Confirmed (metric is `tools.recall._fanout_recall`
   though the fn now runs backend-side post-T2). Rename is a follow-up task, not
   this car.

**Remaining user-decision items:** only q2 (accept 10s buckets — recommended) is
a genuine open call; everything else is resolved. No blocker.
