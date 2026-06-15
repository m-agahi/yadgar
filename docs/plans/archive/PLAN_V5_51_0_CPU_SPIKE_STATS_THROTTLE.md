# PLAN — v5.51.0: Hooks fast-profile tuning + latency budget

**Status:** DRAFT — 2026-05-31. REVISED 2026-06-02 post-opus-review (minor). RESCOPED 2026-06-12 post-audit. Plan-first per I27.

**Revision notes (opus reviewer — ship-as-is verdict):**
- ADD Prometheus counter `yadgar_hook_recall_timeout_total{handler}` — I23 requires writer for every metric. Plan said "WARN log" only; operators need metric.
- Document recall-quality regression in MIGRATION_NOTES: `HOOK_RECALL_TIMEOUT_S` returning `{"text": ""}` on timeout silently degrades recall under load.
- DROP "may fold into v5.52.0" note — unrelated scopes.
- I25 three-way registration for `FAST_PROFILE_CANDIDATE_MULTIPLIER` + `HOOK_RECALL_TIMEOUT_S` (already called out — confirm in acceptance).

**Supersedes:** prior `/api/stats` CPU-spike target (committed d688db6 2026-05-31).
Investigation showed `/api/stats` cadence is 0.6% sustained CPU — not a burst source.
Real bursts traced to `/hooks/*` slow paths. See §4 Investigation context.

**Renumbered:** v5.42.0 → v5.51.0 on 2026-05-31. Numbering locked at v5.50/v5.51/v5.52.

**Depends on:** v5.25.2 hotfix MUST land before this plan ships.
v5.25.2 closes the worst individual burst (subagent-start missing `profile="fast"`,
SecretLeakBlocked poison-pill stalling consolidation daemon). This plan addresses
what remains after the hotfix: the fast profile itself is still too slow.

**Downstream:** none. May fold into v5.52.0 if scope turns out small.

**Effort estimate:** ~1 day (§4.1 already done, §4.5 dropped) / ~1.5 days if §4.5 prerequisite `stats.js` is later built.

---

## Rescope 2026-06-12 (post-audit)

Audit performed 2026-06-12 against current source. Summary of findings:

- **§4.1 DONE (shipped v5.25.3).** `hook_instructions_loaded` already uses `profile="fast"` — evidence: `yadgar/server/http.py:1115`, comment "v5.25.3: use lightweight 'fast' profile". This item is removed from active scope.
- **§4.2 NOT STARTED — still valid.** `CANDIDATE_POOL_MULTIPLIER` is global (`yadgar/config.py:190`, default 20); no fast-profile override exists. `analyze_query` always runs (`yadgar/retrieval/core.py:435`). Keep in scope. Effort S+S.
- **§4.3 NOT STARTED — primary win, keep.** Hook recalls are wrapped in `asyncio.to_thread` only; no `asyncio.wait_for` timeout exists anywhere. Need `HOOK_RECALL_TIMEOUT_S` (config.py, suggested default 2.0), wrapping all three hook recall calls, and a Prometheus counter `yadgar_hook_recall_timeout_total{handler}` in `yadgar/server/metrics.py`. This is the same defensive class as the v5.50.10 OTEL shutdown bound and is the highest-value item remaining in scope.
- **§4.4 line numbers STALE — refreshed.** All three hook handlers already use `profile="fast"` as of v5.25.3. `/api/viz-search` (`http.py:1579`) also uses fast profile. Table updated; no gaps found.
- **§4.5 DROPPED — dead prerequisite.** Depends on `stats.js` which v5.50.0 never created (`yadgar/static/` has no `stats.js`). Marked dropped. If `stats.js` is built in a future version, re-evaluate then.
- **§4.6 NOT STARTED — endpoint confusion fixed.** `/api/system` is already 5s-sampled via a background thread (`yadgar/server/graph_api.py:627`). The slow live-compute endpoint is `/api/stats` (`yadgar/server/http.py:1290`, calls `get_memory_stats` uncached). The TTL cache (`YADGAR_STATS_CACHE_TTL_S`, default 5) targets `/api/stats`. Keep; endpoint naming corrected throughout.
- **Path drift corrected throughout:** all references to `yadgar/settings.py` updated to `yadgar/config.py`; all references to `yadgar/server/api/stats.py` updated to `yadgar/server/http.py`.

---

## 1. Goal

Make the `fast` retrieval profile actually fast.

Target: p95 prompt-recall latency < 500ms, p99 < 2s, zero calls > 5s after deploy.

Current state (histogram, 2-pass verified):
- `prompt_recall` p95 = 5-10s even with `profile="fast"`.
- Root cause: fast profile already disables CE/NLI/multi-passage
  (`yadgar/retrieval/fusion.py` lines 14-19), but the BM25 + vector path
  itself is slow — dominated by `CANDIDATE_POOL_MULTIPLIER` (scales candidate
  pool to `max_results * multiplier`), dual-vector search top_k, FTS5 query
  latency against SurrealDB, and query-analysis overhead
  (`analyze_query` / `_pseudo_hyde_expand` / `_extract_query_entities`).

Two supporting changes:
- Per-call timeout + latency-budget circuit breaker: exceeding budget logs WARN,
  increments `yadgar_hook_recall_timeout_total{handler}`, and returns degraded
  (empty or cached) result instead of blocking the hook handler.
- TTL cache for `/api/stats` (lower priority — §4.6).

---

## 2. Non-goals

- Full rewrite of retrieval pipeline (R2 plugin arch, v5.31 candidate).
- Prometheus scrape changes — verified non-cause of CPU bursts.
- New monitoring infrastructure (OpenTelemetry, etc.) — use existing in-process
  structured logging + `_hook_observe` telemetry already in place.
- Endpoint redesign of `/api/stats` — cadence is contributory at 0.6% CPU,
  not a burst source. Cache/throttle deferred to §4.6 (lower priority).
- Backend rewrite to async where currently sync — v5.6x candidate.

---

## 3. Investigation context (HIGH confidence, 2-pass verified)

### 3.1 Original target was mistargeted

The v5.51.0 plan drafted 2026-05-30 targeted `/api/stats` polling (60s cadence,
360ms/call, 0.6% sustained CPU). This is not a burst. Profiling confirmed the
real burst sources are in `/hooks/*` handlers.

### 3.2 Burst sources identified

**Source 1 — subagent-start (closes with v5.25.2 hotfix)**

`/hooks/subagent-start` calls `retriever.recall()` with no `profile=` argument
(http.py line 1043). Default profile = `balanced` (vector + FTS + PPR +
spreading + cross-encoder). 100% of calls hit the full rerank pipeline:
observed 2.5-10s per call. v5.25.2 hotfix adds `profile="fast"`.

**Source 2 — instructions-loaded (DONE in v5.25.3)**

`/hooks/instructions-loaded` was added `profile="fast"` in v5.25.3
(`yadgar/server/http.py:1115`). Not a v5.51.0 scope item. ~~Not covered
by v5.25.2 — explicitly a v5.51.0 target.~~

**Source 3 — prompt-recall fast profile still too slow**

`/hooks/prompt-recall` already uses `profile="fast"` (http.py line 525), but
42/74 calls still take 2.5-10s. Fast profile disables CE/NLI/MP (confirmed in
`yadgar/retrieval/fusion.py` lines 14-19) — the cost is in:
- `CANDIDATE_POOL_MULTIPLIER`: candidate pool = `max_results * multiplier`.
  With multiplier=20 and `max_results=5`, pool = 100 candidates fetched from DB
  (`yadgar/config.py:190`, default 20 — no per-profile override exists yet).
- `_dual_vector_search`: runs two kNN scans (explicit + implicit vectors).
- FTS5/BM25 query latency against SurrealDB.
- `analyze_query` + `_extract_query_entities` overhead on every call
  (`yadgar/retrieval/core.py:435`).

The throttle gate (2-min rate limit per directory) gates frequency, not
magnitude. A single unthrottled call can still burst.

**Source 4 — consolidation daemon stalled (closes with v5.25.2 hotfix)**

`SecretLeakBlocked` poison-pill in `cleanup._process_action_log()` stalls the
daemon, causing backlog + burst when it eventually drains. v5.25.2 adds a
poison-pill skip.

### 3.3 /api/stats cadence is NOT a burst source

60s poll × 360ms/call = 0.6% sustained CPU. Not a spike pattern. Lower-priority
section kept in §4.6 for completeness.

Note: `/api/system` is already 5s-sampled via background thread
(`yadgar/server/graph_api.py:627`). The slow live-compute path is `/api/stats`
(`yadgar/server/http.py:1290`), which calls `get_memory_stats` uncached on every
request.

---

## 4. Scope — concrete file changes

### 4.1 ~~Fix instructions-loaded missing fast profile~~ DONE — shipped v5.25.3

**This item is complete.** `hook_instructions_loaded` in `yadgar/server/http.py:1115`
already passes `profile="fast"`. Comment reads "v5.25.3: use lightweight 'fast' profile".
No action required. Removed from step plan and effort estimate.

### 4.2 Tune fast profile params in RetrievalEngine

The candidate pool and query-analysis overhead are the primary cost drivers on
the fast path. Tuning targets (exact values: open questions in §5):

| File | Change |
|---|---|
| `yadgar/retrieval/fusion.py` | Add `"fast"` profile entry for `candidate_multiplier` override (e.g. 3 instead of global default 20). Document in PROFILES dict comment. |
| `yadgar/retrieval/core.py` | When `profile_name == "fast"`: skip `_pseudo_hyde_expand` and `_extract_query_entities` (line ~435; query expansion irrelevant for context injection). Gate via `profile.get("skip_query_analysis", False)`. |
| `yadgar/config.py` | Add `FAST_PROFILE_CANDIDATE_MULTIPLIER` (int, default 3). Three-way per I25. |
| `config.yaml` | Add `retrieval.fast_profile_candidate_multiplier: 3`. |

### 4.3 Per-call timeout + latency budget circuit breaker

**Primary win.** Hook recalls currently have no timeout: `asyncio.to_thread(retriever.recall, ...)`
with no `asyncio.wait_for` wrapping. A single slow SurrealDB call blocks the handler
indefinitely. This bounds recall latency defensively — same class as the v5.50.10 OTEL
shutdown bound.

| File | Change |
|---|---|
| `yadgar/server/http.py` | Wrap `asyncio.to_thread(retriever.recall, ...)` in `asyncio.wait_for(..., timeout=settings.HOOK_RECALL_TIMEOUT_S)` in all three hook handlers: `prompt_recall`, `hook_instructions_loaded`, `hook_subagent_start`. On timeout: log WARN (`hook latency budget exceeded`) + increment Prometheus counter + return `{"text": ""}`. |
| `yadgar/config.py` | Add `HOOK_RECALL_TIMEOUT_S` (float, default 2.0). Three-way per I25. |
| `yadgar/server/metrics.py` | Add `yadgar_hook_recall_timeout_total` counter with label `handler`. Increment on timeout. |
| `config.yaml` | Add `hooks.recall_timeout_s: 2.0`. |

Document in `MIGRATION_NOTES.md`: `HOOK_RECALL_TIMEOUT_S` returning `{"text": ""}` on
timeout silently degrades recall quality under load — operators should monitor the counter.

### 4.4 Cross-handler audit table

Verified 2026-06-12 against `yadgar/server/http.py`. All hooks confirmed using fast
profile as of v5.25.3. `/api/viz-search` at `http.py:1579` also uses fast profile.

| Handler | HTTP method | Retrieval call | Profile | Status |
|---|---|---|---|---|
| `/hooks/prompt-recall` | GET | `retriever.recall(profile="fast")` | fast | OK — but fast profile too slow (§3.2 Source 3) |
| `/hooks/subagent-start` | POST | `retriever.recall(profile="fast")` | fast | FIXED in v5.25.2 |
| `/hooks/instructions-loaded` | GET | `retriever.recall(profile="fast")` | fast | DONE in v5.25.3 (http.py:1115) |
| `/hooks/session-context` | GET | `project_brief()` — no retriever call | N/A | OK |
| `/hooks/auto-capture` | POST | no retriever call | N/A | OK |
| `/hooks/pre-compact` | POST | no retriever call | N/A | OK |
| `/hooks/post-compact` | GET | no retriever call | N/A | OK |
| `/hooks/subagent-stop` | POST | no retriever call (writes only) | N/A | OK |
| `/hooks/file-changed` | POST | no retriever call | N/A | OK |
| `/api/viz-search` | GET | `retriever.recall(profile="fast")` | fast | OK — user-initiated, out of hook scope |

No gaps found. Table is current as of 2026-06-12.

### 4.5 ~~Visibility-aware refresh~~ DROPPED — dead prerequisite

**DROPPED.** This section depended on `stats.js` shipping in v5.50.0. That file was
never created: `yadgar/static/` contains no `stats.js`. The prerequisite is absent.

This section is deferred until `stats.js` exists. If v5.50.x builds it, re-evaluate
and re-add to a future plan. Do not treat as active scope for v5.51.0.

~~`yadgar/static/bookmarks.js` exists; `stats.js` does not yet (ships in v5.50.0).~~

### 4.6 /api/stats throttle — lower priority section

Original v5.51.0 target, demoted. Still worth shipping, but not on the critical path.

Clarification: `/api/system` is already 5s-sampled via background thread
(`yadgar/server/graph_api.py:627`). The endpoint that remains unthrottled is
`/api/stats` (`yadgar/server/http.py:1290`), which calls `get_memory_stats`
synchronously on every request with no caching. The TTL cache targets that endpoint.

| File | Change |
|---|---|
| `yadgar/server/http.py` | Add in-memory TTL cache to the `/api/stats` handler (line ~1290). Second call within TTL returns cached result. |
| `yadgar/config.py` | Register `YADGAR_STATS_CACHE_TTL_S` (int, default 5). Three-way per I25. |
| `config.yaml` | Add `stats.cache_ttl_s: 5`. |

### 4.7 Tests

| File | Change |
|---|---|
| `yadgar/tests/test_hook_fast_profile.py` | NEW. `test_instructions_loaded_uses_fast_profile` — regression (was §4.1 target, now confirms v5.25.3 behaviour). `test_prompt_recall_uses_fast_profile` — regression. `test_subagent_start_uses_fast_profile` — regression post-hotfix. |
| `yadgar/tests/test_hook_latency_budget.py` | NEW. `test_timeout_fires_warn_returns_empty` — mock `asyncio.wait_for` to raise `TimeoutError`, confirm WARN logged + empty text returned + counter incremented. `test_timeout_configurable` — `HOOK_RECALL_TIMEOUT_S=0.1` triggers on slow mock. |
| `yadgar/tests/test_fast_profile_tuning.py` | NEW. `test_fast_profile_skips_query_expansion` — retriever with `profile="fast"` does not call `_pseudo_hyde_expand`. `test_fast_profile_candidate_multiplier` — pool size uses `FAST_PROFILE_CANDIDATE_MULTIPLIER`, not global default. |
| `yadgar/tests/test_stats_cache.py` | NEW (§4.6, lower priority). TTL cache semantics: hit/miss/expiry/key-isolation/disabled-at-zero. |

Note: `test_stats_visibility_throttle.py` was previously listed here but is removed from scope along with §4.5.

---

## 5. Open questions

1. **Exact param targets for fast profile.** What candidate multiplier hits <500ms p95?
   Start at 3 (vs. global default 20 in `config.py:190`); benchmark against retrieval recall@K baseline
   (v5.26.0 numbers when available). If recall degrades >10%, raise to 5.

2. **Timeout value.** 2s vs. 1s vs. 5s. Histogram shows p99 = 10s — 2s cuts off ~1%
   of calls that are legitimately slow. 1s is aggressive. Recommend 2.0s default,
   configurable. Re-evaluate after first prod histogram.

3. **Query analysis skip threshold.** `analyze_query` does entity extraction +
   semantic routing (`core.py:435`) — for 2-3 word hook queries, the analysis overhead
   may exceed the retrieval itself. Profile with and without `skip_query_analysis=True`
   on fast profile to confirm.

4. ~~**instructions-loaded — roll into v5.25.2 hotfix or keep in v5.51.0?**~~
   Resolved: shipped in v5.25.3. §4.1 is done. Remove from open questions.

5. **viz_search profile.** User-initiated, not a hook — out of scope for v5.51.0.
   But it runs on the main event loop and can starve hook handlers. Track in
   a follow-up plan.

---

## 6. Step plan (TDD per HARD RULE)

### Step 0 — Verify v5.25.2 + v5.25.3 hotfixes landed (≤ 0.1 day)
- Confirm `subagent-start` uses `profile="fast"`.
- Confirm `SecretLeakBlocked` poison-pill skip is in `cleanup.py`.
- Confirm `instructions-loaded` uses `profile="fast"` at `http.py:1115` (v5.25.3). ← expected green
- If hotfixes not yet landed: this plan BLOCKED. Wait.

### Step 1 — ~~Fix instructions-loaded profile~~ SKIPPED (done in v5.25.3)

### Step 2 — Per-call timeout + circuit breaker (≤ 0.5 day)
- TDD: write `test_hook_latency_budget.py` — red.
- Add `HOOK_RECALL_TIMEOUT_S` setting in `config.py`. Add `yadgar_hook_recall_timeout_total` counter in `metrics.py`.
- Wrap all three hook recall calls in `asyncio.wait_for(...)`.
- Run tests → green. Confirm WARN log + counter increment fires on simulated timeout.

### Step 3 — Tune fast profile params (≤ 0.5 day)
- TDD: write `test_fast_profile_tuning.py` — red.
- Add `FAST_PROFILE_CANDIDATE_MULTIPLIER` + `skip_query_analysis` flag to
  fast profile in `fusion.py` and `core.py`. Register in `config.py`.
- Benchmark: run `scripts/profile_hook_latency.py` (NEW) for 60s of
  `prompt-recall` calls. Compare p95 before/after.
- Green tests + measurable p95 drop.

### Step 4 — Cross-handler audit (≤ 0.25 day)
- Walk every `/hooks/*` handler in http.py.
- Update §4.4 table with confirmed profile + confirm no new unguarded balanced
  calls introduced by other in-flight work.

### Step 5 — Stats cache (≤ 0.25 day, lower priority)
- TDD: write `test_stats_cache.py` — red.
- Implement §4.6 cache in `http.py`. Green tests.
- Note: §4.5 (visibility-aware refresh) is DROPPED — do not implement until `stats.js` exists.

### Step 6 — Acceptance + docs (≤ 0.25 day)
- Run full pytest suite. Zero new failures.
- Append v5.51.0 section to `MIGRATION_NOTES.md`: fast profile changes,
  new config keys, timeout defaults, recall-quality degradation warning.
- `CHANGELOG.md` v5.51.0 entry.

---

## 7. Effort estimate

| Step | Days |
|---|---:|
| Step 0 hotfix verify | 0.1 |
| Step 1 instructions-loaded fix | ~~0.25~~ 0 (done) |
| Step 2 timeout + circuit breaker | 0.5 |
| Step 3 fast profile tuning | 0.5 |
| Step 4 cross-handler audit | 0.1 |
| Step 5 stats cache (lower priority) | 0.25 |
| Step 6 acceptance + docs | 0.25 |
| **Total (all steps)** | **~1.7 days** |
| **Total (core scope — Steps 0, 2, 3, 4)** | **~1.2 days** |

Note: original estimate was ~2.3 days (all steps including §4.1 and §4.5). §4.1 shipped, §4.5 dropped. Adjusted to ~1 day core / ~1.7 days full.

---

## 8. Acceptance criteria

v5.51.0 ships when ALL applicable items are true:

- [ ] v5.25.2 + v5.25.3 hotfixes confirmed landed (Step 0 gate).
- [ ] `hook_instructions_loaded` uses `profile="fast"` (v5.25.3 regression — test green).
- [ ] `HOOK_RECALL_TIMEOUT_S` registered three-way (config.py/config.yaml/code).
      All three hook handlers wrapped in `asyncio.wait_for`. Tests green.
      `yadgar_hook_recall_timeout_total{handler}` counter exists in `metrics.py`. Tests green.
- [ ] `FAST_PROFILE_CANDIDATE_MULTIPLIER` registered three-way.
      `skip_query_analysis` flag in fast profile. Tests green.
- [ ] Benchmark: prompt-recall p95 < 500ms, p99 < 2s, zero calls > 5s.
      Measured by `scripts/profile_hook_latency.py` on a realistic graph
      (≥1000 memories, ≥500 wiki, ≥3000 edges).
- [ ] Cross-handler audit table (§4.4) re-confirmed with no new gaps.
- [ ] `/api/stats` TTL cache implemented (`YADGAR_STATS_CACHE_TTL_S`, `http.py:~1290`). Tests green.
- [ ] `MIGRATION_NOTES.md` v5.51.0 section: config keys, defaults, recall-quality degradation warning.
- [ ] `CHANGELOG.md` v5.51.0 entry.
- [ ] `python scripts/check_versions.py` exit 0.

---

## 9. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Fast profile too fast → recall quality regression | A/B against retrieval recall@K (v5.26.0 numbers when available). If recall degrades >10%, raise `FAST_PROFILE_CANDIDATE_MULTIPLIER` or re-enable one signal. |
| Timeout fires on legitimately slow queries | Default 2.0s is conservative; p99 was 10s, so 2s cuts ~1% of slowest calls. Monitor `yadgar_hook_recall_timeout_total` counter rate. Raise to 5s if rate too high. |
| ~~instructions-loaded fix should have gone into v5.25.2 hotfix~~ | Resolved: shipped v5.25.3. No longer a risk. |
| §4.5 deferred indefinitely | stats.js prerequisite absent from `yadgar/static/`. Explicitly dropped. Re-add to a future plan when stats.js ships. |
| viz_search starvation (fast profile, user-initiated) | Out of scope v5.51.0. Track in follow-up. Not a hook-driven source. |
| Param tuning requires benchmark infra not yet built | `scripts/profile_hook_latency.py` is a NEW artifact in Step 3. Similar to existing `profile_stats_endpoint.py` in prior plan. Low risk. |

---

## 10. TDD test list (write red → implement green)

**Step 2:**
1. `test_hook_latency_budget.py::test_timeout_fires_warn_returns_empty`
2. `test_hook_latency_budget.py::test_timeout_configurable_via_setting`
3. `test_hook_latency_budget.py::test_timeout_does_not_raise_to_caller`
4. `test_hook_latency_budget.py::test_timeout_increments_prometheus_counter`

**Step 3:**
5. `test_fast_profile_tuning.py::test_fast_profile_skips_query_expansion`
6. `test_fast_profile_tuning.py::test_fast_profile_candidate_multiplier_respected`
7. `test_fast_profile_tuning.py::test_balanced_profile_unaffected`

**Regressions (Step 0 confirm):**
8. `test_hook_fast_profile.py::test_instructions_loaded_uses_fast_profile` (regression — v5.25.3)
9. `test_hook_fast_profile.py::test_prompt_recall_uses_fast_profile` (regression)
10. `test_hook_fast_profile.py::test_subagent_start_uses_fast_profile` (regression post-hotfix)

**Step 5 (lower priority):**
11. `test_stats_cache.py::test_cache_hit_within_ttl`
12. `test_stats_cache.py::test_cache_miss_after_ttl`
13. `test_stats_cache.py::test_different_args_dont_collide`
14. `test_stats_cache.py::test_cache_disabled_when_ttl_zero`

Note: `test_stats_visibility_throttle.py` tests are removed from scope (§4.5 dropped).

---

## 11. Dependencies + coordination

- **v5.25.2 hotfix must land first** (Step 0 gate). v5.25.3 (instructions-loaded fix) is already confirmed shipped.
- **v5.50.0 not a blocker for core scope.** §4.5 dropped entirely; §4.6 stats cache targets `http.py` directly, no `stats.js` needed.
- No external dependencies.
- No backend schema changes. All config keys are in-memory.
- Implementation feature branch: `feat/v5.51.0-hooks-fast-profile-tuning` after
  this plan commits.
- Related plans: `docs/PLAN_V5_50_0_*.md` (parent), `docs/PLAN_V5_25_0_*.md`
  (v5.25.2 hotfix), `docs/PLAN_V5_52_0_*.md` (downstream).
- Plan-only doc → direct to master per workflow rule.
