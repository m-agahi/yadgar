# C3 — Task 278: Maintenance envelope unsurvivable — says only "retry shortly" while `/health` says ok and systemd says no vacuum. Needs phase/elapsed/typical + do-not-investigate + resume recipe.

## Status / target

- Status: DRAFT — not started.
- Target train: `feat/c3-bug-bag` (the c1–c10 train of which this is car C3).
- Car: C3.
- Companion cars in this train: `docs/plans/c3-task-{233,62}.md` for the
  TimeoutStartSec + embed_url halves.
- Note: this task is largely ALREADY DONE by the existing module
  `yadgar/_shared/runtime/maintenance.py`. The plan below documents the
  expected SHAPE of the work, not a from-scratch design. If the live
  module already carries every key below, this plan is satisfied — the
  work is then to verify and pin.

## Goal

Make the maintenance envelope an instance (or human) can act on WITHOUT
spinning a research project on whether the vacuum is really running. The
envelope must:

1. Name the operation (vacuum / nightly / backup) and the current phase
   (export, import, verify, …).
2. Report elapsed seconds and the typical duration for the named
   operation, so a reader can compare them.
3. Carry an explicit "DO NOT investigate" list of the four signals that
   lie during a live maintenance window (`/health`, `systemctl status
   yadgar-vacuum`, `list-timers`, on-disk artifacts).
4. Carry an actionable resume recipe (sleep + retry + re-issue writes,
   in order) and a `looks_stuck` cutoff that triggers a hard "STOP and
   report" instead of further retries.
5. Distinguish between "gate engaged" and "gate lifted but backend still
   warming up" — the second window is the one that reads as a new bug.

The legacy envelope (pre-Car-1) was a two-key literal
(`{"error": "maintenance", "message": "retry shortly"}`) — that is the
defect this task names.

## Pre-conditions

- The maintenance envelope is built by
  `yadgar/_shared/runtime/maintenance.py:build_maintenance_envelope`
  (lines 164-193).
- The keys that MUST exist in the envelope dict:
  - `error: "maintenance"` — discriminator.
  - `operation: str` — "vacuum" | "nightly" | "maintenance" (default).
  - `phase: str | None` — the current vacuum phase, set by
    `_st._maintenance_phase`.
  - `started_at: str | None` — ISO-8601 UTC; computed from elapsed.
  - `elapsed_seconds: int | None` — set by `_elapsed_seconds()` at
    lines 150-161.
  - `typical_duration_seconds: int` — per-operation, from
    `_TYPICAL_DURATION_BY_OPERATION` (lines 55-58).
  - `expected_done_by: str | None` — ISO-8601 UTC; started + typical.
  - `looks_stuck: bool` — elapsed > 3 × typical.
  - `retry_after_seconds: int` — `RETRY_AFTER_SECONDS = 60` (line 70).
  - `writes_were_rejected_not_queued: bool` — discriminates from
    "queue drain" UX.
  - `message: str` — the human-readable prose with phase/elapsed/typical
    and the do-not-investigate list.
  - `resume: str` — the recovery recipe; stuck-aware.
- `_MISLEADING_SIGNALS` (lines 79-88) enumerates the four signals that
  lie during a live window. They MUST appear in `message` verbatim.
- `_resume()` (lines 231-253) emits the recipe with the stuck clause.
- `MaintenanceGateError` (lines 91-121) raises the prose+resume+JSON
  tail. The `str(self)` shape must carry the prose first, then resume,
  then the machine-readable fields (dropping the two prose keys to
  avoid duplication, per the comment at line 119).
- Tests pinning this shape:
  - `yadgar/tests/_shared/test_maintenance_mode.py:55` — pins the
    envelope's `key!r` drift detector.
  - `yadgar/tests/server/test_maintenance_envelope.py:1` — the
    "Car 1 (2026-08-20 train)" suite; tests the user-facing shape.

## Step-by-step

The shape is already implemented at `maintenance.py`. The work below
verifies the shape, hardens the messages, and adds the missing
pinning tests.

1. **Verify every required key is present in `build_maintenance_envelope`
   (lines 164-193).**

   The current implementation has all twelve keys. Confirm by reading
   the function and cross-checking against the table in Pre-conditions.
   For any missing key, add it before this plan is satisfied.

   Specifically check:
   - `phase` reflects `_st._maintenance_phase` (line 168) — non-None
     when the caller stamped one.
   - `started_at` and `expected_done_by` are ISO-8601 UTC with
     microsecond=0 (lines 173-176).
   - `looks_stuck` is `elapsed > typical * STUCK_MULTIPLIER` (line 178).
   - `writes_were_rejected_not_queued: True` is unconditional (line 190).
   - `_resume(looks_stuck)` builds the stuck-aware recipe.

2. **Pin the do-not-investigate list to four specific signals.**

   `_MISLEADING_SIGNALS` at lines 79-88 names them:
   - `GET /health` returns `status: ok` throughout (the gate's own
     payload is the only part of `/health` that knows about it).
   - `yadgar-vacuum.service` reads `inactive (dead)` for manually- or
     trigger-initiated vacuums; `list-timers` points at next run days
     away.
   - `state/yadgar/triggers/` is empty — the trigger file is consumed
     at start.
   - On-disk artifacts (`surreal_db.pre-vacuum-*`, `vacuum_export_*`)
     appear only around the midpoint.

   These four MUST appear in `message`. Confirm by reading
   `_message()` at lines 209-228 — the `_MISLEADING_SIGNALS` constant
   is interpolated on line 222.

3. **Pin the resume recipe in `_resume()` (lines 231-253).**

   The four-step recipe:
   1. Sleep `retry_after_seconds`; retry the failed call. Repeat until
      it succeeds.
   2. Re-issue every write attempted during the window, in order.
   3. Verify each one by RE-READING the row, not by trusting the tool's
      success field.
   4. If `looks_stuck`, stop retrying and report to the user.

   The stuck clause (lines 232-237) prepends the "STOP and report"
   instruction when `looks_stuck` is True. The clause mentions
   `looks_stuck: true` literally so an instance grepping the prose
   finds it.

4. **Pin the second-window explanation.**

   Lines 225-227 of `_message()` explain: "Sleep `retry_after_seconds`
   ({RETRY_AFTER_SECONDS}s) and retry the same call — that interval also
   clears the ~30s the backend needs to come up AFTER the gate lifts,
   which is a second window where calls fail for a different reason."

   This must remain in the prose; it is the single biggest contributor
   to the "retry-and-it-works" UX. An instance that retries
   immediately after a successful `recall` re-hits the warm-up window
   and reads it as a separate bug.

5. **Add a test pinning the four-signal list.**

   `yadgar/tests/_shared/test_maintenance_mode.py` (line 55) pins a
   drift detector but does not pin the four-signal list itself. Add
   a test that builds a non-stuck envelope and asserts the message
   contains each of:
   - "`GET /health`"
   - "`yadgar-vacuum.service`"
   - "`state/yadgar/triggers/`"
   - "On-disk artifacts"
   so any future edit that drops one of them fails the suite. Also
   pin the recipe's four steps in `resume`:
   - "Sleep `retry_after_seconds`"
   - "Re-issue every write"
   - "Verify each one by RE-READING"
   - "If `elapsed_seconds` exceeds 3x"

6. **Add a test pinning the stuck-clause text.**

   In `yadgar/tests/server/test_maintenance_envelope.py`, add a case
   where `_elapsed_seconds()` is mocked above `STUCK_MULTIPLIER *
   typical` and assert the envelope's `resume` field contains "STOP
   retrying and report" — the literal phrase that flips a healthy
   "wait a minute" into a hard "report the bug". The existing Car-1
   suite tests a 423s measured case (see maintenance.py:13-15) but does
   not pin the stuck path.

7. **Add a test pinning the second-window explanation.**

   The `RETRY_AFTER_SECONDS = 60` constant (line 70) and its mention
   in `_message()` (line 225-226) together carry the second-window
   semantics. Pin both: a test that asserts the message contains
   "`retry_after_seconds` (60s)" verbatim, and that 60s is the warm-up
   floor. The 60s number is documented as "Deliberately NOT a curve"
   (line 69) — pin that the value is exactly 60 so a future
   micro-optimisation cannot tighten it below the warm-up floor.

## Verification

- **Shape test**: `pytest yadgar/tests/server/test_maintenance_envelope.py`
  passes — every key in the table above is present in the returned
  dict.
- **Do-not-investigate test**: the new test from step 5 passes — every
  one of the four `_MISLEADING_SIGNALS` strings appears in the message.
- **Resume recipe test**: the new test from step 5 passes — all four
  recipe steps appear in the resume prose.
- **Stuck-clause test**: the new test from step 6 passes — a
  `looks_stuck: true` envelope's `resume` field contains "STOP
  retrying and report".
- **Second-window test**: the new test from step 7 passes — the
  message contains "`retry_after_seconds` (60s)" verbatim, and
  `RETRY_AFTER_SECONDS == 60`.
- **Live read-through**: read `_message()` (lines 209-228) end-to-end.
  A reader with no prior context should be able to act on it without
  further investigation. (Manual smoke test; no automated check.)
- **No regressions in adjacent cars**: the maintenance envelope is
  read by `/health` (`apply_maintenance_health`, lines 256-278) and
  the MCP write-gate (`MaintenanceGateError`, lines 91-121). Both
  are pinned by existing tests at
  `yadgar/tests/server/test_health.py` and
  `yadgar/tests/server/test_write_gate.py` respectively.

## Risks / rollback

- **No new failure mode**: this car is documentation-pinning plus
  message-text verification. The envelope shape is already
  implemented and shipped.
- **Rollback**: remove the new tests. The behaviour is unchanged; the
  safety net is what regresses.
- **If a future maintainer DROPS a key**: the new tests fail loudly,
  which is exactly the point. No silent regression path.
- **If `looks_stuck` fires too aggressively**: STUCK_MULTIPLIER = 3
  (line 64) is empirically the right value per the comment at lines
  60-63 ("the instance should stop retrying and tell the user before
  the TTL fires, not after"). Resist any drive to lower it without
  measured evidence the threshold is too high.

## Approx LOC + risk class

- LOC: +~50 (three new tests, ~15 lines each, plus the existing
  envelope).
- Risk class: **low** — purely additive; the production code is
  already in place. Tests are the deliverable.
- Time cost: <30 min for the tests + a manual read-through of
  `_message()` and `_resume()` against this plan.

## Source evidence

- `yadgar/_shared/runtime/maintenance.py:164-193` —
  `build_maintenance_envelope`. Every key listed in Pre-conditions
  lives here.
- `yadgar/_shared/runtime/maintenance.py:209-228` — `_message()`. The
  prose with phase, elapsed, typical, do-not-investigate, and
  second-window explanation.
- `yadgar/_shared/runtime/maintenance.py:231-253` — `_resume()`. The
  stuck-aware four-step recipe.
- `yadgar/_shared/runtime/maintenance.py:79-88` — `_MISLEADING_SIGNALS`.
  The four signals that lie during a live window; verbatim source for
  the test in step 5.
- `yadgar/_shared/runtime/maintenance.py:13-21` — module docstring with
  the 2026-08-20 21:00 measurement that motivated the work. Reads as
  the WHY of every key in the envelope.
- `yadgar/_shared/runtime/maintenance.py:33-38` — the `TYPICAL_DURATION_SECONDS = 600`
  comment with provenance (measured 423s, rounded UP to 10 minutes).
- `yadgar/_shared/runtime/maintenance.py:42-54` — the per-operation
  table; nightly's 3600s is DERIVED, not measured. Pin the comment so
  a future reader knows to replace it with a measurement.
- `yadgar/_shared/runtime/maintenance.py:60-63` — `STUCK_MULTIPLIER = 3`
  rationale; pin in the test suite.
- `yadgar/_shared/runtime/maintenance.py:67-70` — `RETRY_AFTER_SECONDS = 60`
  rationale; pin in the test suite.
- `yadgar/_shared/runtime/maintenance.py:91-121` — `MaintenanceGateError`.
  The prose-first / resume-second / JSON-tail shape. The MCP SDK's
  ToolError delivery means `str(self)` carries everything; the
  rationale at lines 95-112 is load-bearing for the decision to raise
  rather than return.
- `yadgar/_shared/runtime/maintenance.py:256-278` —
  `apply_maintenance_health`. The /health surface that surfaces the
  window without scoring it (lines 260-263 say "ADDITIVE ONLY").
- `yadgar/tests/_shared/test_maintenance_mode.py:55` — existing drift
  detector. Extend with the do-not-investigate pin (step 5).
- `yadgar/tests/server/test_maintenance_envelope.py:1` — existing Car-1
  suite. Extend with stuck-clause (step 6) and second-window (step 7)
  pins.
