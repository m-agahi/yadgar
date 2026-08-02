# Keep the core UP during vacuum — stop only the backend, decouple the systemd cascade

**Date:** 2026-08-01
**Task:** #0111 (vacuum takes the whole memory engine down for ~68 s of a 136 s run)
**Status:** DRAFT — not started.
**Target train:** `feat/v5.172-bug-train`.
**Coupled car:** `0113-vacuum-write-loss-gate.md` — **0111 must land first** (§8).

---

## 0. Verdict up front

| Claim | Verdict |
|---|---|
| Vacuum stops the CORE as well as the backend | **TRUE** — `phases.py:150-151` calls `svc.stop()`, and `ops.py:42` defines `SERVICES = ("yadgar", "yadgar-backend")`. |
| The core-stop is mechanically required | **FALSE.** Every surviving rationale in the code is *backend*-scoped. The core-stop is a vestige of the 2026-05-12 manual rebuild ritual. |
| Removing it is safe on the current install surfaces | **TRUE, with one prerequisite** — the systemd `Requires=` cascade must be flipped to `Wants=` in both generators, or stopping the backend still takes core down. |
| Any surface would kill the core because `/health` 503s while the backend is down | **NO — surveyed, §2.3.** Every runtime healthcheck that exists uses `/health/live`, which is process-local. |

**Measured cost today (19:56:49 run):** core down ~68 s of a 136.3 s vacuum — snapshot 7 s, side-build import+verify 14 s *with both daemons down*, backend restart 31 s, core restart ~16 s. Reclaim was 242.8 MB → 183.9 MB (24 %). The core restart (~16 s) is pure waste, and the other ~52 s of core downtime buys nothing.

---

## 1. Problem statement — with evidence

### 1.1 The stop

```
yadgar/core/vacuum/phases.py:150   print("[vacuum] phase 2: stopping daemons (quiesce before snapshot) ...")
yadgar/core/vacuum/phases.py:151   svc.stop()
yadgar/core/ops/ops.py:42          SERVICES = ("yadgar", "yadgar-backend")
yadgar/core/ops/ops.py:53-61       stop() -> self._systemctl("stop", *self.SERVICES)
```

`start_yadgar()` is then called from exactly one place on the happy path —
`yadgar/core/vacuum/__init__.py:1131`, at the top of `_vacuum_finalize` — plus the
abort-path belt `_restart_services_after_abort` (`__init__.py:656-706`).

### 1.2 The stop is a vestige, not a requirement

Provenance: the two-daemon stop comes from the 2026-05-12 manual DB-rebuild ritual
recorded in `docs/PLAN_V4_8.md` @ `86b63efc`, where the canonical directory *was*
renamed out from under a live backend and everything had to be down. v5.69 P2
(`cfb46dde`) rewrote the flow to side-build + verified atomic swap and changed the
stop/copy **order** (`phases.py:134-142` documents STOP-then-COPY) — it did not
re-examine the **scope**.

Every mechanical rationale that survives in the code today is backend-scoped:

| Rationale | Location | Scope |
|---|---|---|
| Copying a live, lock-held surrealkv dir can capture a torn segment | `phases.py:134-142` | the process holding the store = **backend** |
| Quiescence gate: nothing may hold the store open at swap time | `__init__.py:202-226` | polls `{backend_url}/health` — the **SurrealDB port (8000)**, not core (8765). A live core does not trip it. |
| Chronic unclean surrealkv close on every stop | ADR-0090 | the **backend** stop is the dice roll. This car does not add one; it removes the core stop, so the run goes from two process stops to one. |
| Post-swap inode coherence | `__init__.py:930-965` | `_surreal_open_dir_names` only scans processes whose argv is `surreal … start`. The core is not one. |

### 1.3 The core survives a backend outage at runtime

Verified in code and, where noted, on the live host:

- **The core holds no fd into the store.** ADR-0078 (DB isolation: only backend
  functions touch the DB; core is an HTTP forwarder). Confirmed live: the running
  core process (`/usr/local/bin/python3.14 /usr/local/bin/yadgar --transport
  streamable-http`) has 18 open fds and **none** resolve into any `surreal_db*`
  path. So renaming the canonical under a live core is not the 07-09 split-brain —
  the core has nothing open to strand.
- **Backend calls raise per request and recover.** `_q` raises on each forward; no
  daemon thread exits on the exception. The httpx client is a persistent singleton
  that reconnects.
- **Liveness never probes the backend.** `/health/live` is process-local
  (`yadgar/core/server/http.py:611-645`) — it 503s only on `pool_saturated()`.
- **The G2 runtime-config cache fail-safes without poisoning**
  (`_runtime_config.py:129-130`) — a failed lookup returns the default, it does not
  cache a wrong value.
- **Engram slots read fresh per `allocate()`** — no stale in-memory snapshot.
- **The write-queue drainer lives in the backend**
  (`yadgar/backend/queue_drainer/__init__.py:169-183`), so stopping the backend
  alone already quiesces DB writes. That is the property 0113 builds on.

### 1.4 The cascade that actually exists is systemd

```
scripts/install/yadgar.service.in:4         Requires=yadgar-backend.service
yadgar/core/daemon/systemd.py:232           Requires=yadgar-backend{suffix}.service
yadgar/tests/core/test_daemon_runtime_binary.py:604   assert "Requires=yadgar-backend.service" in core
```

`Requires=` propagates **stop**: `systemctl --user stop yadgar-backend` stops
`yadgar` as a dependency, whether or not vacuum asks for it. So changing
`svc.stop()` → `svc.stop_backend()` alone would change nothing on a systemd
install. Only the out-of-repo private nix module was decoupled to `Wants=` in
v5.3.9; the in-repo generators never were.

`Wants=` keeps the *pull-in* semantics (starting core still starts the backend) and
`After=` keeps the boot ordering. It drops only the stop propagation, which is
exactly the behaviour we want.

Docker-compose mode is unaffected: `_docker_compose("stop", "yadgar-backend")`
(`ops.py:68-71`) has no stop-propagation semantics.

---

## 2. The decided approach — full option A

### 2.1 (a) Vacuum stops only the backend

- `phases.py:151` → `svc.stop_backend()`; update the phase banner from
  "stopping daemons" to "stopping the backend (quiesce before snapshot)" and the
  docstring at `:134-146` to say why the core stays up.
- `_vacuum_finalize` (`__init__.py:1130-1131`): **delete the `svc.start_yadgar()`
  call, keep the `_wait_for_yadgar_health(yadgar_url, timeout_s=180.0)` gate at
  `:1133-1145` unchanged.** This is the free win — core `/health` is *readiness*,
  which round-trips the backend, so a core-stays-up vacuum keeps the same hard
  gate and the same rollback for nothing. What the gate *means* changes and the
  docstring must say so: it stops meaning "core booted" and starts meaning "the
  backend came back on the compacted DB and the core can reach it". 180 s is now
  generous rather than tight; leave the number alone (a smaller number is a new
  flake risk for no benefit).
- **Keep** the `svc.start_yadgar()` calls in `_restart_services_after_abort`
  (`__init__.py:697-706`) and `_restore_db` (`:748`). They become idempotent
  no-ops on a correctly-regenerated host — and they remain load-bearing on a host
  whose units on disk were rendered **before** this change and still carry
  `Requires=`. Generator changes do not rewrite units already installed. This also
  means the existing abort-path tests stay green rather than inverting (§4.3).

  **Constraint, not just a preference — `_restart_services_after_abort` must keep
  BOTH calls AND their order (`start_backend()` before `start_yadgar()`).**
  `yadgar/tests/core/test_vacuum.py:786-788` asserts
  `started_services.index("start_backend") < started_services.index("start_yadgar")`,
  and `test_vacuum_finalize_verification.py:383-395` asserts `start_yadgar` was
  called on every phase-3 abort. A builder who reads "core is never stopped now" and
  simplifies this helper to core-only, backend-only, or reorders the two, breaks
  tests this plan promised were safe. Do not touch `_restart_services_after_abort`
  at all in this car.

### 2.2 (b) Port the v5.3.9 decoupling into the repo

- `scripts/install/yadgar.service.in:4` — `Requires=` → `Wants=`.
- `yadgar/core/daemon/systemd.py:232` — same, in the generated `core_unit`.
- `yadgar/tests/core/test_daemon_runtime_binary.py:604` — flip the assertion from
  `Requires=yadgar-backend.service` to `Wants=yadgar-backend.service`, and keep the
  `After=` assertion at `:606-609` exactly as is. Rewrite the surrounding docstring:
  the test's stated purpose ("the dependency that IS real must survive") is still
  correct, but "real" now means ordering, not lifecycle coupling.
- Read **ADR-0185** before touching either generator: the docker readiness shape
  (`Type=exec` + bounded `ExecStartPost` health gate) and the dependency shape
  interact. Flipping `Requires=`→`Wants=` must not change *start* ordering, which
  is what `After=` + the readiness gate provide. Assert both still hold.

### 2.3 The kill-risk survey (the thing that had to be checked before committing)

With the core up during the swap, core `/health` (readiness) returns 503 for the
whole backend-down window. If any surface paired that probe with an action, the
core would be killed mid-vacuum — strictly worse than today. Surveyed:

| Surface | Core probe | Action on failure | Verdict |
|---|---|---|---|
| Live private nix module (observed argv on this host) | `curl -f http://localhost:8765/health/live` | `--health-on-failure kill` | **SAFE** — liveness is process-local, never probes the backend (ADR-0019). |
| `flake.nix:444` | `curl -f .../health/live` | none | SAFE |
| `yadgar/core/daemon/systemd.py` core unit | **no runtime `--health-cmd` at all** (only the backend has one, `:196`, against `:8001`) | n/a | SAFE |
| `scripts/install/yadgar.service.in` | docker-only `ExecStartPost` curl `/health` | **start-time gate only**, no runtime healthcheck | SAFE |
| `scripts/install/launchd/*.plist.in` | no healthcheck of any kind | n/a | SAFE |

The private nix module is out of repo; the row above is empirical (read off the
live `docker run` argv), not a code read. **The user must re-confirm it before the
first live vacuum under this change** — one grep of
`modules/home/yadgar.nix` for `--health-cmd` on the core container.

### 2.4 (c) Surface in-window failures as "backend restarting", not a raw ConnectError

**Decision: reuse the maintenance gate from car 0113.** An engaged
`_maintenance_mode` already short-circuits every MCP tool at
`yadgar/core/server/_app.py:514-527` with
`{"error": "maintenance", "message": "yadgar nightly maintenance in progress; retry shortly"}`
before any DB call is attempted. That is exactly the desired UX, on exactly the
window we need it, with no new mechanism. Two small edits:

1. Generalise the message — drop "nightly", name the operation
   (`"yadgar maintenance in progress (vacuum); retry shortly"`), since the gate
   will now be engaged by CLI/timer vacuums too.
2. Note in the plan and in the code comment that HTTP viz endpoints are **not**
   behind the `_instrumented` wrapper and therefore not covered. Making them
   cover is out of scope (§9) — the viz already degrades visibly.

**Rejected:** catching `httpx.ConnectError` at the core→backend forward seam and
remapping it to a friendly error. Reasons: (i) it creates a second mechanism for
one symptom, and the two would drift; (ii) it cannot distinguish "vacuum is
swapping" from "the backend genuinely crashed", so it would mask real outages
behind a reassuring message; (iii) the gate is *declared* by the operation that
causes the outage, which is the honest signal.

**Consequence to state plainly:** without 0113, part (c) does not exist. That is
the coupling, not a nice-to-have.

---

## 3. Exact files and functions to change

| File | Change |
|---|---|
| `yadgar/core/vacuum/phases.py` | `_vacuum_snapshot_and_drop` — `svc.stop()` → `svc.stop_backend()`; banner + docstring `:134-146`. |
| `yadgar/core/vacuum/__init__.py` | `_vacuum_finalize` `:1130-1131` — drop `svc.start_yadgar()`, keep the health gate; rewrite the gate's docstring semantics `:1079-1125`. Leave `_restart_services_after_abort` and `_restore_db` untouched. |
| `scripts/install/yadgar.service.in` | `:4` `Requires=` → `Wants=`. |
| `yadgar/core/daemon/systemd.py` | `:232` `Requires=` → `Wants=`. |
| `yadgar/core/server/_app.py` | `:517-521` message text only (part c). |
| `yadgar/tests/core/test_daemon_runtime_binary.py` | `:604` assertion flip + docstring. |
| `docs/CHANGELOG.md`, `docs/contracts/BEHAVIOR_CONTRACT.md` | vacuum no longer stops the core; the finalize health gate's meaning changed. |

---

## 4. The TDD story

**CI gating asymmetry — read before choosing a directory.** `.forgejo/workflows/ci-pr.yaml`
runs suites BY DIRECTORY: `test-fast` = `yadgar/tests/{scripts,server,hooks,_meta,clients}/`,
`test-shared` = `yadgar/tests/_shared/`, `test-backend` = `yadgar/tests/backend/`,
`test-core` = `yadgar/tests/core/` (2-way pytest-split). Nothing else is gated in
`ci-pr`; anything under `yadgar/tests/integration/` runs only in the named viz/graph
steps and is effectively ungated. **A test in the wrong directory is never run.**

### 4.1 RED first — new file `yadgar/tests/core/test_vacuum_core_stays_up.py`

1. `test_snapshot_phase_stops_only_the_backend` — drive `_vacuum_snapshot_and_drop`
   with a recording `ServiceController` double; assert the recorded calls are
   `["stop_backend"]` and contain no `"stop"`. **RED today** (records `["stop"]`).
2. `test_full_vacuum_never_stops_core` — the existing `cmd_vacuum_impl` harness
   (mirror `test_vacuum_preflight.py:96-110`'s patch stack) over a happy-path run;
   assert `"stop"` never appears in the recorded controller calls, and that
   `"start_yadgar"` is not called on the success path.
3. `test_finalize_still_gates_on_core_health` — a finalize whose
   `_wait_for_yadgar_health` returns False must still roll the swap back. This is
   the guard against "we deleted the start, and the wait went with it".
4. `test_finalize_health_gate_precedes_check_invariants` — ordering pin, so a later
   refactor cannot reorder the advisory call ahead of the hard gate.

### 4.2 RED first — extend `yadgar/tests/core/test_daemon_runtime_binary.py`

5. `test_core_unit_wants_backend_not_requires` — assert `Wants=yadgar-backend*.service`
   present, `Requires=yadgar-backend` **absent**, `After=` unchanged. RED today.
6. Cross-generator parity: extend the same file (or, if it needs the shell
   generators, `yadgar/tests/scripts/`) to assert the **`.in` template** and the
   **Python generator** agree on `Wants=`. The repo already has this shape —
   `yadgar/tests/scripts/test_vacuum_trigger_cross_generator.py` and
   `test_backend_unit_queue_base_cross_generator.py`. Put a shell-rendering test in
   `yadgar/tests/scripts/`; a pure-Python one in `yadgar/tests/core/`.

### 4.3 Tests whose premise this car touches — re-read, do not blind-edit

- `yadgar/tests/core/test_vacuum_finalize_verification.py:372-395` parametrizes
  every phase-3 abort path and asserts `svc.start_yadgar()` was called. With the
  §2.1 decision to **keep** the abort-path `start_yadgar()` as an idempotent belt,
  these stay green. Their docstring must be updated to say the call is now a belt
  for pre-flip installed units, not a repair for a stop this vacuum performed.
- `yadgar/tests/core/test_vacuum.py:778-788` carries the same premise in prose and
  assertions. Same treatment.
- `yadgar/tests/core/test_vacuum_safestop.py` — the quiescence gate is
  backend-scoped (`_assert_backend_quiesced` polls the SurrealDB port). Expect no
  change; **re-read it before asserting that**, because its module docstring
  describes `svc.stop()` by name.
- `yadgar/tests/core/test_vacuum_side_launcher.py:148-158` and
  `test_vacuum_preflight.py:96-110` both patch `_vacuum_snapshot_and_drop` wholesale,
  so they are insulated.

---

## 5. Verification

**Local (no VM needed)**

1. `pytest yadgar/tests/core/ -k "vacuum or daemon_runtime"` green, including the new files.
2. Render both generators and diff the unit text: `Wants=` present, `After=` present,
   `Requires=yadgar-backend` gone, readiness shape (ADR-0185) unchanged.

**Fresh VM — `192.168.122.101`** (`sshpass -p 'Aa1234.' ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password root@192.168.122.101`).
This is a **non-nix** install, which is the point: the private nix module was
already decoupled, so only a generated-unit host can prove the port worked.

3. Fresh install; `systemctl --user cat yadgar.service | grep -E '^(Wants|Requires|After)='`
   → `Wants=`, no `Requires=`.
4. **The core-survives-a-backend-bounce proof.** Record `systemctl --user show
   yadgar -p MainPID`. Then `systemctl --user stop yadgar-backend`. Assert:
   - `systemctl --user is-active yadgar` → `active`
   - `MainPID` unchanged
   - `curl -s -o /dev/null -w '%{http_code}' localhost:8765/health/live` → `200`
   - `curl -s -o /dev/null -w '%{http_code}' localhost:8765/health` → `503`
     (readiness correctly reports degraded — ADR-0002)
   Then `systemctl --user start yadgar-backend`; `/health` returns to `200` **without
   a core restart** (MainPID still unchanged). This is the whole acceptance case.
5. A real `systemctl --user start yadgar-vacuum.service` on a seeded DB: exit 0,
   core `MainPID` identical before and after, and the run's wall time down by
   roughly the old core-restart cost.
6. Re-run 4 on a host whose units were installed **before** the flip (simulate by
   editing the unit back to `Requires=` and `daemon-reload`) — the core goes down,
   and the abort-path/`finalize` belt must be what brings it back. This proves the
   §2.1 "keep the idempotent starts" decision is load-bearing rather than dead code.

**User-owned, out of repo**

7. Confirm `modules/home/yadgar.nix` core container still uses `/health/live` (not
   `/health`) with `--health-on-failure kill`. Observed live today; confirm before
   the first vacuum under this change.

---

## 6. Rollback story

Pure revert. Three independent knobs, revertible separately:

- Revert `phases.py` → vacuum stops both units again; nothing else needs to change,
  because the abort/finalize `start_yadgar()` belts were never removed.
- Revert the two generator lines → newly-rendered units carry `Requires=` again;
  **already-installed units are unaffected either way**, so a revert does not
  self-heal a host — the operator must re-run the generator (note this in
  `MIGRATION_NOTES.md`).
- Revert the `_app.py` message string → cosmetic.

No data-path change is involved: the export, the side-build, the exact-count
verification, the atomic swap, the quiescence gate and both finalize hard gates are
untouched. Worst case is a longer outage, not a lost byte.

---

## 7. ADRs

- **New ADR required.** This reverses the standing "vacuum stops the whole engine"
  lifecycle premise and flips a pinned invariant test. It should state: the
  core-stop was a vestige of `PLAN_V4_8`; the surviving rationales are
  backend-scoped; the `Requires=`→`Wants=` port; and the healthcheck survey (§2.3)
  as the safety argument.
- **ADR-0090 (open) is NOT contradicted.** Its mandate is about the *backend* stop
  being a corruption dice roll. This car reduces the run from two process stops to
  one and adds none. Say so explicitly in the new ADR so a reader does not have to
  infer it.
- **ADR-0019** is the load-bearing prior: liveness ≠ readiness is precisely why
  `--health-on-failure kill` on `/health/live` is safe here. Cite, do not supersede.
- **ADR-0185** constrains the generator edit (readiness shape). Cite, do not supersede.
- `test_daemon_runtime_binary.py:604` traces to the `.in` templates via its own
  docstring, **not** to an ADR — so flipping it needs no supersession, only the new
  ADR to record why.

---

## 8. Ordering / dependencies vs the rest of the train

- **0111 → 0113 is a hard order.** `_maintenance_mode` is an in-process flag in the
  core (`yadgar/_shared/runtime/state.py:172`). Until the core survives a vacuum,
  a CLI/timer vacuum kills the process holding the flag and 0113 is a no-op on
  exactly the paths it exists to fix. Land 0111 first.
- 0111 part (c) **depends on** 0113 for its mechanism. Sequence the two in one
  train and let (c) land with 0113, or accept that (c) is inert until then. Say
  which in the PR body.
- **0046** touches the same file (`yadgar/core/vacuum/__init__.py`) but different
  functions (the reap helpers and `_cmd_vacuum_body`'s exit paths). Textual
  conflict risk only. **0107** is fully independent (`launcher.py` + unit templates).

---

## 9. Explicitly out of scope

- Gating the HTTP viz endpoints behind maintenance mode (§2.4) — they are not
  behind the `_instrumented` wrapper; separate task.
- Shortening the 180 s finalize health wait now that its meaning changed.
- Any change to the export / side-build / swap / quiescence / inode-coherence
  machinery. This car changes *who is stopped*, nothing about *how the DB is
  rebuilt*.
- The out-of-repo private nix module — read-only; it was already decoupled in v5.3.9.
- `ServiceController.stop()` itself. It stays as the both-units API; vacuum simply
  stops calling it. Deleting it is a separate cleanup and would collide with §2.1's
  deliberate retention of the abort-path belts.
