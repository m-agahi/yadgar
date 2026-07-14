# macOS launchd Port — Survey & Design Document

**Status:** Draft for review — no plists committed yet
**Branch:** `feat/macos-launchd-port`
**Date:** 2026-06-07
**Scope:** Survey + design answers for porting the 7 systemd unit groups in `~/git/nix/modules/home/yadgar.nix` (lines 333–618) to launchd LaunchAgents.

---

## Part 1 — Current Coverage Inventory

### Systemd unit catalog (from yadgar.nix lines 333–618)

Ten logical entities (counting timer pairs individually), plus one non-unit activation block:

| # | Systemd entity | Type | Existing plist? | Notes |
|---|----------------|------|-----------------|-------|
| 1 | `yadgar-backend.service` | service (daemon) | **Yes** — `com.openfantasy.yadgar-backend.plist.in` | Mostly complete; missing ExecStartPre pre-flight, MemoryHigh, full env passthrough |
| 2 | `yadgar.service` | service (daemon) | **Yes** — `com.openfantasy.yadgar.plist.in` | Mostly complete; same gaps as backend |
| 3 | `yadgar-vacuum.service` | oneshot service | **No** | Host-exec; needs env-file wrapper |
| 4 | `yadgar-vacuum.timer` | timer | **No** | Sunday 04:00; `Persistent=true` — needs `StartCalendarInterval` |
| 5 | `yadgar-nightly-cycle.service` | oneshot service | **No** | Host-exec; needs LD_LIBRARY_PATH wrapper for numpy .so |
| 6 | `yadgar-nightly-cycle.timer` | timer | **No** | Daily 19:00 UTC; `Persistent=true` |
| 7 | `yadgar-vacuum-trigger.path` | path unit | **No** | Watches `~/.yadgar/triggers/vacuum_requested` |
| 8 | `yadgar-vacuum-trigger.service` | oneshot service | **No** | Removes trigger file, then launches vacuum |
| 9 | `yadgar-worktree-sweep.service` | oneshot service | **No** | Runs `cleanup-merged-branches.sh` |
| 10 | `yadgar-worktree-sweep.timer` | timer | **No** | Monday 03:30; `Persistent=true` |
| — | `yadgar-secrets` activation | home-manager activation hook (not a unit) | **No** | `op` inject at install time; launchd analog is a standalone install-time script |

**Coverage: 2 / 10 units = 20%**

### What the existing plists do well
- `com.openfantasy.yadgar.plist.in` — correct Label, ProcessType=Background, RunAtLoad=true, KeepAlive=true, log paths, PATH including both `/opt/homebrew/bin` and `/usr/local/bin`.
- `com.openfantasy.yadgar-backend.plist.in` — same skeleton; backend-specific ports and image reference; idempotent `docker network create yadgar-net` in the command string.

### What the existing plists are missing
- No `KeepAlive` dict (`SuccessfulExit=false`); they use bare `KeepAlive=true` which relaunches on any exit including clean. Correct for daemons; document the distinction for oneshot units.
- No `ExecStartPre` equivalent; the pre-flight docker stop/rm is embedded in the `/bin/sh -c` string, which is fine for the daemon case.
- No `ThrottleInterval` — equivalent to `RestartSec`. Core uses `RestartSec=10`; a 10-second throttle should be added.
- Missing `DOCKER_HOST` variable. On NixOS, podman's socket lives at `/run/podman/podman.sock` and is set in `DOCKER_HOST`. On macOS with podman-machine the env var is not needed (podman CLI finds the socket via XPC/socket path), but it should be documented that this is intentionally absent.

---

## Part 2 — Systemd → launchd Field Mapping

| systemd field | launchd equivalent | Caveats / notes |
|---|---|---|
| `ExecStart` | `ProgramArguments` (array) | launchd takes a true argv array; no shell expansion. Use `["/bin/sh", "-c", "..."]` when shell features (pipes, semicolons, env-file sourcing) are needed. |
| `ExecStartPre=-cmd` (ignore failure) | Embed in the `/bin/sh -c` string before the main command with `|| true` | No native PreStart; `-` prefix (ignore failure) maps to `\|\| true` in the shell wrapper. |
| `ExecStop` | No direct equivalent | launchd sends SIGTERM to the process group on unload. For docker/podman containers, use `AbandonProcessGroup=false` (default) so launchd kills the `/bin/sh` wrapper, then use an ExitTimeout (see `ExitTimeoutEnabled`). A post-stop hook requires a second plist or a wrapper that traps SIGTERM and runs `docker stop`. |
| `EnvironmentFile=-path` | **Two distinct approaches** depending on execution context: (1) Container jobs (yadgar, yadgar-backend): pass `--env-file path` as a `docker`/`podman` runtime flag — the container runtime reads the file natively, no launchd involvement needed. (2) Host-exec jobs (vacuum, nightly-cycle, worktree-sweep): the launchd job must be a wrapper script that does `set -a; source secrets.env; set +a; exec yadgar ...`. | launchd has no native EnvironmentFile key. The two-tier approach avoids duplicating all env vars in every plist's `EnvironmentVariables` block. |
| `Restart=on-failure` + `RestartSec=N` | `KeepAlive` dict: `<dict><key>SuccessfulExit</key><false/></dict>` + `ThrottleInterval` integer (seconds) | Bare `<true/>` relaunches on clean exit too. Use the dict form for daemons. For oneshot timer services set `KeepAlive=<false/>` (no relaunch — timer drives it). |
| `Type=simple` | No equivalent — launchd tracks the launched PID directly | Default launchd behavior is analogous to `Type=simple`: launchd considers the job running until the PID exits. |
| `Type=oneshot` | No `RunAtLoad` + `KeepAlive=false` | launchd has no oneshot type. Use `RunAtLoad=false`, `KeepAlive=false`. The job runs when its trigger fires (timer or WatchPaths) and exits; launchd does not relaunch. |
| `After=X.service` | No native ordering mechanism | launchd loads all LaunchAgents simultaneously. There is no `After=` / `Requires=`. See Q6 for the retry-budget strategy. |
| `Wants=X.service` | No equivalent | Document-only: both plists use `RunAtLoad=true`; user-space agent load order is nondeterministic. Core already retries backend connection on startup. |
| `OnCalendar=Sun *-*-* 04:00:00` | `StartCalendarInterval` dict: `<dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>` | Weekday 0 = Sunday in Apple's convention (0–7, both 0 and 7 accepted as Sunday). Month, Day, Hour, Minute keys. No seconds key. |
| `OnCalendar=*-*-* 19:00:00 UTC` | `StartCalendarInterval` hour/minute matching UTC hour | launchd `StartCalendarInterval` uses **local time**, not UTC. On macOS the system timezone is user-settable. Options: (1) document the time zone assumption; (2) use a wrapper that computes UTC offset at runtime; (3) recommend users set their Mac to UTC if running a headless server. This is a genuine portability gap. |
| `RandomizedDelaySec=30min` | No native equivalent | Implement in the wrapper script via `sleep $((RANDOM % 1800))` before exec. |
| `Persistent=true` | No direct equivalent | `Persistent=true` means "catch up on missed runs". launchd `StartCalendarInterval` fires on next wake after a missed window, which is the same behavior in practice. Document: if the Mac was off during the scheduled window, the job fires on next login (launchd reloads agents at login). |
| `TimeoutStartSec=N` | `ExitTimeoutEnabled=true` + behavior is job-type-dependent; there is no generic `TimeoutStartSec` | For oneshot host-exec jobs, set a time limit in the wrapper script (`timeout 1800 yadgar vacuum ...`). |
| `[Path] PathExists=file` | `WatchPaths` array: `<array><string>/path/to/file</string></array>` | `WatchPaths` fires on any filesystem change at or under the watched path, including directory stat changes. It does **not** guarantee the file exists at fire time — see Q5 for wrapper design. |
| `systemctl --user start X` (from trigger service) | `launchctl kickstart gui/$UID/com.openfantasy.yadgar-vacuum` | The trigger service equivalent must call `launchctl kickstart` instead of `systemctl start`. |
| `MemoryHigh=Ng` | No equivalent in launchd plist | macOS enforces memory limits at the process level via `setrlimit` or Xcode entitlements; not settable in a launchd plist for user agents. Omit. The `--memory` flag on `docker`/`podman run` still applies inside the container. |
| Home Manager activation (`yadgarSecrets`) | Standalone install-time script called by `yadgar-setup.sh` | See Q4. |

**Apple docs reference:** `man launchd.plist` — the authoritative source for all keys. Online: https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html

---

## Part 3 — Design Questions

### Q1: Container Runtime

**Recommendation:** Auto-detect at install time via the existing `detect_runtime.sh`, with runtime preference order `podman > docker`. Bake the resolved runtime path into the generated plists at generation time (current approach, correct). Do not auto-detect at plist launch time — detection logic inside a launchd job adds fragility and slows cold start.

**Rationale:** The existing `generate_launchd.sh` already resolves `$YADGAR_RUNTIME` at generation time and substitutes it into the plist via sed. This is the right model. The plist contains an absolute binary path (e.g., `/opt/homebrew/bin/podman`) rather than relying on PATH lookup at job start time.

**Tradeoffs/caveats:**
- Homebrew podman on Apple Silicon: `/opt/homebrew/bin/podman`
- Homebrew podman on Intel: `/usr/local/bin/podman`
- OrbStack: installs `docker` + `podman` CLIs at `/usr/local/bin/`; socket path differs from standard podman-machine.
- Docker Desktop: installs `docker` at `/usr/local/bin/docker`; socket at `~/.docker/run/docker.sock`.
- The existing PATH in the plists (`/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`) covers all three runtime providers. Prefer absolute path in `ProgramArguments[0]`, keep PATH as fallback for child process resolution.

**Flagged risk:** If the user switches from Docker Desktop to OrbStack (or vice versa) after initial setup, the baked-in runtime path in the plist becomes wrong. `yadgar-setup --doctor` should detect and re-generate plists on runtime mismatch. Not implemented today; flag as a follow-up.

---

### Q2: Path Layout — `~/.yadgar/` vs `~/Library/Application Support/yadgar/`

**Recommendation:** Keep `~/.yadgar/` as the primary data directory on macOS. Do NOT migrate to `~/Library/Application Support/yadgar/`.

**Rationale:**
- Existing Linux and NixOS installations all use `~/.yadgar/`. Changing the default on macOS would break cross-platform users (e.g., someone who uses the same dotfiles on macOS and a Linux VM).
- The `yadgar.nix` module, the existing `.plist.in` files, `generate_launchd.sh`, and `yadgar-setup.sh` all reference `${HOME}/.yadgar` as `YADGAR_INSTALL_PREFIX`. Changing this default on macOS only creates a divergence that must be papered over everywhere.
- `~/Library/Application Support/` is the Apple-blessed location for user-facing app data, but yadgar is a developer tool. The hidden-dotfile convention (`~/.yadgar/`) is standard for CLI daemon tools and does not conflict with any macOS policy.

**Log directory exception:** `~/Library/Logs/yadgar/` is the right location for log files on macOS (already done in the existing `.plist.in` files). macOS Console.app and `log show` can discover logs here. This is correct and should not change.

**Tradeoffs/caveats:**
- Spotlight will index `~/Library/Application Support/` by default; `~/.yadgar/` may be excluded depending on user `.gitignore`-equivalent exclusion rules. For a large SurrealKV database this is desirable — excluding `~/.yadgar/surreal_db/` from Spotlight is a recommended post-install step.
- Time Machine by default backs up `~/Library/` more reliably than dotfiles. Document the manual exclusion of `~/.yadgar/surreal_db/` from Time Machine to prevent multi-GB backups.

---

### Q3: Secrets Management

**Recommendation:** File-based approach first (`~/.config/yadgar/secrets.env` or `~/.yadgar/secrets.env`). macOS Keychain integration via the `security` CLI is a later epic.

**Rationale:**
- The existing design uses `EnvironmentFile=~/.config/yadgar/secrets.env` on Linux. The macOS port should use the same file path — the existing `bootstrap_secrets.sh` already writes this file.
- For container jobs: `--env-file ~/.yadgar/secrets.env` is passed to `docker`/`podman run`. The container runtime reads the file; no launchd involvement.
- For host-exec jobs (vacuum, nightly-cycle): a wrapper script sources the env file before exec (see Q5 wrapper design below).

**Keychain deferred because:**
- `security find-generic-password` returns one value at a time; there is no batch read for an entire env-file's worth of variables without iteration over every key.
- Adding Keychain access requires either a code-signed app bundle (for GUI access) or accepting a user-confirmation dialog on first access — both undesirable for a daemon that restarts at boot.
- The file-based approach with `chmod 600` and `chown $USER` is sufficiently secure for a personal developer tool.

**Flagged risk:** The secrets file lives at `~/.yadgar/secrets.env` in the current plist templates but at `~/.config/yadgar/secrets.env` in `yadgar.nix`. Unify the path before shipping. Recommended canonical: `~/.config/yadgar/secrets.env` (XDG-compatible on both Linux and macOS).

---

### Q4: `op inject` Activation Hooks

**Recommendation:** Implement as a standalone install-time script called from `yadgar-setup.sh`, parallel to the existing `yadgar-secrets` home-manager activation block. Do not attempt to run `op inject` from inside a launchd job.

**Rationale:**
1Password CLI on macOS stores its session token in `~/Library/Group Containers/2BUA8C4S2C.com.1password/Library/Application Support/1Password/Data/B5.sqlite` (the user's claim). The `op` CLI on macOS authenticates via the 1Password desktop app's biometric/touch ID flow, not a detached token file. A launchd agent running at boot may not have access to the authenticated session (desktop app not yet unlocked).

**Recommended implementation:**
```bash
# In yadgar-setup.sh, after bootstrap_secrets:
if command -v op &>/dev/null; then
    op inject -i ~/.config/yadgar/secrets.env.tpl \
              -o ~/.config/yadgar/secrets.env
    chmod 600 ~/.config/yadgar/secrets.env
fi
```

This runs interactively during setup (biometric prompt available), writes the resolved secrets file, and then the launchd agents read the static file at runtime without needing `op` access.

**Tradeoffs/caveats:**
- Secrets go stale if rotated in 1Password without re-running `yadgar-setup`. Add a re-inject step to the `--doctor` flow and a note in the post-install summary.
- The template file (`secrets.env.tpl`) is the artifact to ship; the resolved `secrets.env` is `.gitignore`'d.

---

### Q5: Trigger-File `.path` Unit → `WatchPaths` Wrapper

**Background:** The systemd trigger pattern (yadgar.nix lines 545–567) works as follows:
1. MCP `vacuum_now()` writes `~/.yadgar/triggers/vacuum_requested`
2. `.path` unit `PathExists=...` fires → starts `yadgar-vacuum-trigger.service`
3. Trigger service: `rm -f ...vacuum_requested` → `systemctl --user start yadgar-vacuum.service`

**launchd translation:**

`WatchPaths` on `~/.yadgar/triggers/vacuum_requested` fires on any filesystem change at that path (creation, modification, stat change). It does NOT guarantee the file exists at fire time — the plist job may fire due to a directory-level mtime update, or may fire while a previous invocation is still removing the file.

**Wrapper script flowchart:**

```
yadgar-vacuum-trigger-wrapper.sh
│
├─ Check: does ~/.yadgar/triggers/vacuum_requested exist?
│   └─ NO → exit 0  (spurious fire or already handled by prior instance)
│
├─ Remove trigger file atomically: mv vacuum_requested vacuum_requested.handling
│   └─ If mv fails (race with concurrent invocation): exit 0
│
├─ Verify: mv succeeded and vacuum_requested.handling exists
│
├─ Kick vacuum job:
│   launchctl kickstart gui/$UID/com.openfantasy.yadgar-vacuum
│
├─ Remove handling marker: rm -f vacuum_requested.handling
│
└─ exit 0
```

**Key design decisions:**
- Use `mv` (atomic rename on the same filesystem) rather than `rm -f` to claim the trigger — prevents two wrapper instances both seeing the file and both kicking vacuum.
- Remove `.handling` marker at the end. If the wrapper crashes after `mv` but before cleanup, the marker persists. Add a cleanup check at wrapper start: if `.handling` exists and is older than 10 minutes, remove it and re-kick vacuum (or log a warning).
- `launchctl kickstart gui/$UID/com.openfantasy.yadgar-vacuum` starts the job regardless of whether it was previously loaded. Idempotent if job is already running (launchd returns error, wrapper ignores it with `|| true`).

**WatchPaths plist fragment:**
```xml
<key>WatchPaths</key>
<array>
    <string>/Users/USERNAME/.yadgar/triggers</string>
</array>
```
Watch the directory (not the file) — the file is created anew each time, and `WatchPaths` on a non-existent file path may not fire on creation.

---

### Q6: Service Ordering (No `After=` in launchd)

**Recommendation:** Option (b) — rely on yadgar-core's existing retry-on-connect loop, which already handles backend transient unavailability.

**Rationale:**
The systemd `After=yadgar-backend.service` + `Wants=yadgar-backend.service` in yadgar.nix was weakened to `Wants=` in v5.3.9 (yadgar.nix lines 377–418) specifically because hard coupling caused OOMKill cascades. The core already retries the SurrealDB HTTP connection at startup. Logs show `yadgar-core: Waiting for backend embed service...` during cold start — the retry budget is already implemented.

launchd loads all LaunchAgents simultaneously at login. Both plists use `RunAtLoad=true`. The race is: core starts, attempts HTTP to backend at `http://yadgar-backend:8000`, backend may not yet accept connections. Core retries every N seconds until backend is ready. This is identical to the systemd situation after the v5.3.9 decoupling.

**Document clearly:**
- No ordering guarantee; backend typically wins the race because it's a simpler process (SurrealDB + embeddings start faster than the Python ASGI server).
- If the race becomes observable on macOS (e.g., slower podman-machine startup), the only safe mitigation is increasing core's retry budget, not adding artificial `sleep` in the plist.
- `ThrottleInterval` on core's `KeepAlive` dict should be 10s (matches `RestartSec=10` in yadgar.nix line 414) so rapid restart loops after a backend failure don't thrash.

---

### Q7: Install UX

**Recommendation:** Extend the existing `yadgar-setup.sh`, do NOT create a separate `macos-install.sh`.

**Rationale:**
`yadgar-setup.sh` already has:
- `_detect_os()` returning `"macos"`
- A `macos` branch in `_step_generate_units()` that calls `generate_launchd.sh`
- A `macos` branch in `_step_enable_units()` that calls `launchctl bootstrap gui/$UID`
- A `macos` branch in `_run_doctor()` that runs `plutil -lint` + `launchctl list | grep`
- `--doctor` flag already documented in `--help` output

The macOS branch is structurally complete for the two existing daemon plists. Extending to cover the 8 new units means:
1. Adding 5 new `.plist.in` templates to `scripts/install/launchd/`
2. Extending `generate_launchd.sh` to render them
3. Extending `_step_enable_units()` to loop over all 8 plist labels
4. Extending `_run_doctor()` to check all 8 agents via `launchctl list`

No new setup script needed.

**Flagged risk:** The `_wait_for_daemon()` function in `yadgar-setup.sh` (lines 563–601) shows a comment "macOS: daemon auto-start via launchctl deferred to v5.46.16+". This is stale — auto-start is already wired in `_step_enable_units()`. The comment should be removed to avoid confusion.

---

## Part 4 — Flagged Gaps (Not in Original Suggestion)

### Gap 1: LaunchAgent vs LaunchDaemon

The existing plists are correctly placed in `~/Library/LaunchAgents/` (per-user LaunchAgents, not system LaunchDaemons). This is the **only correct choice** for podman/docker socket access on macOS:
- `podman-machine` runs in user space. The UNIX socket is at `~/.local/share/containers/podman/machine/qemu/podman.sock` (or similar per-version path).
- Docker Desktop similarly exposes `~/.docker/run/docker.sock` in user space.
- A LaunchDaemon (system context, root user) would run before user login and have no access to either socket. It would also be unable to read `~/Library/LaunchAgents/` — wrong directory entirely.

**Document explicitly:** All yadgar launchd jobs are LaunchAgents (user context). This is not negotiable for container socket access.

### Gap 2: ARM64 (Apple Silicon) vs x86_64 (Intel)

The yadgar container images (`openfantasy/yadgar`, `openfantasy/yadgar-backend`) are **amd64-only** — there are no ARM64 native images. On Apple Silicon Macs (M1/M2/M3), Docker Desktop and OrbStack both include Rosetta 2 / emulation layers that transparently run amd64 images, but:
- Performance is reduced (CPU emulation overhead).
- `podman-machine` on Apple Silicon creates a QEMU VM running amd64 Linux — image compatibility is automatic but startup time is longer.
- OrbStack on Apple Silicon uses hardware virtualization (Hypervisor.framework) + Rosetta for amd64 binaries — most compatible option on Apple Silicon.

**Recommended action:** Add an ARM64 detection step in `detect_runtime.sh` or `yadgar-setup.sh` that emits a warning when running on ARM64 with amd64-only images. This does not block functionality but sets correct expectations on performance.

### Gap 3: macOS Sleep/Wake and Missed Timer Fires

`StartCalendarInterval` behaves as follows when the Mac is asleep at the scheduled time:
- The job fires on the next wake **that occurs after the missed scheduled time**.
- If multiple scheduled times were missed (e.g., Mac was off all weekend), only **one** catch-up fire occurs.

This is documented Apple behavior and is functionally equivalent to systemd's `Persistent=true` — which also fires once on next boot for missed runs. The key difference: systemd `Persistent=true` + `RandomizedDelaySec` fires within a random window after boot, reducing thundering-herd on startup. launchd fires at the next calendar-matched time, which may mean the job doesn't run until the next scheduled window.

**Document:** For vacuum (Sunday 04:00) and nightly-cycle (19:00): if the Mac is routinely asleep at those times, the jobs will never run. Recommend users set a wake timer (`pmset schedule wake ...`) or accept that scheduled maintenance runs only when the Mac is awake at the target hour.

### Gap 4: Log Routing

On Linux with systemd, logs go to the journal (`journalctl --user -u yadgar.service`). On macOS, launchd routes `stdout`/`stderr` to the files specified in `StandardOutPath`/`StandardErrorPath` — the existing plists write to `~/Library/Logs/yadgar/`. This is correct.

However:
- `log show --predicate 'process == "docker"'` will show OCI runtime events but not application logs.
- The `log stream` facility tracks ASL/os_log entries, not file-based logs.
- The equivalent of `journalctl -f` on macOS is `tail -f ~/Library/Logs/yadgar/core.err.log`.

**Recommendation:** Add a `yadgar logs` subcommand (or document the tail command) in the `--doctor` output for macOS. This is a follow-up CLI task, not a blocker for launchd port.

### Gap 5: macOS Notarization / Gatekeeper

The install scripts (`yadgar-setup.sh`, `generate_launchd.sh`, wrapper scripts) are shell scripts launched by the user — Gatekeeper does not apply to shell scripts directly. The yadgar container images are OCI images, not app bundles — Gatekeeper does not apply to them either. No notarization required for the current approach.

However, if yadgar ever ships a `.app` bundle, a `pkg` installer, or a signed binary, Gatekeeper and notarization become relevant. Out of scope for this port, but document for future packaging work.

### Gap 6: LD_LIBRARY_PATH Equivalent (nightly-cycle wrapper)

The nix module writes a wrapper script (`yadgar-nightly-cycle-wrapper.sh`) that prepends `${pkgs.stdenv.cc.cc.lib}/lib` to `LD_LIBRARY_PATH` before exec'ing `yadgar-nightly-cycle`. This fixes numpy `.so` loading on NixOS where the Nix store path is not in the system linker search path.

On macOS:
- The equivalent is `DYLD_LIBRARY_PATH` (dynamic linker variable on Darwin).
- macOS System Integrity Protection (SIP) strips `DYLD_LIBRARY_PATH` from processes that are launched with elevated privileges or that are SIP-protected. LaunchAgents run as the user and are not SIP-protected — `DYLD_LIBRARY_PATH` is honored.
- **However:** if yadgar is installed via Homebrew, the numpy `.so` files are linked against the Homebrew-managed libstdc++/libc++ which is in `/opt/homebrew/lib` — already in the linker search path. No wrapper fixup is needed in that case.
- If installed via pip with a non-Homebrew Python, the `.dylib` dependencies may not be on the search path. The host-exec wrapper for nightly-cycle should include: `export DYLD_LIBRARY_PATH="/opt/homebrew/lib:${DYLD_LIBRARY_PATH:-}"`.

---

## Part 5 — Remaining Gaps to Implement (Scope for v5.47.x–v5.49.x)

| Item | Priority | Estimated complexity |
|------|----------|----------------------|
| 5 new `.plist.in` templates (vacuum, vacuum-timer, nightly-cycle, nightly-cycle-timer, worktree-sweep + worktree-sweep-timer, vacuum-trigger-path + vacuum-trigger-service) | High | Medium |
| Wrapper scripts for host-exec jobs (env-file source + exec) | High | Low |
| Extend `generate_launchd.sh` to render all 8 new plists | High | Low |
| Extend `_step_enable_units()` macOS branch to bootstrap all 8 | High | Low |
| Extend `_run_doctor()` macOS branch to check all 8 | Medium | Low |
| ARM64 warning in `detect_runtime.sh` | Medium | Low |
| Secrets path unification (`~/.config/yadgar/secrets.env` canonical) | Medium | Low |
| `op inject` install-time script | Low | Low |
| macOS keychain integration | Low (later epic) | High |
| `yadgar logs` macOS subcommand or doctor output | Low | Low |
| Time Machine exclusion guidance in post-install output | Low | Trivial |
| StartCalendarInterval UTC timezone guidance | Medium | Trivial |

---

## Appendix A — yadgar-vacuum Plist Sketch (NOT for commit — review first)

This is a design sketch for `com.openfantasy.yadgar-vacuum.plist`. It is the simplest of the 8 missing units: a timer-driven oneshot that calls `yadgar vacuum` on the host. The sketch is inline here for review before any template files are written.

Key design decisions baked in:
1. **Host execution, not container** — vacuum orchestrates start/stop of Docker containers itself; it needs `systemctl` (or `launchctl` on macOS) in scope.
2. **Wrapper script** — secrets.env must be sourced before exec; launchd has no `EnvironmentFile` key for host-exec jobs.
3. **No RunAtLoad, no KeepAlive** — timer fires the job; it must not auto-launch at agent load or auto-restart after completion.
4. **No AbandonProcessGroup** — default (false); allows cleanup of any child processes if the wrapper is killed.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- Unique reverse-DNS label. Must match the filename (minus .plist). -->
    <key>Label</key>
    <string>com.openfantasy.yadgar-vacuum</string>

    <!-- Background process type — no GUI, no Aqua. -->
    <key>ProcessType</key>
    <string>Background</string>

    <!-- Do NOT run at agent load — timer drives this job. -->
    <!-- RunAtLoad is omitted (defaults to false). Explicit false shown for clarity. -->
    <key>RunAtLoad</key>
    <false/>

    <!-- Do NOT keep alive after exit — this is a oneshot, not a daemon. -->
    <!-- Bare <false/> (not the KeepAlive dict) is correct here: no relaunch ever. -->
    <key>KeepAlive</key>
    <false/>

    <!--
        Timer schedule: Sunday at 04:00 local time.
        Matches systemd OnCalendar=Sun *-*-* 04:00:00.
        Weekday 0 = Sunday (Apple convention; 7 is also accepted as Sunday).
        NOTE: This fires in LOCAL TIME, not UTC. If the host uses UTC as its
        timezone this matches the nix module. Otherwise the effective fire time
        shifts by the UTC offset. See Q6 in design doc for mitigation options.
    -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>   <!-- 0 = Sunday -->
        <key>Hour</key>
        <integer>4</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <!--
        Working directory: yadgar data dir.
        Vacuum writes temp files relative to this path.
        Sed-substituted from ${YADGAR_INSTALL_PREFIX} at generate time.
    -->
    <key>WorkingDirectory</key>
    <string>${YADGAR_INSTALL_PREFIX}</string>

    <!--
        ProgramArguments: invoke the wrapper script.
        The wrapper (yadgar-vacuum-wrapper.sh) does:
            set -a
            source ${YADGAR_SECRETS_ENV_FILE}
            set +a
            export YADGAR_DB_URL=http://127.0.0.1:8000
            export YADGAR_DATA_DIR=${YADGAR_INSTALL_PREFIX}
            exec ~/.local/bin/yadgar vacuum --service-mode=launchd --yes
        Direct-array form (no /bin/sh -c shell) is preferred for wrappers
        because the wrapper itself handles all shell logic.
    -->
    <key>ProgramArguments</key>
    <array>
        <!-- Absolute path to wrapper — sed-substituted at generate time. -->
        <string>${YADGAR_SCRIPTS_DIR}/yadgar-vacuum-wrapper.sh</string>
    </array>

    <!--
        PATH: must include homebrew bin dirs for docker/podman, plus standard bins.
        Intel Mac: /usr/local/bin. Apple Silicon: /opt/homebrew/bin.
        ~/.local/bin for the pipx-installed yadgar binary.
    -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${YADGAR_HOME}/.local/bin</string>
    </dict>

    <!--
        Log paths: vacuum can take up to 30 minutes; log to file for post-hoc inspection.
        Both stdout and stderr routed here.
        Sed-substituted from ${YADGAR_HOME} at generate time.
    -->
    <key>StandardOutPath</key>
    <string>${YADGAR_HOME}/Library/Logs/yadgar/vacuum.out.log</string>

    <key>StandardErrorPath</key>
    <string>${YADGAR_HOME}/Library/Logs/yadgar/vacuum.err.log</string>

    <!--
        SoftResourceLimits: cap CPU time at 2 hours (vacuum timeout guard).
        Matches systemd TimeoutStartSec=30min plus buffer for retries.
        NOTE: launchd enforces this via RLIMIT_CPU — applies to user-space CPU
        time only, not wall clock. Actual wall-clock timeout must be in the wrapper.
    -->
    <key>SoftResourceLimits</key>
    <dict>
        <key>CPUTime</key>
        <integer>7200</integer>
    </dict>

</dict>
</plist>
```

**Companion wrapper script (NOT a committed file — sketch only):**

```bash
#!/usr/bin/env bash
# yadgar-vacuum-wrapper.sh — launchd wrapper for yadgar vacuum (oneshot, host-exec).
# Sources secrets.env before exec; launchd has no native EnvironmentFile support
# for host-exec jobs. This wrapper is the launchd analog of systemd's EnvironmentFile=.
#
# Wall-clock timeout: 1800s (30 min) matches systemd TimeoutStartSec=30min.
# Wrapper uses `timeout` to enforce wall-clock limit; launchd SoftResourceLimits
# only counts CPU time, not I/O-heavy operations like SurrealDB export/import.

set -euo pipefail

SECRETS_ENV="${YADGAR_SECRETS_ENV_FILE:-${HOME}/.config/yadgar/secrets.env}"

if [ -f "$SECRETS_ENV" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$SECRETS_ENV"
    set +a
fi

export YADGAR_DB_URL="${YADGAR_DB_URL:-http://127.0.0.1:8000}"
export YADGAR_DATA_DIR="${YADGAR_DATA_DIR:-${HOME}/.yadgar}"

exec timeout 1800 "${HOME}/.local/bin/yadgar" vacuum --service-mode=launchd --yes
```

**Review questions for user:**
1. `--service-mode=launchd` — does the vacuum command support a `launchd` mode, or should this be `--service-mode=manual` (or omitted)? The systemd mode calls `systemctl`; on macOS `launchctl kickstart` is the analog. If vacuum's service-mode dispatch is not yet macOS-aware, `--service-mode=manual` with wrapper-controlled start/stop may be needed.
2. The `YADGAR_HOME` substitution in `ProgramArguments` PATH — this is a plist template token, not a shell variable. It must be sed-substituted at generation time, not resolved at launch time. Confirm `generate_launchd.sh` substitutes `${YADGAR_HOME}` → `/Users/username` for the PATH string.
3. `SoftResourceLimits.CPUTime` — valid key in user LaunchAgent context? Some resource limit keys are only honored in LaunchDaemon context. If it fails `plutil -lint`, remove it; the wrapper's `timeout` is the real guard.

---

*End of document — submit appendix review before generating remaining 7 plist templates.*
