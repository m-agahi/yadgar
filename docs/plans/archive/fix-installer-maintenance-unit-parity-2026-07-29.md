# Fix installer maintenance-unit parity (non-nix systemd ships no background maintenance)

**Date:** 2026-07-29
**Status:** PLANNED — awaiting decisions D1–D5 before implementation.
**Target train:** `feat/v5.169-install-runtime-fixes` (ONE car, phased).
**Follows:** `docs/plans/fix-vacuum-trigger-path-and-watcher-2026-07-29.md` (task:0044) — this car
discharges its follow-ups **F1** (non-nix systemd vacuum runner + timer + watcher) and **F3**
(extend the cross-generator net to compose + the `daemon.py` docker-run dev path).
**Precedent (same failure class, read first):**
`docs/plans/archive/fix-systemd-generate-missing-queue-base-2026-07-28.md` (task:0076 — the
`/data` vs `/queue-data` split: correct value differs per surface, so the anti-recurrence
mechanism is a cross-generator test, not a unified constant).

---

## 1. Observed state (verified on `feat/v5.169-install-runtime-fixes`, cited)

### 1.1 What each surface actually renders

| # | Surface | Renderer | nightly unit | vacuum runner | vacuum timer | trigger watcher | host state-dir bind | `-e YADGAR_VACUUM_TRIGGER_PATH` | SurrealDB (:8000) published to host |
|---|---|---|---|---|---|---|---|---|---|
| 1 | **non-nix systemd (Linux)** | `scripts/install/generate_systemd.sh:87-89` | **NO** | **NO** | **NO** | **NO** | **NO** (`yadgar.service.in:19` mounts only `-v @DATA_DIR@:/data`) | **NO** | **NO** (`yadgar-backend.service.in:13` publishes only `-p 127.0.0.1:8001:8001`) |
| 2 | **launchd (macOS)** | `scripts/install/generate_launchd.sh:97-118` | YES | YES | YES (`StartCalendarInterval`) | YES (`WatchPaths`) | YES (`com.openfantasy.yadgar.plist.in:58`) | YES (same line) | **NO** (`com.openfantasy.yadgar-backend.plist.in:49`) |
| 3 | **repo flake.nix** | `flake.nix:568-690` | YES `:659` | YES `:568` | YES `:588` | YES `:614` (`.path`) + `:620` (handler) | YES `:449` | YES `:460` | **YES** `:380` (`-p 127.0.0.1:${backendSurrealPort}:8000`) |
| 4 | **Python systemd generator** | `yadgar/core/daemon/systemd.py` | NO | NO | NO | NO | **impossible** — `/data` is a NAMED volume (`profiles.py:39` `os.environ.get("YADGAR_VOLUME", "yadgar-data")`) | NO | n/a |
| 5 | **docker-compose.yml** | static | NO | NO | NO | NO | NO | NO | **NO** (`docker-compose.yml:46-47`) |
| 6 | **`daemon.py` docker-run dev path** | `yadgar/core/daemon/daemon.py:111,250` | NO | NO | NO | NO | NO | NO | n/a |
| 7 | private nix module (out of repo, reference) | `modules/home/yadgar.nix` | YES | YES | YES | YES | YES | YES | YES |

Surface 1 is the shipped, non-nix Linux install surface. It renders **exactly three** files
(`generate_systemd.sh:87-89`): `yadgar.service`, `yadgar-backend.service`, `yadgar.target`.
`grep -c nightly scripts/install/generate_systemd.sh` → 0. No `*.timer.in` / `*.path.in`
template exists anywhere under `scripts/install/`.

**Consequence:** on a repo Linux install, consolidation, heat decay, episode formation, dream
replay, auto-narrate, and the weekly vacuum **never fire**. The memory system silently stops
consolidating. There is no error, because nothing ever tries.

### 1.2 Bug 2 — SurrealDB is unreachable from the host on BOTH non-nix surfaces

The nightly and vacuum entry points execute **on the host** (not in a container) on every surface
that ships them, because the vacuum flow interleaves phases requiring different daemon states
(export → backend DOWN → reimport → backend UP) and the container image has no `systemctl`
(rationale: `flake.nix:558-566`).

Host execution needs SurrealDB over HTTP:

- `yadgar/core/cli/vacuum.py:19-23` — `--backend-url` defaults to `$YADGAR_DB_URL`, else
  `http://127.0.0.1:8080`. Vacuum calls `/export` on it.
- `yadgar/core/scripts/nightly_cycle.py:478` — same `$YADGAR_DB_URL` default; consolidation opens
  `StorageEngine` in SERVER mode against it (`nightly_cycle.py:9`, `:291`).
- `scripts/install/launchd/yadgar-vacuum-wrapper.sh:36` and
  `scripts/install/launchd/yadgar-nightly-cycle-wrapper.sh:38` both export
  `YADGAR_DB_URL=http://127.0.0.1:8000`.

Nothing publishes 8000 (or 8080) on surfaces 1, 2, or 5. `grep -rn 8000 scripts/install/` finds it
only in `restore.sh:130` (which spins its OWN temporary container with `-p 127.0.0.1:8000:8000`) and
as the *container-internal* `yadgar-backend:8000` address. No `--network host` / `network_mode:
host` anywhere.

**Therefore:** the macOS surface — cited in the task brief as "the working prior art" — is **not
working**. `yadgar-setup.sh::_step_enable_units:575-593` bootstraps all six plists, so the nightly
and vacuum jobs *do* fire on schedule and *do* fail on connection-refused into
`~/.local/share/yadgar/logs/nightly-cycle.err.log`. This is a second real bug in the same family
and the same one-line-per-surface fix repairs both.

This is the hinge of the scope question (§2): building nightly + vacuum systemd units **without**
publishing 8000 ships units that render, activate, fire, and fail — the exact rendered-≠-works bug
class this train exists to kill.

### 1.3 Bug 3 — activation drift already happened, on macOS

Three install entry points enable units, and they have already diverged:

| Call site | macOS plists activated |
|---|---|
| `Makefile:161-175` (`enable-units-macos`) | 2 of 6 (core + backend only) |
| `Makefile:184-200` (`_enable-units-auto`, what `make setup` calls) | 2 of 6 |
| `scripts/install/yadgar-setup.sh:575-593` (`_step_enable_units`) | 6 of 6 |

So `make setup` on macOS renders six plists and activates two. `yadgar-setup --doctor`
(`yadgar-setup.sh:744-765`) lints all six but only greps `launchctl list` — it does not assert the
four maintenance jobs are loaded, so the gap is invisible.

On Linux all three sites do `systemctl --user enable [--now] yadgar.target`. That installs only
`yadgar.target`'s own `WantedBy=default.target` symlink — it does **not** enable units that merely
declare `WantedBy=timers.target` / `paths.target`. **This is the activation trap named in the
brief**: a `.timer`/`.path` wired that way renders correctly, never activates, and every render
assertion still passes.

### 1.4 The existing anti-recurrence net

`yadgar/tests/scripts/test_vacuum_trigger_cross_generator.py` parametrizes unit generators and
asserts host-projection-of-trigger-path == watched dir, with `_NO_WATCHER_SURFACES` (`:60-76`)
**asserting absence** for declared-no-watcher surfaces. `generate_systemd.sh` is currently in that
allowlist.

The net has a hole this car must close: `test_watcher_bearing_generator_trigger_dir_equals_watched_dir`
asserts render-equality only, and `test_flake_watcher_is_activated_not_merely_rendered` is
flake-specific (it greps `Install.WantedBy = [ "paths.target"` out of flake source). A systemd
watcher that renders correctly and is never activated passes the whole parametrization.
**Acceptance criteria below distinguish RENDERED from ACTIVATED.**

Chain-equivalence precedent for the three enable sites already exists:
`yadgar/tests/scripts/test_v5_46_0_yadgar_setup_chain_equivalence.py:70`
(`test_setup_sh_and_make_agree_on_linger_step`), added by the linger car merged today
(`bb237101`, `12f729a6`).

### 1.5 Host CLI availability (bounds the F1 fail-the-install branch)

Nothing under `scripts/install/` creates a `~/.local/bin/yadgar` shim — the CLI arrives via pipx
(`flake.nix:504` does this for nix; `detect_install_method.sh:34` knows the pipx shape) or from a
repo checkout.

But `make setup` **already** hard-requires an importable `yadgar`: `Makefile:115,119,123,133` run
`python3 -m yadgar install-hooks / install-subagents / config sync / seed`. And `yadgar-setup.sh` is
itself a console script (`pyproject.toml:80`). So the F1 "fail the install if no host CLI resolves"
branch is **nearly unreachable in practice** and is not a new burden on either path — it only fires
in a state where `make setup` was already going to fail three steps later, with a worse message.

---

## 2. Scope verdict: **(A) build the full parity — nightly + vacuum units for non-nix systemd**

With one mandatory addition the brief did not anticipate: **publishing SurrealDB on loopback is a
sub-item of this car, not a follow-up.**

Reasoning:

- **Not (C) "document as unsupported".** The surface is not vestigial: it is what `make setup` and
  the pipx `yadgar-setup` produce, i.e. what every non-nix user gets. Declaring background
  maintenance unsupported there means shipping a memory system that silently stops consolidating —
  the failure is invisible, unbounded, and corrupts the product's core value. If we were unwilling
  to publish 8000, **(C) would have to apply to macOS too** (its jobs fire and fail today), and
  "yadgar has no working background maintenance outside NixOS" is not a documentable position, it
  is a product defect.
- **Not (B) "vacuum only".** Vacuum has the *identical* DB-reachability need as nightly
  (`vacuum.py:19-23` vs `nightly_cycle.py:478` — same env var, same default). Once 8000 is
  published and the host CLI is resolved, nightly is two more templates on plumbing that is
  already paid for. Splitting means doing the shared work and shipping half the value.
- **(A) is bounded.** `flake.nix:568-690` is a working, systemd-native reference for exactly these
  six units. This is transcription plus a port publish plus activation wiring — not design.

**Honest counter-position, stated once:** an argument exists that the container-based non-nix
install should not run host-side maintenance at all, and should instead grow an in-container
scheduler. That is a genuinely different architecture, it contradicts the deliberate host-execution
decision recorded at `flake.nix:558-566`, and it would leave users with no maintenance until it
ships. Not this car. If it is ever built, this car's units are what it replaces — that is a normal
migration, not wasted work.

### 2.1 One car, not two

Nightly and vacuum share: the port publish, the host-CLI detection, the activation wiring, the
uninstall list, the doctor probes, and the same three enable call sites. Splitting means doing the
shared plumbing twice or serializing a dependency for no gain. **One car, phased** — phases are
reviewable checkpoints, not separate branches.

---

## 3. Carried decisions (NOT relitigated here)

- **F1 host-CLI detection.** `generate_systemd.sh` substitutes `@VACUUM_EXEC@` at render time,
  preferring the `~/.local/bin/yadgar` shim, falling back to `python3 -m yadgar`, and **failing the
  install** when neither resolves — rather than failing silently at 4am. (Refined in D3 below to
  *verify* importability rather than assume it; the decision itself stands.)
- **The activation trap.** `[Install] WantedBy=yadgar.target` does not activate a `.path`/`.timer`
  unit, because setup only ever enables `yadgar.target`. Acceptance criteria distinguish RENDERED
  from ACTIVATED (§6).
- **Acceptance gate.** `generate_systemd.sh` MOVES OUT of `_NO_WATCHER_SURFACES` and passes the
  watcher-bearing parametrization.
- **`yadgar/core/daemon/systemd.py` stays in the allowlist.** It mounts a NAMED volume
  (`profiles.py:39`), so a host-side watcher is structurally impossible without a new bind.
  Already guarded by `test_python_systemd_data_mount_is_a_named_volume_not_a_host_path`.
- **The trigger handler removes the trigger file BEFORE starting the runner** (`flake.nix:624-641`),
  so a failed vacuum does not pin the `.path` unit active.

---

## 4. Phases

### Phase 0 — RED tests (step zero)

Write the failing assertions first; every later phase turns one green.

1. `yadgar/tests/scripts/test_vacuum_trigger_cross_generator.py`:
   - Remove `"generate_systemd.sh"` from `_NO_WATCHER_SURFACES` (`:61-67`).
   - Add a `_render_systemd_sh_watcher()` helper returning `(run_cmd, watched_dir)` — the shape the
     watcher-bearing parametrization needs. The existing `_render_systemd_sh` (`:147`) returns
     concatenated-all-files + a path list, which is the **wrong shape**; keep it for the
     no-watcher parametrization, add the new one alongside.
   - Add `"docker-compose.yml"` and `"daemon.py docker-run"` to `_NO_WATCHER_SURFACES` with cited
     reasons (F3). Compose is static YAML (read the file); `daemon.py` needs its command list built
     via its own function, not a subprocess render.
   - Add `test_systemd_watcher_is_activated_not_merely_rendered` (see D2 for what it asserts).
   - Add `test_systemd_trigger_handler_removes_trigger_before_starting_vacuum` — the systemd twin of
     the existing flake assertion at `:242`.
2. New `yadgar/tests/scripts/test_v5_169_maintenance_unit_parity.py`:
   - Backend unit publishes the SurrealDB port on loopback — parametrized over
     `generate_systemd.sh`, `generate_launchd.sh`, `flake.nix` (compose excluded, see D1).
   - Render assertions for the six new systemd units (Type=oneshot, OnCalendar, Persistent,
     TimeoutStartSec, `@VACUUM_EXEC@` fully substituted — no `@...@` token survives).
   - `@VACUUM_EXEC@` resolution: with a fake shim on PATH → shim wins; with none and no importable
     `yadgar` → generator exits non-zero with an actionable message.
3. Extend `test_v5_46_0_yadgar_setup_chain_equivalence.py` with an **activation-parity** test
   asserting the Makefile and `yadgar-setup.sh` agree on which units/plists they activate — the
   anti-recurrence net for §1.3, mirroring `test_setup_sh_and_make_agree_on_linger_step:70`.

### Phase 1 — DB reachability (unblocks everything host-executed)

- `scripts/install/yadgar-backend.service.in:13` — add
  `-p 127.0.0.1:${YADGAR_BACKEND_SURREAL_PORT:-8000}:8000`, rendered via a new
  `@BACKEND_SURREAL_PORT@` token so the port stays overridable (mirrors `cfg.backendSurrealPort`,
  `flake.nix:380`).
- `scripts/install/launchd/com.openfantasy.yadgar-backend.plist.in:49` — same publish. This is the
  fix for the already-broken macOS jobs (§1.2).
- Thread the port into the launchd wrappers' `YADGAR_DB_URL` default (currently hardcoded 8000 at
  `yadgar-vacuum-wrapper.sh:36`, `yadgar-nightly-cycle-wrapper.sh:38`).
- Loopback-only. Same posture the flake has shipped since v5.46.

### Phase 2 — core unit: state-dir bind + trigger env

- `scripts/install/yadgar.service.in` — add `-v @STATE_DIR@:/root/.local/state/yadgar` and
  `-e YADGAR_VACUUM_TRIGGER_PATH=/root/.local/state/yadgar/triggers/vacuum_requested`. The launchd
  core plist already has **both** (`com.openfantasy.yadgar.plist.in:58`); systemd has **neither**.
- `generate_systemd.sh` — add `@STATE_DIR@` to the sed list (`:74-80`), defaulting to
  `${HOME}/.local/state/yadgar`, and `mkdir -p "${STATE_DIR}/triggers"` (mirrors
  `generate_launchd.sh:43,88`).
- **Spell the token identically** on the `-v` left side and in the `.path` unit's `PathExists`.
  `_mount_projection.parse_mounts` returns host tokens **verbatim**, and the cross-generator test
  compares exact strings (`test_vacuum_trigger_cross_generator.py:200-213`). See D2.

### Phase 3 — six new systemd templates

Mirror `flake.nix:568-690` (systemd-native shape), **not** the launchd plists:

| Template | Mirrors | Notes |
|---|---|---|
| `yadgar-vacuum.service.in` | `flake.nix:568` | `Type=oneshot`, `TimeoutStartSec=30min`, `ExecStart=@VACUUM_EXEC@ vacuum --service-mode=systemd --yes` |
| `yadgar-vacuum.timer.in` | `flake.nix:588` | `OnCalendar=Sun *-*-* 04:00:00`, `RandomizedDelaySec=30min`, `Persistent=true` |
| `yadgar-vacuum-trigger.path.in` | `flake.nix:614` | `PathExists=@STATE_DIR@/triggers/vacuum_requested` |
| `yadgar-vacuum-trigger.service.in` | `flake.nix:620` | `rm -f` **before** `systemctl --user start yadgar-vacuum.service`; `systemctl` bare (resolved from unit `$PATH`) per the rationale at `flake.nix:628-638` |
| `yadgar-nightly-cycle.service.in` | `flake.nix:659` | `Type=oneshot`, `TimeoutStartSec=1h`, `Environment=YADGAR_DB_URL=…:@BACKEND_SURREAL_PORT@`, `YADGAR_EMBED_URL=…:8001`, `YADGAR_DATA_DIR=@DATA_DIR@` |
| `yadgar-nightly-cycle.timer.in` | `flake.nix:681` | `OnCalendar=*-*-* 19:00:00 UTC`, `Persistent=true` (see D4) |

- No numpy `LD_LIBRARY_PATH` wrapper: that is a NixOS-store artifact (`flake.nix:544-556`), not
  needed on ordinary distros with a pipx/system python.
- `generate_systemd.sh` renders all six and reports them in its closing summary (`:105-108`).
- `@VACUUM_EXEC@` resolution runs at render time and fails the install when unresolved (D3).
- Worktree-sweep (`com.openfantasy.yadgar-worktree-sweep.plist.in`) is **out of scope**: it sweeps
  the *developer's* git worktrees, not yadgar state, and has no analog in the flake. Named here so
  the omission is a decision, not an oversight.

### Phase 4 — activation, in all three enable sites

See D2 for the mechanism choice. Whichever wins, it must land such that
`Makefile::enable-units`, `Makefile::_enable-units-auto`, and
`yadgar-setup.sh::_step_enable_units` cannot drift again (Phase 0 item 3 is the net).

Also in this phase: bring the two Makefile macOS sites up to the six plists that
`yadgar-setup.sh:575-593` already bootstraps (§1.3).

### Phase 5 — removal + observability

- `scripts/install/uninstall.sh:102` — the Linux unit list is hardcoded to three units; add the
  six new ones plus a `systemctl --user disable --now` for the timers/path. The macOS list
  (`:51-58`) is hardcoded to two plists and already omits the four maintenance plists — fold that
  in, same net. **Leave the deliberate linger asymmetry comment (`:83-91`) alone.**
- `yadgar-setup.sh::_run_doctor:765-771` — the Linux branch probes only `yadgar.target` status +
  linger. Add timer/path state probes (`systemctl --user list-timers 'yadgar-*'`,
  `is-active yadgar-vacuum-trigger.path`) so a never-activated unit is **visible**. Add the
  equivalent `launchctl print` probe on the macOS branch for the four maintenance jobs.
- README / install docs: note the loopback SurrealDB publish and the maintenance schedule.

---

## 5. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **Publishing 8000 on loopback widens exposure.** SurrealDB root creds live in `secrets.env`; anything on the host can now reach the DB HTTP API. | Loopback-only bind (`127.0.0.1:`), exactly the flake's shipped posture since v5.46. Port overridable via `@BACKEND_SURREAL_PORT@` for conflicts. Call it out in the release note — this is a real posture change for existing non-nix users, even if it matches precedent. |
| R2 | **Port 8000 is commonly occupied.** A user's dev server may already hold it; backend start then fails where it previously succeeded. | `@BACKEND_SURREAL_PORT@` token + `YADGAR_BACKEND_SURREAL_PORT` env. Doctor probe reports the bind failure explicitly rather than leaving a dead backend. |
| R3 | **New scheduling behaviour on an existing install base.** Users who ran `make setup` months ago and upgrade will suddenly get a nightly job at 19:00 UTC and a weekly vacuum. First nightly on a large never-consolidated DB may run long. | `TimeoutStartSec=1h` bounds it (matches flake). `Persistent=true` means a missed run fires at next boot — expected, but worth the release note. Document how to mask: `systemctl --user mask yadgar-nightly-cycle.timer`. |
| R4 | **Vacuum stops the backend.** `--service-mode=systemd` stops/starts `yadgar-backend` mid-run; an MCP session live at 04:00 loses its connection. | Pre-existing behaviour on flake/launchd, not introduced here. Weekly Sunday 04:00 + `RandomizedDelaySec=30min` is the least-bad window. Note in docs. |
| R5 | **Render-vs-activate false green.** The whole point of §1.3; easy to re-introduce. | Phase 0 activation assertions + the chain-equivalence parity test. No phase is done until its ACTIVATED criterion is green. |
| R6 | **`@VACUUM_EXEC@` resolves to a python that later loses the package** (venv deleted, pipx reinstall changes path). Units then fail at 4am — the exact failure F1 wants to avoid, displaced in time. | Fail-loud at render is the primary guard. Secondary: the doctor probe should execute `@VACUUM_EXEC@ --version` and report, so a broken CLI surfaces at `--doctor` rather than at 4am. Add to Phase 5. |
| R7 | **Token-spelling mismatch** between the `-v` bind and `PathExists` silently breaks the invariant after Nix-style string comparison passes locally. | The cross-generator test compares exact strings by construction; D2 makes the spelling a written decision rather than an implementation detail. |
| R8 | **macOS regression risk.** Phase 1 changes a surface the team cannot easily test. | The change is one `-p` flag mirroring a shipped flake line, plus a wrapper default already hardcoded to that port. `plutil -lint` runs in `generate_launchd.sh:157-165`. Flag as `[manual]` — needs a macOS host before merge, or an explicit "untested on macOS" release note. |

---

## 6. Acceptance criteria

### Rendered

- `[unit]` `generate_systemd.sh` renders nine files: the existing three plus
  `yadgar-vacuum.service`, `yadgar-vacuum.timer`, `yadgar-vacuum-trigger.path`,
  `yadgar-vacuum-trigger.service`, `yadgar-nightly-cycle.service`, `yadgar-nightly-cycle.timer`.
- `[unit]` No `@TOKEN@` placeholder survives in any rendered unit (including `@VACUUM_EXEC@`,
  `@STATE_DIR@`, `@BACKEND_SURREAL_PORT@`).
- `[unit]` `yadgar-backend.service`, `com.openfantasy.yadgar-backend.plist`, and the flake backend
  unit each publish the SurrealDB port on `127.0.0.1`.
- `[unit]` `yadgar.service` mounts the host state dir and sets `-e YADGAR_VACUUM_TRIGGER_PATH` under
  that bind.
- `[unit]` **The gate:** `generate_systemd.sh` is no longer in `_NO_WATCHER_SURFACES` and passes
  `test_watcher_bearing_generator_trigger_dir_equals_watched_dir` — host projection of the trigger
  path equals `yadgar-vacuum-trigger.path`'s watched dir, compared as exact strings.
- `[unit]` `docker-compose.yml` and the `daemon.py` docker-run dev path are in
  `_NO_WATCHER_SURFACES` with cited reasons and pass the assert-absence parametrization (F3).
- `[unit]` The systemd trigger handler removes the trigger file **before** starting
  `yadgar-vacuum.service`.
- `[unit]` `@VACUUM_EXEC@` prefers the shim, falls back to a *verified-importable* `python3 -m
  yadgar`, and exits non-zero with an actionable message when neither resolves.

### Activated (distinct from rendered — the trap)

- `[unit]` Whatever mechanism D2 selects is asserted statically: either `yadgar.target` `Wants=`
  every maintenance unit, or all three enable call sites `enable --now` each of them. Asserting the
  `[Install]` stanza alone does **not** satisfy this criterion.
- `[unit]` Makefile and `yadgar-setup.sh` activate the **same** set of units (Linux) and plists
  (macOS) — the §1.3 drift cannot recur.
- `[e2e]` After `make setup` on a Linux host: `systemctl --user list-timers` lists
  `yadgar-vacuum.timer` and `yadgar-nightly-cycle.timer` with a real `NEXT`, and
  `systemctl --user is-active yadgar-vacuum-trigger.path` returns `active`.
- `[unit]` `uninstall.sh`'s hardcoded Linux unit list (`:102`) and macOS plist list (`:51-58`) each
  cover **every** unit/plist their generator renders — asserted by comparing the literal lists to
  the generator's rendered output, so the two cannot drift. (Cheap, and it catches the drift class
  directly rather than only at e2e time.)
- `[e2e]` `bash scripts/install/uninstall.sh` leaves no `yadgar-*` unit in the output dir, no
  `yadgar-*` timer in `list-timers`, and no leftover persistent-timer stamp under
  `~/.local/share/systemd/timers/`.
- `[e2e]` **D2's assumption, observed:** with `yadgar-nightly-cycle.timer` activated only via
  `yadgar.target`'s `Wants=` (no enablement symlink), a deliberately-missed `OnCalendar` window is
  caught up on next target start — i.e. `Persistent=true` still works. Failure here flips D2.

### Works

- `[e2e]` With the stack up, `curl -sf http://127.0.0.1:<surreal-port>/health` succeeds from the
  host — the reachability Phase 1 exists to provide.
- `[manual]` `touch ~/.local/state/yadgar/triggers/vacuum_requested` → `yadgar-vacuum.service` runs
  (visible in `journalctl --user -u yadgar-vacuum`) and the trigger file is gone.
- `[manual]` MCP `vacuum_now()` on a repo Linux install returns `started=True` **and** a vacuum
  actually completes — no longer the `no_trigger_path_configured` refusal
  (`yadgar/core/ops/ops.py:208`).
- `[manual]` `systemctl --user start yadgar-nightly-cycle.service` exits 0 and a consolidation-log
  row appears.
- `[manual]` macOS: nightly/vacuum jobs no longer connection-refuse in
  `~/.local/share/yadgar/logs/nightly-cycle.err.log`. **Requires a macOS host** (R8).

---

## 7. Open decisions

### D1 — Publish SurrealDB on loopback? (the A-vs-C hinge)

**Recommend: YES**, on `generate_systemd.sh` and `generate_launchd.sh`, loopback-only, port
overridable. It is the flake's own shipped precedent (`flake.nix:380`), and without it every unit
this car adds is a scheduled failure.

Explicitly **excluded**: `docker-compose.yml`. Compose is the dev/CI surface, has no maintenance
units, and is a declared-no-watcher surface under F3 — adding a publish there buys nothing and
widens exposure in CI.

If this is rejected, the honest fallback is **(C) for systemd AND launchd** — not (B) — and this
car becomes "document the surface as unsupported for background maintenance, and remove or mask the
four macOS maintenance plists so they stop firing and failing."

### D2 — Activation mechanism: `Wants=` on the target, or explicit per-unit `enable --now`?

| | `Wants=` in `yadgar.target.in` | Explicit `enable --now` at each site |
|---|---|---|
| Sites to change | **1** (the template) | **3** (Makefile ×2, `yadgar-setup.sh`) |
| Drift risk | none — single source of truth | high — already drifted once (§1.3) |
| `systemctl is-enabled <timer>` | reports `disabled` (confusing; doctor must probe `is-active`/`list-timers`) | reports `enabled` |
| Behaviour if a user starts `yadgar.service` directly instead of the target | timers do not come up | unaffected |
| Existing enable calls | need **zero** change | need editing in three places |

**Recommend `Wants=` on `yadgar.target`** — it composes core + backend there already
(`yadgar.target.in:3`), and one site cannot drift. Keep an `[Install] WantedBy=timers.target` /
`paths.target` stanza on each unit for manual-enable convenience, but the **test asserts the
`Wants=` line**, because that is the actual activation mechanism.

*Dissent recorded:* the advisor review leaned to explicit `enable --now` on the grounds that it is
what a test can assert. Both are statically assertable; the tiebreaker is that the three-site
version has already failed in this exact codebase, on macOS, and the one-site version structurally
cannot. **Decide before Phase 3** — it changes what the templates' `[Install]` sections mean.

**Load-bearing assumption, evidenced but not yet observed:** `Wants=` is a *start-time* dependency,
not an enablement symlink — so does a timer pulled in that way still honour `Persistent=true`
catch-up for a missed window? `man systemd.timer` says the stamp is consulted "when the timer is
**activated**" (activation, not enablement), and the stamp lives under
`~/.local/share/systemd/timers/` as timer-unit machinery independent of the enablement symlink. So
the answer should be yes. That is primary-source evidence, not an observation: it is carried as an
explicit `[e2e]` acceptance criterion (§6). **If it fails in practice, D2 flips to explicit
`enable --now` at all three sites and the dissent above is wrong** — verify before Phase 4 closes.

Related, for Phase 5: `man systemd.timer` recommends `systemctl clean --what=state` on a timer unit
before uninstalling it, to remove the persistent timestamp file. `uninstall.sh` should do that for
both timers.

### D3 — `@VACUUM_EXEC@` resolution order, and the fail-the-install branch

Carrying F1's decision. Refinement: resolution must **verify** rather than assume, because
`python3 -m yadgar` fails on any python without the package installed:

1. `$YADGAR_HOST_CLI` env override (escape hatch for odd layouts).
2. `~/.local/bin/yadgar` if executable (the pipx shape, per F1's stated preference).
3. `command -v yadgar` (brew, other prefixes).
4. `python3 -c 'import yadgar'` succeeds → `python3 -m yadgar`.
5. Otherwise **exit non-zero** with a message naming `pipx install yadgar` as the fix.

Per §1.5 the fail branch is nearly unreachable: `make setup` already requires
`python3 -m yadgar` at four steps (`Makefile:115,119,123,133`), so branch 4 succeeds wherever
`make setup` was going to succeed anyway.

**Open:** should there be a `YADGAR_SKIP_MAINTENANCE_UNITS=1` opt-out? It reintroduces the exact
silent-no-maintenance state this car fixes, but explicitly and by user choice. **Recommend: no**,
for now — the fail message is actionable and the branch is nearly unreachable. Revisit if a real
user hits it.

### D4 — Nightly schedule: 19:00 UTC or 19:00 local?

`flake.nix:684` uses `OnCalendar=*-*-* 19:00:00 UTC`. The launchd plist uses **local** time with a
comment admitting it is a compromise (`com.openfantasy.yadgar-nightly-cycle.plist.in:20-27` —
launchd's `StartCalendarInterval` has no UTC option). Vacuum: flake `Sun *-*-* 04:00:00` is
**local** (`flake.nix:591`) while the launchd comment at
`com.openfantasy.yadgar-vacuum.plist.in:22` claims the nix side is UTC — that comment is stale.

**Recommend:** mirror the flake exactly — nightly `19:00 UTC`, vacuum `Sun 04:00` local — so the
two systemd surfaces are byte-comparable, and fix the stale launchd comment while we are in there.
One line each, but it is a real consistency call and someone will ask.

### D5 — State-dir token spelling: `@STATE_DIR@` or systemd's `%h`?

Both work and both compare exactly under `parse_mounts` (host tokens are returned verbatim,
`_mount_projection.py:20-34`).

- `@STATE_DIR@` — symmetric with the existing `@DATA_DIR@` and with launchd's `@YADGAR_HOME@`;
  lets `generate_systemd.sh` `mkdir -p` the triggers dir at render time; overridable in tests.
- `%h` — no new sed token; systemd expands it in both `ExecStart` and `PathExists`.

**Recommend `@STATE_DIR@`** for symmetry with the sibling generator and because the generator needs
the concrete path anyway to pre-create `triggers/`. Whichever is chosen, it must be spelled
**identically** in the `-v` left side and in `PathExists=` (R7).

---

## 8. Out of scope (decisions, not oversights)

- `yadgar/core/daemon/systemd.py` — stays in `_NO_WATCHER_SURFACES` (named volume, `profiles.py:39`).
- `docker-compose.yml` and the `daemon.py` docker-run dev path — gain **test coverage** (F3) as
  declared-no-watcher surfaces, but no units and no port publish.
- Worktree-sweep on systemd — sweeps developer git worktrees, not yadgar state; no flake analog.
- In-container scheduling as an alternative architecture — see §2.
- The private nix module at `/home/max/git/nix` — read-only, out of repo. If any change there is
  implied, it is a hand-off, not part of this car.
- The linger install/uninstall asymmetry (`uninstall.sh:83-91`) — deliberate, decided today.
