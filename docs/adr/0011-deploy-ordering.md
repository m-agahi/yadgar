# ADR-0011 — Deploy ordering: handshake + BindsTo + restart order

Tags: adr-pending

| | |
|---|---|
| Status | adr-pending (engine-#2 ledger not seeded — captured as wiki; promote after Car A) |
| Date | 2026-08-14 |
| Car | F (task #61) |
| Author | car/f-deploy-ordering handoff |

## Context

The Yadgar core and the backend image live on **independent version tracks** (core is the `yadgar` pip package, backend is the `yadgar-backend` Docker image; `server.json::backend_version` is the single source of truth for the backend side). The systemd units that ship them are likewise independent — two files, two `ExecStart=`s, only a `Wants=` and `After=` relating them. On an upgrade, three things go wrong at once, opening a **~101 s** window where the old core is still answering the wire with the new backend's data layout, and neither side can tell that the other is from a different epoch:

1. **`yadgar.service` only has `Wants=yadgar-backend.service` + `After=…`**, no `BindsTo=`. A backend crash leaves the core running, hammering a dead container. The MCP client gets 503s but the daemon itself looks healthy (`/health/live` is 200) so P0 doesn't restart it.

2. **The orchestrator's `_default_service_restart` restarts only `yadgar.service`** (orchestrator.py:653). After a successful `yadgar upgrade`, the upgrade pulled the *new* core image but the backend is whatever the *old* `install-service` rendered the unit with — the systemd unit's `ExecStart` still references the old backend tag. The new core and the old backend now share the wire, with no version negotiation.

3. **`_default_image_pull` resolves the backend tag from the *installed* `server.json`** (via `DOCKERHUB_BACKEND_IMAGE` at runtime.py:162). This is the **inverse pairing** — on a downgrade (e.g. 5.183 → 5.170), the installed `server.json` reports the 5.183 backend_version (5.74), so the orchestrator pulls `yadgar-backend:5.74` even though the new core 5.170 expects 5.65. A wire-incompatible mismatch the install surface will then write to disk.

4. **`yadgar daemon install-service` prints the right order** (`start backend && start core` in `core/daemon/systemd.py:145`) but does not call out the measured ~101 s window, so an operator running the command manually during a rolling upgrade can restart the core before the backend is ready and see a 503 spike they have no explanation for.

## Decision

Three orthogonal fixes, layered:

### 1. Version handshake on `/health`

Add a single new field, `versions_compatible`, to the existing `/health` payload (no new endpoint). The field is the result of `yadgar._shared.version_compat.versions_compatible(self_version, peer_version)`, which compares against bounds in a JSON sidecar (`yadgar/_shared/version_compat.json`) — major.minor only, patch never breaks the wire. The bounds are the single place the supported window is declared; both the core and the backend read the same sidecar so a single edit widens the window for both sides.

The handshake is **permissive on read, strict on write**: an unverifiable peer (a fresh install that hasn't read its own version yet, a transient probe failure) reports `compatible=true` so a half-upgraded system never loops itself. The orchestrator's restart-order fix and `BindsTo=` are the *write* half — they make a mismatch unreachable in the first place.

### 2. `BindsTo=yadgar-backend.service` on the core unit

Add a `BindsTo=yadgar-backend.service` directive to the `[Unit]` section of `yadgar.service` (units.py:build_core_unit). The existing `Wants=` and `After=` stay — `Wants=` keeps the soft pull-in at start (a core start pulls the backend up too), `After=` keeps the ordering, and `BindsTo=` is the new tighten-up: **stop the core if the backend stops**. This is the single change that converts "old core ↔ new backend silent misbehaviour" into a hard 503 — the core is gone, the MCP client reconnects to a fresh process that came up against the new backend.

The `Wants=` comment block is updated to document why BOTH relations are present (one for the start-time pull-in, one for the runtime kill). The `BindsTo=` is unconditional across the prod and dev arms; the dev arm uses the `-dev` suffix on both names so the lint tests pin suffix-aware binding.

### 3. Orchestrator restart order: backend FIRST, then core

Change `_default_service_restart` (orchestrator.py:653) to:
1. `systemctl --user restart yadgar-backend.service` (clean re-provision — the backend unit's own `ExecStartPre` does `podman stop && rm yadgar-backend`)
2. `systemctl --user start yadgar.service` (START, not restart — the orchestrator's GRACEFUL_STOPPING step at :456 already stopped the core)

This is the simplest fix that makes the ~101 s window impossible: the new backend is up before the new core starts, and the BindsTo is satisfied for the duration of the swap (the core is already stopped). The core's first `/health` probe sees the new backend's version, the handshake reports `compatible=true`, and the deploy window collapses to the time systemd takes to `start` the core process.

### 4. Inverse-pairing fix in `_default_image_pull`

Change `_default_image_pull` (orchestrator.py:603) to resolve the backend tag from the **new** core image's bundled `server.json::backend_version`, not the installed one. The probe is a one-shot `runtime run --rm --entrypoint cat <core_image> /app/server.json` against the freshly-pulled image; on any probe failure (image layout, runtime, parse error) the hook falls back to the installed tag, never aborts, and the Car F handshake catches the mismatch on the OTHER side.

The inverse pairing was the subtle bug: `DOCKERHUB_BACKEND_IMAGE` reads the *currently installed* `server.json`, but the upgrade target is the *new* version. A downgrade from 5.183 (backend 5.74) to 5.170 (backend 5.65) would otherwise pull 5.74 — a wire-incompatible backend that the downgraded core cannot talk to. The handshake from decision 1 then *refuses* it (the bound check is exact), and the orchestrator would roll back. The fix is to pull the right tag the first time, so the handshake and the rollback path are the safety net rather than the primary path.

### 5. `install-service` operator message

Add a `deploy_window_seconds: 101` field to the dict returned by `install_systemd_service` (systemd.py:120). The CLI handler (`yadgar/core/cli/daemon.py::_handle_install_service`) reads it and prints:

```
Note: there is a measured ~101 s window between the backend start
and the core handshake completing; do not restart the core manually
during that window.
```

The Start line is unconditionally `start backend && start core` — the dependency-ordered shape the install always shipped with — so the message is documentation of WHY the order is what it is, not a re-arrangement of the install itself.

## Consequences

- **Positive**: the ~101 s silent-misbehaviour window is closed by three independent guards (handshake detects, BindsTo enforces, restart-order prevents). Each one alone would catch the common cases; together they make the window impossible to hit in the install surface.
- **Positive**: a downgrade path now works — the inverse pairing is gone, so a 5.183 → 5.170 upgrade pulls backend 5.65, the 5.170 core's expected tag, not 5.74.
- **Positive**: a sidecar pin (`version_compat.json`) is the single place the supported window lives, replacing the prior implicit "core and backend version together" coupling.
- **Negative**: a third runtime call (`runtime run --rm …`) in the upgrade hot path. Probe is bounded to 30s and falls back to the installed tag on any error; cost is one extra `podman` invocation per upgrade. Acceptable.
- **Negative**: existing characterization fixtures (`yadgar/tests/core/snapshots/install_systemd_service/`) and the convergence test's `INTENTIONAL_DELTAS` allowlist need to be updated to carry the new `BindsTo=` directive. Updated in this car.
- **Trade-off (rejected)**: a TTL'd handshake (a window after which the peer is considered incompatible) was the alternative to a "permissive on read" handshake. Rejected: TTL silently cripples a session parked for hours. The Car B nonce-burning design is the analog for session state, but the deploy-time handshake is process-scoped, not session-scoped, and a per-process handshake with a TTL buys nothing over BindsTo + restart order.

## Verification

- **Targeted tests** (one per decision above): `yadgar/tests/core/test_version_compat.py` (6 tests), `yadgar/tests/scripts/test_car_f_deploy_ordering.py` (5 tests), `yadgar/tests/scripts/test_car_f_binds_to.py` (2 tests), `yadgar/tests/scripts/test_car_f_install_service_message.py` (4 tests).
- **Characterization fixture**: `yadgar/tests/core/snapshots/install_systemd_service/{podman,docker}/yadgar.service` regenerated via `YADGAR_UPDATE_UNIT_FIXTURES=1`; the convergence test's `INTENTIONAL_DELTAS` carries the new `BindsTo=` directive with a Car F rationale.
- **Regression net**: the existing orchestrator test suite (`yadgar/tests/scripts/test_upgrade_orchestrator.py`, 11 tests), the systemd generator convergence test (14 tests), the install characterization test (3 tests), and the health tests all pass with the new code.
- **Skipped**: a real `systemctl` test for `BindsTo=` would require systemd in CI. The lint pin (`BindsTo=yadgar-backend.service` must appear in the rendered template, with the same suffix as the Wants=/After= on the same line) is the cheap, correct shape; the test file is `test_car_f_binds_to.py`.
