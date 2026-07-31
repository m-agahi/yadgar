# Plan: run the vacuum side-build in a one-shot backend container

**Date:** 2026-08-01
**Task:** #0092 (full fix; the preflight half shipped in v5.170.0)
**Depends on:** #0100 (`converge-backend-db-mount-2026-08-01.md`) — **must land first**
**Status:** design locked, not started

---

## 1. Problem

`spawn_surreal` (`yadgar/core/_surreal_runner/_surreal_runner.py:107-121`) is a bare
PATH-resolved `subprocess.Popen(["surreal", "start", ...])`, called from
`yadgar/core/vacuum/__init__.py:512` inside `_build_and_verify_side_db`. On a container install
that binary exists **only inside the backend image** (`Dockerfile.backend:20`,
`COPY --from=surrealdb/surrealdb:v3.1.5`).

v5.170.0 shipped the preflight (task #0092 car): vacuum now SKIPs cleanly before the destructive
phases instead of dying after export + stopping both units + a full-size `copytree`. **That
stops the damage; it does not let a container-only host vacuum.** This plan does.

### 1.1 Why not just ship a host-side `surreal`

Rejected. It converts today's coincidental version alignment into a permanent maintenance
invariant tracking `Dockerfile.backend`'s pin, plus a per-OS/arch download path in
`yadgar-setup.sh`. The one-shot container runs **the identical binary that will later open the
result**, making skew structurally impossible rather than merely currently-absent.

Version skew is latent, not present: two binaries exist on the reference workstation —
`/etc/profiles/per-user/max/bin/surreal` (3.1.5, nix, wins PATH) and `/home/max/.local/bin/surreal`
(3.0.5, shadowed). A bare `surreal` resolves to 3.1.5 today, matching the container. One PATH
change activates the skew silently.

---

## 2. Prerequisite — why #0100 gates this

The original investigation justified the one-shot container with: *"the DB is a host bind mount
(`scripts/install/yadgar-backend.service.in`, `-v @DATA_DIR@:/data`), so host↔container path
translation is a `$DATA_DIR`→`/data` prefix rewrite the codebase already models
(`yadgar/core/vacuum/__init__.py:844-848`)."*

Live QA (2026-07-31) showed that holds for the **systemd-unit path only** — `yadgar daemon start`
mounts a named volume and vacuum cannot even find the DB. #0100 converges all three paths on the
bind mount. **Do not start this car until #0100 has landed on the train**; its premise is
otherwise false on one of three install shapes.

---

## 3. Design

Introduce a **launcher seam** with two implementations, selected by a preflight:

| implementation | when |
|---|---|
| host binary (today's `spawn_surreal`) | a usable `surreal` is on PATH — dev boxes, nix hosts, manual installs |
| **one-shot container** | otherwise, when the backend image is present |
| neither | the v5.170.0 SKIP path, unchanged |

Everything else in vacuum survives untouched: export, snapshot, count capture, exact-count
verify, promote, atomic swap, finalize, recovery.

### 3.1 Enablers already in the repo

- image ref in-process: `yadgar/core/daemon/runtime.py:162` `DOCKERHUB_BACKEND_IMAGE`, with a
  `YADGAR_BACKEND_IMAGE` override (`daemon.py:222`)
- loopback publish already used by the backend unit
  (`-p 127.0.0.1:@BACKEND_SURREAL_PORT@:8000`), so a side URL on an alternate port is reachable
- orchestration stays **host-side**, so `systemctl` still works — required, since
  `yadgar-vacuum.service.in`'s own comment notes the image ships no `systemctl`

### 3.2 The load-bearing rewrite — `_stop_side_backend_clean`

`yadgar/core/vacuum/__init__.py:453-489`. Its data-safety contract is that
`proc.wait(timeout=15.0)` **proves a graceful exit** — a SIGKILL'd surrealkv dir is half-flushed
and corrupt-on-reopen (ADR-0090), so it *raises* rather than swap. Under `podman run --rm` there
is no `Popen` exit code to inspect.

Replacement must preserve the same proof:

```
podman stop --time 30 <name>     →  podman wait <name>  →  podman inspect --format '{{.State.ExitCode}}'
```

A non-zero or timed-out stop MUST raise exactly as today. **This assertion is the whole safety
property of the swap — do not weaken it to get the car green.**

### 3.3 Container invocation requirements

- `--user root --security-opt label=disable`, matching the backend unit, so the built directory's
  ownership and SELinux labels match the canonical store under rootless userns
- mount the data dir the same way the backend does (post-#0100: the `DATA_DIR` bind mount)
- publish the side port on loopback only
- a deterministic container name so a crashed run can be found and cleaned up
- reap any leftover side container **before** starting (a previous crash must not block this run)

---

## 4. Acceptance criteria

- Vacuum completes end-to-end on a **container-only host with no host `surreal`** — this is the
  bug; a test that only exercises the host-binary path proves nothing.
- The graceful-stop assertion is preserved: a killed/failed side container **raises and does not
  swap**. Test it explicitly by forcing a non-zero exit.
- Host-binary path unchanged where a `surreal` exists (no regression for dev/nix hosts).
- A leftover side container from a previous crashed run is reaped, not fatal.
- The v5.170.0 SKIP path still fires when neither a host binary nor the backend image is available,
  with its `skip_reason` intact.
- No real vacuum against the live workstation DB during development — fixtures and temp dirs only.

## 5. Out of scope

- Version-compat gating between host binary and image (existence check only, as in v5.170.0).
- Reaping the old named volume from #0100.
- `yadgar-vacuum.service.in` / `flake.nix` not setting PATH for the unit — noted below, separate.

## 6. Known adjacent issue (do not fix here)

Neither `scripts/install/yadgar-vacuum.service.in` nor `flake.nix`'s
`systemd.user.services.yadgar-vacuum` sets PATH or puts surrealdb on the unit's path — the
nightly's `surreal` resolution depends entirely on the systemd user manager's inherited
environment. Not a regression (it was always the spawn's condition), but it is what decides
whether a given host takes the host-binary branch or the container branch. Worth a follow-up task
once this car defines the branch point.
