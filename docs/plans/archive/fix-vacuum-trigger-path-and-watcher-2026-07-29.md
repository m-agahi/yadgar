# Fix vacuum trigger path + missing watcher (cross-surface coherence)

**Date:** 2026-07-29
**Task:** #0044 — vacuum trigger path contradicts the XDG-state watcher; non-nix systemd ships no watcher at all.
**Status:** PLANNED — awaiting decisions D1–D4 before implementation.
**Target train:** `feat/v5.169-install-runtime-fixes` (ONE car).
**Deferred out of:** PR #235 (mac launchd + flake trigger-path fix).
**Precedent:** `docs/plans/archive/fix-systemd-generate-missing-queue-base-2026-07-28.md` (task:0076 — the `/data` vs `/queue-data` split). Read that first; this is the same failure class.

---

## 1. Observed state (verified, cited)

### 1.1 The writer

`vacuum_now()` (`yadgar/core/server/tools/admin_vacuum.py:13`) calls `_fire_vacuum_service()`
(`yadgar/core/ops/ops.py:171`), which resolves the path at `ops.py:188`:

```python
trigger_path = Path(os.environ.get("YADGAR_VACUUM_TRIGGER_PATH", _DEFAULT_VACUUM_TRIGGER_PATH))
```

with `_DEFAULT_VACUUM_TRIGGER_PATH = "/data/triggers/vacuum_requested"` at `ops.py:167`.
`ops.py:199` does `mkdir(parents=True, exist_ok=True)` — so the write **always succeeds** and
`vacuum_now()` returns `{"started": True, ...}` regardless of whether anything is watching.
That is the silent-no-op mechanism.

The same trigger fires from the auto-vacuum backstop
(`ConsolidationScheduler._maybe_auto_vacuum()`, per `docs/contracts/CAPABILITY_REGISTRY.md:1995`).

### 1.2 The registry entry — display-only, NOT the runtime default

`yadgar/_shared/config/config_registry.py:235`:

```python
ConfigEntry("YADGAR_VACUUM_TRIGGER_PATH", "/data/triggers/vacuum_requested", "string"),
```

The registry's own docstring (`config_registry.py:1-25`) states it is consumed by
`GET /admin/config`, the `event="startup.config"` dump, and the `yadgar_config_value` gauge, and
that *"the existing `os.getenv()` call sites are intentionally NOT refactored."* `config_registry.py`
never writes `os.environ` (only reads it, `:143`).

**Consequence:** there are **two** independently-declared defaults for one knob. They agree with each
other (`/data/triggers/vacuum_requested`) and disagree with the XDG-state design. Changing only the
registry changes what `/admin/config` *reports* — it does not change one byte of daemon behaviour.
The task's premise "the registry default should resolve via XDG_STATE_HOME" is therefore **half
wrong**: the registry is not the load-bearing default, and (see §3) an XDG-derived default is the
wrong shape anyway.

### 1.3 Surface matrix (the actual asymmetry)

| # | Surface | Renderer | Host state-dir mount | `-e YADGAR_VACUUM_TRIGGER_PATH` | vacuum runner unit | trigger watcher | Result |
|---|---|---|---|---|---|---|---|
| 1 | **non-nix systemd (Linux)** | `scripts/install/generate_systemd.sh` + `yadgar.service.in` | **NO** | **NO** | **NO** | **NO** | Writes `/data/triggers/…` → host `~/.local/share/yadgar/triggers/`. Nothing watches; nothing to start even if it did. |
| 2 | **repo flake.nix** | `flake.nix:432-460`, `:563-591` | YES `:444` | YES `:455` | YES (`yadgar-vacuum.service` `:563` + weekly timer `:583`) | **NO** — `grep systemd.user.paths flake.nix` → zero hits | Path is coherent but **inert**; the comment at `flake.nix:450-454` admits it. Weekly timer still runs. |
| 3 | **launchd (macOS)** | `scripts/install/generate_launchd.sh` | YES (`com.openfantasy.yadgar.plist.in:58`) | YES (same line) | YES (`com.openfantasy.yadgar-vacuum.plist.in`) | YES — `WatchPaths` on `~/.local/state/yadgar/triggers` (`com.openfantasy.yadgar-vacuum-trigger.plist.in:29-32`) | **WORKS.** Tested. |
| 4 | **Python systemd generator** | `yadgar/core/daemon/systemd.py:108-142` | **NO** — core mounts `-v {profile.volume_name}:/data` (`:130`), and `volume_name` is `os.environ.get("YADGAR_VOLUME", "yadgar-data")` (`profiles.py:39`) — a **named volume**, never a host path | **NO** | **NO** | **NO** | Host watcher on `/data/triggers` is **physically impossible**; the same named volume is the shared queue volume (mounted `/queue-data` on the backend, `systemd.py:85`). |
| 5 | private nix (out of repo, reference only) | `/home/max/git/nix/modules/home/yadgar.nix` | YES `:552` | YES `:552` | YES `:624` | YES — `systemd.user.paths.yadgar-vacuum-trigger` `:751`, `PathExists` `:754`, handler `:759-770` | **WORKS.** |

Surface 4 was not named in the task brief. It is a fifth generator and it is the one where the
"just resolve XDG" fix is provably unimplementable.

### 1.4 Container-vs-host split (the trap)

- `/data` is the **data** volume (host `~/.local/share/yadgar`, or a *named docker volume* on surface 4).
- The XDG **state** dir is a separate bind — `-v $HOME/.local/state/yadgar:/root/.local/state/yadgar`
  — present only on surfaces 2, 3, 5.
- All core units run `--user root` (`yadgar.service.in:15`, `systemd.py:129`, `flake.nix:435`,
  `com.openfantasy.yadgar.plist.in:58`), so container `HOME=/root` and an XDG-derived path resolves to
  `/root/.local/state/yadgar/triggers`. The image itself declares `USER 1001` (`Dockerfile:21`)
  with `useradd -m` → `HOME=/home/yadgar`; the XDG derivation is therefore **coupled to every unit
  keeping `--user root`**. One surface dropping that flag silently relocates the trigger.

### 1.5 Existing test coverage

- `yadgar/tests/core/test_macos_launchd_plists.py:264-345` — `TestVacuumTriggerPathConsistency`
  already implements exactly the invariant this task asks for (parse `-v` mounts, project the
  container trigger path to the host, assert it equals the watcher's `WatchPaths[0]`). **launchd only.**
- `yadgar/tests/core/test_systemd_unit_template.py` — five tests, **zero** vacuum/trigger assertions.
- `yadgar/tests/scripts/test_backend_unit_queue_base_cross_generator.py` — the task:0076 anti-drift
  net; parametrized across three generators, asserts an env value is an actual `-v host:target`.
  **This is the structural template for the new test.**
- Nothing asserts anything about `flake.nix`.

---

## 2. Diagnosis

Two symptoms, one defect: **the trigger path is declared in code and the watcher is declared per
surface, and nothing forces the two to agree.** (a) alone is cosmetic (§1.2) or actively harmful
(§3). (b) alone leaves a lying default in two files. **This is ONE car**, and the deliverable is
the invariant, not the two edits.

Scope bound, taken from the task's own wording: *the invariant binds any surface that ships both a
writer and a watcher.* Surfaces that ship a daemon but deliberately ship no watcher must say so
explicitly, so the test asserts the absence rather than passing silently.

---

## 3. Design: why NOT an XDG-derived default

The obvious fix — make `ops.py:167` resolve `paths.TRIGGERS_DIR` — is wrong:

1. **It only helps surfaces already fixed.** Inside the container it yields
   `/root/.local/state/yadgar/triggers`, which is correct on surfaces 2/3/5 — but those three
   already pass an explicit `-e`, so they gain nothing.
2. **It regresses surface 1.** `yadgar.service.in` has no state mount. Today the trigger lands on
   the host at `~/.local/share/yadgar/triggers/` (unwatched, but persisted and inspectable).
   After the change it lands on a container-internal path that **vanishes with `--rm`**. Strictly
   worse.
3. **It is unimplementable on surface 4.** `-v {profile.volume_name}:/data` (`systemd.py:130`) is a
   named volume — `volume_name` is `os.environ.get("YADGAR_VOLUME", "yadgar-data")`
   (`profiles.py:39`), never a host path. No host path exists to watch.
4. **It is coupled to `--user root`** (§1.4) — a derived default whose value depends on a flag in
   a different file is exactly the drift this plan exists to kill.

**Recommended shape: the env var is explicit per surface; the code default becomes non-load-bearing
and honest.** Every surface that ships a watcher sets `-e YADGAR_VACUUM_TRIGGER_PATH` to a path
under a bind mount it also declares. The default is what happens when nobody configured a watcher.

This is the `/data` vs `/queue-data` lesson from task:0076: the correct answer differs per surface,
and the anti-recurrence mechanism is a cross-generator test, not a unified constant.

---

## 4. Car scope

**One car**, on the live train `feat/v5.169-install-runtime-fixes`.

### 4.1 Phases

**P0 — RED tests (TDD, per repo rules).**
New `yadgar/tests/scripts/test_vacuum_trigger_cross_generator.py`, modelled on
`test_backend_unit_queue_base_cross_generator.py`. Parametrized over every in-repo generator.
Per generator, assert either:
- **watcher-bearing:** (i) `-e YADGAR_VACUUM_TRIGGER_PATH` is set, (ii) its value is under a
  `-v <host>:<target>` bind in the same rendered unit, (iii) the host projection's **directory**
  equals the watched dir of the watcher unit *rendered by the same generator*; or
- **declared-no-watcher:** the generator is in an explicit `_NO_WATCHER_SURFACES` allowlist with a
  cited reason, and the test asserts it renders **no** watcher unit (so a half-fix fails loudly).

Reuse the mount-parsing + projection helpers from `test_macos_launchd_plists.py:280-345` — extract
them to a shared helper rather than copy-paste. `flake.nix` is in-repo and text-assertable
(regex the `-e`, the `-v`, and the `.path` unit's `PathExists`).

**P1 — non-nix systemd gets the whole vacuum surface** (the bulk of the work):
- `scripts/install/yadgar.service.in` — add `-v @STATE_DIR@:/root/.local/state/yadgar` and
  `-e YADGAR_VACUUM_TRIGGER_PATH=/root/.local/state/yadgar/triggers/vacuum_requested`.
- New `scripts/install/yadgar-vacuum.service.in` — the runner (see D2 for ExecStart).
- New `scripts/install/yadgar-vacuum.path.in` + handler, mirroring private nix
  `yadgar.nix:751-770` (`PathExists` → handler removes the file *then* starts the runner, so a
  failed vacuum does not pin the path unit active).
- `scripts/install/generate_systemd.sh` — render the three new templates; add `@STATE_DIR@` to the
  `sed` substitution set (`:75-83`); `mkdir -p` the triggers dir alongside the existing
  `upgrade.env` seeding (`:95-105`), matching `generate_launchd.sh:91`.
- **`scripts/install/yadgar.target.in` — the `.path` unit must actually be pulled in.**
  Rendering the watcher is not enabling it. `yadgar.target.in` currently reads
  `Wants=yadgar.service yadgar-backend.service` only, and both the setup flow
  (`yadgar-setup.sh:_step_enable_units`) and `Makefile:136-138` enable **only `yadgar.target`**.
  A `[Install] WantedBy=yadgar.target` on the `.path` unit creates the `yadgar.target.wants/`
  symlink *only* when `systemctl --user enable yadgar-vacuum.path` is run — which nothing does.
  Left as-is the watcher renders, never activates, every unit test passes, and manual criterion 11
  fails. **This is the exact silent-half-fix shape this car exists to kill.**
  **Recommended:** add `yadgar-vacuum.path` to `Wants=` / `After=` in `yadgar.target.in` **and**
  assert it in the tests (criterion 5b below). Enabling it explicitly in `_step_enable_units` is
  the alternative; do one, not neither.
- `scripts/install/yadgar-setup.sh` — add the new units to `_run_doctor` (`~:703-726`); the macOS
  branches already list their equivalents. Add to `_step_enable_units` (`~:511-525`) only if the
  target-Wants route above is *not* taken.
- `Makefile` — `clean` target (`:248-252`) must remove the new units.
- `scripts/install/uninstall.sh` — verify it removes the new units.

**P2 — repo flake.nix gets the missing `.path` watcher.**
Add `systemd.user.paths.yadgar-vacuum-trigger` + the handler service, mirroring `yadgar.nix:751-770`.
The mount and `-e` already exist (`flake.nix:444`, `:455`); delete the now-false
"currently inert here" comment at `flake.nix:450-454`.

**P3 — make the code defaults honest.**
- `yadgar/core/ops/ops.py:167` — keep or fail-loud per **D1**; update the docstring at `:174-175`
  which currently claims "a systemd path-watch unit on the host watches the file", true on 3 of 5
  surfaces.
- `yadgar/_shared/config/config_registry.py:235` — align with whatever D1 picks, so
  `/admin/config` stops reporting a value the daemon may not use.
- `yadgar/core/server/tools/admin_vacuum.py:18-19` — same docstring correction.

**P4 — docs.**
`docs/contracts/CAPABILITY_REGISTRY.md:1504` and `:1995` both describe the watcher as if it exists
everywhere. Correct both. CHANGELOG entry. If D1 picks the fail-loud option, that is a behaviour
change and needs a `BEHAVIOR_CONTRACT.md` line.

### 4.2 File seam vs. the live train

Cars already merged into `feat/v5.169-install-runtime-fixes` touch `yadgar/core/daemon/{daemon,profiles,runtime}.py`,
`yadgar/core/cli/{daemon,setup,install}.py`, `yadgar/core/install/*`, `flake.nix`,
`scripts/install/yadgar-backend.service.in`, `scripts/install/launchd/com.openfantasy.yadgar-backend.plist.in`.

| File | Train status | Verdict |
|---|---|---|
| `scripts/install/yadgar.service.in` | untouched | clean |
| `scripts/install/yadgar.target.in` | untouched | clean — **required**, see P1 (`.path` activation) |
| `scripts/install/generate_systemd.sh` | untouched | clean |
| `scripts/install/yadgar-setup.sh` | untouched | clean |
| `scripts/install/yadgar-vacuum.{service,path}.in` | new files | clean |
| `yadgar/core/ops/ops.py` | untouched | clean |
| `yadgar/_shared/config/config_registry.py` | untouched | clean |
| `Makefile`, `scripts/install/uninstall.sh` | untouched | clean |
| **`flake.nix`** | **TOUCHED by the train** | **conflict risk** — P2 adds a new top-level `systemd.user.paths` block; rebase before the P2 edit. Merge-order issue, not a blocker. |
| `yadgar/core/daemon/systemd.py` | **directory owned by train**, file itself untouched | see **D3**; prefer deferring the edit and covering it via the allowlist instead. |

`scripts/install/` overlap is therefore **narrower than feared**: the train's only two files there
are both *backend* artifacts; this car touches only *core* + new vacuum artifacts.

---

## 5. Acceptance criteria

**[unit]**
1. `test_vacuum_trigger_cross_generator.py` — for each watcher-bearing generator, the rendered
   `YADGAR_VACUUM_TRIGGER_PATH`'s host projection dir **equals** the watcher unit's watched dir.
   *This is the anti-recurrence mechanism.*
2. Same test — each declared-no-watcher generator renders no watcher unit AND appears in the
   allowlist with a cited reason. A generator that grows a half-fix (watcher, no env; or env, no
   mount) fails.
3. `generate_systemd.sh` renders `yadgar-vacuum.service`, `yadgar-vacuum.path`, and the handler,
   with no unsubstituted `@…@` placeholders remaining.
4. `yadgar.service.in` renders a `-v <host>:/root/.local/state/yadgar` bind and an
   `-e YADGAR_VACUUM_TRIGGER_PATH` under it.
5. `flake.nix` contains a `systemd.user.paths.yadgar-vacuum-trigger` whose `PathExists` equals the
   host projection of its own `-e` value.
5b. **Activation, not just rendering:** the rendered `yadgar.target` `Wants=` the rendered
   `yadgar-vacuum.path` (or `_step_enable_units` explicitly enables it). Criterion 3 passes on a
   watcher that never activates — this is the criterion that catches that.
6. Existing `TestVacuumTriggerPathConsistency` (launchd) still green after helper extraction.
7. `config_registry.py:235` default == `ops.py:167` default (single-source or assert-equal).

**[e2e]**
8. Rendered `.path` + `.service` + `.target` pass `systemd-analyze verify` (skip-if-absent, matching
   the `plutil -lint` skip pattern in `generate_launchd.sh:18-20`).
9. Existing install e2e still green: `yadgar/tests/scripts/test_cli_setup_module.py`,
   `test_v5_45_1_launchd_render.py`, `test_backend_unit_queue_base_cross_generator.py`.

**[manual]**
10. Fresh non-nix Linux install → `vacuum_now(force=True)` → `journalctl --user -u yadgar-vacuum`
    shows the run. Today: nothing.
11. `systemctl --user list-units 'yadgar-vacuum*'` shows `.path` active after `make setup`.
12. `make clean` + `uninstall.sh` remove the new units (no orphans).

---

## 6. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | XDG-derived default silently relocates the trigger if a surface drops `--user root` (§1.4). | D1 recommendation avoids derived defaults entirely; test asserts the literal rendered value. |
| R2 | `flake.nix` merge conflict with the train. | Rebase before P2; the edit is an additive top-level block. |
| R3 | No host `yadgar` CLI on the `make setup` path (§D2) → `yadgar-vacuum.service` ExecStart fails at runtime, not at render, so tests pass and the user is still broken. | D2 must be decided before P1. Add an `ExecStartPre` existence check that fails loudly. |
| R4 | `.path` unit fires repeatedly if the handler fails to remove the trigger. | Mirror private nix `yadgar.nix:765-770` ordering: remove file **before** starting the runner. |
| R5 | Enabling vacuum on a surface that never had it could vacuum a fresh install unexpectedly. | The 200 MiB floor (`admin_vacuum.py:48-56`) already gates it. No new timer in this car. |
| R6 | Surface 4 (`systemd.py`) named volume is **shared with the queue** (`profiles.py:39` → `/queue-data` on the backend `systemd.py:85`, `/data` on the core `:130`); a future "just add the mount" fix breaks the layout the queue-base fix just settled. | Cover via the allowlist (D3), do not touch the mount in this car. |
| R8 | The `.path` unit renders but never activates (see P1). | Criterion 5b + manual criterion 11. Do not rely on `[Install] WantedBy` alone. |
| R7 | Scope creep into a full vacuum-scheduling redesign (timers, nightly cycle parity). | Explicitly out of scope: this car ships **coherence**, not new scheduling. |

---

## 7. Open decisions for the user

**D1 — What is the code default when no surface configured a watcher?**
- (i) Keep `/data/triggers/vacuum_requested`, document it as container-internal and inert unless
  overridden. Zero behaviour change; the lie moves to a comment.
- (ii) **RECOMMENDED** — `_fire_vacuum_service()` fails loud when `YADGAR_VACUUM_TRIGGER_PATH` is
  unset, so `vacuum_now()` returns `started: False, skipped_reason: "no_trigger_path_configured"`
  instead of `started: True` into a void. Turns the silent no-op into a diagnosable one. Costs a
  `BEHAVIOR_CONTRACT` line and a `test_vacuum_now.py` update.
- (iii) XDG-derived default — **not recommended**, see §3.

**D2 — `ExecStart` for the new `yadgar-vacuum.service` on non-nix Linux.**
The repo has two Linux install flows with *different* host-CLI stories:
`yadgar-setup.sh` (pipx/brew/nix) resolves a `~/.local/bin/yadgar` shim, but `make setup` uses
`python3 -m yadgar …` (`Makefile:103,107,111,121`) and never guarantees the shim exists.
flake.nix (`:578`) and launchd (`yadgar-vacuum-wrapper.sh:40`) both hardcode `~/.local/bin/yadgar`.
- (i) `%h/.local/bin/yadgar vacuum --service-mode=systemd --yes` — matches flake + launchd; breaks
  for `make setup` users without pipx.
- (ii) `/usr/bin/env python3 -m yadgar vacuum …` — matches `make setup`; diverges from the other
  two surfaces.
- (iii) **RECOMMENDED** — `generate_systemd.sh` detects the host CLI at render time (shim first,
  `python3 -m yadgar` fallback), substitutes `@VACUUM_EXEC@`, and **fails the render** if neither
  resolves. Fails at install time, not silently at 4am.

**D3 — Surface 4 (`yadgar/core/daemon/systemd.py`, named `/data` volume).**
Verified: `profile.volume_name` = `os.environ.get("YADGAR_VOLUME", "yadgar-data")`
(`profiles.py:39`) — always a named volume, never a host path, and the *same* volume the backend
mounts at `/queue-data` (`systemd.py:85`).
- (i) **RECOMMENDED** — defer; add it to `_NO_WATCHER_SURFACES` with the named-volume reason cited.
  The test then asserts it ships no watcher, so a future half-fix fails. Avoids touching a
  train-owned directory.
- (ii) Add a host bind + full vacuum units now — larger blast radius, collides with train cars,
  and interacts with the `/queue-data` layout the queue-base fix just settled.

**D4 — Does the non-nix systemd surface also get the weekly `yadgar-vacuum.timer`?**
flake.nix (`:583`) and private nix (`:669`) both ship one; launchd ships
`com.openfantasy.yadgar-vacuum.plist`. Non-nix systemd has none.
- (i) **RECOMMENDED** — no. This car ships the trigger→watcher path only; a timer is new scheduling
  behaviour on an existing install base. File as a follow-up.
- (ii) Ship it for parity — larger diff, changes runtime behaviour for existing users.

---

## 8. Explicitly out of scope

- The private nix module (`/home/max/git/nix`) — out of repo, already correct, read-only here.
- Vacuum scheduling/timer redesign (see D4).
- `yadgar-nightly-cycle` parity across surfaces.
- Refactoring `config_registry` into the live resolution path (its docstring says that is a
  deliberate future PR).
