# PLAN — v5.45.0: Setup Foundation (Makefile + Interactive Installer + Multi-OS Daemons)

**Status:** drafted 2026-05-31. Plan-first per I27.

**Audit lineage:** identified during v5.41-set viz-planning sweep + post-v5.25.0 setup audit. Current installer (`scripts/setup.sh`) hard-codes `docker` and assumes Linux + systemd. `yadgar/cli/setup.py` is a fragmented config-only wrapper. NixOS-managed installs work; everything else degrades silently.

**Ships in train:** v5.45.0 → v5.46.0 → v5.47.0 (foundation → distribution → updates). User-locked sequence; ships BEFORE v5.50/v5.51/v5.52 viz.

**Pipeline insertion:** between v5.26.0 (benchmark Phase 2 QA) and v5.27.0 (DuckDB). Shifts existing pipeline:
- DuckDB v5.27 → v5.49
- Bi-temporal v5.29 → v5.??? (pipeline renumber pending user decision; not committed in this plan)

**Effort estimate:** 2–3 calendar days.

**Downstream:**
- v5.46.0 (`PLAN_V5_46_0_DISTRIBUTION.md`) packages on top of v5.45 install layout.
- v5.47.0 (`PLAN_V5_47_0_UPDATE_MECHANISM.md`) depends on v5.45 install-method detection.

See also `docs/DECISIONS.md` — 2026-05-31 PD-37 (setup mechanism decision).

---

## Goal — portable `make setup` / `make uninstall` with interactive installer

Replace the NixOS-specific `scripts/setup.sh` and fragmented `yadgar/cli/setup.py` with a portable, OS-aware installer that:

1. Detects container runtime (podman / docker / others) at runtime — no hardcoded `/run/current-system/sw/bin/docker`.
2. Detects host OS (Linux / macOS / others) and selects daemon mechanism (systemd / launchd / none).
3. Provides a single entry-point UX: `pipx install yadgar; yadgar install`.
4. Provides `make setup` / `make uninstall` / `make uninstall-purge` for repo-checkout users.
5. Refuses to overwrite an existing NixOS-managed install — suggests using v5.46 nix flake instead.

The `install` subcommand is **interactive** — asks systemd-vs-compose, pulls container, creates hooks, optionally generates units. No curl-pipe-sh attack surface.

---

## Non-goals (explicit)

- **No PyPI metadata polish.** That's v5.46.0.
- **No Homebrew tap, no Nix flake.** v5.46.0.
- **No release automation, no SBOM.** v5.46.0.
- **No `yadgar update` subcommand, no auto-check.** v5.47.0.
- **No Control-tab Update button.** v5.47.0 ships HTTP API; v5.50 wires UI.
- **No version bump cadence policy.** Bump script is delivered in v5.46 (single-source-of-truth scope).
- **No new container image variants.** Existing `openfantasy/yadgar` + `openfantasy/yadgar-backend` are sufficient.
- **No new env knobs.** Existing config.yaml + I25 sync rules unchanged.

---

## Current state (verified from code, 2026-05-31)

| Component | Path | Status | Gap for v5.45 |
|---|---|---|---|
| Bash installer | `scripts/setup.sh` (433 lines) | Linux-only; hardcodes `docker = command -v docker`; writes systemd user units inline. Works on NixOS. | Remove NixOS hardcoding (lines 41, 244, 277). Extract systemd unit generation. Add OS gating. |
| CLI setup | `yadgar/cli/setup.py` (123 lines) | Writes config.yaml + secrets.env + prints MCP snippet. No container pull, no daemon install, no hook install. | Promote to interactive installer entry point; delegate to scripts/install/ shell helpers. |
| Daemon check | `yadgar/daemon.py:check_docker()` | Already detects docker via `command -v` + `docker info`. | Generalize to runtime detection (podman first, then docker, then others). |
| Hooks installer | MCP `install_hooks` tool + `yadgar/install_hooks_lib.py` | Production-ready; installs Claude Code hooks idempotently. | Makefile target delegates to this; do not re-implement. |
| systemd units | hardcoded heredocs in `scripts/setup.sh:238-302` | Linux-only. Hardcoded `${DOCKER}` path. No `yadgar.target` group. | Extract to template files in `scripts/install/`. Generate from sh. Add `yadgar.target` aggregating yadgar.service + yadgar-backend.service. |
| macOS daemon | none | No launchd plists exist. | Add `scripts/install/launchd/` skeleton + interactive prompt to install. |
| NixOS detection | none | Installer would clobber `/home/max/.local/pipx/venvs/yadgar` if user had nix-managed install with same path. | Detect `/etc/NIXOS` or `command -v nixos-version`; refuse + suggest nix flake (v5.46). |
| Data preservation | `make uninstall` does not exist | n/a | New: preserve `~/.yadgar/` by default; `make uninstall-purge` for full wipe. |
| Makefile | none at repo root | n/a | New: top-level Makefile. |

---

## Scope — concrete file changes

### New files

| Path | Purpose |
|---|---|
| `Makefile` (repo root) | Top-level targets: `setup`, `uninstall`, `uninstall-purge`, `install-hooks`, `clean`, `check`. Thin wrapper — calls `scripts/install/*.sh` for OS-specific work; delegates hook install to `yadgar install-hooks` (MCP tool wrapper). |
| `scripts/install/detect_runtime.sh` | Emits `podman` / `docker` / `none` on stdout. Probes via `command -v` + `<runtime> info` health check. Honors `YADGAR_CONTAINER_RUNTIME` override if set. |
| `scripts/install/detect_os.sh` | Emits `linux` / `macos` / `other` + daemon-capability hint (`systemd` / `launchd` / `none`). Linux detection checks `/etc/os-release` + `command -v systemctl`. NixOS detection: emits `linux-nixos` if `/etc/NIXOS` exists OR `command -v nixos-version` succeeds. |
| `scripts/install/generate_systemd.sh` | Renders `yadgar.service`, `yadgar-backend.service`, `yadgar.target` from `.in` templates. Substitutes runtime path, image tag, data dir, secrets-env path. Writes to `~/.config/systemd/user/`. Reloads systemd user manager. |
| `scripts/install/generate_launchd.sh` | Renders LaunchAgent plists from `.in` templates. Writes to `~/Library/LaunchAgents/`. Loads via `launchctl load`. macOS only. |
| `scripts/install/yadgar.target.in` | systemd target template: `Wants=yadgar.service yadgar-backend.service`. Lets `systemctl --user start yadgar.target` bring up the whole stack. |
| `scripts/install/yadgar.service.in` | systemd unit template; placeholders for `@RUNTIME@`, `@IMAGE@`, `@DATA_DIR@`. |
| `scripts/install/yadgar-backend.service.in` | systemd unit template; same placeholder set. |
| `scripts/install/com.openfantasy.yadgar.plist.in` | launchd plist template for core; placeholders `@RUNTIME@`, `@IMAGE@`, `@DATA_DIR@`. `RunAtLoad=true`, `KeepAlive=true`. |
| `scripts/install/com.openfantasy.yadgar-backend.plist.in` | launchd plist for backend; same placeholder set. |
| `scripts/install/uninstall.sh` | Stops + removes daemons (systemd/launchd), removes hooks, optionally wipes `~/.yadgar/` (per `--purge` flag). |

### Modified files

| Path | Change |
|---|---|
| `scripts/setup.sh` | Remove NixOS hardcoding. Convert to thin wrapper that calls `scripts/install/detect_*.sh` + `generate_*.sh`. Keep `--db` / `--archive` restore flags. Backward compatible: existing CLI flags unchanged. |
| `yadgar/cli/setup.py` | Promote `cmd_setup` to interactive installer. Calls `scripts/install/*.sh` via subprocess (yadgar binary may be installed via pipx without repo checkout — bundle install scripts under `yadgar/install_assets/` and `pkg_resources`-resolve at runtime). Adds `--non-interactive` flag for CI. New flags: `--runtime <auto|podman|docker>`, `--daemon <auto|systemd|launchd|none>`. |
| `yadgar/__main__.py` | Register `install` subcommand alongside existing `setup`. `install` is the new canonical name; `setup` becomes alias for backward compatibility. |
| `yadgar/daemon.py` | `check_docker()` → `check_runtime()`. Returns `{ok, runtime: "podman"|"docker", version}`. Existing callers updated to the new signature (callsite count: 3, all in `yadgar/cli/`). |
| `pyproject.toml` | Add `yadgar/install_assets/` to package data so installed wheel ships the shell scripts + `.in` templates. |
| `MIGRATION_NOTES.md` | Append v5.45.0 section: removed `scripts/setup.sh` direct invocation pattern (still works, but documented as legacy); new canonical UX is `yadgar install`. |
| `docs/DECISIONS.md` | Append PD-37 entry (separate commit per workflow — see Step 5 below). |

### Bundled install assets

`yadgar/install_assets/` ships inside the wheel. Layout:
```
yadgar/install_assets/
├── detect_runtime.sh
├── detect_os.sh
├── generate_systemd.sh
├── generate_launchd.sh
├── uninstall.sh
├── systemd/
│   ├── yadgar.service.in
│   ├── yadgar-backend.service.in
│   └── yadgar.target.in
└── launchd/
    ├── com.openfantasy.yadgar.plist.in
    └── com.openfantasy.yadgar-backend.plist.in
```

`scripts/install/` is a symlink (or git-shared dir) pointing to `yadgar/install_assets/` so repo-checkout users and wheel-installed users share the same scripts.

---

## Open questions (must resolve during implementation)

1. **macOS launchd plist exact content + management commands.** Decisions needed:
   - `RunAtLoad=true` + `KeepAlive=true`, OR `OnDemand`?  Lean: `RunAtLoad=true KeepAlive=true` (matches systemd `Restart=on-failure WantedBy=default.target` semantic).
   - Use `launchctl bootstrap gui/$UID` or `launchctl load`? `bootstrap` is newer macOS API (Catalina+); `load` is deprecated but more compatible.  Lean: `bootstrap` for macOS 11+, `load` fallback. Probe via `sw_vers -productVersion`.
   - Auto-reload on plist content change?  systemd has `daemon-reload`; launchd needs `unload` + `load` cycle.  Lean: yes, regenerate-script always unloads-then-loads.

2. **Container-runtime detect ordering.** Lean: `podman` first (rootless-friendly + no daemon required + matches NixOS pattern), then `docker`, then error. Override via `YADGAR_CONTAINER_RUNTIME` env knob.  Confirm during implementation that podman-on-macOS (via `podman machine`) behaves correctly.

3. **`yadgar install` vs `yadgar setup` — which is canonical?** Lean: `install` is new canonical; `setup` is alias for backward compat. Will deprecate `setup` in v5.50+ (long lead time; no immediate removal).

4. **Hook installation delegation.** Makefile target `install-hooks` calls `yadgar install-hooks` (which invokes the existing MCP `install_hooks` tool). Confirm hook install works without the daemon running yet — Makefile must invoke this AFTER `make setup` brings the daemon up.  Or: hook install is stand-alone via `yadgar/install_hooks_lib.py` direct call?  Lean: stand-alone (no daemon needed; the lib is pure-Python and writes to `~/.claude/`).

5. **Backward compatibility for systemd unit names.** Existing nix-managed installs have `yadgar.service` + `yadgar-backend.service`. New `yadgar.target` is additive — does not conflict. Confirm `systemctl --user list-unit-files` round-trips cleanly on a mixed install.

---

## Plan steps (concrete, executable)

### Step 0 — Pre-flight (≤ 0.25 day)

- Confirm task UX with user (interactive prompts: order, defaults, exit codes).
- Read `yadgar/install_hooks_lib.py` to confirm hook install is daemon-independent.
- Inspect a podman-rootless install on macOS (if available) to validate runtime detect ordering. If no macOS host available, manual smoke test deferred to acceptance step.
- Lock NixOS detection probe (`/etc/NIXOS` vs `nixos-version`).  Both are reliable; use either.

### Step 1 — TDD scaffolding (≤ 0.5 day)

Tests added under `yadgar/tests/test_install.py`:

- `detect_runtime.sh` returns `podman` when both podman + docker present, `docker` when only docker, `none` when neither (mock via `PATH` override).
- `detect_os.sh` returns `linux` on Linux, `linux-nixos` on NixOS (mock `/etc/NIXOS`), `macos` on Darwin (mock `uname`).
- `cmd_install` interactive flow: with `--non-interactive`, accepts defaults; refuses on NixOS-detected host; prompts for systemd-vs-compose on Linux.
- `cmd_install --runtime podman` overrides auto-detect.
- `make uninstall` preserves `~/.yadgar/`; `make uninstall-purge` removes it. Mock `~/.yadgar` location via `YADGAR_DATA_DIR` env knob.
- Generated systemd unit content matches expected template (placeholders substituted).
- Generated launchd plist content matches expected template (skipped on Linux).

### Step 2 — Extract systemd unit generation (≤ 0.5 day)

- Create `scripts/install/yadgar.service.in`, `yadgar-backend.service.in`, `yadgar.target.in` with `@RUNTIME@`, `@IMAGE@`, `@DATA_DIR@`, `@SECRETS_ENV_FILE@` placeholders.
- Create `scripts/install/generate_systemd.sh` — reads templates, substitutes placeholders via `sed`, writes to `~/.config/systemd/user/`, runs `systemctl --user daemon-reload`.
- Convert `scripts/setup.sh` lines 238-302 to a single call into `generate_systemd.sh`.
- Verify generated units match the units currently shipped by `scripts/setup.sh` byte-for-byte (after placeholder substitution).

### Step 3 — Add OS + runtime detection (≤ 0.5 day)

- Create `scripts/install/detect_runtime.sh` + `detect_os.sh`.
- Wire into `scripts/setup.sh` and `yadgar/cli/setup.py`.
- Remove hardcoded `DOCKER="$(command -v docker)"` from setup.sh line 41 — replaced by detect_runtime.sh output.
- Add NixOS refusal path: if `linux-nixos` detected, print "yadgar appears to be running on NixOS. Use the nix flake (v5.46+) — see https://codeberg.org/maxagahi/yadgar#nixos-install" and exit 1.

### Step 4 — Add macOS launchd path (≤ 0.5 day)

- Create `scripts/install/com.openfantasy.yadgar.plist.in` + `com.openfantasy.yadgar-backend.plist.in`.
- Create `scripts/install/generate_launchd.sh` — renders + loads via `launchctl bootstrap gui/$UID` (with `load` fallback for older macOS).
- Wire into `yadgar/cli/setup.py` interactive flow when `--daemon=launchd` (or auto-detected on macOS).
- Tested via mock on Linux + manual smoke on macOS (deferred to acceptance).

### Step 5 — Makefile + interactive `yadgar install` (≤ 0.5 day)

- Create top-level `Makefile`. Targets:
  - `make setup` → calls `yadgar install --non-interactive` if `INSTALL_NONINTERACTIVE=1`, else `yadgar install`.
  - `make uninstall` → calls `scripts/install/uninstall.sh` (preserves data).
  - `make uninstall-purge` → calls `scripts/install/uninstall.sh --purge`.
  - `make install-hooks` → calls `yadgar install-hooks` (delegates to MCP tool wrapper).
  - `make check` → `python -m pytest yadgar/tests/test_install.py -q`.
  - `make clean` → removes generated systemd units / launchd plists (does NOT touch data).
- Promote `yadgar/cli/setup.py` `cmd_setup` to `cmd_install` with interactive prompts. Keep `cmd_setup` as a thin alias.

### Step 6 — Package install assets in wheel (≤ 0.25 day)

- Add `[tool.hatch.build.targets.wheel.shared-data]` (or equivalent hatchling include) so `yadgar/install_assets/` ships in the wheel.
- `scripts/install/` becomes a symlink to `yadgar/install_assets/` so repo-checkout dev and wheel users share the same scripts.
- Verify wheel + sdist both contain the install assets.

### Step 7 — DECISIONS.md PD-37 entry (separate commit)

Append to `docs/DECISIONS.md` under a new dated section. Content:

- **Item:** Setup mechanism for non-NixOS installs.
- **Decision:** ADOPT — Compose-canonical with systemd opt-in + interactive installer + auto-detect runtime + auto-detect OS.
- **Reason:**
  - Compose is portable across Linux/macOS/Windows/WSL2.
  - systemd opt-in path supports power users + matches NixOS-managed pattern.
  - Interactive installer eliminates curl-pipe-sh attack surface.
  - Auto-detect runtime/OS removes per-distro tribal knowledge.
  - macOS launchd path bundles the same UX as Linux systemd.
- **Alternatives considered:**
  - Per-service systemd units only → rejected; macOS users excluded.
  - Detect-OS hybrid only (no compose path) → rejected; loses portability.
  - Compose-only (no systemd) → rejected; loses daemon supervision on power-user Linux installs.
- **Revisit triggers:** macOS launchd path proves unreliable in field; OR compose v3 spec deprecates a feature we depend on; OR user demand for FreeBSD/Windows-native paths.

### Step 8 — Wiki update (best-effort)

Mirror PD-37 to wiki page `yadgar-decisions-log` via `wiki_add` or `wiki_update`. If yadgar MCP unavailable from the implementer's session, document as a follow-up.

### Step 9 — Version bump + MIGRATION_NOTES + CHANGELOG (≤ 0.25 day)

- Bump `pyproject.toml` 5.25.0 → 5.45.0 (skip-1 minor convention).
- Bump `server.json` core version to 5.45.0.
- `CHANGELOG.md` entry: "feat(install): portable installer + Makefile + multi-OS daemon support."
- `MIGRATION_NOTES.md` v5.45.0 section: legacy `scripts/setup.sh` still works; new canonical `yadgar install`.

---

## Acceptance criteria

v5.45.0 ships when ALL of the following are true:

- [ ] `make setup` works on Linux + macOS (manual smoke tests on both).
- [ ] `make uninstall` preserves `~/.yadgar/`; `make uninstall-purge` removes it.
- [ ] `yadgar install --non-interactive` accepts defaults + completes without prompts (CI-friendly).
- [ ] `yadgar install` on a NixOS host refuses with a clear "use nix flake" message + exits non-zero.
- [ ] `scripts/install/detect_runtime.sh` returns `podman` / `docker` / `none` deterministically; honors `YADGAR_CONTAINER_RUNTIME`.
- [ ] `scripts/install/detect_os.sh` returns `linux` / `linux-nixos` / `macos` / `other`.
- [ ] Generated systemd units pass `systemd-analyze verify` (Linux).
- [ ] Generated launchd plists pass `plutil -lint` (macOS).
- [ ] `yadgar.target` brings up both core + backend via `systemctl --user start yadgar.target`.
- [ ] `pytest yadgar/tests/test_install.py` green.
- [ ] DECISIONS.md PD-37 entry merged (separate commit per workflow rule).
- [ ] Wiki page `yadgar-decisions-log` updated with PD-37 entry (best-effort; flagged if MCP unavailable).
- [ ] CHANGELOG.md v5.45.0 entry exists.
- [ ] MIGRATION_NOTES.md v5.45.0 section documents legacy path + new canonical UX.
- [ ] `python scripts/check_versions.py` exit 0.

**NOT in scope:** PyPI metadata polish, Homebrew tap, Nix flake, release automation, SBOM, update mechanism.

---

## Effort estimate (calendar days)

| Step | Days |
|---|---:|
| Step 0 pre-flight | 0.25 |
| Step 1 TDD scaffolding | 0.5 |
| Step 2 systemd unit extraction | 0.5 |
| Step 3 OS + runtime detection | 0.5 |
| Step 4 macOS launchd path | 0.5 |
| Step 5 Makefile + interactive installer | 0.5 |
| Step 6 wheel asset packaging | 0.25 |
| Step 7 DECISIONS.md PD-37 | 0.1 |
| Step 8 wiki update | 0.1 |
| Step 9 version bump + docs | 0.25 |
| **Total** | **2 – 3 calendar days** |

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Generated systemd units differ subtly from existing nix-managed units → existing user installs break on upgrade | Step 2 acceptance: byte-for-byte template match against current `scripts/setup.sh` output. Verify on a real NixOS-managed install. |
| launchd plist semantics differ from systemd → macOS daemon behavior surprises users | Document explicit `RunAtLoad=true KeepAlive=true` semantic in MIGRATION_NOTES. Mark macOS path as "beta" in v5.45.0 README. |
| Podman-on-macOS via `podman machine` fails health checks unexpectedly | Detect script probes via `podman info` (not just `command -v`). Fail clean + suggest docker fallback in interactive prompt. |
| NixOS user runs `yadgar install` on a host that has both nix-managed AND pipx install → undetectable conflict | NixOS-detection refusal short-circuits before any state mutation. If user `--force`-bypasses, document data-loss risk in MIGRATION_NOTES. |
| Wheel package asset path resolution fails when installed via pipx → install scripts not found at runtime | `pkg_resources` / `importlib.resources` resolution tested in Step 1 TDD. Fallback: bundle assets in `yadgar/_install_assets_data/` and resolve via `Path(__file__).parent`. |
| Interactive prompts break in non-TTY environments (CI, agent dispatch) | `--non-interactive` flag is the explicit CI path. Default behavior probes `sys.stdin.isatty()`; if false, refuses + suggests `--non-interactive`. |
| Existing `scripts/setup.sh` callers (NixOS home-manager activation) break on the refactor | Keep `scripts/setup.sh` as-is for backward compatibility; the v5.45 refactor adds NEW entry points. NixOS users continue via `home.activation.pipxYadgar` unchanged. |

---

## Dependencies + blockers

- **None blocking start.** Code exists; refactor is additive + extraction.
- **Does NOT block v5.26.0** — benchmark Phase 2 QA ships first via the existing setup path.
- **Blocks v5.46.0** — distribution work depends on the new install asset layout.
- **Blocks v5.47.0** — update mechanism depends on install-method detection (pipx/brew/nix/container).

---

## TDD test list

Add under `yadgar/tests/test_install.py` (new file). Markers: `not integration` (no live containers — all subprocess + filesystem mocks).

1. `test_detect_runtime_prefers_podman_over_docker` — both binaries on PATH → returns `podman`.
2. `test_detect_runtime_falls_back_to_docker` — only docker on PATH → returns `docker`.
3. `test_detect_runtime_returns_none_when_neither_present` — neither on PATH → returns `none`.
4. `test_detect_runtime_honors_env_override` — `YADGAR_CONTAINER_RUNTIME=docker` forces docker even when podman present.
5. `test_detect_os_linux` — `/etc/os-release` `ID=ubuntu` → `linux`.
6. `test_detect_os_nixos` — `/etc/NIXOS` exists → `linux-nixos`.
7. `test_detect_os_macos` — `uname` returns `Darwin` → `macos`.
8. `test_cmd_install_non_interactive_accepts_defaults` — `yadgar install --non-interactive` completes; writes config + secrets + units; no prompts.
9. `test_cmd_install_refuses_on_nixos` — mock NixOS detect → cmd_install exits non-zero with explicit message.
10. `test_cmd_install_prompts_systemd_vs_compose_on_linux` — interactive flow; mock stdin selects "systemd"; units written.
11. `test_cmd_install_runtime_override` — `--runtime docker` honored even when podman detected.
12. `test_make_uninstall_preserves_data` — runs uninstall.sh without `--purge`; `~/.yadgar/` still exists.
13. `test_make_uninstall_purge_removes_data` — uninstall.sh `--purge`; `~/.yadgar/` removed.
14. `test_generate_systemd_unit_matches_template` — render with known placeholders; output matches expected file byte-for-byte.
15. `test_generate_launchd_plist_matches_template` — same as above for launchd. Skipped on Linux via `@pytest.mark.skipif(sys.platform != "darwin")`. Actually: render must work on Linux too (template substitution is OS-agnostic); only the `launchctl load` step is macOS-only. Test renders on Linux, asserts content.
16. `test_yadgar_target_includes_both_services` — generated `yadgar.target` has `Wants=yadgar.service yadgar-backend.service`.

---

## Coordination notes for main thread

- Plan-only doc → direct to master per workflow rule (wiki slug `yadgar-workflow-plan-commits-direct-to-master`).
- Implementation work requires a feature branch — `feat/v5.45.0-setup-foundation` is the obvious name. Branch from latest master after this plan commits.
- DECISIONS.md PD-37 entry committed separately per workflow.
- Related plans: `docs/PLAN_V5_46_0_DISTRIBUTION.md` (depends on this) + `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` (depends on this).
- Implementer must read `docs/DECISIONS.md` PD-37 before re-scoping the setup mechanism choice.
