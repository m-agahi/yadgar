# OpenCode port — follow-ups (post-v5.166.0)

**Date:** 2026-07-26
**Task:** #0057 — Track follow-up work from the opencode port train (v5.166.0).
**Status:** PROPOSED (queued; no commit yet — pick up in a later train).
**Builds on:** `docs/plans/port-opencode-re-audit-2026-07-26.md` (active), `docs/plans/surrealmigrate-fork-2026-07-26.md` (parent investigation), `docs/plans/investigation-migration-script-system-2026-07-26.md` (multi-user migration work), `docs/plans/port-opencode-2026-07-20.md` (archived).

## Context

The opencode port train (v5.166.0, 6 cars + 4 follow-up cars = 10 commits total on `feat/opencode-hook-port-train-2026-07-26`) shipped a working 4-of-5 functional + 1-of-5 non-blocking opencode hook layer (cap 3/5/1/1). Car 7 hard-removed the parallel `yadgar install-hooks` CLI; Car 8 added Node 22 + the clients/ test path to test-fast in CI.

This plan catalogues the work that **remains** after v5.166.0 — items deferred during the train because (a) they need a separate container/runtime that's not in this train's env (Bun + opencode runtime), (b) they need cleanup that touches registries/snapshots outside this PR's scope, or (c) they are deliberately follow-up per the re-audit plan's design.

## Follow-up items

### F1 — Real headless `opencode run` test (sst/opencode#16626 + #34321 gates)
- **Source:** Re-audit plan §4.5 + §4.6. The `chat.message parts[] mutation` event handler is intentionally absent from the canonical plugin template, gated on a real headless test that proves the parts[] mutation appears in the same-turn context for an LLM provider.
- **What it needs:** Bun runtime + opencode CLI + a real (or mocked) LLM API key. The Node 22 + clients-test addition in Car 8 makes the smoke cover everything below the headless-test threshold; this gap is the one piece above it.
- **Scope:** Add `yadgar/tests/e2e/test_opencode_plugin_e2e.py` (new LEGIT-CONDITIONAL skip-inventory entry `opencode-plugin-e2e-01`, parallels the existing `code-graph-e2e-smoke-01` pattern). Driver spawns `opencode run --prompt <fixture>` with the installed plugin, captures stdout for `yadgar_recent_memories` entries (postToolUse capture) and the compacted output (preCompact drain). Asserts on shape; not on retrieval quality.
- **Why a follow-up:** No Bun or opencode binary in the current env (CAR 3 noted this). Building the test requires installing both — either via nix shell (per the project's nix-managed deps) or a dedicated CI job. Out of scope for the opencode port train itself.
- **Effort estimate:** M (1-2 days; mostly env setup + driver plumbing).

### F2 — Promote session.idle → session.stopping (sst/opencode#16626 upstream)
- **Source:** Re-audit plan §4.5 Stop is NON-BLOCKING observer only. sst/opencode#16626 is the upstream feature request for `session.stopping`, which would let yadgar re-enter the agent loop (matching Claude Code's Stop hook semantics — exit-code-2 → stderr injected as prompt).
- **What it needs:** Watch upstream. When sst/opencode#16626 lands, the plugin template's generic event callback switches from `event.type === "session.idle"` to `event.type === "session.stopping"`, with the same `yadgar hook stop` payload. Output plugin → no API change.
- **Why a follow-up:** No way to advance without upstream. Pure watch + one-line change when it lands.
- **Effort estimate:** XS (single-line plugin template change; one new smoke assertion; CAP-INFRA-034 update).

### F3 — UserPromptSubmit (chat.message) wiring once headless test passes (F1)
- **Source:** Re-audit plan §4.5. The 5th event (chat.message → output.parts mutation) is deferred to a follow-up, gated on F1.
- **What it needs:** F1 to land first (the headless test proves the mutation path works). Once F1 passes, add a 5th event handler to the template mirroring the tool.execute.after block shape; the smoke (CAR 3) adds a 10th assertion that the handler IS now present.
- **Why a follow-up:** Same root cause as F1 (can't validate parts[] without a real opencode runtime).
- **Effort estimate:** S (template-only change once F1 exists; one smoke update).

### F4 — ADR-0168: capture the 6 design decisions (D1-D6)
- **Source:** Re-audit plan §3 + §6. The 6 design decisions from the re-audit (D1 = 5/5 events wired, 3/5 functional OOTB; D2 = IPC = execa shell-out; D3 = install path = `yadgar install`; D4 = UserPromptSubmit OPTIONAL gated on headless test; D5 = single global install per ADR-0161; D6 = pin plugin SDK versions) deserve a formal ADR for the historical record + future cross-references.
- **What it needs:** One ADR file (`docs/contracts/ADR-0168-opencode-port.md` or similar). Auto-generated via `yadgar_adr_add` MCP tool or hand-written following the established ADR format. Cross-references: re-audit plan, port-opencode-2026-07-20 (archived), CAP-INFRA-034.
- **Why a follow-up:** The train shipped the code + docs; the formal ADR captures the *why*. Not blocking any further work but the project convention (every feature train gets an ADR per AGENTS.md pattern) says to add one.
- **Effort estimate:** XS (15 min, mostly cut-and-paste from the plan + ADR template).

### F5 — Per-emitter cataloguing of claude_code and cursor (existing emitters)
- **Source:** Car 4 noted that the pre-existing claude_code (`_emit_claude_json`) and cursor (`_emit_cursor_hooks`) emitters are not catalogued in CAPABILITY_REGISTRY.md. The new CAP-INFRA-034 covers opencode explicitly; the other two should follow for completeness.
- **What it needs:** Two new CAP-INFRA-NNN entries (likely 035 + 036, or higher) following the same shape as CAP-INFRA-034. Add a BC-* row each pointing at the legacy install tests that exercise them. Verify the registry's `_VERIFIED` constant + the per-client `verified_date` field stays consistent.
- **Why a follow-up:** Out of scope for the opencode port train (the car 4 entry explicitly carved out). The next time someone touches the hooks emitter subsystem (F2, F3, or a new client port), this is a natural cleanup.
- **Effort estimate:** S (1-2 hours, copy-paste + I32 lint).

### F6 — Verified-date bump on the opencode `_OPENCODE` ClientDescriptor row
- **Source:** Car 4 noted that `_VERIFIED` is a SHARED constant in `registry.py` (line 34) used by all 9 clients. Bumping it to 2026-07-26 affects the 8 unrelated clients. The opencode row still claims `verified_date="2026-07-18"` (the original snapshot date).
- **What it needs:** Two clean options: (a) per-client override: change the opencode row to carry its own `verified_date="2026-07-26"` and have the constant fallback for the other 8 rows; or (b) keep the shared constant but add a per-row `last_re_verified` field that overrides only when set. (b) is cleaner long-term but requires a small schema change.
- **Why a follow-up:** Tiny scope; intentionally deferred because the shared-constant change has a non-trivial ripple (8 unrelated rows affected). A separate train can do it cleanly with a focused test that asserts the per-client override works.
- **Effort estimate:** XS.

### F7 — Plugin SDK pin in package.json (D6 from the re-audit)
- **Source:** Re-audit plan §3.2 mentions: "Pin `@opencode-ai/plugin` and `@opencode-ai/sdk` to the bundled versions (currently 1.14.31 in `~/.config/opencode/node_modules/`, even though the binary is 1.18.4 and npm latest is 1.18.5)."
- **What it needs:** Decide: (a) pin to the bundled version (1.14.31) for maximum compatibility, (b) pin to the npm latest (1.18.5) for newest features, (c) pin to a range that covers both. Update the `_EXECA_DEP_BLOCK` to include `@opencode-ai/plugin` (currently only adds `execa`). The plugin's `import type { Plugin } from "@opencode-ai/plugin"` is type-only and gets erased, so the runtime dep isn't strictly required — but typing the package.json explicitly documents the contract.
- **Why a follow-up:** Car 1 deferred the package.json pinning because the type-only import means there's no runtime dep to enforce. The pin is purely documentary + drift-detection. Worth doing once a real opencode install is exercised end-to-end.
- **Effort estimate:** XS.

## Phasing

| Phase | Scope | Order of work | When |
|---|---|---|---|
| F4 | ADR-0168 | One PR | Anytime — independent of others |
| F5 | Cataloguing claude_code + cursor emitters | One PR | Anytime — independent |
| F6 | Per-row verified_date | One PR | Anytime — small |
| F2 | Watch upstream; one-line plugin change when #16626 lands | One commit | When sst/opencode#16626 ships |
| F7 | package.json pin | One commit | Bundled with F2 or F3 (touch the same file) |
| F1 | Real headless test infra (Bun + opencode binary) | One PR (infra + driver + smoke) | Blocked on: getting Bun + opencode into nix-managed dev shells |
| F3 | chat.message wiring (after F1 passes) | One commit | Blocked on: F1 |

## Tied ADRs / plans

- ADR-0143 (multi-client porting, #59 verification gate satisfied for opencode) — closes
- ADR-0154 (Path A core-only, no backend bump) — closes
- ADR-0161 (global-authoritative hook install) — closes
- Plan `port-opencode-re-audit-2026-07-26.md` — closes
- Plan `surrealmigrate-fork-2026-07-26.md` — separate train (m-agahi/surrealmigrate fork)
- Plan `investigation-migration-script-system-2026-07-26.md` — separate train (DB migration scripts)

## YADGAR findings footer (handoff contract)

- The opencode port train delivered the working layer (4/5 functional + 1/5 non-blocking) with strong unit-test coverage. The remaining 1-of-5 (chat.message) + the Stop blocking promotion are gated on upstream issues that are out of yadgar's control. F4/F5/F6/F7 are documentation/registry polish — independent of upstream.
- The biggest open question is F1: how to actually exercise the plugin under opencode at runtime. The current path is local-dev smoke + CI auto-skip; the proper gate is a headless test that proves parts[] mutates in the same-turn context. The infra (Bun + opencode binary) is missing — addressing that requires a dedicated effort.
- Car 7's hard-removal of `yadgar install-hooks` is **NOT** in this follow-up — it landed in v5.166.0 itself and was an explicit user choice (not deferred).
- The Docker image upgrade in Car 8 (Node 22 via NodeSource) is the only follow-up that affects future CI behavior; the smoke auto-skips cleanly on older images (skip-inventory entry `opencode-plugin-smoke-01`), so the upgrade is optional.

