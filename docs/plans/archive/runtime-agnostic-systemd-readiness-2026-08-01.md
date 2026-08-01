> ARCHIVED 2026-08-01 — SHIPPED: Car 0105 (`01bb4c11`) "fix(systemd): docker install path was dead — Type=notify with no READY=1 source". Core 5.170.12, v5.171 train. §6 stated residuals tracked separately (residual (1) discharged by Car 0106; residual (2) still open).

# Plan: runtime-agnostic systemd readiness (close the docker half of the install path)

**Date:** 2026-08-01
**Task:** #0105
**Follows:** #0104 (generated units resolve the runtime binary)
**Status:** design locked

---

## 1. Problem

Task #0104 fixed the runtime **binary** in the two generated systemd units — 13 hardcoded
`docker` literals now resolve via `_get_runtime()`. That unblocked podman-only hosts. It did
not make the generator runtime-agnostic end to end, and #0104 said so in its own module
docstring (`yadgar/core/daemon/systemd.py:8-17`).

The residual defect is **readiness signalling**, and it is podman-shaped in three separate
places.

| surface | directive | podman | docker |
|---|---|---|---|
| `yadgar/core/daemon/systemd.py:91` backend | `Type=notify` | ok | **no READY=1 source** |
| `yadgar/core/daemon/systemd.py:104` backend | `--sdnotify=healthy` | ok | **unknown flag → `docker run` exits non-zero** |
| `yadgar/core/daemon/systemd.py:141` core | `Type=notify` (no `--sdnotify`) | ok via podman's default `--sdnotify=container` proxy | **no READY=1 source** |
| `scripts/install/yadgar.service.in:13` core | `Type=notify` | ok, same proxy | **no READY=1 source** |
| `scripts/install/yadgar.service.in:15` core | `Environment=DOCKER_HOST=unix:///run/podman/podman.sock` | inert | **actively wrong** — points the docker CLI at a podman socket |
| `scripts/install/yadgar-backend.service.in:21` backend | same `DOCKER_HOST` | inert | **actively wrong** |

Docker has no sd_notify proxy of any kind: it never sets `NOTIFY_SOCKET` inside the container
and has no `--sdnotify` flag. So on a docker host the backend unit dies on an unknown flag and
the core unit sits until `TimeoutStartSec` because nothing ever sends `READY=1`.

`yadgar/core/daemon/sd_notify.py:35` makes that concrete: the in-container emit is a **silent
no-op** when `NOTIFY_SOCKET` is unset. On docker it is always unset. The core daemon calls
`ready()` (`yadgar/core/bootstrap/bootstrap.py:134`) and the packet goes nowhere.

**#0104 explicitly rejected gating `--sdnotify` on podman alone as cosmetic**, and that
rejection was right: removing the flag leaves `Type=notify` with no readiness source, which is
the same dead unit by a slower route. Closing the docker path means designing docker readiness
semantics, which is what this plan does.

### 1.1 Pre-existing defect found while reading the mechanism — the podman arm is *also* dead

The premise handed to this task ("podman already works — do not regress it") is **false for the
Python generator's backend unit**. `yadgar/core/daemon/systemd.py:105` emits:

```
    --health-cmd curl -f http://localhost:8001/health || exit 1 \
```

systemd `Exec*=` lines are **not** shell — `man systemd.service` parses them into argv itself.
With no quoting this splits into six separate argv words, so podman receives
`--health-cmd=curl` followed by a bare `-f`, which is not a `podman run` flag. The unit cannot
start on podman either.

`flake.nix:377` — the NixOS generator, the one running in production — has the correct shape:

```nix
"--health-cmd 'curl -f http://localhost:8001/health || exit 1'"
```

One quoted element. The Python generator is the deviation. This is fixed here (§3.4) because
it *is* the readiness mechanism being made runtime-aware; shipping a "podman path works" claim
on top of an argv that cannot parse would be laundering. It gets its own CHANGELOG bullet.

---

## 2. Decision

### 2.1 What readiness means per runtime

**Podman — unchanged, byte-for-byte apart from §3.4's quoting fix.**

* backend: `Type=notify` + `--sdnotify=healthy` + `--health-cmd`. Podman's notify proxy runs
  the healthcheck and synthesises `READY=1` when it first passes.
* core: `Type=notify` with **no** `--sdnotify` flag, i.e. podman's default `--sdnotify=container`
  mode. Podman passes `NOTIFY_SOCKET` into the container and forwards the daemon's own
  `READY=1` (`yadgar/core/daemon/sd_notify.py`). Per ADR-0017 this proxy forwards `READY=1` and
  drops `WATCHDOG=1`; only `READY=1` is relied on here.

**Docker — `Type=exec` plus an `ExecStartPost=` health gate.**

`man systemd.service` (line 356 of the rendered page): *"the execution of `ExecStartPost=` is
taken into account for the purpose of `Before=`/`After=` ordering constraints."* That is the
whole mechanism: a unit whose `ExecStartPost` blocks does not reach `active`, so anything
ordered `After=` it waits — exactly what `Type=notify` buys on podman.

The gate is a single `curl` with its own retry budget, no shell:

```
ExecStartPost=curl --fail --silent --show-error --output /dev/null \
  --retry N --retry-delay 2 --retry-connrefused --retry-all-errors <health-url>
```

`--retry-connrefused` covers "port not listening yet"; `--retry-all-errors` covers "listening
but not healthy yet" (`--fail` turns a 5xx into an error, which plain `--retry` would not
retry). No `$(...)`, no `$VAR`, no `/bin/sh -c` — systemd's own `$`-expansion rules never come
into play, and there is nothing to escape as `$$`.

`curl` is written bare, resolved from the unit's `$PATH`. That is existing repo precedent, not
a new assumption: `scripts/install/yadgar.service.in:28` already ships
`ExecStartPre=mkdir -p @STATE_DIR@/triggers` with a comment stating exactly that. It requires
systemd new enough to search `$PATH` for `Exec*=` binaries; that requirement is already in
force on this surface. Host `curl` is likewise already assumed — `scripts/install/yadgar-setup.sh:699`
polls `http://localhost:8765/health` with an unguarded `curl` on this same install path.

#### Why the alternatives lose

| option | verdict |
|---|---|
| **`Type=exec` + `ExecStartPost` curl gate** | **CHOSEN.** No new files, no new install/uninstall surface, no shell quoting, no `$` escaping, and it works identically for both generators. |
| wrapper script that polls then calls `systemd-notify --ready`, keeping `Type=notify` | Rejected. Adds a script that must be installed, uninstalled, and version-matched on two generators plus launchd. `systemd-notify` sends from a *different* process than the main one, so systemd may refuse to credit it (mitigable with `NotifyAccess=all`, but it is a race we would be adding, not removing). Strictly more surface for the same outcome. |
| `ExecStartPost` polling `<runtime> inspect --format '{{.State.Health.Status}}'` | Rejected. Needs a shell loop → `$((n+1))`/`$n` → systemd `$$` escaping, the exact fragility the curl form avoids. Also depends on the image's baked `HEALTHCHECK` being present in `Config.Healthcheck`, and `flake.nix:18` records that podman build does **not** propagate it. Depending on healthcheck propagation to fix a readiness bug is a second unreliable link. |
| Keep `Type=notify` on docker, drop only `--sdnotify` | Rejected — this is #0104's already-rejected half-measure. The unit sits until `TimeoutStartSec` and is killed. |
| `Type=simple`, no gate | Rejected. `active` would mean "docker CLI forked", so `After=yadgar-backend.service` guarantees nothing and core races a backend that is still loading its embedding model. Silently wrong is worse than loudly dead. |

### 2.2 Which endpoint the gate polls

**Both gates poll `/health`, not `/health/live`.**

On podman the core's readiness is the in-container `sd_notify.ready()`, emitted **last**, after
the full engine set and daemons are live (`yadgar/core/bootstrap/bootstrap.py:134`).
`/health/live` — the endpoint in the image's baked `HEALTHCHECK` (`Dockerfile:24`) and, per
ADR-0019, a pure liveness probe — goes green as soon as the HTTP server binds, well before
that. Gating on it would mark the docker unit `active` *earlier* than podman does: a semantic
regression dressed up as reuse. `/health` is the readiness probe (db + embed dependent) and is
what `yadgar/core/daemon/daemon.py:414` itself polls to decide the daemon is up. It is the
closest available proxy for `READY=1`, so it is the one used.

For the backend, `/health` is also what `--sdnotify=healthy` effectively waits on, since
`--health-cmd` is `curl -f http://localhost:8001/health`. Same signal, different transport.

Neither endpoint is auth-gated; `YADGAR_MCP_AUTH_TOKEN` guards `/admin/*` only.

### 2.3 One unit shape with conditionals, not two

**One shape.** This repo already carries four cross-generator invariant tests
(`test_admin_token_cross_generator.py`, `test_backend_db_mount_cross_generator.py`,
`test_backend_unit_queue_base_cross_generator.py`, `test_vacuum_trigger_cross_generator.py`)
because generator drift is its recurring defect class. Forking podman and docker into separate
templates would create that drift *inside a single generator*, and every future change to a
mount, an `-e`, or an image ref would have to be made twice with nothing checking the second
copy.

* **Python generator:** trivial — the unit is an f-string, so the runtime-specific lines are
  computed into local variables and interpolated.
* **`.in` templates:** `sed` cannot branch, so line-prefix markers are used and
  `generate_systemd.sh` decides per runtime whether to **strip** the marker or **delete** the
  line:

  ```
  Type=@SERVICE_TYPE@
  @PODMAN_ONLY@NotifyAccess=all
  @DOCKER_ONLY@ExecStartPost=curl --fail ... http://127.0.0.1:8765/health
  ```

  ```bash
  if [[ "${RUNTIME##*/}" == "podman" ]]; then
      SERVICE_TYPE=notify; RUNTIME_SED=(-e "s|@PODMAN_ONLY@||g" -e "/@DOCKER_ONLY@/d")
  else
      SERVICE_TYPE=exec;   RUNTIME_SED=(-e "/@PODMAN_ONLY@/d"  -e "s|@DOCKER_ONLY@||g")
  fi
  ```

  Deleting the whole line rather than substituting an empty value keeps the rendered unit free
  of stray blank lines, and keeps the podman render byte-identical to today's output.

### 2.4 What `DOCKER_HOST=unix:///run/podman/podman.sock` is for, and the docker equivalent

It is a podman-only artefact, inherited from `flake.nix:362`/`414`. It points at podman's
**rootful** system socket. These are `--user` units, so for the local (non-remote) podman CLI
the variable is inert — podman reads `CONTAINER_HOST`, and honours `DOCKER_HOST` only on the
docker-compat/remote path. It survives as belt-and-braces for hosts where podman is reached
through the docker-compat shim.

**The docker equivalent is to omit it entirely.** Setting `DOCKER_HOST` to a podman socket path
on a docker host is not merely useless, it redirects the docker CLI at a socket that does not
exist, so every `ExecStartPre`/`ExecStart`/`ExecStop` in the unit fails. Unset, the docker CLI
uses its own default (`/var/run/docker.sock`, or the active docker context). So the line becomes
`@PODMAN_ONLY@`-gated in both `.in` templates.

The Python generator never emitted `DOCKER_HOST` at all, on either runtime — that stays true;
this is a `.in`-only change.

### 2.5 Timeout budget — a gate that is too tight is a crashloop, not a failure

`man systemd.service`: a non-zero `ExecStartPost=` (unprefixed) **fails the unit**. Combined
with the existing `Restart=on-failure`, an under-budgeted gate converts "slow first start" into
a restart loop. Budgets are therefore set from the slowest real path, which is a first start:
`docker run` pulls the image inline, and with `Type=exec` the gate begins polling as soon as
`execve` succeeds — i.e. *during* the pull.

| unit | docker `TimeoutStartSec` | gate budget | basis |
|---|---|---|---|
| backend | **180** | `--retry 75 --retry-delay 2` ≈ 150s | matches `flake.nix:366` (`TimeoutStartSec = 180`, comment: "covers cold model load") and clears `--health-start-period=60s` plus a first pull |
| core (both generators) | **120** (already the `.in` value; added to the Python docker arm) | `--retry 45 --retry-delay 2` ≈ 90s | no model load; fits inside the pre-existing core budget |

These land on the **docker arm only**. See §6 for the podman-arm timeout residual.

---

## 3. Design

### 3.1 `yadgar/core/daemon/systemd.py`

`runtime = _get_runtime()` already exists (#0104). Add `_is_podman = os.path.basename(runtime) == "podman"`
and derive four fragments:

* backend `Type=` block — `Type=notify\nNotifyAccess=all` vs `Type=exec\nTimeoutStartSec=180`
* backend `--sdnotify=healthy \` ExecStart line — podman only
* backend `ExecStartPost=` gate on `http://127.0.0.1:{DEFAULT_BACKEND_EMBED_PORT}/health` — docker only
* core equivalents, gate on `http://127.0.0.1:{profile.port}/health`, `TimeoutStartSec=120`

Both host ports are already published by the units (`-p {DEFAULT_BACKEND_EMBED_PORT}:8001`,
`-p {profile.port}:8765`), so the gate needs no new port exposure.

### 3.2 `scripts/install/yadgar.service.in` + `yadgar-backend.service.in`

Markers per §2.3. The core template gets `Type=@SERVICE_TYPE@`, `@PODMAN_ONLY@NotifyAccess=all`,
`@PODMAN_ONLY@Environment=DOCKER_HOST=…`, and a `@DOCKER_ONLY@ExecStartPost=` gate on
`http://127.0.0.1:8765/health`. The backend template gets **only** the `@PODMAN_ONLY@` gate on
its `DOCKER_HOST` line — it is `Type=simple` on both runtimes and stays that way (§7).

### 3.3 `scripts/install/generate_systemd.sh`

`render_template()` gains `@SERVICE_TYPE@` plus the `RUNTIME_SED` array from §2.3, both computed
once from the already-detected `${RUNTIME}`.

### 3.4 `--health-cmd` quoting (§1.1)

`yadgar/core/daemon/systemd.py:105` →
`--health-cmd "curl -f http://localhost:8001/health || exit 1" \`. This preserves the substring
`curl -f http://localhost:8001/health` that
`test_generated_backend_unit_has_sdnotify_healthy_and_health_cmd` asserts on, so no existing
assertion is touched, let alone weakened. Applies to both runtimes — `docker run` supports
`--health-cmd`/`--health-start-period` too; only `--sdnotify` is podman-exclusive.

### 3.5 launchd

**No change.** `scripts/install/launchd/*.in` already carry no `--sdnotify` and no `DOCKER_HOST`
(their headers say so explicitly: *"No DOCKER_HOST: podman-machine manages its own socket lookup
on macOS"*). launchd has no notify protocol, so there is nothing runtime-conditional to express.
This is pinned by assertion rather than left to inspection (§4.2).

---

## 4. Tests

### 4.1 Extend the #0104 unit-directive guard — the markers create a blind spot

`_UNIT_EXEC_RUNTIME` is `^\s*Exec[A-Za-z]*\s*=\s*-?\s*(docker|podman)\b`. A source line
`@DOCKER_ONLY@ExecStart=docker run …` does not begin with `Exec`, so **introducing the markers
opens a channel through which a future hardcoded runtime literal passes the guard silently** —
precisely the "a narrow scope only relocates the recurrence" failure the guard's own docstring
is about.

`yadgar/tests/core/test_daemon_runtime_binary.py` is therefore **extended, not duplicated**:
`_literal_runtime_unit_directives` strips a leading `@[A-Z_]+@` marker before matching, plus a
RED fixture proving a marked-up hardcoded literal is flagged. Same module, same detector, same
defect class; `test_unit_directive_guard_scope_covers_both_unit_generators` keeps the scope
pinned.

### 4.2 New cross-generator invariant

`yadgar/tests/scripts/test_runtime_readiness_cross_generator.py`, shaped like its four siblings:
**no generator may emit a podman-only readiness construct into a unit rendered for docker.**
Covers all three surfaces — the Python generator, the `.in` templates, and the launchd plists —
with both a docker arm (no `--sdnotify`, no `Type=notify`, no `DOCKER_HOST`, gate present) and a
podman arm (`Type=notify` present, `--sdnotify=healthy` present on the backend, no gate). This is
a new invariant, not a re-implementation of the runtime-binary detector, so it earns its own file.

### 4.3 The three pinned tests become runtime-aware — assertion for assertion

None of these lose an assertion. `Type=exec` keeps every existing `assert "Type=simple" not in
content` true on **both** arms, so those survive verbatim everywhere.

| test | today | change |
|---|---|---|
| `test_systemd_unit_template.py::test_unit_template_has_type_notify` | reads `yadgar.service.in` **source text** | source now says `Type=@SERVICE_TYPE@`, so it must read a **rendered** unit via `render_systemd(tmp_path, {"YADGAR_RUNTIME": …})`. Podman arm keeps both assertions verbatim; new docker arm asserts `Type=exec` + gate. |
| `test_daemon_cli_fixes_v5_49_1.py::test_generated_backend_unit_is_type_notify` / `…_has_sdnotify_healthy_and_health_cmd` | pins **no** runtime — inherits ambient detection | pin `YADGAR_CONTAINER_RUNTIME=podman` (a *strengthening*: the assertion stops depending on the developer's host) and keep every assertion; add a docker arm. |
| `test_upgrade_orchestrator.py::test_install_systemd_service_type_notify` | pins no runtime | same treatment. |

### 4.4 Docker must be pinned, not podman

`docker_env`/`podman_env` fixtures already exist in `test_daemon_runtime_binary.py`. Per the
#0101/#0104 lesson, a test run under podman cannot tell a resolved value from a hardcoded
podman literal. Every "the docker path is correct" assertion pins `YADGAR_CONTAINER_RUNTIME=docker`.

---

## 5. Acceptance criteria

1. Units rendered with `YADGAR_RUNTIME=docker` / `YADGAR_CONTAINER_RUNTIME=docker` contain **no**
   `--sdnotify`, **no** `Type=notify`, and **no** `DOCKER_HOST=…podman.sock` — on all three
   surfaces (Python generator, `.in` templates, launchd plists).
2. Those docker units carry `Type=exec` and a bounded `ExecStartPost=` gate on `/health`, with a
   `TimeoutStartSec` that exceeds the gate's own retry budget.
3. **The podman render is unchanged**, byte-for-byte, except for the §3.4 quoting fix — asserted
   by the podman arms of §4.2/§4.3 keeping their original assertions verbatim.
4. The #0104 unit-directive guard flags a hardcoded runtime literal **behind a marker prefix**
   (RED fixture), so the new syntax does not widen the blind spot it guards.
5. No existing assertion deleted or weakened; `check_no_silent_test_weakening` passes.
6. Nothing touches the live install — every assertion is on generated text in `tmp_path`.

---

## 6. Stated residuals (not silent)

* **Podman backend `TimeoutStartSec` stays at systemd's 90s default.** `flake.nix:366` uses 180
  for the same container with the comment "covers cold model load", so 90s is arguably too tight
  for a first start with a cold HF cache. Fixing §1.1 makes this newly reachable — the unit could
  not start at all before. Not changed here because it is a **behavioural change to the podman
  arm** and this task's acceptance criterion is that the podman arm is unchanged; a first-start
  timeout still self-heals via `Restart=on-failure` with a now-warm cache. Follow-up car.
* **The `.in` backend is `Type=simple` on both runtimes**, so core's `After=yadgar-backend.service`
  means "the backend's `docker`/`podman` process forked", not "the backend is ready". Pre-existing
  and identical on both runtimes; the core gate compensates on docker, and podman's core
  `--sdnotify=container` proxy waits on the daemon's own `READY=1` which cannot fire until the
  backend answers. Not made `Type=notify` here — that would be a podman-arm behaviour change.
* **Docker's readiness is a poll, not a signal.** It is coarser than podman's proxy: `active`
  means "`/health` answered 200 once", not "the container told us". Accepted; there is no sd_notify
  transport on docker to do better.

---

## 7. Out of scope

* **`flake.nix`** — a fourth unit generator, deliberately podman-pinned (`flake.nix:241-244`:
  "Default runtime: podman via the docker-compat shim"), and not in this task's surface list. It
  already has the correct `--health-cmd` quoting and `TimeoutStartSec=180`.
* **`yadgar/core/systemd/yadgar.service`** — a checked-in `Type=simple` unit that runs
  `python3 -m yadgar` directly, no container involved. Unaffected.
* **`docker-compose.yml`** — a self-contained dev/CI stack, no systemd.
* **ADR-0017's watchdog decision** — `WATCHDOG=1` is dropped by podman's proxy in every
  `--sdnotify` mode and is not relied on by either arm. Unchanged.
