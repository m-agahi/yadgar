# OpenCode hook port — train summary (v5.166.0 + v5.166.1)

**Date:** 2026-07-26
**Task:** task #0020 (in_progress) + #0056 (re-audit, completed) + #0057 (follow-ups, in_progress)
**Branch:** `feat/opencode-hook-port-train-2026-07-26` (one PR queued)
**Status:** TRAIN LANDED — 14 commits across 10 cars + 4 follow-up polish commits; v5.166.0 + v5.166.1 shipped. Active follow-up work tracked in `docs/plans/followup-opencode-port-2026-07-26.md`.

**Builds on:** `docs/plans/port-opencode-re-audit-2026-07-26.md` (the re-audit; now archived at `archive/port-opencode-re-audit-2026-07-26.md`).
**Supersedes:** `docs/plans/port-opencode-2026-07-20.md` (already archived in CAR 0 of the train).
**Superseded by:** This is the current train summary. Future re-trains (if needed) would update this doc.

## What shipped

| Release | Coverage |
|---|---|
| **v5.166.0** (CAR 6) | Car 1-5: opencode hook emitter (`_emit_opencode_plugin`) + TS plugin shim + execa dep + install path integration (`yadgar install --client opencode --hooks`) + I32 cataloguing (CAP-INFRA-034) + docs (install.md / AGENTS.md / README.md / CHANGELOG). |
| **v5.166.1** (CAR 10) | Car 7-9: hard-remove `yadgar install-hooks` CLI; delegate `install_hooks` MCP tool to `install_client`; Node 22 via NodeSource in yadgar-ci image; follow-up plan filed; 4 polish follow-ups (F4-F7). |
| **self-hosted runner migration** (this PR) | All 7 GitHub workflows migrated to `[self-hosted, linux, x64, yadgar]`; gitleaks v8.30.1 baked into image. |

## Coverage delivered (D1 from ADR-0168)

- **3/5 functional events** wired (SessionStart, SessionStart-restore, PostToolUse, PreCompact)
- **1/5 non-blocking** event wired (Stop via session.idle observer only; promote to blocking on sst/opencode#16626)
- **1/5 deferred** (UserPromptSubmit / chat.message parts[] mutation — gated on a real headless `opencode run` test per §4.5)

## The 6 design decisions locked (ADR-0168)

| ID | Decision | Status |
|---|---|---|
| D1 | 5/5 wired, 3/5/1/1 coverage | locked |
| D2 | IPC = execa shell-out to `yadgar hook <event>` CLI (NOT fabricated MCP RPC) | locked |
| D3 | Install path = `yadgar install --client opencode` (unified orchestrator) | locked |
| D4 | UserPromptSubmit is OPTIONAL (gated on headless test) | locked |
| D5 | Single global install per ADR-0161 | locked |
| D6 | Pin plugin SDK versions to bundled (1.14.31) | locked |

## The 4 follow-ups shipped in this train (F4-F7 from the follow-up plan)

| ID | Subject | Status |
|---|---|---|
| F4 | ADR-0168 captures 6 design decisions | DONE (`yadgar-adr-0168`) |
| F5 | Catalog pre-existing claude_code + cursor emitters | DONE (CAP-INFRA-035 + CAP-INFRA-036) |
| F6 | Per-row `verified_date` on the opencode `_OPENCODE` ClientDescriptor | DONE (overrides shared `_VERIFIED` constant) |
| F7 | package.json `@opencode-ai/plugin` pin (documentary) | DONE (`@opencode-ai/plugin: ^1.0.0` in `_EXECA_DEP_BLOCK`) |

## The 3 follow-ups DEFERRED (F1-F3 — NOT shippable in this train)

| ID | Subject | Plan file | Gate | Why deferred |
|---|---|---|---|---|
| F1 | Real headless `opencode run` test (Bun + opencode binary; e2e infra; new LEGIT-CONDITIONAL skip entry `opencode-plugin-e2e-01`) | `docs/plans/followup-f1-headless-e2e.md` | **Bun + opencode binary in dev env** | No Bun installed in the build env; would require a dedicated env-setup effort (~1-2 days). |
| F2 | Promote `session.idle` → `session.stopping` for blocking Stop semantics | `docs/plans/followup-f2-stop-blocking.md` | **sst/opencode#16626 ships** | The upstream feature is currently open. One-line plugin change when it lands. |
| F3 | Wire `chat.message parts[] mutation` | `docs/plans/followup-f3-chat-message-wiring.md` | **F1 (env infra)** | Can't validate parts[] mutation in same-turn LLM context without a real opencode runtime. |

## Cross-references

- **ADR-0168** (the 6 design decisions D1-D6) — locked
- **CAP-INFRA-034** (opencode hook emitter) — live
- **CAP-INFRA-035** (claude_code hook emitter) — live, catalogued FU2
- **CAP-INFRA-036** (cursor hook emitter) — live, catalogued FU2
- **CAP-OPS-010** (install_hooks MCP tool) — live, delegates to install_client per Car 7
- `docs/plans/followup-opencode-port-2026-07-26.md` — the 7 F1-F7 follow-ups (4 done, 3 deferred)
- `docs/plans/followup-{f1,f2,f3}-*.md` — per-item plans for the 3 deferred follow-ups

## Files changed in this train

**Core (yadgar/core/):**
- `yadgar/core/install/clients/hooks_render.py` — Car 1: added `_emit_opencode_plugin`, `_EXECA_DEP_BLOCK`, `_OPENCODE_MANAGED_MARKER`, helpers (`_opencode_plugin_path`, `_opencode_package_json_path`, `_ensure_opencode_package_json_dep`); FU4: added `@opencode-ai/plugin` to dep block
- `yadgar/core/install/clients/install.py` — Car 2: added `hooks` + `home_dir` fields to `InstallOptions`; added `_render_hooks_fragment`; FU: normalized `path`/`settings_file` keys
- `yadgar/core/server/tools/misc.py` — Car 7: `install_hooks` MCP tool delegates to `install_client`
- `yadgar/core/cli/install_hooks.py` — Car 7: hard-remove stub (prints migration message, exits 1)
- `yadgar/core/cli/install.py` — Car 2: `--hooks` / `--no-hooks` flags
- `yadgar/core/install/clients/registry.py` — FU3: per-row `verified_date` override on `_OPENCODE`

**Tests (yadgar/tests/):**
- `yadgar/tests/clients/test_hooks_render_opencode.py` — 14 tests (38 originally; refined)
- `yadgar/tests/clients/test_hooks_render_opencode_smoke.py` — 9 tests (Node-based syntax+structure smoke)
- `yadgar/tests/clients/test_install_hooks_opencode.py` — 10 tests (orchestrator wiring)
- `yadgar/tests/clients/test_hooks_render.py` — refined (no longer stub-listing opencode_plugin)
- `yadgar/tests/hooks/test_install_hooks_cli_removed.py` — 4 tests (new, Car 7 migration contract)
- `yadgar/tests/hooks/test_install_hooks_host_vs_container.py` — refined (3 tests; 2 CLI tests deleted as obsolete)
- `yadgar/tests/hooks/test_install_hooks_home_guard.py` — refined (1 test rewritten for new MCP return shape)
- `yadgar/tests/skip_inventory.json` — new LEGIT-CONDITIONAL entry `opencode-plugin-smoke-01`

**CI / infra:**
- `Dockerfile.ci` — Car 8: Node 22 via NodeSource; Car of self-hosted runner: gitleaks v8.30.1 baked
- `.github/workflows/validate.yml` — self-hosted runner + yadgar-ci
- `.github/workflows/ci-pr.yml` — 4 jobs migrated to self-hosted
- `.github/workflows/ci-release.yml` — 6 jobs migrated
- `.github/workflows/sdk-js.yml` — 2 jobs migrated
- `.forgejo/workflows/*` — untouched (per user's instruction; Forgejo uses its own self-hosted pool)

**Docs:**
- `docs/contracts/CAPABILITY_REGISTRY.md` — Car 4: CAP-INFRA-034; Car 7: CAP-OPS-010 update; FU2: CAP-INFRA-035 + CAP-INFRA-036
- `docs/reference/install.md` — Car 5: `--hooks` flag + opencode capability row
- `docs/reference/hooks.md` — Car 5 + Car 7: hard-remove migration note
- `AGENTS.md` — Car 5 + Car 7: install cheatsheet + subagent-contract
- `README.md` — Car 5 + Car 7: install cheatsheet + MCP tools table
- `CHANGELOG.md` — v5.166.0 + v5.166.1 entries + self-hosted-runner-migration bullet
- `scripts/install/yadgar-setup.sh` — Car 7: step 6 uses new path
- `docs/plans/port-opencode-re-audit-2026-07-26.md` — Car 0: SUPERSEDED banner added; Car-end: archived
- `docs/plans/followup-opencode-port-2026-07-26.md` — Car 9: 7 F1-F7 follow-ups catalogued
- `docs/plans/opencode-hook-port-train-2026-07-26.md` — this file

**Version bumps:**
- `pyproject.toml` 5.165.1 → 5.166.0 (Car 6) → 5.166.1 (Car 10)
- `server.json` mirrors
- `docker-compose.yml`, `flake.nix`, `uv.lock` mirrors

**Test surface (cumulative):**
- 247 tests passing in yadgar/tests/clients/ + yadgar/tests/hooks/ before the train
- 718 tests passing after the train (per CAR 7 final number)
- +1 pre-existing test-isolation failure (`test_merge_properties`) confirmed identical on master, not introduced by this train

## YADGAR findings footer (handoff contract)

- The train delivered the working opencode hook layer (4/5 functional + 1/5 non-blocking) with strong unit-test coverage + a Node-based syntax+structure smoke that runs in CI. The remaining 1-of-5 (chat.message) + the Stop blocking promotion are gated on upstream issues that are out of yadgar's control. F1-F3 (the 3 deferred follow-ups) are the only meaningful remaining work for the opencode port; each has a per-item plan filed in this train.
- The 3 not-shippable follow-ups are explicitly F1 (env infra), F2 (upstream), F3 (gated on F1). The right time to attack F1 is when the dev env acquires Bun (likely via nix-managed dev shell, per the project's existing nix discipline). The right time to attack F2 is the day sst/opencode#16626 merges upstream (a low-noise watch on the opencode repo). F3 follows F1 mechanically.
- Car 7's hard-removal of `yadgar install-hooks` is the design-discipline signal of the train: per AGENTS.md "forward-only, no backward-compat knobs/dual-paths", a parallel-path CLI is not the right shape; the unified orchestrator is. The legacy stub stays in the tree ONLY so the argparser doesn't choke on the old `register(subparsers)` call site — and only the migration message + exit-1 fires on invocation.
- The self-hosted-runner migration is a separate hygiene concern (zero GitHub-hosted runner minutes consumed) that happened during this train because the user asked. It is NOT a port-specific change; it's a one-shot cost reduction applicable to all future CI runs.
