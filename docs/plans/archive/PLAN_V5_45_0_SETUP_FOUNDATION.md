# PLAN — v5.45.0: Setup Foundation (Linux only, scope cut)

**Status:** drafted 2026-05-31. REVISED 2026-06-02 post-opus-review (MAJOR cut). REVISED 2026-06-04 per V5_45_0_DP_OVERRIDES.md (make-canonical, DP6 fold-back). Plan-first per I27.

**Revision notes (opus reviewer):**
- SCOPE CUT — was 9 steps + 2-3d. Now Linux core only.
- macOS launchd path SPLIT to **v5.45.1** (separate plan, once verifying host available).
- Seed-anchors + CLAUDE.md fragment FOLDED into make targets per V5_45_0_DP_OVERRIDES.md (was split to v5.45.1; override cancelled the split).
- Symlink scheme REPLACED with `importlib.resources.files()` (works across sdist/wheel/pipx/editable).
- Test 14 byte-for-byte template match REPLACED with semantic equivalence.
- NixOS detection refusal path: confirm `/etc/NIXOS` + error message BEFORE shipping (data-loss-shaped).
- Revised effort: ~1.5-2d (was 2-3d, then ~1d; updated per DP6 fold-back). Steps 0-9; make-canonical.
- HIGHEST-RISK plan — blocks v5.46 + v5.47 chain. De-scope MANDATORY before dispatch.

**Audit lineage:** identified during v5.41-set viz-planning sweep + post-v5.25.0 setup audit. Current installer (`scripts/setup.sh`) hard-codes `docker` and assumes Linux + systemd. `yadgar/cli/setup.py` is a fragmented config-only wrapper. NixOS-managed installs work; everything else degrades silently.

**Ships in train:** v5.45.0 → v5.46.0 → v5.47.0 (foundation → distribution → updates). User-locked sequence; ships BEFORE v5.50/v5.51/v5.52 viz.

**Pipeline insertion:** between v5.26.0 (benchmark Phase 2 QA) and v5.27.0 (DuckDB). Shifts existing pipeline:
- DuckDB v5.27 → v5.49
- Bi-temporal v5.29 → v5.??? (pipeline renumber pending user decision; not committed in this plan)

**Effort estimate:** ~1.5-2 calendar days.

**Downstream:**
- v5.46.0 (`PLAN_V5_46_0_DISTRIBUTION.md`) packages on top of v5.45 install layout.
- v5.47.0 (`PLAN_V5_47_0_UPDATE_MECHANISM.md`) depends on v5.45 install-method detection.

See also `docs/DECISIONS.md` — 2026-05-31 PD-37 (setup mechanism decision).

---

## Goal — portable `make setup` / `make uninstall` with interactive make-based installer

Replace the NixOS-specific `scripts/setup.sh` and fragmented `yadgar/cli/setup.py` with a portable, OS-aware installer that:

1. Detects container runtime (podman / docker / others) at runtime — no hardcoded `/run/current-system/sw/bin/docker`.
2. Detects host OS (Linux / macOS / others) and selects daemon mechanism (systemd / launchd / none).
3. Provides a single entry-point UX: `git clone <repo>; make setup` (repo-checkout canonical path).
4. Provides `make setup` / `make uninstall` / `make uninstall-purge` with individual standalone targets.
5. Refuses to overwrite an existing NixOS-managed install — suggests using v5.46 nix flake instead.

`make setup` is the canonical user-facing install entrypoint. It orchestrates all building blocks:
hook installation, agent template deployment, config.yaml sync, CLAUDE.md rules deployment, seed anchors.
No curl-pipe-sh attack surface.

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
- **No new `yadgar install` CLI subcommand.** `make setup` is the entrypoint; pipx-only path deferred to v5.46.

---

## Current state (verified from code, 2026-05-31)

| Component | Path | Status | Gap for v5.45 |
|---|---|---|---|
| Bash installer | `scripts/setup.sh` (433 lines) | Linux-only; hardcodes `docker = command -v docker`; writes systemd user units inline. Works on NixOS. | Remove NixOS hardcoding (lines 41, 244, 277). Extract systemd unit generation. Add OS gating. |
| CLI setup | `yadgar/cli/setup.py` (123 lines) | Writes config.yaml + secrets.env + prints MCP snippet. No container pull, no daemon install, no hook install. | Becomes a building block `make setup` orchestrates; no promotion to interactive installer. |
| Daemon check | `yadgar/daemon.py:check_docker()` | Already detects docker via `command -v` + `docker info`. | Generalize to runtime detection (podman first, then docker, then others). |
| Hooks installer | MCP `install_hooks` tool + `yadgar/install_hooks_lib.py` | Production-ready; installs Claude Code hooks idempotently. | Makefile target delegates to this; do not re-implement. |
| systemd units | hardcoded heredocs in `scripts/setup.sh:238-302` | Linux-only. Hardcoded `${DOCKER}` path. No `yadgar.target` group. Note: `yadgar/daemon.py:install_systemd_service()` writes `yadgar-db.service` (non-canonical drift vs deployed `yadgar-backend.service`). Do not use this method; it will be deprecated in v5.46.0. | Extract to template files in `scripts/install/`. Generate from sh. Add `yadgar.target` aggregating yadgar.service + yadgar-backend.service. |
| macOS daemon | none | No launchd plists exist. | SPLIT to v5.45.1 — out of scope for v5.45.0. |
| NixOS detection | none | Installer would clobber `/home/max/.local/pipx/venvs/yadgar` if user had nix-managed install with same path. | Detect `/etc/NIXOS` or `command -v nixos-version`; refuse + suggest nix flake (v5.46). |
| Data preservation | `make uninstall` does not exist | n/a | New: preserve `~/.yadgar/` by default; `make uninstall-purge` for full wipe. |
| Makefile | none at repo root | n/a | New: top-level Makefile (GNU make required). |
| v5.44.0 building blocks | `yadgar install-hooks`, `yadgar install-subagents`, `yadgar config sync` | Production-ready (v5.44.0). All three work standalone. | Wire as Makefile target dependencies — `make setup` orchestrates them. No re-implementation needed. |

---

## Scope — concrete file changes

### New files

| Path | Purpose |
|---|---|
| `Makefile` (repo root) | Top-level GNU-make targets: `pre-setup`, `setup`, `uninstall`, `uninstall-purge`, `detect-runtime`, `pull-images`, `install-units`, `install-agents`, `install-hooks`, `config-sync`, `install-rules`, `seed-anchors`, `check`, `help`. GNU make required; `pre-setup` target refuses non-GNU make. |
| `scripts/install/detect_runtime.sh` | Emits `podman` / `docker` / `none` on stdout. Probes via `command -v` + `<runtime> info` health check. Honors `YADGAR_CONTAINER_RUNTIME` override if set. |
| `scripts/install/detect_os.sh` | Emits `linux` / `macos` / `other` + daemon-capability hint (`systemd` / `launchd` / `none`). Linux detection checks `/etc/os-release` + `command -v systemctl`. NixOS detection: emits `linux-nixos` if `/etc/NIXOS` exists OR `command -v nixos-version` succeeds. |
| `scripts/install/generate_systemd.sh` | Renders `yadgar.service`, `yadgar-backend.service`, `yadgar.target` from `.in` templates. Substitutes runtime path, image tag, data dir, secrets-env path. Writes to `~/.config/systemd/user/`. Reloads systemd user manager. |
| `scripts/install/yadgar.target.in` | systemd target template: `Wants=yadgar.service yadgar-backend.service`. Lets `systemctl --user start yadgar.target` bring up the whole stack. |
| `scripts/install/yadgar.service.in` | systemd unit template; placeholders for `@RUNTIME@`, `@IMAGE@`, `@DATA_DIR@`. |
| `scripts/install/yadgar-backend.service.in` | systemd unit template; same placeholder set. |
| `scripts/install/uninstall.sh` | Stops + removes daemons (systemd), removes hooks, optionally wipes `~/.yadgar/` (per `--purge` flag). |
| `scripts/install/append_claude_rules.sh` | Appends CLAUDE.md fragment from `install_assets/CLAUDE.md.fragment` to user's `~/.claude/CLAUDE.md`; deduplicates on re-run. |
| `install_assets/CLAUDE.md.fragment` | Yadgar workflow rules snippet (wiki read-modify-write, tag-or-fail, restore after clear, memorize context path, etc.). Appended by `make install-rules`. |
| `install_assets/seeds/anchors.yaml` | Seed anchor definitions (~8 entries: canonical workflow pain-points). Loaded by `yadgar seed --anchors`. |
| `yadgar/cli/seed.py` (new `--anchors` flag) | Adds `--anchors <file>` flag to `yadgar seed` command. Reads YAML, calls `memorize()` for each entry, deduplicates via content-hash. |

### Modified files

| Path | Change |
|---|---|
| `scripts/setup.sh` | Remove NixOS hardcoding. Convert to thin wrapper that calls `scripts/install/detect_*.sh` + `generate_*.sh`. Keep `--db` / `--archive` restore flags. Backward compatible: existing CLI flags unchanged. |
| `yadgar/cli/setup.py` | Remains as config.yaml + secrets.env writer (building block). No promotion to interactive installer. No deprecation warning. Becomes one of the chain pieces `make setup` orchestrates via `yadgar config sync`. |
| `yadgar/daemon.py` | `check_docker()` → `check_runtime()`. Returns `{ok, runtime: "podman"|"docker", version}`. Existing callers updated. Note: `yadgar/daemon.py` has ~20+ docker-hardcoded subprocess callsites; v5.45.0 migrates check_docker() + highest-traffic paths; remaining callsites deferred to v5.46.0 with TODO comments. |
| `pyproject.toml` | Add `install_assets/` to package data so installed wheel ships the shell scripts + `.in` templates + seeds. |
| `MIGRATION_NOTES.md` | Append v5.45.0 section: removed `scripts/setup.sh` direct invocation pattern (still works, but documented as legacy); new canonical UX is `make setup` (repo checkout required; pipx-only path ships in v5.46). |
| `docs/DECISIONS.md` | Append PD-37 entry (separate commit per workflow — see Step 7 below). |

### Bundled install assets

`install_assets/` ships inside the wheel. Layout:
```
install_assets/
├── CLAUDE.md.fragment
├── seeds/
│   └── anchors.yaml
└── systemd/
    ├── yadgar.service.in
    ├── yadgar-backend.service.in
    └── yadgar.target.in
```

`scripts/install/` is a symlink (or git-shared dir) pointing to `install_assets/` so repo-checkout users and wheel-installed users share the same scripts.

---

## Open questions (must resolve during implementation)

1. **macOS launchd plist exact content + management commands.** [DEFERRED → v5.45.1, plan not yet written]
   - `RunAtLoad=true` + `KeepAlive=true`, OR `OnDemand`?  Lean: `RunAtLoad=true KeepAlive=true` (matches systemd `Restart=on-failure WantedBy=default.target` semantic).
   - Use `launchctl bootstrap gui/$UID` or `launchctl load`? `bootstrap` is newer macOS API (Catalina+); `load` is deprecated but more compatible.  Lean: `bootstrap` for macOS 11+, `load` fallback. Probe via `sw_vers -productVersion`.
   - Auto-reload on plist content change?  systemd has `daemon-reload`; launchd needs `unload` + `load` cycle.  Lean: yes, regenerate-script always unloads-then-loads.

2. **Container-runtime detect ordering.** [RESOLVED — see V5_45_0_DP_RESOLUTIONS.md]
   - `podman` first (rootless-friendly + no daemon required + matches NixOS pattern), then `docker`, then error. Override via `YADGAR_CONTAINER_RUNTIME` env knob.
   - v5.45.0 Linux only; podman-on-macOS via `podman machine` deferred to v5.45.1.

3. **`make setup` vs `yadgar install` — canonical naming.** [RESOLVED via V5_45_0_DP_OVERRIDES.md — make-canonical, no yadgar install CLI]
   - `make setup` is the only canonical user-facing entrypoint.
   - No new `yadgar install` CLI subcommand. Existing `yadgar install-hooks` / `yadgar install-subagents` / `yadgar config sync` stay as building blocks make targets call.
   - `yadgar setup` (config.yaml + secrets.env writer) stays as a building block with no deprecation noise.

4. **Hook installation delegation.** [RESOLVED — see V5_45_0_DP_RESOLUTIONS.md]
   - Hook install is daemon-independent (install_hooks_lib.py is pure-Python with no daemon imports). `make install-hooks` can run before, after, or without `make setup`.

5. **Backward compatibility for systemd unit names.** [RESOLVED — see V5_45_0_DP_RESOLUTIONS.md]
   - Existing nix-managed installs have `yadgar.service` + `yadgar-backend.service`. New `yadgar.target` is additive — does not conflict.

6. **Seed-anchors + bundled CLAUDE.md rules.** [RESOLVED via V5_45_0_DP_OVERRIDES.md — folded back, make targets cover]
   Should `make setup` bootstrap a fresh DB with canonical workflow anchors + matching CLAUDE.md rule snippets? Captures pain-points learned the hard way so new installs don't repeat them.

   **Candidate seed anchors:**
   - Wiki read-modify-write rule (the 2026-05-31 corruption that destroyed `yadgar-roadmap-future-improvements`)
   - `wiki_query` tag-or-fail (untagged queries score ~0.34, low recall)
   - `restore(directory=...)` after `/clear` / `/compact` (NOT `recall`)
   - `memorize(context=<abs-path>)` must be literal CWD, not description (breaks `project_brief`)
   - `wiki_add` then `wiki_approve(slug)` for new pages (draft → published)
   - Anchor hygiene: prefer `audit_anchors(dry_run=True)` over manual count
   - SurrealKV vacuum schedule + `vacuum_now()` reclaim path
   - "Never directly query DB; always MCP tools" (already in user's global CLAUDE.md but worth seeding per-install)

   **Design questions (carry forward to impl):**
   - Mechanism: shipped as `install_assets/seeds/anchors.yaml` + loaded by `make seed-anchors` (opt-in default ON for `make setup`).
   - Format: structured YAML → `memorize(..., tags=["_anchor"], is_protected=True)` calls via `yadgar seed --anchors <file>`.
   - CLAUDE.md fragment: `install_assets/CLAUDE.md.fragment` appended by `make install-rules` (dedupes on re-run).
   - Combination strategy: seed-anchor + matching CLAUDE.md fragment paired by reference.
   - Versioning: seeds shipped per-yadgar-release; re-running skips already-present anchors (dedup via content-hash).
   - Scope: per-install (global) vs per-project (when invoked inside a project dir).
   - Opt-out: `--no-seeds` flag + config knob `YADGAR_INSTALL_SEED_ANCHORS=0`.
   - Update path: when v5.46+ adds a new pain-point seed, how does an existing install pick it up? `yadgar update --apply-new-seeds`? Or surface as `audit_seeds()` MCP tool that lists missing canonical anchors.

   **Resolution:** scope confirmed as seeds + rules. `make setup` runs both. `make seed-anchors` and `make install-rules` are standalone targets. No v5.45.1 split needed.

---

## Plan steps (concrete, executable)

### Step 0 — Pre-flight (≤ 0.25 day)

- Confirm task UX with user (interactive prompts: order, defaults, exit codes).
- Read `yadgar/install_hooks_lib.py` to confirm hook install is daemon-independent.
- Lock NixOS detection probe (`/etc/NIXOS` vs `nixos-version`). Both are reliable; use either.
- Review v5.44.0 building blocks: `yadgar install-hooks`, `yadgar install-subagents`, `yadgar config sync` — confirm they work standalone and identify any flags needed by Makefile targets.

### Step 1 — TDD scaffolding (≤ 0.5 day)

Tests added under `yadgar/tests/test_install.py`:

- `detect_runtime.sh` returns `podman` when both podman + docker present, `docker` when only docker, `none` when neither (mock via `PATH` override).
- `detect_os.sh` returns `linux` on Linux, `linux-nixos` on NixOS (mock `/etc/NIXOS`), `macos` on Darwin (mock `uname`).
- `test_make_setup_non_interactive_accepts_defaults` — `make setup INSTALL_NONINTERACTIVE=1` completes; writes config + secrets + units; no prompts.
- `test_make_setup_refuses_on_nixos` — mock NixOS detect → exits non-zero with explicit "use nix flake" message.
- `test_make_setup_systemd_path` — mock Linux systemd path; units written to expected location.
- `test_make_setup_runtime_override` — `YADGAR_CONTAINER_RUNTIME=docker` honored even when podman detected.
- `test_make_uninstall_preserves_data` — runs uninstall.sh without `--purge`; `~/.yadgar/` still exists.
- `test_make_uninstall_purge_removes_data` — uninstall.sh `--purge`; `~/.yadgar/` removed.
- `test_generate_systemd_unit_matches_template` — render with known placeholders; output matches expected file byte-for-byte.
- `test_yadgar_target_includes_both_services` — generated `yadgar.target` has `Wants=yadgar.service yadgar-backend.service`.
- `test_seed_anchors_idempotent` — running `make seed-anchors` twice does not duplicate entries (content-hash dedup).
- `test_install_rules_deduplicates` — running `make install-rules` twice does not duplicate CLAUDE.md fragment.
- `test_make_target_chain` — `make setup` triggers all expected sub-targets in correct order.
- `test_nix_guard_in_generate_systemd` — defense-in-depth: `generate_systemd.sh` detects nix-symlinked units and exits non-zero.

### Step 2 — Extract systemd unit generation (≤ 0.5 day)

- Create `scripts/install/yadgar.service.in`, `yadgar-backend.service.in`, `yadgar.target.in` with `@RUNTIME@`, `@IMAGE@`, `@DATA_DIR@`, `@SECRETS_ENV_FILE@` placeholders.
- Create `scripts/install/generate_systemd.sh` — reads templates, substitutes placeholders via `sed`, writes to `~/.config/systemd/user/`, runs `systemctl --user daemon-reload`.
- Add nix-symlink detection to `generate_systemd.sh`: check if any existing unit is a symlink into `/nix/store`; if so, exit 1 with "managed by Nix" message. Defense-in-depth per DP5.
- Convert `scripts/setup.sh` lines 238-302 to a single call into `generate_systemd.sh`.
- Verify generated units match the units currently shipped by `scripts/setup.sh` byte-for-byte (after placeholder substitution).

### Step 3 — Add OS + runtime detection (≤ 0.5 day)

- Create `scripts/install/detect_runtime.sh` + `detect_os.sh`.
- Wire into `scripts/setup.sh`.
- Remove hardcoded `DOCKER="$(command -v docker)"` from setup.sh line 41 — replaced by detect_runtime.sh output.
- Add NixOS refusal path: if `linux-nixos` detected, print "yadgar appears to be running on NixOS. Use the nix flake (v5.46+) — see https://codeberg.org/maxagahi/yadgar#nixos-install" and exit 1.
- `yadgar/daemon.py`: `check_docker()` → `check_runtime()`. Returns `{ok, runtime: "podman"|"docker", version}`. Replace `["docker", ...]` with `[_RUNTIME, ...]` in highest-traffic callsites; add `# TODO(v5.46): propagate runtime var through all subprocess calls` for remaining ~20 callsites.

### Step 4 — Make target authoring (≤ 0.5 day)

Create top-level `Makefile` (GNU make required). Canonical target inventory:

```makefile
SHELL := /bin/bash
.PHONY: pre-setup setup uninstall uninstall-purge detect-runtime pull-images \
        install-units install-agents install-hooks config-sync install-rules \
        seed-anchors check help

pre-setup:
	@make --version | grep -q "GNU Make" || \
		(echo "ERROR: GNU make required. Install it and re-run." && exit 1)

setup: pre-setup detect-runtime pull-images install-units install-agents \
       install-hooks config-sync install-rules seed-anchors
	@echo "yadgar bootstrap complete."

detect-runtime:
	bash scripts/install/detect_runtime.sh
	bash scripts/install/detect_os.sh

pull-images:
	$$(scripts/install/detect_runtime.sh --print) pull docker.io/openfantasy/yadgar:$(VERSION)
	$$(scripts/install/detect_runtime.sh --print) pull docker.io/openfantasy/yadgar-backend:$(VERSION)

install-units:
	bash scripts/install/generate_systemd.sh
	systemctl --user daemon-reload
	systemctl --user enable --now yadgar.target

install-agents:
	yadgar install-subagents

install-hooks:
	yadgar install-hooks --scope global

config-sync:
	yadgar config sync

install-rules:
	bash scripts/install/append_claude_rules.sh

seed-anchors:
	yadgar seed --anchors install_assets/seeds/anchors.yaml

uninstall:
	bash scripts/install/uninstall.sh

uninstall-purge:
	bash scripts/install/uninstall.sh --purge

check:
	python -m pytest yadgar/tests/test_install.py -q

help:
	@grep -E '^[a-z][a-zA-Z_-]+:' Makefile | cut -d: -f1 | sort -u
```

Notes:
- `install-hooks` is daemon-independent — does not depend on `install-units`. Targets are independent (not ordered).
- NixOS guard fires inside `install-units` via `generate_systemd.sh` nix-symlink check.
- `yadgar install-hooks`, `yadgar install-subagents`, `yadgar config sync` are v5.44.0 building blocks — no re-implementation.

### Step 5 — Interactive prompt flow in `scripts/setup.sh` (≤ 0.5 day)

- Wire `detect_runtime.sh` + `detect_os.sh` output into `scripts/setup.sh` interactive flow.
- On NixOS: print refusal message + exit 1.
- On Linux/systemd: confirm before writing units (interactive) or proceed without prompt if `INSTALL_NONINTERACTIVE=1`.
- Keep `--db` / `--archive` restore flags from existing setup.sh.

### Step 6 — Package install assets in wheel (≤ 0.25 day)

- Add `[tool.hatch.build.targets.wheel.include]` (or equivalent hatchling include) so `install_assets/` ships in the wheel — systemd templates + seeds + CLAUDE.md fragment.
- `scripts/install/` becomes a symlink to `install_assets/scripts/` (or equivalent layout) so repo-checkout dev and wheel users share the same scripts.
- Verify wheel + sdist both contain the install assets.

### Step 7 — DECISIONS.md PD-37 entry (separate commit)

Append to `docs/DECISIONS.md` under a new dated section. Content:

- **Item:** Setup mechanism for non-NixOS installs.
- **Decision:** ADOPT — make-canonical with `make setup` as single entrypoint; composing standalone targets: `install-units` (systemd), `install-hooks` (v5.44.0 building block), `install-agents` (v5.44.0 building block), `config-sync` (v5.44.0 X5), `install-rules` (new: CLAUDE.md fragment), `seed-anchors` (new: workflow anchors bootstrap).
- **Reason:**
  - `make setup` is portable across any checkout-based workflow; single UX to learn.
  - v5.44.0 building blocks (`install-hooks`, `install-subagents`, `config-sync`) already exist, production-ready — make-canonical wires them together rather than re-implementing.
  - Seed-anchors + CLAUDE.md fragment fold back into v5.45.0: make-target structure keeps each capability one target (~30-100 LOC), no scope-creep risk.
  - Avoids two parallel paths (pipx + make) requiring separate maintenance.
  - pipx-only path (no repo checkout) deferred to v5.46.0 (Distribution) where PyPI/Homebrew/Nix flake metadata lands.
- **Alternatives considered:**
  - `yadgar install` as CLI entrypoint → rejected; creates two paths to learn and maintain.
  - `pipx install yadgar; yadgar install` → rejected for v5.45.0; pipx users have no repo path. Ship in v5.46.
  - Compose-canonical with systemd opt-in → superseded by make-canonical.
  - Seed anchors split to v5.45.1 → rejected; make-target structure makes scope manageable within v5.45.0.
- **Revisit triggers:** make unavailable on target platform; OR pipx-only user demand exceeds v5.46 schedule.

### Step 8 — Wiki update (best-effort)

Mirror PD-37 to wiki page `yadgar-decisions-log` via `wiki_add` or `wiki_update`. If yadgar MCP unavailable from the implementer's session, document as a follow-up.

### Step 9 — Version bump + MIGRATION_NOTES + CHANGELOG (≤ 0.25 day)

- Bump `pyproject.toml` 5.25.0 → 5.45.0 (skip-1 minor convention).
- Bump `server.json` core version to 5.45.0.
- `CHANGELOG.md` entry: "feat(install): portable make-canonical installer + Makefile + systemd unit extraction + seed anchors + CLAUDE.md rules."
- `MIGRATION_NOTES.md` v5.45.0 section: legacy `scripts/setup.sh` still works; new canonical UX is `make setup` (requires repo checkout; pipx-only path ships v5.46).

---

## Acceptance criteria

v5.45.0 ships when ALL of the following are true:

- [ ] `make setup` works on Linux (manual smoke test).
- [ ] `make uninstall` preserves `~/.yadgar/`; `make uninstall-purge` removes it.
- [ ] `make setup INSTALL_NONINTERACTIVE=1` accepts defaults + completes without prompts (CI-friendly).
- [ ] `make setup` on a NixOS host refuses with a clear "use nix flake" message + exits non-zero.
- [ ] `scripts/install/detect_runtime.sh` returns `podman` / `docker` / `none` deterministically; honors `YADGAR_CONTAINER_RUNTIME`.
- [ ] `scripts/install/detect_os.sh` returns `linux` / `linux-nixos` / `macos` / `other`.
- [ ] Generated systemd units pass `systemd-analyze verify` (Linux).
- [ ] `yadgar.target` brings up both core + backend via `systemctl --user start yadgar.target`.
- [ ] `make install-hooks` runs standalone (no daemon required).
- [ ] `make install-agents` runs standalone.
- [ ] `make install-rules` runs standalone; re-running deduplicates.
- [ ] `make seed-anchors` runs standalone; re-running is idempotent (no duplicate anchors).
- [ ] `pytest yadgar/tests/test_install.py` green.
- [ ] DECISIONS.md PD-37 entry merged (separate commit per workflow rule).
- [ ] Wiki page `yadgar-decisions-log` updated with PD-37 entry (best-effort; flagged if MCP unavailable).
- [ ] CHANGELOG.md v5.45.0 entry exists.
- [ ] MIGRATION_NOTES.md v5.45.0 section documents legacy path + new canonical UX.
- [ ] `python scripts/check_versions.py` exit 0.

**NOT in scope:** PyPI metadata polish, Homebrew tap, Nix flake, release automation, SBOM, update mechanism, macOS launchd (v5.45.1).

---

## Effort estimate (calendar days)

| Step | Days |
|---|---:|
| Step 0 pre-flight | 0.25 |
| Step 1 TDD scaffolding | 0.5 |
| Step 2 systemd unit extraction | 0.5 |
| Step 3 OS + runtime detection | 0.5 |
| Step 4 make target authoring | 0.5 |
| Step 5 interactive setup flow | 0.25 |
| Step 6 wheel asset packaging | 0.25 |
| Step 7 DECISIONS.md PD-37 | 0.1 |
| Step 8 wiki update | 0.1 |
| Step 9 version bump + docs | 0.25 |
| **Total** | **~1.5-2 calendar days** |

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Generated systemd units differ subtly from existing nix-managed units → existing user installs break on upgrade | Step 2 acceptance: byte-for-byte template match against current `scripts/setup.sh` output. Verify on a real NixOS-managed install. |
| Podman-on-macOS via `podman machine` fails health checks unexpectedly | Out of scope for v5.45.0 (Linux only). Deferred to v5.45.1 macOS launchd plan. |
| NixOS user runs `make setup` on a host that has both nix-managed AND pipx install → undetectable conflict | NixOS-detection refusal short-circuits before any state mutation. Defense-in-depth: `generate_systemd.sh` checks for nix-symlinked units. |
| Wheel package asset path resolution fails when installed via pipx → install scripts not found at runtime | `importlib.resources` resolution tested in Step 1 TDD. Fallback: bundle assets in `install_assets/` and resolve via `Path(__file__).parent`. |
| Interactive prompts break in non-TTY environments (CI, agent dispatch) | `INSTALL_NONINTERACTIVE=1` is the explicit CI path. Default behavior probes `sys.stdin.isatty()`; if false, refuses + suggests env knob. |
| Existing `scripts/setup.sh` callers (NixOS home-manager activation) break on the refactor | Keep `scripts/setup.sh` as-is for backward compatibility; v5.45 refactor adds NEW entry points. NixOS users continue via `home.activation.pipxYadgar` unchanged. |
| GNU make not available on target host | `pre-setup` target detects non-GNU make and exits with message. Document GNU make as prerequisite in README. |
| `make setup` requires repo checkout — pipx-only users excluded | Explicitly out of scope per user direction; v5.46.0 (Distribution) handles non-repo install via PyPI/Homebrew/Nix flake. |

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
8. `test_make_setup_non_interactive_accepts_defaults` — `make setup INSTALL_NONINTERACTIVE=1` completes; writes config + secrets + units; no prompts.
9. `test_make_setup_refuses_on_nixos` — mock NixOS detect → exits non-zero with explicit "use nix flake" message.
10. `test_make_setup_systemd_path` — mock Linux systemd; units written to expected location.
11. `test_make_setup_runtime_override` — `YADGAR_CONTAINER_RUNTIME=docker` honored even when podman detected.
12. `test_make_uninstall_preserves_data` — runs uninstall.sh without `--purge`; `~/.yadgar/` still exists.
13. `test_make_uninstall_purge_removes_data` — uninstall.sh `--purge`; `~/.yadgar/` removed.
14. `test_generate_systemd_unit_matches_template` — render with known placeholders; output matches expected file byte-for-byte.
15. `test_yadgar_target_includes_both_services` — generated `yadgar.target` has `Wants=yadgar.service yadgar-backend.service`.
16. `test_seed_anchors_idempotent` — running `make seed-anchors` twice does not duplicate entries (content-hash dedup).
17. `test_install_rules_deduplicates` — running `make install-rules` twice does not duplicate CLAUDE.md fragment.
18. `test_make_target_chain` — `make setup` triggers all expected sub-targets in correct order.
19. `test_nix_guard_in_generate_systemd` — defense-in-depth: `generate_systemd.sh` detects nix-symlinked units and exits non-zero.

---

## Coordination notes for main thread

- Plan-only doc → direct to master per workflow rule (wiki slug `yadgar-workflow-plan-commits-direct-to-master`).
- Implementation work requires a feature branch — `feat/v5.45.0-setup-foundation` is the obvious name. Branch from latest master after this plan commits.
- DECISIONS.md PD-37 entry committed separately per workflow.
- Related plans: `docs/PLAN_V5_46_0_DISTRIBUTION.md` (depends on this) + `docs/PLAN_V5_47_0_UPDATE_MECHANISM.md` (depends on this).
- macOS launchd split: `docs/PLAN_V5_45_1_MACOS_LAUNCHD.md` to be created before v5.45.1 is dispatched. Seed content: DP1 discussion from this plan's Open Question §1 + DP1 section of V5_45_0_DP_RESOLUTIONS.md.
- Implementer must read `docs/DECISIONS.md` PD-37 before re-scoping the setup mechanism choice.
