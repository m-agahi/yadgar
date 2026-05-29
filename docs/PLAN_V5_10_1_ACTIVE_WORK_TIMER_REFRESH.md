# PLAN — v5.10.1: `_active_work` timer-based refresh + soft warning tier

**Status:** drafted 2026-05-29 after investigation. Renumbered to v5.10.1 (patch on v5.10 train) per user direction 2026-05-29 evening.

**Master at draft time:** core v5.10.0 + backend v5.3.1 deployed.

**Sequencing rationale:** small, surgical, additive — fits v5.10.x patch semantics rather than waiting for a v5.12+ minor.

---

## Why

Investigation 2026-05-29 confirmed observed pattern: `_active_work_age_hours` consistently approaches 22-23h before refresh fires, almost crossing the `ACTIVE_WORK_STALE_HOURS=24` threshold each session.

**This is NOT a bug.** Design intent of v5.7.12: refresh actions fire when stop hook calls `project_brief(mode="signals")`. Stop hook fires every `INTERVAL=25` human messages (see `yadgar/hooks/stop-memory-checkpoint.py:22`). Sessions with <25 messages/24h do NOT trigger the hook at all — age accumulates until the message count finally crosses the boundary, which often lands at hour 25+ rather than hour 24.

Result: refresh actions are reliable but EVENTUALLY-fire — never strictly time-bounded.

**Operational impact:** users observe age repeatedly close-to-but-not-over threshold. Looks like a bug; isn't. UX-confusing. Also: idle sessions (long-running but low-message) can hold a stale `_active_work` for days.

---

## What ships

Three coordinated changes. All optional, kill-switchable.

### 1. Soft warning tier in `recommended_actions`

Add a soft `consider_refresh_active_work` action emitted when age > `ACTIVE_WORK_WARN_HOURS` (default 12, NEW env knob) but ≤ `ACTIVE_WORK_STALE_HOURS` (default 24, EXISTING).

```json
{
  "action": "consider_refresh_active_work",
  "reason": "age_hours=14.2 > warn=12; not yet stale (24h)",
  "suggested_call": "update_active_work(directory='...', content='...')"
}
```

Caller (Claude in session) sees the soft signal earlier and can proactively refresh during natural pauses. Hard `refresh_active_work` still fires at 24h+ unchanged.

Same soft tier for checkpoint: `consider_refresh_checkpoint` at `CHECKPOINT_WARN_HOURS` (default 12, NEW env knob).

### 2. Optional yadgar-internal time-based trigger (systemd-user unit)

NEW systemd-user timer + service shipped as `scripts/systemd-user/yadgar-active-work-watchdog.{timer,service}`:

```ini
# yadgar-active-work-watchdog.timer
[Unit]
Description=Yadgar active_work age watchdog
[Timer]
OnBootSec=1h
OnUnitActiveSec=6h
Persistent=true
[Install]
WantedBy=timers.target
```

```ini
# yadgar-active-work-watchdog.service
[Unit]
Description=Yadgar active_work age watchdog — fires project_brief signals
[Service]
Type=oneshot
ExecStart=/usr/bin/env bash -c \
  'find ~/.yadgar/active-work-tracked -type d -maxdepth 1 -mindepth 1 | while read d; do \
     dir=$(cat "$d/directory.txt" 2>/dev/null); \
     [ -n "$dir" ] && curl -sf -X POST http://127.0.0.1:8765/mcp \
       -H "Content-Type: application/json" \
       -d "{\"method\":\"tools/call\",\"params\":{\"name\":\"project_brief\",\"arguments\":{\"directory\":\"$dir\",\"mode\":\"signals\"}}}" \
       > /dev/null; \
   done'
```

**Behavior:** every 6h, scans the tracked-directories registry (populated by `update_active_work()` writing a marker into `~/.yadgar/active-work-tracked/<hash>/directory.txt`) and POSTs a signals-mode `project_brief` call for each.

The signals call itself is read-only — it surfaces `recommended_actions` to whatever caller is listening. **For Claude sessions, this doesn't help directly** — Claude can only see the response if it's in an active session.

**However:** if `recommended_actions` includes `refresh_active_work` AND the watchdog is configured with `YADGAR_AUTO_REFRESH_ACTIVE_WORK=true` (NEW env knob, default false), it auto-writes a stub `_active_work` row with content `"<watchdog-refreshed at TIMESTAMP — no active session>"`. This dilutes the user-curated semantic but bounds the staleness deterministically.

User opt-in via env knob. NOT enabled by default. Documented in MIGRATION_NOTES.

### 3. Track `_active_work` directories in a sidecar registry

`update_active_work()` writes `~/.yadgar/active-work-tracked/<sha256(directory)[:12]>/directory.txt` containing the absolute directory path. The watchdog reads from this registry to know which directories to poll.

Registry is purely additive — never auto-pruned (paths can be revisited after long gaps). Manual prune via:
`find ~/.yadgar/active-work-tracked -type d -mtime +30 -exec rm -rf {} +`

Documented in MIGRATION_NOTES.

### 4. 3 new env knobs (I25-registered)

| Knob | Default | Type | Purpose |
|---|---|---|---|
| `ACTIVE_WORK_WARN_HOURS` | 12.0 | float | Soft warning threshold |
| `CHECKPOINT_WARN_HOURS` | 12.0 | float | Soft warning threshold |
| `YADGAR_AUTO_REFRESH_ACTIVE_WORK` | false | bool | Watchdog opt-in for stub auto-refresh |

(`ACTIVE_WORK_STALE_HOURS` + `CHECKPOINT_STALE_HOURS` from v5.7.12 unchanged.)

---

## What does NOT ship

| Item | Why deferred |
|---|---|
| Lowering `ACTIVE_WORK_STALE_HOURS` default from 24h to 12h | Symptom-masking, not root-cause fix. User can override via env if desired. |
| Removing `INTERVAL=25` message-frequency hook | Still useful — captures session-end. Removal would hide all `recommended_actions` to interactive sessions. |
| Real-time WebSocket notification of `recommended_actions` to active Claude sessions | Out of yadgar scope. Claude session has its own session model. |
| Audit who actually consumes `recommended_actions` (the stop hook is just one consumer) | Useful but scope drift. |

---

## Implementation order

1. **TDD scaffolding** — `yadgar/tests/test_active_work_warn_tier.py`:
   - `recommended_actions` includes `consider_refresh_active_work` when `12h < age ≤ 24h`.
   - Hard `refresh_active_work` still fires when `age > 24h`.
   - Both NOT emitted when `age ≤ 12h`.
   - Same logic for checkpoint.
   - `_active_work` write registers directory in `~/.yadgar/active-work-tracked/`.

2. **Soft warning tier in project.py** — extend `recommended_actions` builder. ~30 LOC.

3. **Active-work directory registry** — `update_active_work()` writes marker file. ~10 LOC.

4. **3 env knobs three-way registered (I25)** — yaml + Settings + registry. New section `active_work_watchdog` in yaml.

5. **systemd-user unit files** — `scripts/systemd-user/yadgar-active-work-watchdog.{timer,service}`. Plain files, user-managed install.

6. **MIGRATION_NOTES.md** — v5.10.1 section with:
   - New action type semantic.
   - Watchdog installation steps + opt-in env knob.
   - Registry pruning command.

7. **CHANGELOG.md** — terse entry.

---

## Acceptance criteria

- `pytest yadgar/tests/test_active_work_warn_tier.py` green.
- `project_brief(mode="signals")` on a directory with `_active_work` age 15h emits `consider_refresh_active_work` (not `refresh_active_work`).
- `project_brief(mode="signals")` on a directory with age 25h still emits `refresh_active_work`.
- `update_active_work("/home/max/git/yadgar", "...")` creates marker at `~/.yadgar/active-work-tracked/<hash>/directory.txt` with the path inside.
- I13 + I23 + I24 + I25 lints green.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Watchdog auto-refresh dilutes user-curated `_active_work` semantic | Opt-in via env knob, default OFF. Documented clearly. |
| Soft warning adds noise to recommended_actions for short sessions | Threshold 12h is intentionally high — short sessions stay below. |
| Registry `~/.yadgar/active-work-tracked/` accumulates stale paths over years | Manual prune command documented. v5.12 candidate: auto-prune. |
| Signals payload size bloat from new `consider_*` actions | `recommended_actions` already has `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN` cap. Soft tier counts against same cap. Verify in test. |
| `_active_work_age_hours` race between watchdog read + concurrent session write | Watchdog is read-only via signals mode. Sessions write via `update_active_work`. No conflict — last-writer-wins is fine. |

---

## Estimate

~150 LOC implementation + ~100 LOC tests + 2 systemd unit files + docs. Small train. Single agent dispatch.

---

## Sequencing

Ships as v5.10.1 patch on v5.10 train (test harness hardening) — additive, no schema change. Decoupled from v5.11 cross-project + Jira train.

Possible parallel with backend v5.4.0 — they don't overlap files.

---

## Open / parked questions

- **Should `_active_work_age_hours` use `last_modified` instead of `created_at`?** Currently `created_at` is set on insert (matches DELETE+INSERT pattern). For semantic of "user-curated freshness", a separate `user_refreshed_at` column might better separate watchdog stub refreshes from real user refreshes. **Lean: add `user_refreshed_at` column in v5.12.** v5.10.1 keeps `created_at` for simplicity.
- **Registry-less alternative:** could yadgar use SurrealDB to track active-work-having directories instead of a filesystem registry? **Lean yes** — query `SELECT DISTINCT directory_context FROM memory WHERE tags CONTAINS '_active_work'`. No filesystem state. Replace step 3 above. Defer to implementation review.
- **Watchdog cadence 6h vs 12h vs 1h:** 6h gives 4 polls/day → average refresh latency ~3h. 12h → ~6h avg. 1h → wasted CPU. **Lean: 6h default**, env knob to override.
- **Auto-refresh stub content:** could include last-N user-prompted tool calls to make it more useful even as a stub. **Lean: ship minimal stub first**, enrich in v5.12.

---

## v5.12 follow-up (deferred)

- `user_refreshed_at` separate column to distinguish watchdog stubs from real user refreshes.
- Enriched watchdog stubs (include recent action_log entries).
- Auto-prune registry entries with no `_active_work` row in the past N days.
- Watchdog cadence env knob.
