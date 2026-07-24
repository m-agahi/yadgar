# PreCompact async hook + global-authoritative install — 2026-07-22

**Task:** #103 Phase 2 (PreCompact slows `/compact`).
**Status:** PR-1 IN PROGRESS (`fix/precompact-async-hook`, core 5.162.0). PR-2 PLANNED.

## Context

Claude Code `PreCompact` hooks are **blocking + serial** — the harness waits for the
hook to exit before compacting. Yadgar's drain hook (`hook_pre_compact_drain`,
`yadgar/core/cli/hook.py:246`) runs synchronously (Python cold-start + host transcript
parse + backend POST), so `/compact` stalls. User's direct observation: `/compact` was
instantaneous before any PreCompact hook existed. (#103 Phase 1 already removed the
older 34s OTLP-taxed global `.sh` drain; this is the residual block.)

Fix = the official `async: true` command-hook field
(`{"type":"command","command":"…","async":true}`) — fire-and-forget, non-blocking,
timeout still applies. Verified against code.claude.com/docs/en/hooks.md; supported on
all events incl PreCompact; user on Claude Code 2.1.191. Our drain is a **pure
side-effect, stdout-silent** hook (no print / additionalContext / systemMessage) → the
exact case async is for. Correctness holds: the `.jsonl` transcript is append-only and
compaction never rewrites it, so a background drain still captures the full
pre-compaction state — async removes only the *wait*, not the capture.

Investigation also surfaced a pre-existing mess to fix in PR-2: **two hook
implementations** — nix jq hand-writes 11 global hook events using individual
`yadgar-<event>` scripts; `yadgar install` writes a subset via the unified
`hook_runner.py` dispatcher — and SessionStart/PostToolUse/UserPromptSubmit **already
double-fire** in the yadgar repo (global + project both define them).

## PR-1 — async on pre-compact-drain (core 5.162.0)

Targeted: async ONLY on pre-compact-drain. Never on the blocking/injecting hooks —
prompt-recall (stdout memory injection), SessionStart (context inject), PreToolUse
(permission deny) would all break under async.

- `yadgar/core/install/_settings.py`
  - `_make_hook_entry(cmd, matcher, env_block, async_: bool = False)` — set
    `entry["hooks"][0]["async"] = True` only when `async_`; omit the key otherwise.
  - `_runner_entry(hook_type, matcher="", async_=False)` (nested in `_build_core_hooks`,
    line 173) threads it through.
  - Line 177 → `_runner_entry("pre-compact-drain", async_=True)`. Others unchanged.
- Version: core 5.161.0 → 5.162.0 (backend unchanged); `scripts/check_versions.py` green.
- **TDD:** `_make_hook_entry(async_=True)` sets the key, default omits it;
  `_build_core_hooks` output has async on PreCompact and NOT on the other four events;
  extend existing fixtures (`yadgar/tests/hooks/test_install_hooks_stable_python.py`
  ~L275).
- Ship: branch off `origin/master`, codeberg bot identity, `.forgejo/PULL_REQUEST_TEMPLATE.md`
  (5 sections), no `--no-verify`, no co-author trailer.

## PR-2 — global-authoritative install (Option B)

Converge to ONE product-owned implementation, driven by nix activation calling the
installer. Fixes drift, the two-impl split, and the double-fire.

### Product (`yadgar/core/install/`)
- **Foreign-preserve `_build_core_hooks`** (`_settings.py:156`): replace the
  replace-always `hooks_config[event] = [...]` assignments with the existing
  `_append_if_absent(..., managed_basename="yadgar-<event>.py")` pattern
  (`_settings.py:75`) — strips only yadgar-managed entries (substring match on the
  managed basename), preserves foreign entries. This is what keeps the **caveman**
  SessionStart hook alive.
- CLI already supports `--scope global` (`yadgar/core/cli/install_hooks.py:44`, default
  `global`). MCP `install_hooks(scope=…)` defaults `project`.
- Global scope already writes the full set (core + append hooks
  InstructionsLoaded/SubagentStart/FileChanged + Stop/SessionEnd). Note: SubagentStop
  append hook was removed in ADR-0156 — the install_hooks_impl return string still
  lists it (stale message, `install_hooks_lib.py:258`); nix still copies+wires
  `subagent-stop.py` → drop on convergence.
- Own version bump + ADR (global-authoritative hook install).

### nix (`nix/modules/home/yadgar.nix`) — handed to user via MIGRATION_NOTES (guard)
- Replace the `home.activation.yadgarHooks` jq block (~L299-394) with an activation
  call: `yadgar install-hooks --scope global` (DAG: `entryAfter [ "pipxYadgar" ]`,
  before/independent of `claudeCodeSettings` caveman inject).
- Retire manual `yadgarCopyHooks` (~L259-288) once the installer copies its own scripts.
- Drop the PreCompact-strip filter (~L315-322) — async replaces its reason.
- `~/.claude/settings.json` is a **mutable** file (no `home.file` symlink) — installer
  can jq/atomic-write it in place. Confirmed.
- Caveman SessionStart entry lives in `nix/dotfiles/common/claude-settings.jq` (keyed on
  `plugins/cache/caveman`), injected by `claudeCodeSettings`. The foreign-preserve
  installer must not clobber it (its strip predicate is the yadgar basename only).

### Kill double-fire
- With global authoritative, no per-project core-hook install. Clear this repo's
  project `.claude/settings.json` core entries (untracked + gitignored → no git coord).

## Sequence
PR-1 async (now) → user confirms `/compact` instant in a fresh session → PR-2 Option B
(product foreign-preserve + nix→installer + kill double-fire) → nix switch (user) →
verify single-fire + fast compact across projects.

## Verification
- PR-1: unit + fixture tests green; post-deploy fresh session `/compact` returns
  instantly; emitted PreCompact entry has `"async": true`, other events none.
- PR-2: `yadgar install-hooks --scope global --dry-run` shows all 11 events + preserves
  caveman; after real switch, `~/.claude/settings.json` has one entry per event, project
  file has no core hooks, no double-fire, caveman intact.
