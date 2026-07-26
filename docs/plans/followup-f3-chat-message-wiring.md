# F3 — Wire `chat.message parts[] mutation` (UserPromptSubmit)

**Date:** 2026-07-26 (filed by the opencode port train, FU of F3 from `docs/plans/followup-opencode-port-2026-07-26.md`)
**Task:** proposed — to be added to yadgar-task-list as a new task item
**Status:** PROPOSED (queued; blocked on F1 env infra)
**Builds on:** `docs/plans/opencode-hook-port-train-2026-07-26.md` (the train summary), `docs/plans/followup-f1-headless-e2e.md` (the test infra that gates this)
**Follows up:** the opencode port train's re-audit §4.5 + ADR-0168 D1 + D4 (the UserPromptSubmit is OPTIONAL gated on a real headless test)
**Predecessors:** F1 (the headless test infra) — the only way to validate that the parts[] mutation actually surfaces in the same-turn LLM context

## Why this is needed

In v5.166.0, the opencode plugin template does NOT include a `chat.message` handler. The 5th event (UserPromptSubmit) is explicitly deferred per the re-audit plan §4.5:

> "the parts[] mutation in the same-turn LLM context" is undocumented and we cannot test it.

The smoke test (`yadgar/tests/clients/test_hooks_render_opencode_smoke.py`) explicitly asserts `chat.message` is **absent** from the template. This is a feature, not a bug — faking a non-functional handler would violate plan R7 ("never fake a hook"). The right gate is F1's headless test (Bun + opencode binary + real-runtime validation).

## What's needed (once F1 passes)

1. **Add a `chat.message` handler** to the canonical plugin template (`yadgar/core/install/clients/hooks_render.py::_OPENCODE_PLUGIN_TEMPLATE`):
   ```ts
   "chat.message": async (_input, output) => {
     // UserPromptSubmit — auto-recall on every user turn.
     // The yadgar hook prompt-recall CLI returns a context block that we
     // push into the conversation parts[] so the LLM sees the relevant
     // memory on the same turn (no round-trip).
     const r = await YADGAR({ event: "prompt-recall", directory })
     if (r && typeof r.stdout === "string" && r.stdout.length > 0) {
       output.parts.push({ type: "text", text: r.stdout })
     }
   },
   ```
2. **Add a `prompt-recall` event to the `yadgar hook <event>` CLI** (in `yadgar/core/cli/hook.py` + `yadgar/core/server/tools/hook*.py` MCP tool family). This is the read-side counterpart to the post-tool-capture / pre-compact-drain / session-start events that already exist.
3. **Update the smoke test** — change `test_emitted_plugin_does_not_wire_chat_message` (currently asserts absence) to assert presence.
4. **Update CAP-INFRA-034** — D1 coverage math changes from "4 functional events" to "5 functional events" (3/5/1/1 → 4/5/1/0).
5. **Re-evaluation of ADR-0168 D1** — the "3/5/1/1" math no longer holds. The ADR needs a revision (D1 wording + the revisit-trigger clause).

## Phasing

| Phase | Scope | Order |
|---|---|---|
| P1 | (BLOCKED on F1) | F1 first |
| P2 | Add `yadgar hook prompt-recall` CLI event + MCP tool. This is independent of F1 but needs to exist before the plugin can call it. | Independent of F1; can land before F1 if the CLI plumbing is small |
| P3 | Wire `chat.message` handler in the plugin template (the diff above) | Unblocked by F1 (real headless test confirms mutation surfaces) |
| P4 | Update the smoke test + CAP-INFRA-034 + ADR-0168 | Unblocked by P3 |
| P5 | Version bump (5.166.x → 5.167.0 minor — wire-shape change), CHANGELOG entry, PR | Unblocked by P4 |

## Test surface (proposed)

**Unit tests (no env infra):**
- `test_chat_message_handler_in_template`: re-run the smoke test (CAR 3) with the `chat.message` handler now present. Updates the existing `test_emitted_plugin_does_not_wire_chat_message` → `test_emitted_plugin_wires_chat_message` (asserts presence + correct shape).
- `test_prompt_recall_cli_event_exists`: ensures `yadgar hook prompt-recall` is registered + handles the right shape (stdin payload → read memory context → stdout the context block).

**E2E test (gated on F1):**
- `test_chat_message_parts_injection` (in `yadgar/tests/e2e/test_opencode_plugin_e2e.py`): runs `opencode run --prompt '<user question that should trigger a memory recall>'`, captures the LLM's response, asserts the response contains the memory-context injection. This is the load-bearing test for F3.

## Risks + trade-offs

- **Same-turn visibility is undocumented.** The opencode plugin SDK's `chat.message` callback signature shows `output.parts: Part[]` but does not document that mutations to `parts[]` are visible to the LLM in the same turn. The re-audit plan §4.5 flags this as a "verify in Car A's payload spike" item. F1's headless test is the only thing that can confirm it.
- **Prompt-recall could be expensive.** Every user prompt triggers a memory recall — that's `yadgar_recall` over the full corpus. The Claude Code equivalent (UserPromptSubmit) is opt-in via `auto_recall: true`; the same opt-in should apply here. Default OFF, enabled per the same config flag.
- **The 5th handler is the highest-value surface** for users (auto-recall on every prompt is the killer feature for AI coding agents). But it's also the highest-risk (per-turn latency, noise in the conversation). F1 is the gate; do not skip the test.

## Why a separate plan file (not just a bullet in the follow-up plan)

F3 is **mechanically simple** (the plugin handler is ~10 lines; the CLI plumbing is ~50 lines) but **strategically load-bearing** — it closes the last gap in the 5/5 coverage and is the visible payoff of the train. The follow-up plan's bullet format is right for tracking; this plan file documents the exact CLI/tool shape and the load-bearing risks so the future PR is mechanical, not exploratory.

## Tied ADRs / plans

- **ADR-0168 D1** + **D4** — the UserPromptSubmit is OPTIONAL gated on a real headless test; coverage 3/5/1/1 until this is wired
- `docs/plans/opencode-hook-port-train-2026-07-26.md` — the train summary
- `docs/plans/followup-opencode-port-2026-07-26.md` — the umbrella follow-up plan (F1-F7)
- `docs/plans/followup-f1-headless-e2e.md` — F1 (the gating test infra)

## YADGAR findings footer

- F3 is the **highest-value** of the 7 follow-ups (auto-recall is the killer feature) but the **most-gated** (F1). The mechanical work is small (~60 lines across template + CLI + smoke test); the gate is the env infra.
- F3 is also the ONLY follow-up that triggers an ADR-0168 revision (D1's coverage math changes from 3/5/1/1 to 4/5/1/0). That's a meaningful signal: the ADR's design contract is load-bearing, and a coverage change is a contract change.
- The right time to do F3 is the day F1's headless test passes. Until then, F3 is mechanical but unvalidated — do NOT skip the test, or the coverage claim becomes theater.
