# v5.45.0 DP Resolutions — Setup Foundation (Linux only)

Resolved: 2026-06-04. Branch: docs/v5.45.0-dp-resolutions.
All empirical probes run against repo at /home/max/git/yadgar on NixOS host.

---

## DP1 — macOS launchd plist specifics

**STATUS:** DEFERRED — split to v5.45.2 (PLAN FILE NOT YET WRITTEN)

**DECISION:** macOS launchd work is out-of-scope for v5.45.0. v5.45.2 plan must be created before that version is dispatched.

**RATIONALE:**
- Plan header (line 7) correctly states: "macOS launchd path SPLIT to v5.45.2".
- No `PLAN_V5_45_2_*.md` file exists on disk. Split is declared but not yet materialized.
- Plan body still contains launchd content (Step 4 lines 207-213, new-files table entries for launchd templates, bundled assets layout, TDD test #15, acceptance criterion line 273, risks table entry) — all contradicting the header's scope cut.
- `RunAtLoad=true KeepAlive=true` vs `bootstrap`/`load` decision is valid and should carry forward verbatim into v5.45.2 plan when written.

**ACTION ITEMS:**
1. Main thread creates `docs/PLAN_V5_45_2_MACOS_LAUNCHD.md` before v5.45.2 is dispatched. Content seed: DP1 discussion from lines 131-135 of PLAN_V5_45_0.
2. Plan body amendments required — see "Plan body amendments" section below.

---

## DP2 — Container runtime detect ordering

**STATUS:** RESOLVED (with scope caveat)

**DECISION:** `detect_runtime.sh` and `detect_os.sh` (shell) + `detect_runtime()` (Python shim in `yadgar/daemon.py`) use ordering: podman → docker → error. Override via `YADGAR_CONTAINER_RUNTIME`. For v5.45.0 (Linux only), podman-on-macOS via `podman machine` is out-of-scope.

**RATIONALE:**

Empirical probes:

- Host has both `/run/current-system/sw/bin/podman` and `/run/current-system/sw/bin/docker` — both installed.
- `scripts/setup.sh` line 41 hardcodes `DOCKER="$(command -v docker)"` — no podman preference today.
- `yadgar/daemon.py:check_docker()` (lines 663-684) is docker-hardcoded: `["docker", "version", ...]`. No podman fallback.
- `yadgar/daemon.py` has ~20+ additional docker-hardcoded callsites beyond `check_docker()`: `subprocess.run(["docker", ...])` appears in `start()`, `stop()`, `start_backend()`, `pull()`, `push()`, `build()`, `exec_in_container()`, `_image_exists()`, `_container_running()`, `_container_exists()`, `_ensure_network()`.

Scope note: The plan describes `check_docker()` → `check_runtime()` as if it's a small rename. It is not — generalizing all ~20 callsites is a significant refactor. v5.45.0 must decide scope explicitly.

**Recommended scope for v5.45.0:**
- `detect_runtime.sh` (shell): full podman-first logic + `YADGAR_CONTAINER_RUNTIME` override. Used by `scripts/setup.sh` and Makefile.
- Python (`yadgar/daemon.py`): rename `check_docker()` → `check_runtime()` + add detection logic. Leave `subprocess.run(["docker", ...])` callsites untouched in v5.45.0 — just replace the literal string with a variable resolved at startup via `check_runtime()`. Add a `TODO(v5.46): propagate runtime var through all subprocess calls` comment.
- Full subprocess generalization deferred to v5.46 (distribution) where the daemon code gets a proper overhaul.

Linux-only fallback chain (no macOS podman machine complexity):
1. If `YADGAR_CONTAINER_RUNTIME` set: use that value; validate with `<runtime> info`; if fails → error + message "YADGAR_CONTAINER_RUNTIME=<value> set but <value> info failed — is the daemon running?"
2. Probe `podman info` → if exit 0, use podman.
3. Probe `docker info` → if exit 0, use docker.
4. Neither: exit 1 with message: "No container runtime found. Install podman (recommended) or docker, ensure the daemon is running, then re-run yadgar install."

Error message text (canonical):
```
No container runtime found.
  Install podman: https://podman.io/getting-started/installation
  Or install docker: https://docs.docker.com/engine/install/
Ensure the daemon is running, then re-run: yadgar install
```

Env knob name: `YADGAR_CONTAINER_RUNTIME` (already in plan, confirmed canonical).

**ACTION ITEMS:**
1. `scripts/install/detect_runtime.sh`: implement 4-step chain above.
2. `yadgar/daemon.py`: `check_docker()` → `check_runtime()`, returns `{ok, runtime: "podman"|"docker", version}`. Add `_RUNTIME` module-level var populated at first call. Replace `["docker", ...]` with `[_RUNTIME, ...]` in the 3-4 highest-traffic callsites; add TODO for the rest.
3. Plan body line 101: update the "Existing callers updated to the new signature (callsite count: 3)" claim — actual callsite count is ~20+; v5.45.0 only fully migrates check_docker + the highest-traffic paths.

---

## DP3 — `yadgar install` vs `yadgar setup` — canonical naming

**STATUS:** RESOLVED

**DECISION:** `install` is the new canonical top-level command. `setup` stays as a registered alias pointing to the same handler. Deprecation notice added to `setup` output starting v5.45.0. Formal removal in v5.50.0 (provides ~5 minor versions lead time).

**RATIONALE:**

Current state:
- `yadgar/__main__.py` registers: `drain`, `export`, `restore`, `capture`, `context`, `stats`, `vacuum`, `seed`, `config`, `rules`, `viz`, `setup`, `daemon`, `install_hooks`, `install_subagents`.
- No `install` subcommand exists today.
- `install_hooks` and `install_subagents` are siblings — adding bare `install` could conflict if future subcommands are `install hooks`, `install subagents` etc.

Decision on verb structure: `install` is a standalone command, not a parent verb with subcommands. Rationale: the existing sibling commands `install_hooks` and `install_subagents` use underscores, not hierarchical subparsers — so `install` as a parent would be an inconsistent pattern. Keep flat: `yadgar install` (interactive installer), `yadgar install-hooks` (or keep `install_hooks`), `yadgar install-subagents` (or keep `install_subagents`). Rename of siblings to use hyphen is in scope for v5.45.0 cosmetic cleanup.

Deprecation mechanism:
```python
# in cmd_setup:
import warnings
warnings.warn(
    "'yadgar setup' is deprecated since v5.45.0 and will be removed in v5.50.0. "
    "Use 'yadgar install' instead.",
    DeprecationWarning,
    stacklevel=2,
)
print("Warning: 'yadgar setup' is deprecated. Use 'yadgar install' instead.", file=sys.stderr)
```

References needing updates:
- `yadgar/__main__.py`: add `install.register(subparsers)` + keep `setup.register(subparsers)`.
- `yadgar/cli/setup.py`: add deprecation warning in `cmd_setup`; `cmd_install` (new name for the promoted function) lives in `yadgar/cli/install.py`.
- `README.md`, `MIGRATION_NOTES.md`, `scripts/setup.sh` user-facing output: update `yadgar setup` references to `yadgar install`.
- Any existing docs referencing `yadgar setup`: scan with `grep -r "yadgar setup" docs/`.

**ACTION ITEMS:**
1. Create `yadgar/cli/install.py` with promoted `cmd_install`.
2. `yadgar/cli/setup.py:cmd_setup`: thin alias calling `cmd_install` + emitting deprecation warning.
3. `yadgar/__main__.py`: import + register `install` subcommand; keep `setup` registration.
4. Scan docs/ for `yadgar setup` references and update.

---

## DP4 — Hook installation delegation

**STATUS:** RESOLVED

**DECISION:** Hook install is stand-alone. No daemon required. Makefile `install-hooks` target may be invoked before daemon is up. Preferred Makefile ordering: `make setup` (daemon start) and `make install-hooks` are independent targets; `make all` sequences setup → install-hooks, but both work in isolation.

**RATIONALE:**

`yadgar/install_hooks_lib.py` read in full (369 lines). Key findings:
- Pure Python. Zero imports from `yadgar.daemon`, `yadgar.server`, `yadgar.config`, or any module that reads from a running daemon.
- Only I/O: filesystem reads/writes to `~/.claude/` and `<project>/.claude/`.
- Auth env block reads `os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")` — optional; writes empty dict if not set. Does not require daemon to be running to get the token.
- `is_running_in_container()` checks `YADGAR_IN_CONTAINER` env var — pure env read, no daemon call.
- All hook scripts are copied from `Path(__file__).parent / "hooks"` — package-relative path, no daemon dependency.

Conclusion: stand-alone confirmed. No daemon dependency path found.

Makefile target ordering recommendation:
```makefile
.PHONY: setup install-hooks all

setup:
	yadgar install $(INSTALL_FLAGS)

install-hooks:
	yadgar install-hooks --scope global

# Convenience: full stack in one shot
all: setup install-hooks

uninstall:
	scripts/install/uninstall.sh

uninstall-purge:
	scripts/install/uninstall.sh --purge

check:
	python -m pytest yadgar/tests/test_install.py -q
```

`install-hooks` does NOT depend on `setup`. Users who only want hooks (e.g. existing NixOS install managed by nix) can run `make install-hooks` without touching daemon config.

**ACTION ITEMS:**
1. Plan body line 140: update text "Makefile must invoke this AFTER `make setup` brings the daemon up" — this is WRONG per code evidence. Change to: "Hook install is daemon-independent; Makefile targets are independent (not ordered)."
2. Makefile template above is the canonical one for Step 5.

---

## DP5 — Backward compatibility for systemd unit names

**STATUS:** RESOLVED (with drift flag)

**DECISION:** `yadgar.target` uses `Wants=yadgar.service yadgar-backend.service`. `yadgar-backend.service` is the canonical backend unit name. `yadgar-db.service` (from `daemon.py:install_systemd_service`) is drift — do not use. Conflict detection logic required in installer.

**RATIONALE:**

Empirical probe — `systemctl --user list-unit-files | grep yadgar` on this host:
```
yadgar-vacuum-trigger.path                  enabled
yadgar-backend.service                      enabled
yadgar-nightly-cycle.service                linked
yadgar-vacuum-trigger.service               linked
yadgar-vacuum.service                       linked
yadgar-worktree-sweep.service               linked
yadgar.service                              enabled
yadgar-nightly-cycle.timer                  enabled
yadgar-vacuum.timer                         enabled
yadgar-worktree-sweep.timer                 enabled
```

Canonical names on this (NixOS-managed) host: `yadgar.service` + `yadgar-backend.service`. No `yadgar.target` exists — additive, no collision.

Drift detected: `yadgar/daemon.py:install_systemd_service()` (lines 455-557) writes `yadgar-db.service` (line 543 `db_service_name = f"yadgar-db{suffix}.service"`). This is inconsistent with `scripts/setup.sh` (line 238 writes `yadgar-backend.service`) and the deployed units on this host. `yadgar-db.service` name is either dead code or was never deployed. Implementer must not introduce `yadgar-db.service` as a third canonical name.

`yadgar.target` content (canonical):
```ini
[Unit]
Description=Yadgar Memory Engine — full stack
Wants=yadgar.service yadgar-backend.service
After=yadgar.service yadgar-backend.service

[Install]
WantedBy=default.target
```

Conflict detection logic for installer (in `generate_systemd.sh`):
```bash
# Detect if existing units are nix-managed (symlinks into nix store)
for unit in yadgar.service yadgar-backend.service; do
    unit_path="$HOME/.config/systemd/user/$unit"
    if [ -L "$unit_path" ]; then
        target=$(readlink "$unit_path")
        if echo "$target" | grep -q "/nix/store"; then
            echo "ERROR: $unit is managed by Nix (symlink → $target)."
            echo "  Do not use 'yadgar install' on NixOS — use the nix flake (v5.46+)."
            exit 1
        fi
    fi
done
```

Note: NixOS refusal should already be caught upstream by `detect_os.sh` returning `linux-nixos`. This is a defense-in-depth check for the case where `--force` bypass is added later.

Mixed install scenario (nix-managed yadgar.service + new yadgar.target): yadgar.target is purely additive — `Wants=` does not conflict with independently-managed `yadgar.service`. systemd ignores Wants= targets for already-running units. No collision.

**ACTION ITEMS:**
1. `scripts/install/yadgar.target.in`: use content above.
2. `scripts/install/generate_systemd.sh`: add nix-symlink detection logic above before writing any unit files.
3. `yadgar/daemon.py:install_systemd_service()`: add a deprecation warning; note `yadgar-db.service` name is non-canonical drift; do not use in v5.45.0 template. Consider removing this method in v5.46.
4. Plan body line 68: update "No `yadgar.target` group" gap note — correct. But add note that `yadgar-db.service` name in daemon.py is drift vs deployed `yadgar-backend.service`.
5. Acceptance criterion (line 274): update to `systemctl --user start yadgar.target` — already correct.

---

## DP6 — Seed anchors + bundled CLAUDE.md fragment

**STATUS:** DEFERRED — split to v5.45.1 (PLAN FILE NOT YET WRITTEN)

**DECISION:** Seed anchors + CLAUDE.md fragment are out-of-scope for v5.45.0. v5.45.1 plan must be created before that version is dispatched.

**RATIONALE:**
- Plan header (line 8) correctly states: "Seed-anchors + bundled CLAUDE.md fragment SPLIT to v5.45.1".
- No `PLAN_V5_45_1_*.md` file exists on disk. The plan's header declares the split but the target plan has not been written.
- Plan body lines 144-168 still describe DP6 in full detail — this content belongs in PLAN_V5_45_1, not PLAN_V5_45_0.

**Recommendation on lines 144-168:** PRUNE from PLAN_V5_45_0. The detail is the v5.45.1 seed for the plan author. Keeping it in PLAN_V5_45_0 is misleading — it signals scope that was explicitly cut. Move verbatim to PLAN_V5_45_1_SEED_ANCHORS_CLAUDE_FRAGMENT.md when creating that file.

**ACTION ITEMS:**
1. Main thread creates `docs/PLAN_V5_45_1_SEED_ANCHORS_CLAUDE_FRAGMENT.md` before v5.45.1 is dispatched. Seed content: lines 144-168 from PLAN_V5_45_0 (transplant verbatim).
2. Main thread creates `docs/PLAN_V5_45_2_MACOS_LAUNCHD.md` before v5.45.2 is dispatched. Seed content: DP1 lines 131-135 from PLAN_V5_45_0.
3. Plan body amendments — see section below.

---

## Plan body amendments (apply to PLAN_V5_45_0_SETUP_FOUNDATION.md)

Main thread applies these edits. Listed by line range. All changes bring body into sync with header scope cut (Steps cap: 1-3 + 5 + 8-9 only; ~1d revised effort; Linux only).

### 1. Remove Step 4 — macOS launchd (lines 207-213)

Delete entire Step 4 block:
```
### Step 4 — Add macOS launchd path (≤ 0.5 day)
...
- Tested via mock on Linux + manual smoke on macOS (deferred to acceptance).
```
Replace with a one-liner:
```
### Step 4 — REMOVED (macOS launchd split to v5.45.2)
```
Or simply delete and renumber: Step 5 becomes Step 4. (Renumbering preferred to avoid confusion.)

### 2. Remove Step 6 — wheel asset packaging

Delete entire Step 6 block (lines 225-229). Per header "Steps cap: 1-3 + 5 + 8-9 only" — Step 6 is excluded. Wheel packaging deferred to v5.46.0.

### 3. Remove Step 7 — DECISIONS.md PD-37 entry

Delete entire Step 7 block (lines 231-248). Per header steps cap — Step 7 excluded. Note: if implementer chooses to include it anyway as "low-effort", that's a judgment call — but the plan should not list it as an in-scope step.

### 4. Update effort table (lines 288-300)

Remove rows for Step 4, 6, 7 from the table. Adjust total from "2-3 calendar days" to "~1 calendar day" per header. The revised steps cap should reflect Steps 0-3 + 5 (renamed) + 8-9.

### 5. Remove launchd entries from "New files" table (lines 86, 90-91)

Remove these rows:
- `scripts/install/generate_launchd.sh`
- `scripts/install/com.openfantasy.yadgar.plist.in`
- `scripts/install/com.openfantasy.yadgar-backend.plist.in`

### 6. Remove launchd from bundled assets layout (lines 120-122)

Remove the `launchd/` directory subtree from the layout block:
```
└── launchd/
    ├── com.openfantasy.yadgar.plist.in
    └── com.openfantasy.yadgar-backend.plist.in
```

### 7. Remove macOS acceptance criteria (line 266, 273)

Line 266: `- [ ] make setup works on Linux + macOS (manual smoke tests on both).`
→ Change to: `- [ ] make setup works on Linux (manual smoke test).`

Line 273: `- [ ] Generated launchd plists pass plutil -lint (macOS).`
→ DELETE (macOS only; out of scope).

### 8. Remove TDD test #15 (lines 344-345)

Delete `test_generate_launchd_plist_matches_template` entry. macOS-only test moved to v5.45.2.

### 9. Remove launchd risk from risks table (lines 309-310)

Delete row: `launchd plist semantics differ from systemd...`

### 10. Prune DP6 body (lines 144-168)

Delete lines 144-168 (the full DP6 discussion including candidate seed anchors, design questions, why-this-slot, resolve-before-impl). Replace with a single pointer:
```
6. **Seed-anchors + bundled CLAUDE.md fragment** — SPLIT to v5.45.1. See `docs/PLAN_V5_45_1_SEED_ANCHORS_CLAUDE_FRAGMENT.md` (to be created).
```

### 11. Fix DP4 hook delegation text (line 140)

Current: "Makefile must invoke this AFTER `make setup` brings the daemon up."
Replace with: "Hook install is daemon-independent (install_hooks_lib.py is pure-Python with no daemon imports). `make install-hooks` can run before, after, or without `make setup`."

### 12. Fix daemon.py callsite count (line 101)

Current: "Existing callers updated to the new signature (callsite count: 3, all in `yadgar/cli/`)"
Replace with: "Existing callers updated. Note: `yadgar/daemon.py` has ~20+ docker-hardcoded subprocess callsites; v5.45.0 migrates check_docker() + highest-traffic paths; remaining callsites deferred to v5.46.0 with TODO comments."

### 13. Add drift note for yadgar-db.service (line 68)

In the systemd units row, append to the Gap column:
"Note: `yadgar/daemon.py:install_systemd_service()` writes `yadgar-db.service` (non-canonical drift vs deployed `yadgar-backend.service`). Do not use this method; it will be deprecated in v5.46.0."

### 14. Update Open Questions section numbering/status

After applying amendment #10 (prune DP6 body), add status tags to each DP line:
- DP1: add `[DEFERRED → v5.45.2, plan not yet written]`
- DP2: add `[RESOLVED — see V5_45_0_DP_RESOLUTIONS.md]`
- DP3: add `[RESOLVED — see V5_45_0_DP_RESOLUTIONS.md]`
- DP4: add `[RESOLVED — see V5_45_0_DP_RESOLUTIONS.md]`
- DP5: add `[RESOLVED — see V5_45_0_DP_RESOLUTIONS.md]`
- DP6: add `[DEFERRED → v5.45.1, plan not yet written]`

---

*Generated by subagent run 2026-06-04 against branch docs/v5.45.0-dp-resolutions.*
