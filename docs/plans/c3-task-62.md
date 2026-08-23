# C3 — Task 62: `yadgar-vacuum.service` sets no `YADGAR_EMBED_URL` — Car 0113's drain nudge is inert

## Status / target

- Status: DRAFT — not started.
- Target train: `feat/c3-bug-bag` (the c1–c10 train of which this is car C3).
- Car: C3.
- Companion cars in this train: `docs/plans/c3-task-{233,278}.md` for the
  TimeoutStartSec + maintenance-envelope halves.

## Goal

Set `YADGAR_EMBED_URL` on the `yadgar-vacuum.service` unit so the queue-drain
nudge introduced by car 0113 is actually reachable. Today the vacuum
service is rendered without that env var (only `YADGAR_DB_URL` and
`YADGAR_DATA_DIR` are exported at `yadgar/core/daemon/maintenance_units.py:297-298`),
so the core forwarder (`yadgar/core/forward.py:115-120`) raises
`RuntimeError: YADGAR_EMBED_URL is not set; cannot forward admin op to backend`,
the drain wraps the exception in a warning at
`yadgar/core/vacuum/__init__.py:1809-1812`, and the entire safety
mechanism degrades to a no-op on every systemd-fired vacuum.

The fix is small (one new `Environment=` line in the rendered unit) but the
scope of "which host entry point resolves what" is worth pinning because
the nix twin has the same shape and the cross-generator tests will catch
any drift.

## Pre-conditions

- The unit is rendered by `yadgar/core/daemon/maintenance_units.py:
  build_vacuum_service` at lines 268-306.
- Currently exported variables: `YADGAR_DB_URL` (line 297) and
  `YADGAR_DATA_DIR` (line 298). No `YADGAR_EMBED_URL`.
- `_forward_admin` in `yadgar/core/forward.py:115-120` reads
  `YADGAR_EMBED_URL` from the env and refuses to forward without it.
- `_drain_backend_queue` at `yadgar/core/vacuum/__init__.py:1796` calls
  `_forward_admin("drain_now", {})` and depends on the env var.
- `_drain_queue_best_effort` at
  `yadgar/core/vacuum/__init__.py:1800-1812` wraps the call in a WARN
  on any exception, so the absence of the env var surfaces as a warning,
  not an abort. The drain is silent-failing today.
- The snapshot at
  `yadgar/tests/scripts/snapshots/systemd/docker/yadgar-vacuum.service`
  pins the current shape (line 15 has `Environment=YADGAR_DB_URL=...`,
  line 16 has `Environment=YADGAR_DATA_DIR=...`). Same for the podman
  twin.
- `flake.nix` renders an independent copy (units.py:75-79); the
  cross-generator parity test
  (`yadgar/tests/scripts/test_systemd_generator_convergence.py`, named
  in systemd.py:32) catches drift on this directive.
- The nightly-cycle service (`build_nightly_service` at maintenance_units.py
  :401-420) DOES set `YADGAR_EMBED_URL` at line 413. The vacuum unit
  should mirror that — same backend host (loopback), same published port
  (8001), same format.

## Step-by-step

1. **Add `YADGAR_EMBED_URL` to the vacuum service's `[Service]` block.**

   `yadgar/core/daemon/maintenance_units.py:297-298` is the existing block:
   ```python
   Directive("Environment", f"YADGAR_DB_URL=http://127.0.0.1:{surreal_port}"),
   Directive("Environment", f"YADGAR_DATA_DIR={data_dir}"),
   ```

   Insert a third line immediately after `YADGAR_DB_URL` so the URL pair
   stays grouped (mirrors nightly-cycle's shape at line 411-413):
   ```python
   Directive("Environment", f"YADGAR_DB_URL=http://127.0.0.1:{surreal_port}"),
   Directive("Environment", "YADGAR_EMBED_URL=http://127.0.0.1:8001"),
   Directive("Environment", f"YADGAR_DATA_DIR={data_dir}"),
   ```

   Why a literal `8001`: that is the published embed-port the backend
   unit renders (`units.py:306` in `_backend_exec_start`, and the doc
   comment at maintenance_units.py:198-200 explains why nightly hard-codes
   it instead of reading from a spec). The vacuum unit should match
   exactly. If `surreal_port` ever becomes configurable separately, the
   embed port follows the same convention as nightly-cycle — a literal
   `8001` — because the backend unit emits `-p 127.0.0.1:{embed_port}:8001`
   and that render target IS `8001` (DEFAULT_BACKEND_EMBED_PORT in
   `yadgar/core/daemon/runtime.py`).

2. **Add a one-line `EnvironmentFile` rationale comment if missing.**

   The nightly cycle at maintenance_units.py:408 has:
   ```
   # Leading '-' — a missing secrets file must not wedge the timer.
   ```
   The vacuum unit's `EnvironmentFile=` is at line 295 with the rationale
   already in the `VACUUM_SECRETS_DOC` block (lines 130-133). No new
   comment is needed for this car; the directive speaks for itself in the
   same way the existing two `Environment=` lines do.

3. **Update the snapshot fixtures.**

   The committed snapshots at
   `yadgar/tests/scripts/snapshots/systemd/{docker,podman}/yadgar-vacuum.service`
   must show the new line in the same position as the rendered unit.
   The fixture regeneration test
   `yadgar/tests/scripts/test_install_systemd_service_characterization.py`
   (named in systemd.py:14) handles the regeneration on `--snapshot-update`.

   ```diff
    Environment=YADGAR_DB_URL=http://127.0.0.1:8000
   +Environment=YADGAR_EMBED_URL=http://127.0.0.1:8001
    Environment=YADGAR_DATA_DIR=/home/testuser/.local/share/yadgar
   ```

   The `cross_generator` suite
   (`yadgar/tests/scripts/test_systemd_generator_convergence.py`, named at
   systemd.py:32) does the python-vs-nix comparison and would fail if
   the nix twin does not get the same line.

4. **Mirror in `flake.nix` so the cross-generator test stays green.**

   The nix unit is a separate rendering of the same nine units. Read
   `flake.nix` for the `yadgar-vacuum.service` block and add
   `Environment=YADGAR_EMBED_URL=http://127.0.0.1:8001` in the same
   position. If the nix twin already declares a different value
   (e.g. depends on a cfg option), reconcile — the literal `8001` is the
   same value nightly-cycle uses and the literal `127.0.0.1` is the
   loopback publish from `yadgar-backend.service` (units.py:305).

5. **Verify the drain reachability without changing drain code.**

   The drain path is already wired (`_drain_backend_queue` →
   `_forward_admin`). No code change in `yadgar/core/vacuum/__init__.py`
   is required for this car. The fix is PURELY in the unit rendering so
   the env var reaches the host process.

   To smoke-test: `sudo systemctl --user start yadgar-vacuum.service` on a
   machine where `yadgar-backend` is up. The vacuum log line
   `[vacuum] queue drain nudge: {...}` at `__init__.py:1797` should now
   show a non-empty result (the `result` dict the backend returned) instead
   of being preceded by a `WARNING: queue drain nudge failed` at
   `__init__.py:1812`.

## Verification

- **Snapshot diff**: the vacuum service fixtures at
  `yadgar/tests/scripts/snapshots/systemd/{docker,podman}/yadgar-vacuum.service`
  contain the new `Environment=YADGAR_EMBED_URL=http://127.0.0.1:8001`
  line in the same position as the existing `Environment=YADGAR_DB_URL=...`.
- **Cross-generator parity**: `pytest yadgar/tests/scripts/test_systemd_generator_convergence.py`
  passes after both renders carry the line.
- **Live drain reachability**: a `systemctl --user start
  yadgar-vacuum.service` run produces a `[vacuum] queue drain nudge:` log
  line with a non-error payload (no `WARNING: queue drain nudge failed`
  preceding it). The backend's `/admin/drain_now` endpoint responds.
- **Behavioural regression check**: the weekly timer-fired vacuum still
  completes — this car adds a new env var, does not change the drain
  semantics. `TimeoutStartSec=30min` (maintenance_units.py:300) is
  unchanged.
- **No new failure mode**: the new env var matches nightly-cycle's
  literal, so any test that pins nightly's render remains valid; the
  vacuum now mirrors nightly on this dimension.

## Risks / rollback

- **Wrong host or port**: a typo here would silently break the drain on
  every host. Mitigation: the literal `127.0.0.1:8001` matches the
  nightly-cycle unit (maintenance_units.py:413) AND the backend's
  loopback publish (units.py:306), so two already-tested renderings
  agree on the value. The cross-generator test catches any nix/python
  drift immediately.
- **Other forwarders (`read_query`, `restore`, `viz`) have the same
  requirement**: `forward.py:242-251, 313-322` both refuse without
  `YADGAR_EMBED_URL`. They are not exercised by the vacuum flow, but if
  the vacuum ever grows a viz-style pre-step, the env var is already in
  place.
- **Rollback**: remove the single `Environment=` line. No data risk;
  no fixture regeneration beyond reverting one line per snapshot.
- **The fix is render-time only**: the vacuum flow code
  (`yadgar/core/vacuum/__init__.py`) does not change.

## Approx LOC + risk class

- LOC: +3 (one `Environment=` line in the builder, one line in each of
  two snapshots, one line in `flake.nix` if applicable).
- Risk class: **trivial** — adds an env var, no behaviour change in the
  drain logic. The drain was already a WARN-and-proceed; it now
  succeeds.
- Time cost: <10 min for the edit + snapshot regeneration + a manual
  vacuum start to confirm the log line.

## Source evidence

- `yadgar/core/daemon/maintenance_units.py:289-303` — `build_vacuum_service`,
  the `[Service]` block. Currently emits only `YADGAR_DB_URL` and
  `YADGAR_DATA_DIR`. The fix lives at line 297-298.
- `yadgar/core/daemon/maintenance_units.py:411-413` —
  `build_nightly_service` already emits `YADGAR_EMBED_URL=http://127.0.0.1:8001`.
  The literal value to mirror lives here.
- `yadgar/core/daemon/maintenance_units.py:198-200` (`NIGHTLY_EMBED_DOC`)
  — explains WHY the embed URL is a literal `8001` rather than a spec
  field: "pipx-installed host yadgar has no [ml] extra". The same logic
  applies to the vacuum unit.
- `yadgar/core/forward.py:115-120` — `_forward_admin` raises
  `RuntimeError: YADGAR_EMBED_URL is not set` when the env var is absent.
  This is the test the live drain currently fails.
- `yadgar/core/forward.py:242-251` — same check in the read_query
  forwarder. Confirms the env var is the single point of truth across
  all four forwarders.
- `yadgar/core/vacuum/__init__.py:1796` — `_drain_backend_queue` calls
  `_forward_admin("drain_now", {})`. The recipient of the env var.
- `yadgar/core/vacuum/__init__.py:1800-1812` —
  `_drain_queue_best_effort` warns-and-proceeds on any exception. Today
  it warns on every systemd-fired run because the env var is unset;
  after this car it succeeds and does not warn.
- `yadgar/core/vacuum/__init__.py:1806-1807` — the comment that names
  this exact defect: "A backend without ``YADGAR_EMBED_URL`` (today's
  ``yadgar-vacuum.service``) lands here every run." This car closes
  the issue that comment documents.
- `yadgar/core/daemon/units.py:75-79` — the `flake.nix` is independent;
  this car needs a flake.nix sync line.
- `yadgar/core/daemon/systemd.py:31-32` — names the cross-generator
  test suites that must pass.
- `yadgar/tests/scripts/snapshots/systemd/docker/yadgar-vacuum.service:15-16`
  — the current snapshot. Pin the new line position.
