# PLAN — v5.46.2: Runtime Detection UX Hotfix

**Status:** IN PROGRESS (2026-06-05)

**Origin:** User testing in fresh VM 2026-06-05 found `yadgar-setup.sh` + `detect_runtime.sh` fail abruptly when no container runtime is installed — stale "Run: yadgar install" error message (post-make-canonical DP-3 override) and no install guidance. Blocker for fresh installs from PyPI/pipx.

**Slot rationale:** v5.46.2 was empty (retired by PD-40 with no shipped artifacts). Slot reuse is clean — retired plan archived to `docs/PLAN_V5_46_2_CROSS_REPO_PR_AUTO_OPEN_RETIRED.md` per PD-41.

**Related decisions:** See `docs/DECISIONS.md` PD-41.

---

## Goal

1. Fix stale error message in `detect_runtime.sh` (line 98: "yadgar install" → "yadgar-setup").
2. Add OS-aware install hints to `detect_runtime.sh` error block (7 distros + macOS).
3. New shared helper `scripts/install/install_runtime.sh` — interactive prompt + install execution + post-install retry.
4. Wire `yadgar-setup.sh` to call `install_runtime.sh` on detect failure.
5. Wire `Makefile` `install-runtime` target to call same helper (DRY, in-sync).
6. `--install-runtime` / `--no-install-runtime` flags on `yadgar-setup.sh`.
7. Non-interactive path (`INSTALL_NONINTERACTIVE=1` / `--noninteractive`) prints install command + exits non-zero (no prompt).

---

## Non-goals

- No brew tap PR, no cross-repo automation.
- No actual `sudo apt-get install` executed in CI or tests (mocked via `YADGAR_TEST_INSTALL_DRYRUN=1`).
- No full macOS machine-start automation — `brew install podman` only; `podman machine init && podman machine start` printed as follow-up guidance.
- No changes to detection priority order (YADGAR_CONTAINER_RUNTIME override unchanged).

---

## Architecture Conformance (P1)

- `detect_runtime.sh`: stays read-only detection. Error block improved UX only.
- `install_runtime.sh`: new script, install path only. Test seams baked in from start.
- `yadgar-setup.sh`: thin wrapper around install_runtime.sh; no new flag parsing complexity.
- `Makefile`: new `install-runtime` target; `setup` target unchanged except for failure lane.
- No Python changes, no MCP changes, no schema changes.

---

## Touched Invariants (P2)

| Invariant | Verb | Notes |
|---|---|---|
| I9 (hot path latency) | **preserves** | Install path; not on daemon hot path. |
| I27 (plan-first) | **preserves** | This doc. |

---

## Config Knob Lifecycle (P3)

New env vars (test seams only, not user-facing config):

| Var | Purpose |
|---|---|
| `YADGAR_TEST_OS_RELEASE` | Override `/etc/os-release` path in `install_runtime.sh` |
| `YADGAR_TEST_INSTALL_DRYRUN` | Print install command without execing `sudo` |
| `YADGAR_TEST_TTY` | Override TTY detection (`0`=no-TTY, `1`=TTY) |

User-facing:

| Var/Flag | Purpose |
|---|---|
| `INSTALL_NONINTERACTIVE=1` (existing) | Non-interactive mode (already in Makefile) |
| `--install-runtime` (new flag) | Skip prompt; install directly |
| `--no-install-runtime` (new flag) | Skip prompt; print hint + exit non-zero |

---

## Schema Constraint Lifecycle (P4)

None.

---

## MCP Contract Changes (P5)

None.

---

## Cross-Plan Coordination (P6)

| Plan | Relationship |
|---|---|
| `docs/PLAN_V5_46_2_CROSS_REPO_PR_AUTO_OPEN_RETIRED.md` | Archaeology; this plan reuses its slot. |
| `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` | Downstream. No conflict. |

---

## Bug Class Precedent (P7)

**Precedent 1 — DP-3 stale message (v5.46.0 ship):** `detect_runtime.sh` was updated post-make-canonical but retained "yadgar install" message from pre-canonical era. Fix: replace with "yadgar-setup" at line 98.

**Precedent 2 — TTY detection in Makefile recipes:** `test -t 0` is unreliable under `make` (no controlling terminal in recipe sub-shell). Use `INSTALL_NONINTERACTIVE` env var as the authoritative non-interactive gate; don't rely on TTY in Makefile. Align with existing Makefile var.

---

## Rollback Path (P9)

Rollback = revert `detect_runtime.sh` error block, delete `install_runtime.sh`, revert `yadgar-setup.sh` + Makefile additions. Zero data migration needed. Can be done in a single commit.

---

## Dependency Pinning (P10)

No new dependencies. Shell only.

---

## Agent Dispatch Budget (P11)

Hotfix scope. Single agent session.

---

## Plan Steps

### Step 0 — Plan rename + PD-41 + docs scaffold

- `git mv docs/PLAN_V5_46_2_CROSS_REPO_PR_AUTO_OPEN.md docs/PLAN_V5_46_2_CROSS_REPO_PR_AUTO_OPEN_RETIRED.md`
- Add archaeology header to renamed file.
- Write this file.
- Add PD-41 to `docs/DECISIONS.md`.

### Step 1 — TDD scaffolding (RED)

Three test files:
- `yadgar/tests/test_v5_46_2_detect_runtime_error_messages.py` — no stale "yadgar install"; install hints per 7 distros; correct final message.
- `yadgar/tests/test_v5_46_2_offer_install_runtime.py` — install_runtime.sh prompt/install/retry logic; test seams.
- `yadgar/tests/test_v5_46_2_makefile_install_runtime.py` — Makefile `install-runtime` target; `setup` chain.

### Step 2 — `detect_runtime.sh` fix

- Replace stale "yadgar install" with "yadgar-setup" in error block.
- Add `YADGAR_TEST_OS_RELEASE` seam.
- Read `/etc/os-release` ID field; print distro-specific install hint.
- `--quiet` flag: suppress install hint when called by chained scripts (avoids double-print).

### Step 3 — `scripts/install/install_runtime.sh` (NEW)

- Shared helper for both `yadgar-setup.sh` and Makefile.
- Reads `YADGAR_TEST_OS_RELEASE` (default `/etc/os-release`) for distro detection.
- Reads `YADGAR_TEST_INSTALL_DRYRUN` (default `0`) — print cmd only if `1`.
- Reads `YADGAR_TEST_TTY` (default derived from `test -t 0`) — interactive gate.
- Reads `INSTALL_NONINTERACTIVE` (default `0`) — non-interactive gate (overrides TTY).
- Distro map:
  - `ubuntu|debian|pop|linuxmint|raspbian` → `sudo apt-get install -y podman`
  - `fedora|rhel|centos|rocky|almalinux` → `sudo dnf install -y podman`
  - `arch|manjaro|endeavouros` → `sudo pacman -S --noconfirm podman`
  - `alpine` → `sudo apk add podman`
  - `opensuse*|sles` → `sudo zypper install -y podman`
  - darwin/macOS → `brew install podman` (+ print follow-up: `podman machine init && podman machine start`)
  - fallback → print URL + die
- Interactive mode: prompt "Install podman now? [Y/n]"; on Y: exec install cmd; if exit 0: retry detect_runtime.sh; if exit non-zero: die with install failure message.
- Non-interactive mode: print install cmd + exit 1.

### Step 4 — `yadgar-setup.sh` + `Makefile` sync

- `yadgar-setup.sh`: replace bare `die()` in `_detect_runtime()` inline fallback with call to `install_runtime.sh`; update `_step_detect()` to call `_offer_install_runtime()` on failure; add `--install-runtime` / `--no-install-runtime` flags.
- `Makefile`: add `install-runtime` target calling `install_runtime.sh`; add `INSTALL_NONINTERACTIVE` pass-through.

### Step 5 — Docs

- `CHANGELOG.md` v5.46.2 entry.
- `MIGRATION_NOTES.md` v5.46.2 section.
- `docs/INSTALL.md`: note about first-run install-runtime prompt.

### Step 6 — Version bump 5.46.1 → 5.46.2

- Edit `pyproject.toml`.
- Pre-commit auto-syncs `server.json`, `flake.nix`, `docker-compose.yml`, `uv.lock`.

---

## Deliverables

| File | Status |
|---|---|
| `docs/PLAN_V5_46_2_RUNTIME_DETECTION_HOTFIX.md` | This file |
| `docs/PLAN_V5_46_2_CROSS_REPO_PR_AUTO_OPEN_RETIRED.md` | Renamed (archaeology) |
| `docs/DECISIONS.md` PD-41 | Added |
| `yadgar/tests/test_v5_46_2_detect_runtime_error_messages.py` | TDD |
| `yadgar/tests/test_v5_46_2_offer_install_runtime.py` | TDD |
| `yadgar/tests/test_v5_46_2_makefile_install_runtime.py` | TDD |
| `scripts/install/detect_runtime.sh` | Fixed |
| `scripts/install/install_runtime.sh` | New |
| `scripts/install/yadgar-setup.sh` | Updated |
| `Makefile` | Updated |
| `CHANGELOG.md` | Updated |
| `MIGRATION_NOTES.md` | Updated |
| `docs/INSTALL.md` | Updated |
| `pyproject.toml` | Bumped |

---

## USER ACTION CHECKLIST

None — pure dev work / UX fix. No secrets, no infrastructure changes.

---

## Acceptance Criteria

- [ ] `detect_runtime.sh` error block: no "yadgar install" string; correct distro hints for 7 distros + macOS; final message says "yadgar-setup".
- [ ] `install_runtime.sh` interactive prompt tested via mocked seams.
- [ ] `INSTALL_NONINTERACTIVE=1` path: prints install command + exits 1.
- [ ] `--install-runtime` flag: skips prompt, runs install.
- [ ] `--no-install-runtime` flag: skips prompt, prints hint + exits 1.
- [ ] Makefile `install-runtime` target invokes `install_runtime.sh`.
- [ ] `make setup` on detect failure + `INSTALL_NONINTERACTIVE=1`: prints hint + fails loudly.
- [ ] All TDD tests GREEN.
- [ ] `shellcheck scripts/install/*.sh` clean.
- [ ] `check_versions.py` exits 0 at 5.46.2.
- [ ] Pre-commit passes on version bump commit.
