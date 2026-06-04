# v5.45.0 DP Overrides — Make-canonical setup

Drafted 2026-06-04 after user direction post-agent-resolution. Supersedes specific decisions in `V5_45_0_DP_RESOLUTIONS.md`. Agent's empirical findings (DP2 runtime detection, DP4 hook-lib daemon-independence, DP5 systemd unit drift) stand unchanged.

---

## User directives (verbatim intent)

1. **`make setup` is the canonical entrypoint.** All setup happens through make. No "pipx install + yadgar install" gymnastics. Repo checkout is the contract.
2. **All v5.44.0 bootstrap features must be wired into `make setup`:** Claude Code hooks, bundled agent templates, config.yaml sync, CLAUDE.md rules deployment.
3. Implication: v5.45.1 (seed anchors + CLAUDE.md fragment) **un-splits** back into v5.45.0 — make-canonical means make covers everything.

---

## DP3 OVERRIDE — Make-canonical, no user-facing `yadgar install`

**STATUS:** RESOLVED — overrides agent's `yadgar install` canonical decision.

**DECISION:**
- `make setup` is the only canonical user-facing install entrypoint.
- No new `yadgar install` CLI subcommand. The agent's plan to add `yadgar install` + deprecate `yadgar setup` is dropped.
- Existing `yadgar/cli/setup.py` (config.yaml + secrets.env writer) remains but is RENAMED internally to a non-user-facing helper (or kept as `yadgar setup` for back-compat with no deprecation noise — it just becomes one of the building blocks `make setup` orchestrates).
- Sibling commands `yadgar install-hooks` and `yadgar install-subagents` (v5.44.0) stay as building blocks make targets invoke. No rename to hyphenated form needed (DP3 cosmetic cleanup item dropped).
- `yadgar config sync` (v5.44.0 X5) stays as building block.

**RATIONALE:**
- User explicitly directed: "the setup needs to be as simple as `make setup`. all the setup needs to be done through make."
- Adding a `yadgar install` CLI on top of make targets creates two paths to learn + two surfaces to keep in sync. Worse UX, more code.
- Existing `yadgar install-hooks` / `yadgar install-subagents` are MCP-tool-backed primitives. They stay. Make orchestrates them.
- pipx-distributed users (no repo checkout) — out of scope for v5.45.0. v5.46.0 (Distribution) handles that path with PyPI/Homebrew/Nix flake metadata. If make is unavailable, v5.46 ships an alternate.

**ACTION ITEMS:**
1. Do NOT create `yadgar/cli/install.py`.
2. Do NOT register `install` subcommand in `yadgar/__main__.py`.
3. Do NOT add deprecation warning to `cmd_setup`.
4. Plan body amendment: rewrite "interactive `yadgar install`" references in goal/scope to "interactive `make setup`".

---

## DP6 OVERRIDE — Seed anchors + CLAUDE.md fragment fold BACK into v5.45.0

**STATUS:** RESOLVED — overrides agent's "defer to v5.45.1" decision.

**DECISION:**
- v5.45.1 split is cancelled. Seed anchors + CLAUDE.md fragment fold back into v5.45.0 as separate make targets.
- New make targets: `make install-rules` (CLAUDE.md fragment append, dedupe) + `make seed-anchors` (bootstrap canonical anchors via memorize).
- `make setup` invokes both as part of full bootstrap chain.
- Standalone invocation supported (user can run just `make install-rules` if they already have a working setup).

**RATIONALE:**
- User directive: "all the setup needs to be done through make" → make-canonical means CLAUDE.md rules deployment is a make target, not a separately-shipped v5.45.1 release.
- Splitting was originally motivated by scope-creep risk. With make-target structure, each capability is one target = ~30-100 LOC. No ballooning.
- Keeps v5.45 release atomic: one user-visible release for "yadgar bootstrap exists." Avoids forcing user to remember v5.45.0 vs v5.45.1 install order.

**ACTION ITEMS:**
1. Restore lines 144-168 in PLAN_V5_45_0 (do NOT prune as agent's amendment #10 suggests).
2. Update DP6 section header: status = RESOLVED (was DEFERRED).
3. Add `seed-anchors` + `install-rules` to make target inventory (next section).
4. Cancel `docs/PLAN_V5_45_1_SEED_ANCHORS_CLAUDE_FRAGMENT.md` creation (was action item from DP6 deferral).
5. Renumber: v5.45.2 (macOS launchd) becomes v5.45.1 (next split-out). Or keep .2 numbering for stability.

---

## Make target inventory (canonical)

```makefile
.PHONY: setup uninstall uninstall-purge install-hooks install-agents install-rules config-sync seed-anchors check help

# Canonical full-bootstrap entry
setup: detect-runtime pull-images install-units install-agents install-hooks config-sync install-rules seed-anchors
	@echo "yadgar bootstrap complete."

# Building blocks (each independently invokable)

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
	bash scripts/install/append_claude_rules.sh   # dedupes + appends fragment from install_assets/CLAUDE.md.fragment

seed-anchors:
	yadgar seed --anchors install_assets/seeds/anchors.yaml

uninstall:
	bash scripts/install/uninstall.sh

uninstall-purge:
	bash scripts/install/uninstall.sh --purge   # also removes ~/.yadgar/

check:
	python -m pytest yadgar/tests/test_install.py -q

help:
	@grep -E '^[a-z][a-zA-Z_-]+:' Makefile | cut -d: -f1 | sort -u
```

Notes:
- `setup` chains all building blocks. Each is also a standalone target.
- NixOS guard fires inside `install-units` via `scripts/install/generate_systemd.sh` nix-symlink check (DP5 agent finding stands).
- macOS launchd path absent — v5.45.2 (still deferred).
- `yadgar seed --anchors <file>` is a new building-block command. v5.45.0 ships it as part of the make-seed-anchors chain.

---

## New scope additions (vs agent's resolution)

| Item | Source | LOC estimate |
|---|---|---|
| `scripts/install/append_claude_rules.sh` | DP6 fold-back | ~40 |
| `install_assets/CLAUDE.md.fragment` | DP6 fold-back | ~150 chars body |
| `install_assets/seeds/anchors.yaml` | DP6 fold-back | ~8 entries (per DP6 candidates list) |
| `yadgar/cli/seed.py` — new `--anchors <file>` flag | DP6 fold-back | ~50 |
| `Makefile` at repo root | DP3 override (make-canonical) | ~50 |
| 4 new test entries (seed loader idempotency + rules dedupe + make target chain + nix-guard) | TDD | n/a (per I27) |

Effort revision: agent's ~1d revised to ~1.5-2d. Still surgical. Make-centric structure means each target is small + independently testable.

---

## What stays from agent's resolution (unchanged)

- **DP1 (macOS launchd):** deferred to v5.45.2. Plan file PLAN_V5_45_2_MACOS_LAUNCHD.md still to be written.
- **DP2 (runtime detection):** podman → docker → error. `YADGAR_CONTAINER_RUNTIME` override. Scope caveat: v5.45.0 migrates `check_docker()` + highest-traffic paths only; ~20 callsite full sweep → v5.46.0.
- **DP4 (hook install delegation):** stand-alone confirmed. `install_hooks_lib.py` has zero daemon imports. `make install-hooks` is daemon-independent. Plan line 140 still needs fix (text says "after daemon up" — wrong).
- **DP5 (systemd unit drift):** `yadgar.target` uses `Wants=yadgar.service yadgar-backend.service`. `yadgar-db.service` in `daemon.py:install_systemd_service()` is drift — deprecate in v5.46. Nix-symlink defense-in-depth check stands.

---

## Revised plan body amendments to PLAN_V5_45_0_SETUP_FOUNDATION.md

Supersedes agent's 14-item amendment list where overlapping.

### Apply from agent's amendments (DP1/DP2/DP4/DP5 stand)

- **#1** Remove Step 4 macOS launchd ✓ apply (deferred to v5.45.2)
- **#5** Remove launchd entries from "New files" table ✓ apply
- **#6** Remove launchd from bundled assets ✓ apply
- **#7 line 273** Delete launchd plutil acceptance ✓ apply (line 266 keep cross-OS smoke for Linux only)
- **#8** Remove TDD test #15 (launchd template match) ✓ apply
- **#9** Remove launchd risk row ✓ apply
- **#11** Fix DP4 hook delegation text ✓ apply
- **#12** Fix daemon.py callsite count ✓ apply
- **#13** Add yadgar-db.service drift note ✓ apply

### Override agent's amendments (DP3/DP6 reframed)

- **#2** (Remove Step 6 wheel packaging): KEEP STEP 6 partial — wheel bundles `install_assets/{seeds,agents,CLAUDE.md.fragment,systemd templates}`. v5.45.0 needs wheel-asset packaging because make targets reference these assets via `importlib.resources.files()`. Don't fully delete; reduce scope to bundled-assets only (no PyPI metadata polish — that's v5.46).
- **#3** (Remove Step 7 PD-37 DECISIONS entry): KEEP STEP 7 with revised content — PD-37 now records make-canonical decision + seed-anchors fold-back, not install-vs-setup naming.
- **#4** (Effort table → ~1d): UPDATE to ~1.5-2d to reflect seed-anchors + rules fold-back.
- **#10** (Prune DP6 body lines 144-168): DO NOT PRUNE — content stays in PLAN_V5_45_0. Update status from "DISCUSSION ITEM" to "RESOLVED (folded; see make targets)."
- **#14** Update DP status tags:
  - DP1: `[DEFERRED → v5.45.2, plan not yet written]`
  - DP2: `[RESOLVED — see V5_45_0_DP_RESOLUTIONS.md]`
  - DP3: `[RESOLVED via V5_45_0_DP_OVERRIDES.md — make-canonical, no yadgar install CLI]`
  - DP4: `[RESOLVED — see V5_45_0_DP_RESOLUTIONS.md]`
  - DP5: `[RESOLVED — see V5_45_0_DP_RESOLUTIONS.md]`
  - DP6: `[RESOLVED via V5_45_0_DP_OVERRIDES.md — folded back, make targets cover]`

### New amendments (from overrides)

- **#15 NEW:** Goal section (line 33-45): rewrite "pipx install yadgar; yadgar install" → "git clone + make setup". Goal entrypoint = make.
- **#16 NEW:** Current state table (line 60-72): add row for v5.44.0 building blocks — `yadgar install-subagents`, `yadgar install-hooks`, `yadgar config sync` (all exist, production-ready) — and note they become make-target dependencies.
- **#17 NEW:** Scope file changes (line 76-105): add `Makefile` at repo root + `scripts/install/append_claude_rules.sh` + `install_assets/CLAUDE.md.fragment` + `install_assets/seeds/anchors.yaml` + `yadgar/cli/seed.py` `--anchors` flag.
- **#18 NEW:** Plan steps: add Step 4 (renumbered, was macOS launchd) = "make target authoring" — Makefile + chain wiring + standalone targets. Step ordering: 0 pre-flight, 1 TDD scaffolding, 2 systemd extract, 3 OS+runtime detect, 4 NEW make targets, 5 interactive prompt, 6 wheel assets (reduced scope), 7 PD-37 DECISIONS, 8 wiki update, 9 version bump.
- **#19 NEW:** Acceptance: add `make setup` end-to-end smoke acceptance + standalone `make install-hooks` / `make install-agents` / `make install-rules` / `make seed-anchors` acceptance.

---

## Risk update

- **NEW RISK — make Makefile portability.** GNU make vs BSD make difference matters if any user is on a non-GNU host. Mitigation: declare `SHELL=/bin/bash` + use GNU-only syntax explicitly; document GNU make as a prerequisite. Refusal on non-GNU make detected via `make --version | grep -q "GNU Make"` in `pre-setup` target.
- **NEW RISK — repo-checkout assumption.** `make setup` requires the repo cloned. pipx-only users have no path in v5.45.0. Mitigation: out-of-scope per user direction; v5.46.0 (Distribution) handles non-repo install via PyPI/Homebrew/Nix flake.

---

## Open items for main thread before dispatch

1. Confirm: drop `yadgar install` CLI entirely (no thin shim, no deprecation). [User direction implies yes.]
2. Confirm: cancel v5.45.1 plan creation. v5.45.1 number is now free; reassign to v5.45.2 (macOS) if user wants tighter numbering, or leave .1 as gap and ship .2 next.
3. Confirm: GNU-make-only prerequisite acceptable (or do we need BSD-make-compatible Makefile)?

If all three default to "yes / leave as gap / GNU-only OK", main thread can apply the 19 amendments + dispatch implementation.

---

*Generated 2026-06-04 by main thread synthesis of user directives + agent resolution.*
