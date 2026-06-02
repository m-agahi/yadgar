# PLAN — v5.51.0: Hooks fast-profile tuning + latency budget

**Status:** DRAFT — 2026-05-31. REVISED 2026-06-02 post-opus-review (minor). Plan-first per I27.

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

**Effort estimate:** 1-2 calendar days.

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
- Per-call timeout + latency-budget circuit breaker: exceeding budget logs WARN
  and returns degraded (empty or cached) result instead of blocking the hook handler.
- Visibility-aware refresh for browser tabs (lower priority — keep `/api/stats`
  poll as a section).

---

## 2. Non-goals

- Full rewrite of retrieval pipeline (R2 plugin arch, v5.31 candidate).
- Prometheus scrape changes — verified non-cause of CPU bursts.
- New monitoring infrastructure (OpenTelemetry, etc.) — use existing in-process
  structured logging + `_hook_observe` telemetry already in place.
- Endpoint redesign of `/api/stats` — cadence is contributory at 0.6% CPU,
  not a burst source. Cache/throttle deferred to §6.5 (lower priority).
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

**Source 2 — instructions-loaded (NOT in hotfix — v5.51.0 scope)**

`/hooks/instructions-loaded` (http.py line 953) also calls `retriever.recall()`
without `profile="fast"`. Fires on every session_start and compact. Not covered
by v5.25.2 — explicitly a v5.51.0 target.

**Source 3 — prompt-recall fast profile still too slow**

`/hooks/prompt-recall` already uses `profile="fast"` (http.py line 525), but
42/74 calls still take 2.5-10s. Fast profile disables CE/NLI/MP (confirmed in
`yadgar/retrieval/fusion.py` lines 14-19) — the cost is in:
- `CANDIDATE_POOL_MULTIPLIER`: candidate pool = `max_results * multiplier`.
  With multiplier=10 and `max_results=5`, pool = 50 candidates fetched from DB.
- `_dual_vector_search`: runs two kNN scans (explicit + implicit vectors).
- FTS5/BM25 query latency against SurrealDB.
- `analyze_query` + `_extract_query_entities` overhead on every call.

The throttle gate (2-min rate limit per directory) gates frequency, not
magnitude. A single unthrottled call can still burst.

**Source 4 — consolidation daemon stalled (closes with v5.25.2 hotfix)**

`SecretLeakBlocked` poison-pill in `cleanup._process_action_log()` stalls the
daemon, causing backlog + burst when it eventually drains. v5.25.2 adds a
poison-pill skip.

### 3.3 /api/stats cadence is NOT a burst source

60s poll × 360ms/call = 0.6% sustained CPU. Not a spike pattern. Lower-priority
section kept in §6.5 for completeness.

---

## 4. Scope — concrete file changes

### 4.1 Fix instructions-loaded missing fast profile

| File | Change |
|---|---|
| `yadgar/server/http.py` | Add `profile="fast"` to `retriever.recall()` call in `hook_instructions_loaded` (line ~953). One-liner, same pattern as prompt-recall. |

### 4.2 Tune fast profile params in RetrievalEngine

The candidate pool and query-analysis overhead are the primary cost drivers on
the fast path. Tuning targets (exact values: open questions in §6):

| File | Change |
|---|---|
| `yadgar/retrieval/fusion.py` | Add `"fast"` profile entry for `candidate_multiplier` override (e.g. 3 instead of global default). Document in PROFILES dict comment. |
| `yadgar/retrieval/core.py` | When `profile_name == "fast"`: skip `_pseudo_hyde_expand` and `_extract_query_entities` (query expansion irrelevant for context injection). Gate via `profile.get("skip_query_analysis", False)`. |
| `yadgar/config.py` / `yadgar/settings.py` | Add `FAST_PROFILE_CANDIDATE_MULTIPLIER` (int, default 3). |
| `config.yaml` | Add `retrieval.fast_profile_candidate_multiplier: 3`. |

### 4.3 Per-call timeout + latency budget circuit breaker

| File | Change |
|---|---|
| `yadgar/server/http.py` | Wrap `asyncio.to_thread(retriever.recall, ...)` in `asyncio.wait_for(...)` with configurable timeout in all hook handlers. If timeout fires: log WARN (`hook latency budget exceeded`) + return `{"text": ""}`. Handlers: `prompt_recall`, `instructions_loaded`, `subagent_start` (post-hotfix). |
| `yadgar/config.py` / `yadgar/settings.py` | Add `HOOK_RECALL_TIMEOUT_S` (float, default 2.0). |
| `config.yaml` | Add `hooks.recall_timeout_s: 2.0`. |

### 4.4 Cross-handler audit table

Every `/hooks/*` handler + retrieval profile used (to be confirmed during implementation):

| Handler | HTTP method | Retrieval call | Profile | Status |
|---|---|---|---|---|
| `/hooks/prompt-recall` | GET | `retriever.recall(profile="fast")` | fast | OK — but fast profile too slow (§3.3) |
| `/hooks/subagent-start` | POST | `retriever.recall()` | **balanced** | FIXED in v5.25.2 |
| `/hooks/instructions-loaded` | GET | `retriever.recall()` | **balanced** | v5.51.0 target (§4.1) |
| `/hooks/session-context` | GET | `project_brief()` — no retriever call | N/A | OK |
| `/hooks/auto-capture` | POST | no retriever call | N/A | OK |
| `/hooks/pre-compact` | POST | no retriever call | N/A | OK |
| `/hooks/post-compact` | GET | no retriever call | N/A | OK |
| `/hooks/subagent-stop` | POST | no retriever call (writes only) | N/A | OK |
| `/hooks/file-changed` | POST | no retriever call | N/A | OK |

Notes:
- `viz_search` (line ~1388) calls `retriever.recall()` without profile — but this
  is user-initiated, not hook-driven. Out of scope for v5.51.0; track separately.
- Table to be re-verified during implementation against current http.py.

### 4.5 Visibility-aware refresh (lower priority)

`yadgar/static/bookmarks.js` exists; `stats.js` does not yet (ships in v5.50.0).

| File | Change |
|---|---|
| `yadgar/static/stats.js` (v5.50.0, not yet created) | EXTEND in v5.51.0: gate poll on `document.visibilityState === "visible"`. Backoff: 30s normal, 60s after 5 min hidden. Immediate repoll on visibility restored. |
| `yadgar/static/tabs.js` (v5.50.0, not yet created) | EXTEND in v5.51.0: emit `tab:change` event for stats.js instant repoll on activation. |

If v5.50.0 slips, this section defers with it.

### 4.6 /api/stats throttle — lower priority section

Original v5.51.0 target, demoted. Still worth shipping, but not on the critical path.

| File | Change |
|---|---|
| `yadgar/server/api/stats.py` | Add in-memory TTL cache (default 5s via `YADGAR_STATS_CACHE_TTL_S`). Second call within TTL returns cached. |
| `yadgar/settings.py` | Register `YADGAR_STATS_CACHE_TTL_S` (int, default 5). |
| `config.yaml` | Add `stats.cache_ttl_s: 5`. |

### 4.7 Tests

| File | Change |
|---|---|
| `yadgar/tests/test_hook_fast_profile.py` | NEW. `test_instructions_loaded_uses_fast_profile` — mock retriever, confirm profile kwarg. `test_prompt_recall_uses_fast_profile` — regression. `test_subagent_start_uses_fast_profile` — regression post-hotfix. |
| `yadgar/tests/test_hook_latency_budget.py` | NEW. `test_timeout_fires_warn_returns_empty` — mock `asyncio.wait_for` to raise `TimeoutError`, confirm WARN logged + empty text returned. `test_timeout_configurable` — `HOOK_RECALL_TIMEOUT_S=0.1` triggers on slow mock. |
| `yadgar/tests/test_fast_profile_tuning.py` | NEW. `test_fast_profile_skips_query_expansion` — retriever with `profile="fast"` does not call `_pseudo_hyde_expand`. `test_fast_profile_candidate_multiplier` — pool size uses `FAST_PROFILE_CANDIDATE_MULTIPLIER`, not global default. |
| `yadgar/tests/test_stats_cache.py` | NEW (§4.6, lower priority). TTL cache semantics: hit/miss/expiry/key-isolation/disabled-at-zero. |
| `yadgar/tests/test_stats_visibility_throttle.py` | NEW (§4.5, lower priority). jsdom: hidden pauses poll, visible resumes, inactivity backoff, tab activation triggers immediate repoll. |

---

## 5. Open questions

1. **Exact param targets for fast profile.** What candidate multiplier hits <500ms p95?
   Start at 3 (vs. global default ~10); benchmark against retrieval recall@K baseline
   (v5.26.0 numbers when available). If recall degrades >10%, raise to 5.

2. **Timeout value.** 2s vs. 1s vs. 5s. Histogram shows p99 = 10s — 2s cuts off ~1%
   of calls that are legitimately slow. 1s is aggressive. Recommend 2.0s default,
   configurable. Re-evaluate after first prod histogram.

3. **Query analysis skip threshold.** `analyze_query` does entity extraction +
   semantic routing — for 2-3 word hook queries, the analysis overhead may exceed
   the retrieval itself. Profile with and without `skip_query_analysis=True` on
   fast profile to confirm.

4. **instructions-loaded — roll into v5.25.2 hotfix or keep in v5.51.0?**
   One-liner patch, same risk profile as subagent-start fix. Main thread to decide
   whether to fold into the in-flight hotfix or land here. If folded, remove §4.1.

5. **viz_search profile.** User-initiated, not a hook — out of scope for v5.51.0.
   But it runs on the main event loop and can starve hook handlers. Track in
   a follow-up plan.

---

## 6. Step plan (TDD per HARD RULE)

### Step 0 — Verify v5.25.2 hotfix landed (≤ 0.1 day)
- Confirm `subagent-start` uses `profile="fast"`.
- Confirm `SecretLeakBlocked` poison-pill skip is in `cleanup.py`.
- If hotfix not yet landed: this plan BLOCKED. Wait.

### Step 1 — Fix instructions-loaded profile (≤ 0.25 day)
- TDD: write `test_instructions_loaded_uses_fast_profile` — red.
- One-liner: add `profile="fast"` to `retriever.recall()` call in
  `hook_instructions_loaded` (http.py ~line 953).
- Run tests → green. Run `pytest -x yadgar/tests/test_hook_fast_profile.py`.

### Step 2 — Per-call timeout + circuit breaker (≤ 0.5 day)
- TDD: write `test_hook_latency_budget.py` — red.
- Add `HOOK_RECALL_TIMEOUT_S` setting. Wrap all three hook recall calls in
  `asyncio.wait_for(..., timeout=settings.HOOK_RECALL_TIMEOUT_S)`.
- Run tests → green. Confirm WARN log fires on simulated timeout.

### Step 3 — Tune fast profile params (≤ 0.5 day)
- TDD: write `test_fast_profile_tuning.py` — red.
- Add `FAST_PROFILE_CANDIDATE_MULTIPLIER` + `skip_query_analysis` flag to
  fast profile. Gate query expansion skip in `core.py`.
- Benchmark: run `scripts/profile_hook_latency.py` (NEW) for 60s of
  `prompt-recall` calls. Compare p95 before/after.
- Green tests + measurable p95 drop.

### Step 4 — Cross-handler audit (≤ 0.25 day)
- Walk every `/hooks/*` handler in http.py.
- Update §4.4 table with confirmed profile + confirm no new unguarded balanced
  calls introduced by other in-flight work.

### Step 5 — Visibility-aware refresh + stats cache (≤ 0.5 day, lower priority)
- Conditional on v5.50.0 having shipped (stats.js exists).
- TDD: write throttle + cache tests — red.
- Implement §4.5 + §4.6. Green tests.

### Step 6 — Acceptance + docs (≤ 0.25 day)
- Run full pytest suite. Zero new failures.
- Append v5.51.0 section to `MIGRATION_NOTES.md`: fast profile changes,
  new config keys, timeout defaults.
- `CHANGELOG.md` v5.51.0 entry.

---

## 7. Effort estimate

| Step | Days |
|---|---:|
| Step 0 hotfix verify | 0.1 |
| Step 1 instructions-loaded fix | 0.25 |
| Step 2 timeout + circuit breaker | 0.5 |
| Step 3 fast profile tuning | 0.5 |
| Step 4 cross-handler audit | 0.25 |
| Step 5 vis-refresh + stats cache (conditional) | 0.5 |
| Step 6 acceptance + docs | 0.25 |
| **Total (all steps)** | **~2.3 days** |
| **Total (skip step 5 — v5.50 not yet shipped)** | **~1.5 days** |

---

## 8. Acceptance criteria

v5.51.0 ships when ALL applicable items are true:

- [ ] v5.25.2 hotfix confirmed landed (Step 0 gate).
- [ ] `hook_instructions_loaded` uses `profile="fast"`. Test green.
- [ ] `HOOK_RECALL_TIMEOUT_S` registered three-way (settings/config/code).
      All three hook handlers wrapped in `asyncio.wait_for`. Tests green.
- [ ] `FAST_PROFILE_CANDIDATE_MULTIPLIER` registered three-way.
      `skip_query_analysis` flag in fast profile. Tests green.
- [ ] Benchmark: prompt-recall p95 < 500ms, p99 < 2s, zero calls > 5s.
      Measured by `scripts/profile_hook_latency.py` on a realistic graph
      (≥1000 memories, ≥500 wiki, ≥3000 edges).
- [ ] Cross-handler audit table (§4.4) re-confirmed with no new gaps.
- [ ] IF v5.50.0 shipped: visibility throttle + stats cache tests green.
- [ ] `MIGRATION_NOTES.md` v5.51.0 section documents config keys + defaults.
- [ ] `CHANGELOG.md` v5.51.0 entry.
- [ ] `python scripts/check_versions.py` exit 0.

---

## 9. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Fast profile too fast → recall quality regression | A/B against retrieval recall@K (v5.26.0 numbers when available). If recall degrades >10%, raise `FAST_PROFILE_CANDIDATE_MULTIPLIER` or re-enable one signal. |
| Timeout fires on legitimately slow queries | Default 2.0s is conservative; p99 was 10s, so 2s cuts ~1% of slowest calls. Monitor `hook_latency_budget_exceeded` log rate. Raise to 5s if rate too high. |
| instructions-loaded fix should have gone into v5.25.2 hotfix | Main thread decides (open question §5.4). If folded, remove §4.1 from this plan and update scope. |
| v5.50.0 slips → stats.js doesn't exist at v5.51.0 implementation time | §4.5 and §4.6 defer with it — explicitly gated in Step 5. |
| viz_search starvation (no profile=, user-initiated) | Out of scope v5.51.0. Track in follow-up. Not a hook-driven source. |
| Param tuning requires benchmark infra not yet built | `scripts/profile_hook_latency.py` is a NEW artifact in Step 3. Similar to existing `profile_stats_endpoint.py` in prior plan. Low risk. |

---

## 10. TDD test list (write red → implement green)

**Step 1:**
1. `test_hook_fast_profile.py::test_instructions_loaded_uses_fast_profile`
2. `test_hook_fast_profile.py::test_prompt_recall_uses_fast_profile` (regression)
3. `test_hook_fast_profile.py::test_subagent_start_uses_fast_profile` (regression)

**Step 2:**
4. `test_hook_latency_budget.py::test_timeout_fires_warn_returns_empty`
5. `test_hook_latency_budget.py::test_timeout_configurable_via_setting`
6. `test_hook_latency_budget.py::test_timeout_does_not_raise_to_caller`

**Step 3:**
7. `test_fast_profile_tuning.py::test_fast_profile_skips_query_expansion`
8. `test_fast_profile_tuning.py::test_fast_profile_candidate_multiplier_respected`
9. `test_fast_profile_tuning.py::test_balanced_profile_unaffected`

**Step 5 (conditional — v5.50.0 shipped):**
10. `test_stats_cache.py::test_cache_hit_within_ttl`
11. `test_stats_cache.py::test_cache_miss_after_ttl`
12. `test_stats_cache.py::test_different_args_dont_collide`
13. `test_stats_cache.py::test_cache_disabled_when_ttl_zero`
14. `test_stats_visibility_throttle.py::test_hidden_tab_pauses_poll`
15. `test_stats_visibility_throttle.py::test_visible_tab_resumes_poll`
16. `test_stats_visibility_throttle.py::test_inactive_tab_backoff`
17. `test_stats_visibility_throttle.py::test_tab_activation_triggers_immediate_repoll`

---

## 11. Dependencies + coordination

- **v5.25.2 hotfix must land first** (Step 0 gate). If in-flight at plan-read time,
  wait for merge before beginning implementation.
- **v5.50.0 may need to ship first** for §4.5/§4.6 (stats.js prerequisite). Steps 1-4
  are independent and can start immediately after v5.25.2.
- No external dependencies.
- No backend schema changes. All config keys are in-memory.
- Implementation feature branch: `feat/v5.51.0-hooks-fast-profile-tuning` after
  this plan commits. If scope shrinks (Steps 1-4 only), may fold into v5.52.0.
- Related plans: `docs/PLAN_V5_50_0_*.md` (parent), `docs/PLAN_V5_25_0_*.md`
  (v5.25.2 hotfix), `docs/PLAN_V5_52_0_*.md` (downstream).
- Plan-only doc → direct to master per workflow rule.
