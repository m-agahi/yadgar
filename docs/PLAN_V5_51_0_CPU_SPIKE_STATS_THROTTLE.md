# PLAN — v5.51.0: CPU Spike Investigation + Stats Refresh Throttle

**Status:** drafted 2026-05-31. Plan-first per I27.

**Renumbered:** v5.42.0 → v5.51.0 on 2026-05-31. User explicitly bumped the viz train forward so the setup-refactor (v5.45-v5.47) ships first. Numbering is locked at v5.50 / v5.51 / v5.52. Do NOT revert to v5.42 anywhere.

**Depends on:** v5.50.0 shipped — the Stats tab is the symptomatic surface (30s fixed poll, no visibility awareness). Backend hot-path profiling does not require v5.50, but the throttle work targets the new Stats tab specifically.

**Downstream:** none. May fold into v5.50.0 or v5.52.0 if scope turns out small.

**Effort estimate:** 0.5-1.5 calendar days depending on what the profile reveals.

---

## Goal — find the real CPU contributor, then throttle correctly

Two-part:

1. **Profile the CPU spike root cause.** Spike has been observed on Stats refresh under v5.24.x but never properly profiled. The reflexive fix ("just raise the poll interval") is likely treating a symptom. Hypothesis: one of the `/api/stats`-adjacent endpoints does expensive work per call — graph snapshot serialization, vector index sample, FTS query — and the cost is per-poll, independent of cadence. Raising cadence reduces frequency of pain but not magnitude per spike.

2. **Apply a Stats refresh throttle ONLY IF cadence is contributory.** v5.50.0 ships Stats at fixed 30s poll, no visibility awareness. If profiling shows cadence is contributory, add visibility-aware throttle: poll only when the Stats tab is the active tab AND the browser tab is visible (`document.visibilityState === "visible"`). Backoff to 60s after 5 minutes idle.

The hard rule from prior context: **profile before throttling**. If the spike is one expensive endpoint, the fix is endpoint-side, not cadence-side.

---

## Non-goals (explicit)

- **No speculative throttle without profile evidence.** If the profile is inconclusive, ship the profile harness as a debug tool and defer the throttle to v5.52 or later.
- **No endpoint redesign.** If a specific endpoint is the offender, the fix is to cache or short-circuit, not to rewrite it from scratch. Cache TTL ≤ poll interval.
- **No CPU mini-overlay redesign.** The Health tab (v5.50.0) is the live CPU surface. This plan does not add another.
- **No new monitoring infrastructure** (Prometheus, OpenTelemetry, etc.). Use the existing in-process profiler + structured logging.
- **No backend rewrite to async if it's currently sync.** That's a v5.6x candidate, not this plan.

---

## Current state (verified from code, 2026-05-31)

| Asset | Path | Status |
|---|---|---|
| Stats poll | `yadgar/server/static/index.html` `pollStats()` (pre-v5.50) / `static/js/stats.js` (post-v5.50) | 5s interval pre-v5.50; 30s in v5.50.0. Both fixed. No visibility-awareness. |
| Stats endpoints | `yadgar/server/api/stats.py` | `GET /api/stats` aggregates: memory count, wiki count, edge count, hot memories, recent episodes. Calls into SurrealDB. Not profiled. |
| Adjacent endpoints suspected | `GET /api/graph/snapshot`, `GET /api/vector/sample`, `GET /api/fts/preview` | Called by Home + Stats tabs at unclear intervals. |
| CPU spike observation | anecdotal | Reported during Stats tab being open. Magnitude not measured. Spike cadence not measured. |
| Profile harness | none | No structured CPU profile capture exists. `cProfile` is the obvious choice; nothing wired. |

---

## Scope — concrete file changes

### Profiling (Step 1)

| File | Change |
|---|---|
| `scripts/profile_stats_endpoint.py` | NEW. Drives `/api/stats` + adjacent endpoints in a tight loop with `cProfile`. Saves `prof.out` + a flame-graph SVG via `snakeviz` or `flameprof`. Reads bearer token from env. |
| `docs/CPU_SPIKE_PROFILE_v5.51.md` | NEW. Captures the profile output, hot functions ranked by cumulative time, interpretation. Source of truth for what got fixed and why. |

### Endpoint fix (Step 2 — IF profile points to an endpoint)

| File | Change |
|---|---|
| `yadgar/server/api/stats.py` | IF needed: cache the expensive sub-call with a TTL ≤ poll interval. Cache key = arguments tuple. TTL configurable via `YADGAR_STATS_CACHE_TTL_S` (default 5s — under the 30s poll). |
| `yadgar/server/api/<offender>.py` | IF a sibling endpoint is the offender: same treatment. |
| `yadgar/settings.py` | Register `YADGAR_STATS_CACHE_TTL_S` (int, default 5) if a cache lands. |
| `config.yaml` | Add `stats.cache_ttl_s=5` if a cache lands. |

### Visibility-aware throttle (Step 3 — IF cadence is contributory)

| File | Change |
|---|---|
| `yadgar/server/static/js/stats.js` | EXTEND. Poll only when `(location.hash === "#stats" \|\| location.hash === "")` (Home tab does not need Stats data) AND `document.visibilityState === "visible"`. Backoff: 30s normal, 60s after 5 min of inactivity, immediate on visibility-restored. |
| `yadgar/server/static/js/tabs.js` | EXTEND. Emit `tab:change` event that `stats.js` subscribes to for instant repoll on tab activation. |

### Tests

| File | Change |
|---|---|
| `yadgar/tests/test_stats_cache.py` | NEW (if cache lands). Second call within TTL returns cached. After TTL, refetches. Different arguments don't collide. |
| `yadgar/tests/test_stats_visibility_throttle.py` | NEW (if visibility throttle lands). Jsdom. `visibilityState=hidden` pauses poll. `=visible` resumes within 1 tick. Inactivity backoff trips after 5 min. |

---

## Hypotheses to test in profile (Step 1)

Ranked by likelihood:

1. **Graph snapshot endpoint** — `GET /api/graph/snapshot` serializes the full node + edge set on each call. Expected O(N) per call. For N=5000 nodes, this is ~5-20ms in JSON encode. At 5s poll, that's 0.1-0.4% CPU sustained — not enough to cause a "spike". Probably not the offender unless N is much larger.

2. **Vector index sample** — `GET /api/vector/sample` likely calls into SurrealDB's vector index. If it re-runs `kNN` or full-scan on each call, this is the most plausible spike source. Expected hot.

3. **FTS query at every Stats refresh** — if Stats includes a "recent text matches" preview, full-text search is O(corpus). Plausible spike source.

4. **Hot memories aggregation** — re-sorts the entire memory table by heat on each call. Plausible if no index on `heat`.

5. **Edge count via COUNT(*)** — full table scan on the edges table. Plausible if no count cache.

6. **Cadence alone** — frontend polls too often, each call is cheap individually, but the sum across tabs / open browsers is sustained CPU. Possible but unlikely as the primary cause.

Profile harness in Step 1 ranks these by cumulative time. The first hypothesis confirmed wins the fix slot in Step 2.

---

## Open questions (must resolve during implementation)

1. **Spike magnitude.** What's "expensive"? Define: any per-call cumulative time > 100ms in cProfile, OR observed CPU > 15% sustained over 30s, qualifies. Tighter thresholds increase false positives; looser miss real issues.
2. **Reproducibility of the spike.** Anecdotal observation isn't sufficient. Step 0 confirms repro on a known graph size. If the spike can't be reproduced locally, this plan deferred until a repro is available — DO NOT ship speculative fixes.
3. **Cache invalidation.** If Step 2 lands a cache, when does cache invalidate on writes? Two options: (a) TTL only — accepted staleness for the TTL window; (b) write-through invalidation — mutations bust the cache. Lean (a) for simplicity. Stats displays are not transactional; 5s staleness is fine.
4. **Should the Health tab (v5.50.0) also get the throttle?** Health shows live CPU/RSS. Throttling Health makes the live display laggy. Lean: leave Health at its existing cadence; only Stats gets the throttle.
5. **Scope folding.** If Step 1 reveals one expensive endpoint with a trivial cache fix (<50 LOC total), this plan should fold into v5.50.0 as a final commit. If the profile reveals a structural issue, this plan stays standalone in v5.51.0. If neither — no spike reproducible, no fix needed — the plan defers and the slot is reused. Decision happens after Step 1.

---

## Step plan (TDD per HARD RULE)

### Step 0 — Reproduce the spike (≤ 0.25 day)
- Boot yadgar daemon on a graph with realistic size (≥1000 memories, ≥500 wiki, ≥3000 edges). If the local graph is smaller, seed via existing test fixtures or skip-and-defer.
- Open the viz Stats tab. Watch `top -p <pid>`. Capture 5 minutes of CPU samples at 1Hz.
- If CPU never exceeds 5% sustained, the spike is not reproducible on this host. STOP. Report and defer.
- If reproducible, proceed.

### Step 1 — Profile (≤ 0.25 day)
- Run `scripts/profile_stats_endpoint.py` for 60s of `/api/stats` calls at 1-second cadence.
- Save `prof.out`. Generate flame graph.
- Write `docs/CPU_SPIKE_PROFILE_v5.51.md` with ranked hot functions and interpretation.
- Decision gate: if the top hot function is an endpoint with cumulative time > 100ms per call, this is the offender. If everything is uniformly cheap and cadence is the only variable, throttle becomes the fix.

### Step 2 — Endpoint fix (≤ 0.5 day) — CONDITIONAL ON Step 1 OUTCOME
- TDD: write `test_stats_cache.py` — fast double-call returns cached, post-TTL refetches.
- Implement the cache (decorator or in-handler dict) with TTL from `YADGAR_STATS_CACHE_TTL_S`.
- Re-run profile harness. Cumulative time per `/api/stats` call should drop by ≥50%.

### Step 3 — Visibility-aware throttle (≤ 0.25 day) — CONDITIONAL ON Step 1 OUTCOME
- TDD: `test_stats_visibility_throttle.py` — jsdom toggle of `visibilityState`.
- Implement: poll gated on `(active_tab in ["#stats", ""]) && visibilityState === "visible"`.
- Backoff: 30s → 60s after 5 minutes of `visibilityState !== "visible"` OR active tab not Stats.

### Step 4 — Document hot-path in MIGRATION_NOTES (≤ 0.1 day)
- Append a section to `MIGRATION_NOTES.md` v5.51.0: "Stats CPU hot-path identified: [function]. Fix: [cache / throttle / both]. Configurable via [knob]. Expected sustained CPU drop: [Δ %]."
- Link `docs/CPU_SPIKE_PROFILE_v5.51.md` from MIGRATION_NOTES.

### Step 5 — Acceptance + CHANGELOG (≤ 0.1 day)
- Run full pytest suite.
- Manual smoke: open Stats tab, watch CPU. Confirm spike is mitigated.
- `CHANGELOG.md` v5.51.0 entry.

---

## Effort estimate (calendar days)

| Step | Days |
|---|---:|
| Step 0 reproduce | 0.25 |
| Step 1 profile | 0.25 |
| Step 2 endpoint fix (conditional) | 0.5 |
| Step 3 visibility throttle (conditional) | 0.25 |
| Step 4 MIGRATION_NOTES | 0.1 |
| Step 5 acceptance | 0.1 |
| **Total (both fixes land)** | **~1.5 days** |
| **Total (one fix lands)** | **~1.0 day** |
| **Total (no spike repro)** | **~0.5 day (profile + report only)** |

---

## Acceptance criteria

v5.51.0 ships when ALL applicable items are true:

- [ ] `scripts/profile_stats_endpoint.py` exists and runs successfully against a live daemon.
- [ ] `docs/CPU_SPIKE_PROFILE_v5.51.md` documents the profile output and identifies (or rules out) the hot path.
- [ ] IF an endpoint cache landed: `test_stats_cache.py` green, sustained CPU drop measurable, `YADGAR_STATS_CACHE_TTL_S` registered three-way.
- [ ] IF visibility throttle landed: `test_stats_visibility_throttle.py` green, hidden-tab CPU at idle equivalent.
- [ ] `MIGRATION_NOTES.md` v5.51.0 section documents the hot path + fix.
- [ ] `CHANGELOG.md` v5.51.0 entry references the profile doc.
- [ ] `python scripts/check_versions.py` exit 0.

**Alternative ship — if no spike reproducible (decision after Step 0):** ship the profile harness only (`scripts/profile_stats_endpoint.py`) + a MIGRATION_NOTES note documenting the negative result. No version bump if scope is purely the script — fold the script into v5.50.0 instead. Decision point.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Spike not reproducible on dev hardware | Step 0 gate. If no repro, defer the plan. Do not ship speculative fixes. |
| Profile points at a function with no clean cache surface (e.g. deep inside SurrealDB driver) | Document the finding and defer the actual fix to a database-side investigation (separate plan). Land the throttle as a partial mitigation. |
| Cache returns stale data during a high-write window | Cache TTL ≤ 5s. Stats is not transactional; users tolerate 5s staleness. If higher writes appear later, write-through invalidation is a cheap extension. |
| Visibility throttle interacts badly with multi-window users (one window visible, others hidden) | Per-document visibility is correct — each window throttles independently. Backend cache absorbs the cost of multiple visible windows. |
| `prefers-reduced-motion` users disable visibility transitions, breaking the throttle hook | Use `document.visibilityState` (event), not animation hooks. Independent of motion prefs. |
| Folding decision changes mid-flight (start as v5.51, scope shrinks, want to fold into v5.50) | Decision gate after Step 1. If folding, abandon v5.51 plan + version, append commits to v5.50 branch. Document the fold in the v5.50 CHANGELOG entry. |

---

## TDD test list (write red, then implement green) — conditional

If Step 2 endpoint cache lands:

1. `test_stats_cache.py::test_cache_hit_within_ttl` — two calls within TTL, second returns cached value.
2. `test_stats_cache.py::test_cache_miss_after_ttl` — sleep past TTL, next call refetches.
3. `test_stats_cache.py::test_different_args_dont_collide` — different request args use different cache keys.
4. `test_stats_cache.py::test_cache_disabled_when_ttl_zero` — `YADGAR_STATS_CACHE_TTL_S=0` disables caching entirely.

If Step 3 visibility throttle lands:

5. `test_stats_visibility_throttle.py::test_hidden_tab_pauses_poll` — `visibilityState=hidden` for 60s → no poll fired.
6. `test_stats_visibility_throttle.py::test_visible_tab_resumes_poll` — toggle back to `visible` → next poll fires within 1 tick.
7. `test_stats_visibility_throttle.py::test_inactive_tab_backoff` — Stats tab not active for 5 minutes → poll cadence increases to 60s.
8. `test_stats_visibility_throttle.py::test_tab_activation_triggers_immediate_repoll` — switching to Stats tab fires an immediate poll regardless of last-poll timestamp.

---

## Dependencies + blockers

- **v5.50.0 must ship first.** Stats tab is introduced there.
- **No backend schema changes.** Cache is in-memory.
- **No new external dependencies.**

---

## Coordination notes for main thread

- Plan-only doc → direct to master per workflow rule.
- Implementation feature branch: `feat/v5.51.0-cpu-spike-stats-throttle` after this plan commits, IF the plan ships standalone. If it folds into v5.50.0, the work goes on the v5.50.0 branch directly.
- Related plans: `docs/PLAN_V5_50_0_*.md` (parent), `docs/PLAN_V5_52_0_*.md` (downstream).
- Decision gate after Step 1: profile result determines whether v5.51.0 ships standalone, folds into v5.50.0, or defers.
