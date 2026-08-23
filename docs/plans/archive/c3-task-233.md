# C3 — Task 233: `yadgar-backend.service` cannot survive a version bump — `TimeoutStartSec=3min` vs. a cold 3.68 GB pull, `Restart=on-failure/5s`. Kills the pull mid-copy and re-pulls forever — never converges.

## Status / target

- Status: DRAFT — not started.
- Target train: `feat/c3-bug-bag` (the c1–c10 train of which this is car C3).
- Car: C3.
- Companion cars in this train: C1/C2 (sibling task plans in `docs/plans/c2-task-*.md`), and `docs/plans/c3-task-{62,278}.md` for the embed_url + maintenance-envelope halves.

## Goal

Make `yadgar-backend.service` survive the cold-pull of a NEW backend image —
the FIRST START after a version bump — without systemd killing the pull
mid-copy and looping forever between `Start=systemd Start` →
`TimeoutStartSec=180` expiry → `Restart=on-failure` → `RestartSec=5` → pull
from byte zero.

The class of failure is observed on the live corpus: a cold 3.68 GB pull
takes longer than `TimeoutStartSec`, systemd kills the unit, `Restart=on-failure`
with `RestartSec=5` starts it again, the runtime re-issues the pull from
scratch (no partial-image cache because the layer was never committed), and
the loop NEVER converges. The visible symptom is the unit forever in
`activating (start)` with no progress.

## Pre-conditions

- The unit is rendered by `yadgar/core/daemon/units.py:build_backend_unit`
  (call site: `yadgar/core/daemon/systemd.py:install_systemd_service:127`).
- `TimeoutStartSec` value is computed by `units.readiness_for` at
  `yadgar/core/daemon/units.py:246-268` and is the `budget` argument.
- Backend `budget` is `180` at `yadgar/core/daemon/units.py:329` — hard-coded
  for the readiness gate (75 × 2s = 150s inside 180s).
- `Restart=on-failure` and `RestartSec=5` are emitted at
  `yadgar/core/daemon/units.py:345-346`. Unconditional.
- `Type=exec` (docker) and `Type=notify` (podman) are emitted by
  `readiness_for` at `yadgar/core/daemon/units.py:261-268`.
- The snapshot tests pin the current shape:
  `yadgar/tests/scripts/snapshots/systemd/{docker,podman}/yadgar-backend.service`.
- `flake.nix` renders an INDEPENDENT copy (see
  `yadgar/core/daemon/systemd.py:30-32` and `yadgar/core/daemon/units.py:75-79`)
  — both surfaces must stay byte-comparable for the cross-generator suites.
  This car is an EXECUTION-only change, not a directive shape change, so the
  existing cross-generator tests are unaffected.

## Step-by-step

1. **Define a separate, larger startup budget for the cold-pull path.**

   The existing 180s `TimeoutStartSec` covers the WARM path (image cached,
   model loaded). The cold path has TWO bounded phases:
   a. Image pull — observed 3.68 GB at typical ~30 MB/s network = ~120s.
   b. Backend model load — measured 20–40s (units.py:303 comment).

   The pull phase is bounded by network/disk, not by the readiness gate,
   and conflating them is the bug. Add a `cold_budget` field to `Readiness`
   (units.py:223-242) defaulting to the same value as `budget` for the
   existing warm path, then wire backend-specific override.

   ```python
   # yadgar/core/daemon/units.py
   @dataclass(frozen=True)
   class Readiness:
       type_directives: tuple[Directive, ...]
       budget: int                      # warm path: readiness gate fits here
       cold_budget: int = budget        # cold path: image pull + warm-up
       sdnotify: str = ""
       gate: str | None = None
   ```

   Plumb `cold_budget` through `readiness_for(spec.runtime, *, url, retries,
   budget, cold_budget=budget)` and emit it as the `TimeoutStartSec`
   directive. The warm gate (the `ExecStartPost=curl` line) keeps polling
   under the SHORTER `budget` because it has its own `--retry` arithmetic
   (75 × 2s = 150s < 180s for the backend; units.py:329) and the GATE is the
   readiness contract, not the pull.

2. **Emit `TimeoutStartSec=cold_budget` and leave the readiness gate
   `ExecStartPost=` inside that ceiling.**

   `units.py:347-349` is the directive block. Replace
   ```python
   *ready.type_directives,
   Directive("TimeoutStartSec", str(ready.budget)),
   ```
   with
   ```python
   *ready.type_directives,
   Directive("TimeoutStartSec", str(ready.cold_budget)),
   ```
   The gate (units.py:341-342) still uses `ready.gate` and is bounded by
   `retries × retry_delay` (built into `_gate()` at units.py:206-220). For
   the backend, that is 75 × 2s = 150s — well inside a 180s cold budget.

3. **Set the cold budget to a size that absorbs a measured 3.68 GB pull
   plus the 40s backend model-load plus a safety margin.**

   Network pull @ 30 MB/s = 122s. Model load ≤ 40s. Backend init / readiness
   probe ≤ 30s. Total floor = 192s. With a 50% safety margin, **cold_budget
   = 300 (5 minutes)** for the backend. The core unit keeps its existing
   120s budget because the core image is small (~400 MB) and its warm-up
   is faster (no model load).

   ```python
   # yadgar/core/daemon/units.py:323-330
   ready = readiness_for(
       spec.runtime,
       url=f"http://127.0.0.1:{spec.backend_embed_port}/health",
       retries=75,
       budget=180,
       cold_budget=300,
   )
   ```

   Pinned in the cross-generator test
   `yadgar/tests/scripts/test_systemd_generator_convergence.py` (referenced
   by systemd.py:32) — update the snapshot tests at
   `yadgar/tests/scripts/snapshots/systemd/{docker,podman}/yadgar-backend.service`
   to show `TimeoutStartSec=300` (line 53 in the docker snapshot).

4. **Cap the restart-loop so a permanent failure cannot pin the unit in
   forever-starting state.**

   `Restart=on-failure` + `RestartSec=5` (units.py:345-346) is the loop
   that fails to converge when the image pull is killed by TimeoutStartSec.
   With the bigger cold budget in (3), the pull finishes in the FIRST
   attempt in the common case. But a TRULY stalled pull (registry down,
   disk full) would still loop forever.

   Add `StartLimitBurst=3` and `StartLimitIntervalSec=600` to the `[Unit]`
   section of the backend unit (units.py:350-364). systemd then stops
   restarting after 3 attempts within 10 minutes and surfaces a `failed`
   state, which `yadgar-setup --doctor` and `systemctl status` both flag.
   Without these, the user has a perpetually `activating (start)` unit with
   no signal that it is stuck.

   ```python
   # units.py:323 (inside build_backend_unit, before Section("Unit", ...))
   # Stops the restart-loop from being silent: 3 failed starts in 10 minutes
   # is a stuck pull (registry / network / disk), not a transient flake.
   unit_limits = (Directive("StartLimitBurst", "3"),
                   Directive("StartLimitIntervalSec", "600"))
   ```

   Emitted only for the backend; the core keeps its current shape because
   its warm-up is fast (no pull phase) and silent-restart-loops there are
   not the same class of failure.

5. **Update the snapshot fixtures so the test suite reflects the new shape.**

   The pinned snapshots are at
   `yadgar/tests/scripts/snapshots/systemd/docker/yadgar-backend.service`
   (read above; line 53 has `Type=simple` at the end — note this snapshot
   predates the move to `Type=exec`/`Type=notify`, the test that
   regenerates them is `yadgar/tests/scripts/test_systemd_generator_convergence.py`)
   and the podman twin. Both must show:
   - `TimeoutStartSec=300` instead of `TimeoutStartSec=180`
   - `StartLimitBurst=3` and `StartLimitIntervalSec=600` in `[Unit]`

   The cross-generator test `test_systemd_generator_convergence.py` and
   the maintenance-parity test `test_v5_169_maintenance_unit_parity.py`
   (`yadgar/core/daemon/units.py:73`) need the same update because both
   pin the literal text.

6. **Verify the flake.nix copy stays consistent.**

   The repo carries two renderers (systemd.py:30-32). The nix one builds
   its own systemd user units at nix eval time (units.py:75-79) and is
   kept honest by the *_cross_generator.py suites. This car is an
   EXECUTION semantics change (`TimeoutStartSec` numeric value), not a
   shape change — the nix twin already uses a separate value or would
   need the same bump. Confirm by reading `flake.nix` and update if the
   nix unit hard-codes `TimeoutStartSec=180`. Pinned by
   `yadgar/tests/scripts/test_runtime_readiness_cross_generator.py` and
   `yadgar/tests/scripts/test_systemd_generator_convergence.py` (both
   named at systemd.py:31-32).

## Verification

- **Fixture diff**: after the change, the snapshot at
  `yadgar/tests/scripts/snapshots/systemd/docker/yadgar-backend.service`
  shows `TimeoutStartSec=300` and `StartLimitBurst=3` /
  `StartLimitIntervalSec=600`. The podman twin shows the same shape.
- **Cross-generator parity**: `pytest yadgar/tests/scripts/test_systemd_generator_convergence.py`
  passes — nix and python renderers agree on the numeric budget.
- **Snapshot regeneration**:
  `pytest yadgar/tests/scripts/test_install_systemd_service_characterization.py --snapshot-update`
  (the test named at systemd.py:14) regenerates committed fixtures with
  the new values; the diff is reviewed manually before commit.
- **Live convergence**: a fresh backend pull on a clean install completes
  in the first activation. The `systemctl --user status yadgar-backend`
  output shows `active (running)` within the cold budget, not
  `activating (start-pre)` looping.
- **Stuck-pull behavior**: with the registry unreachable, the unit stops
  restarting after 3 attempts in 10 minutes. `systemctl --user status`
  reports `failed` with `ActivationNoResets` count 3 — visible signal.
- **Warm-path regression**: with the image cached, the backend reaches
  `/health` 200 in under 150s (readiness gate limit), unchanged from
  before this car.
- **Core unaffected**: `yadgar.service` keeps `TimeoutStartSec=120` and no
  `StartLimitBurst` (this car touches the backend unit only).

## Risks / rollback

- **Bigger timeout = longer time-to-detect for a TRULY hung backend.** With
  `TimeoutStartSec=180`, a misconfigured backend (e.g. wrong port) was
  killed in 3 minutes; with `TimeoutStartSec=300`, it takes 5 minutes.
  Mitigation: the `StartLimitBurst=3` ceiling stops the loop after 15
  minutes total even when every attempt times out. Net effect: slower
  detection of a hung backend (5 min vs 3 min per attempt) but a HARD
  failure after ~15 min instead of forever.
- **Snapshot test churn**: three fixture files update. Mechanical, no
  behavioural risk.
- **flake.nix parity**: if the nix twin has its own `TimeoutStartSec`,
  that value also needs the same bump. Read `flake.nix` before merging;
  if it does not have the nix twin, add the bump there too.
- **Rollback**: revert the four numeric / directive changes (timeout
  bump + three StartLimit additions). Snapshots regenerate to the old
  shape; tests pass; behaviour returns to the forever-loop state. No
  data risk in either direction.
- **The Readiness dataclass gains a field**: existing call sites of
  `readiness_for` (units.py:325, 453) pass positionally or by keyword;
  the new `cold_budget` defaults to `budget`, so callers that don't
  override it get the same behaviour. Zero breaking change.

## Approx LOC + risk class

- LOC: +~12 (one new field, one new directive pair, three fixture lines
  across two files, one nix sync if applicable).
- Risk class: **medium** — fixes a real forever-loop, but expanding
  timeouts has a cost in hung-backend detection latency. Mitigated by
  `StartLimitBurst`.
- Time cost: <30 min for the code + fixture updates + a manual
  convergence check on a clean backend image.

## Source evidence

- `yadgar/core/daemon/units.py:246-268` — `readiness_for`, the function
  that emits `TimeoutStartSec`. `budget` is the only knob today; the
  cold-pull path has no separate ceiling.
- `yadgar/core/daemon/units.py:323-330` — `build_backend_unit`. Hard-codes
  `budget=180` for the backend (line 329); the cold-pull size is not
  represented anywhere in the unit model.
- `yadgar/core/daemon/units.py:344-349` — the directive block that emits
  `Restart=on-failure` and `RestartSec=5` and `TimeoutStartSec=180`. The
  loop-with-no-ceiling shape lives here.
- `yadgar/core/daemon/units.py:206-220` — `_gate()`. Builds the readiness
  `ExecStartPost` with `--retry N --retry-delay 2`. 75 × 2 = 150s for the
  backend, sized inside the 180s budget. UNCHANGED by this car; the gate
  arithmetic stays inside the new 300s ceiling with room to spare.
- `yadgar/core/daemon/systemd.py:30-32` — cross-generator note naming
  `test_runtime_readiness_cross_generator.py` and
  `test_systemd_generator_convergence.py`. These are the gates that catch
  a nix/python drift on this change.
- `yadgar/core/daemon/systemd.py:14` — names
  `yadgar/tests/core/test_install_systemd_service_characterization.py`,
  the snapshot-regeneration test that pins every directive.
- `yadgar/tests/scripts/snapshots/systemd/docker/yadgar-backend.service:53`
  — committed snapshot showing current `Type=simple` (older rendering,
  pre-Type=exec/notify). Update alongside this car.
- `yadgar/core/daemon/units.py:73` — names
  `yadgar/tests/scripts/test_v5_169_maintenance_unit_parity.py`, the
  test that keeps the unit-name list (and therefore the unit shape) in
  sync between nix and python. Same update path.
