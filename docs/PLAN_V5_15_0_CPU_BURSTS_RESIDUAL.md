# PLAN — v5.15.0: CPU burst residual investigation + detection infrastructure + secret-gate caller plumbing

**Renumbered:** v5.12.0 → v5.15.0 on 2026-05-30. Reason: skip-1 minor convention adopted 2026-05-30 — odd-only minors for sequential features, even slots reserved for hotfix patches between them.

**Scope bundle (added 2026-05-30, post-v5.13.0 ship):** v5.13.0 secret-gate allowlist mechanism shipped DORMANT — `gate_or_reject(tags=, source=)` accepts context but no production write tool (`memorize`, `wiki_add`, `anchor`) forwards its `tags` to the gate. Allowlist tested via direct `is_allowlisted()` calls only; will not fire in real tool invocations until caller plumbing lands. **v5.15.0 bundles tag-plumbing** so allowlist becomes effective in production.

Tag-plumbing sub-scope:
- `yadgar/server/tools/memorize.py` — pass `tags=tags` to `gate_or_reject` call
- `yadgar/server/tools/wiki.py::wiki_add` — pass `tags=tags`
- `yadgar/server/tools/admin_other.py::anchor` (and any other `@_tool(power=True)` write paths) — pass `tags=tags`
- Audit ALL `gate_or_reject()` call sites + plumb where applicable
- Tests: regression gate per call site asserting `tags=` forwarded
- Acceptance: memorize a test-fixture-tagged memory containing fake `ghp_*` token + allowlist entry for `test-fixture` tag → write succeeds (was previously blocked by v5.10.2 gate)

**Status:** drafted 2026-05-30 based on empirical investigation. No burst visible at time of writing. Plan focuses on detection infrastructure and fix verification rather than speculative new root cause.

**Master at draft time:** v5.10.3 shipped + tagged. v5.10.4 (consolidate_now) in flight.

**Sequencing:** v5.15.0. Slots after v5.13.0 (secret-gate). Independent of v5.13.x hotfix space. Can ship after v5.13.0 but does not block on it. The consolidate_now fix in v5.10.4 is relevant context (sleep cycle bypassing gate was itself a CPU burst vector).

---

## Prior Fix History (What Has Already Shipped)

### v4.8 — Consolidation Cooldown (2026-05-14, id:473625)

**Root cause:** `ConsolidationScheduler._daemon_loop` (idle-triggered background thread) fired every 30 minutes. After each cycle, `last_activity` remained stale → idle gate immediately re-triggered. Back-to-back consolidation cycles ran 58-60 seconds each, repeated every ~90 seconds. Fan spin confirmed via `journalctl` timestamps.

**Fix shipped:** `CONSOLIDATION_COOLDOWN_SECONDS` setting (default 1800s). Daemon skip if `time_since_last_cycle < cooldown`. `force_consolidate()` ignores cooldown (explicit request).

**Current state:** Daemon removed entirely in v5.7.0 PR-0 (commit `bac9540`). The daemon loop itself no longer exists. The v4.8 cooldown is now dead code for the daemon case (daemon gone), but `CONSOLIDATION_COOLDOWN_SECONDS` may still protect the force path in some callers.

**Why insufficient by itself:** Daemon removal (v5.7.0) made the v4.8 fix redundant for the daemon case. But the discovery of the consolidate_now sleep-cycle bypass (v5.10.4 plan) shows a NEW path that produced similar behavior — the explicit MCP call was running 13-minute cycles.

### v5.4.x — Circuit Breaker CB-1 Probe Fix (2026-05-22, id:501148)

**Root cause:** `embed_service` in `yadgar-backend` entered a degraded state (32h+ uptime, 158% CPU on `--cpus 2` container). CB-1 in `yadgar/ml_client.py` entered HALF_OPEN every 60 seconds and fired a full CE inference probe against the already-saturated model thread. Probe waited full `BACKEND_HTTP_TIMEOUT_SEC` (5s), failed → back to OPEN → repeat. 36 `/hooks/auto-capture` requests/hour each triggering recall → rerank compounded the load.

**Fix shipped (partial):** CB-1 probe timeout reduction (2-3s) + exponential backoff on HALF_OPEN. Reduces CPU cost of probing the saturated service.

**Why insufficient by itself:** Symptom-suppression. The UNDERLYING cause — why does embed_service saturate after 32h — was deferred to F5 (lazy-load rerankers OR cap batch OR cgroup bump). F5 status unknown (not confirmed shipped as of v5.10.3 investigation).

### v5.10.0 — Orphan Surreal/Pytest Worker Cleanup (2026-05-28, id:518848)

**Root cause identified:** Orphan SurrealDB pytest workers and leftover `pytest` processes consuming CPU. v5.10.0 added `pytest-timeout` + cleanup.

**Current state (2026-05-30 06:45 UTC investigation):**
- `pgrep -af 'surreal'` shows ONE surreal process: PID 3674007, started 2026-05-29T15:19:44, running 15h. This is the production container process, not an orphan.
- `pgrep -af 'pytest'` — no results. No pytest orphans.
- `ps aux --sort=-%cpu | head -20` — top CPU consumers are Brave browser renderers (14.6%, 6.0%, 5.9%, 5.4%), yadgar-backend container at 4.12%, claude processes at 3.0% and 2.8%. NO yadgar-related CPU burst observed.
- `docker stats yadgar yadgar-backend` at time of check: yadgar 1.35% CPU, yadgar-backend 4.12% CPU.
- yadgar-backend uptime: 15.4 hours. Below the 32h saturation threshold observed in incident 501148.

**v5.10.0 fix verdict:** Orphan cleanup appears to be working. No orphan processes detected at investigation time.

---

## Current Empirical State (2026-05-30 ~06:45 UTC)

**No CPU burst is actively occurring.** The investigation found:

| Signal | Value | Verdict |
|--------|-------|---------|
| yadgar container CPU | 1.35% | Normal |
| yadgar-backend CPU | 4.12% | Normal for active embed model load |
| yadgar-backend uptime | 15.4h | Well below 32h saturation threshold |
| Orphan surreal procs | 0 (only production process) | v5.10.0 fix holding |
| Orphan pytest procs | 0 | v5.10.0 fix holding |
| Yadgar service logs (4h) | No consolidation/sleep phase entries | No cycle ran in 4h window |
| yadgar container logs | Only `/hooks/auto-capture` request traffic | Normal hook activity |

**Conclusion: Bursts are intermittent and not reproducing at investigation time.** The system is idle (yadgar started 8h ago from a clean restart at 21:59 CEST; backend started 15h ago). The absence of bursts is consistent with:
1. v5.7.0 daemon removal fixed the primary background cycle trigger.
2. v5.10.0 orphan cleanup fixed the test-process CPU leak.
3. The consolidate_now sleep-cycle bypass (v5.10.4) was the most recent new burst vector, but it only fires on explicit `consolidate_now` MCP calls — user sessions, not idle background load.

**What we cannot confirm from this snapshot:**
- Whether embed_service will saturate again after 32h+ uptime (pattern from 501148 requires sustained runtime to reproduce).
- Whether the CB-1 exponential backoff fix from v5.4.2 actually shipped (F5 status unclear).
- Whether any automatic trigger (cron, hook, internal timer) is calling consolidate_now unexpectedly.

---

## Proposed Work: Detection Infrastructure

Since we cannot reproduce the burst, the correct approach is to build detection capabilities so the NEXT burst is diagnosable in real time.

### D1 — Per-Phase Duration Alerting in consolidate_now / sleep cycle

The existing `phase_start`/`phase_end` log lines in `_consolidation_cycle()` already emit `duration_ms`. No new instrumentation needed. But there is no alert threshold — a 13-minute phase passes silently.

**Work:** Add a threshold check after each phase. If `duration_ms > PHASE_DURATION_WARN_MS` (new config, default 60000 = 1 minute), emit a `CRITICAL` log with the phase name and actual duration. This makes bursts immediately visible in `journalctl`.

### D2 — Timed-Inference Health Check for embed_service

The current `/health` endpoint on yadgar-backend only checks model-loaded state, not whether inference actually completes in reasonable time. A degraded embed_service passes `/health` but times out on every real inference call (as observed in incident 501148).

**Work:** Add a timed-inference probe to the health check: make one small real embedding inference call, measure duration, fail if > 2s. Expose as `/health/inference`. The CB-1 probe (v5.4.2) can then use this endpoint instead of the `ce/nli/pair` endpoints — lighter footprint.

### D3 — embed_service Uptime Metric + 32h Alert

From incident 501148: saturation occurs after 32h+ uptime, likely due to memory growth or PyTorch thread pool state. This is F5 (deferred).

**Work:** Expose embed_service container uptime as a Prometheus metric. Alert (CRITICAL log) if uptime > 28h (4h warning before the empirical 32h threshold). This gives the user a heads-up to restart the backend before saturation hits.

### D4 — Automatic Trigger Audit

Currently there is no centralized record of what triggers consolidation or sleep cycles. Manual audit needed:

```bash
# All places that call consolidate_now / run_sleep_cycle / force_consolidate
grep -rn "consolidate_now\|run_sleep_cycle\|force_consolidate\|_maybe_sleep_cycle" \
  yadgar/ --include="*.py" | grep -v test_ | grep -v "__pycache__"
```

Expected results:
- `admin_other.py`: `consolidate_now` MCP tool (explicit user invocation)
- `nightly_cycle.py`: `force_consolidate()` (nightly cron at 19:00 UTC)
- `orchestrator.py`: `_maybe_sleep_cycle()` definition (no callers in current code)

If any NEW caller appears that is not in this list, it is a regression.

**Work:** Add a grep-based test that asserts the exact set of callers of `run_sleep_cycle` and `force_consolidate`. Fails immediately if a new automatic trigger is added without explicit design review.

### D5 — Verify F5 (embed_service saturation fix) Ships

The incident 501148 anchor noted F5 (lazy-load rerankers OR cap batch OR cgroup bump) as the true root fix for embed_service saturation. Confirm F5 is in a shipped version or open an explicit ticket.

---

## Why Prior Fixes Were Insufficient (If Bursts Recur)

If CPU bursts recur after the current clean state, the most likely remaining vectors are:

1. **consolidate_now sleep-cycle bypass** (now fixed by v5.10.4): Any automatic caller of `consolidate_now` (e.g., a script, a hook, a test harness) would have triggered the full 13-minute cycle. With v5.10.4 merged, default calls are light mode.

2. **embed_service 32h saturation** (F5 unconfirmed): Backend runs for 32h+ → model thread contention → every embedding call takes 5+ seconds instead of <100ms → all memory operations involving embeddings (recall, memify, reembed_stale) dramatically slow → CPU stays pegged. Fix: restart backend before 32h OR implement F5.

3. **dream_replay pair explosion**: `DREAM_REPLAY_PAIRS` default controls how many memory pairs are examined. If this setting is misconfigured to a large value, dream_replay alone could run for minutes. Current default unknown — check config. With 500+ memories, O(DREAM_REPLAY_PAIRS) can be large.

4. **generate_cluster_summaries N+1**: `get_all_memories_with_embeddings()` called PER CLUSTER. With 20 clusters and 500+ memories, this is 20 full table fetches. DB-bound, not CPU-bound, but can cascade into slow embedding calls for centroid computation.

---

## Tests (Red-First TDD)

```python
# D1: Phase duration threshold test
def test_phase_duration_warn_emits_critical_log():
    # GIVEN: PHASE_DURATION_WARN_MS = 100
    # AND: a phase that takes 200ms (mock time.monotonic)
    # WHEN: _consolidation_cycle() completes
    # THEN: CRITICAL log emitted with phase name and duration

def test_phase_duration_under_threshold_no_warn():
    # GIVEN: PHASE_DURATION_WARN_MS = 100000
    # WHEN: _consolidation_cycle() completes normally
    # THEN: no CRITICAL log emitted for phase duration

# D2: Timed inference health check
def test_health_inference_endpoint_returns_ok_fast(mock_fast_embeddings):
    # GIVEN: /health/inference endpoint
    # AND: encoding < 2s
    # THEN: returns {"status": "ok", "inference_ms": < 2000}

def test_health_inference_endpoint_returns_degraded(mock_slow_embeddings):
    # GIVEN: encoding > 2s
    # THEN: returns {"status": "degraded", "inference_ms": > 2000}

# D4: Automatic trigger audit
def test_no_unexpected_sleep_cycle_callers():
    # Static analysis: grep source for run_sleep_cycle callers
    # THEN: only admin_other.py and orchestrator.py contain references
    # (nightly_cycle.py does NOT call run_sleep_cycle — confirmed)
```

---

## Acceptance Criteria

1. D1 implemented: phase duration warn threshold emits CRITICAL when exceeded.
2. D2 implemented: `/health/inference` endpoint on yadgar-backend returns timed result.
3. D3 implemented: embed_service uptime metric exposed and alert log fires at 28h.
4. D4 confirmed: static audit test passes showing no unexpected callers.
5. F5 status documented: either confirmed shipped or explicit issue opened.
6. All new tests pass. Existing tests unbroken.

---

## Open Questions

1. **Is F5 (embed_service saturation) shipped?** Check CHANGELOG for v5.4.2 and v5.5.x for any embed_service lazy-load or cgroup changes.

2. **What is `DREAM_REPLAY_PAIRS` set to in production?** If this is high (>500), dream_replay could be a significant CPU contributor in the sleep cycle. Check `~/.yadgar/config.yaml` or config defaults.

3. **Does the nightly cron script need to call `_maybe_sleep_cycle()` after v5.10.4?** Post-v5.10.4, sleep cycle no longer runs via `consolidate_now` (unless mode="full" is passed). The cron calls `force_consolidate()` only. If sleep cycle is supposed to run nightly, the cron needs explicit wiring. See open question 2 in v5.10.4 plan.

4. **Should there be an explicit "sleep cycle last ran" health metric?** If sleep cycle doesn't run for more than 48h, that's anomalous. A metric would help.

---

## Dependencies

- v5.10.4 (consolidate_now fix): remove the biggest known burst vector before adding detection.
- Does NOT require DB schema changes.
- D2 requires a backend endpoint addition (yadgar-backend release needed).
- D3 requires a Prometheus metric addition (yadgar-backend or yadgar core).

## Risk and Rollback

Detection changes (D1, D4) are purely additive — they only add log output and tests. Zero risk. D2 and D3 require backend changes; those carry the standard backend release risk (coordinated deploy).

## Files to Modify

| File | Change |
|------|--------|
| `yadgar/consolidation/orchestrator.py` | D1: phase duration warn threshold after each phase |
| `yadgar/config.py` | D1: add `PHASE_DURATION_WARN_MS` setting (default 60000) |
| `yadgar/tests/test_cpu_burst_detection.py` | New test file (D1 + D4 tests) |
| `yadgar-backend` (separate repo) | D2: `/health/inference` endpoint; D3: uptime metric |
| `yadgar/tests/test_trigger_audit.py` | D4: static caller audit test |
