# F2 — Promote `session.idle` → `session.stopping` for blocking Stop semantics

**Date:** 2026-07-26 (filed by the opencode port train, FU of F2 from `docs/plans/followup-opencode-port-2026-07-26.md`)
**Task:** proposed — to be added to yadgar-task-list as a new task item
**Status:** WATCH-ONLY (queued; depends on upstream merge)
**Builds on:** `docs/plans/opencode-hook-port-train-2026-07-26.md` (the train summary)
**Follows up:** the opencode port train's re-audit §3.1 + ADR-0168 D1 (the non-blocking note)
**Predecessors:** upstream sst/opencode#16626 must merge

## Why this is needed

In v5.166.0, the opencode hook layer wires `event.type === "session.idle"` as a **non-blocking observer only**. The `session.idle` event fires AFTER the agent loop has already broken, which means:

- The plugin can call `yadgar hook stop` to record a checkpoint
- But the plugin cannot prevent the session from ending (re-prompting, redirecting, etc.)

This is functionally weaker than Claude Code's `Stop` hook, which can block + inject stderr-as-prompt to re-enter the agent loop (the `exit 2` pattern from the Claude Code subagent contract).

The upstream feature request [sst/opencode#16626](https://github.com/sst/opencode/issues/16626) asks for a `session.stopping` hook that fires BEFORE the loop breaks, with an `output.stop = false` + `output.message` pattern for re-entry. Once that ships, yadgar can re-enter the agent loop exactly the way Claude Code's Stop hook does.

## What's needed (in yadgar, once upstream ships)

One-line change in the canonical plugin template (`yadgar/core/install/clients/hooks_render.py::_OPENCODE_PLUGIN_TEMPLATE`):

```diff
-    } else if (event.type === "session.idle") {
+    } else if (event.type === "session.stopping") {
       // Non-blocking observer only. The blocking equivalent
-      // (session.stopping) is gated on sst/opencode#16626; see the re-audit
-      // plan §4.5.
+      // (`output.stop = false` + `output.message` for re-entry, mirroring
+      // Claude Code's Stop-hook style).
       await YADGAR({ event: "stop", directory });
+      if (someYadgarCondition) {
+        output.stop = false;
+        output.message = "Re-prompt: yadgar stop checkpoint found unprocessed work";
+      }
     }
```

Plus:
- Update the smoke test (`yadgar/tests/clients/test_hooks_render_opencode_smoke.py`) — the `hasAllLifecycleDispatches` check currently asserts the 3 lifecycle dispatches; if `session.idle` is removed and `session.stopping` added, the smoke needs to match.
- Update CAP-INFRA-034 — the `D1` wording changes from "non-blocking observer" to "blocking via sst/opencode#16626 when it ships; current build uses non-blocking session.idle observer".
- Update the canonical plugin template comment that references `#16626` as the gate (remove the "gated on" wording since the gate has now opened).

## Phasing

| Phase | Scope | Order |
|---|---|---|
| P1 | Watch sst/opencode#16626 — re-verify state weekly (the issue is open as of 2026-07-26) | starts now, ongoing |
| P2 | When #16626 ships: validate the upstream API matches the re-audit plan's expectation (output.stop + output.message); do a fresh re-verification of `@opencode-ai/plugin` type defs (the `event` callback type likely gains the `EventSessionStopping` member) | Unblocked by upstream |
| P3 | Update `_OPENCODE_PLUGIN_TEMPLATE` (the diff above) | Unblocked by P2 |
| P4 | Update the smoke test + CAP-INFRA-034 | Unblocked by P3 |
| P5 | Version bump (5.166.x → 5.167.0 minor — wire-shape change is enough to warrant minor), CHANGELOG entry, PR | Unblocked by P4 |

## Test surface (proposed update)

The existing smoke test (`hasAllLifecycleDispatches`) currently asserts:
- `event.type === "session.created"`
- `event.type === "session.compacted"`
- `event.type === "session.idle"`

After F2, it should assert:
- `event.type === "session.created"`
- `event.type === "session.compacted"`
- `event.type === "session.stopping"` (replacing session.idle)

This is a 1-line test update. The real validation comes from F1's headless test, which would exercise the actual `output.stop = false` re-entry path.

## Risks + trade-offs

- **Upstream API surface uncertainty.** The sst/opencode#16626 issue proposes a specific API (`output.stop` + `output.message`), but the actual shipped API may differ. The re-audit plan §3.1 calls this out as a re-verification gate per ADR-0143 (#59). F2's P2 is exactly that re-verification.
- **Re-entry could be abused.** A plugin that always sets `output.stop = false` re-enters the loop forever (per the upstream issue's "Known limitation" note). yadgar's stop checkpoint would need its own termination condition (e.g. "no unprocessed work" → don't re-prompt). The yadgar hook layer already has this in scope (the stop hook is supposed to be cheap, fast, idempotent), but it deserves a comment in the plugin template.
- **Test infra gap.** The current Node-based smoke (CAR 3) doesn't exercise the stop→re-entry path. F1's headless test is the only thing that would catch a re-entry-loop bug before production. Per F1's plan, this is gated on env infra.

## Why a separate plan file (not just a bullet in the follow-up plan)

F2 is the **smallest** of the 7 follow-ups (one-line plugin change) but it depends on an external event (upstream merge). The follow-up plan's bullet format is right for tracking; this plan file documents the **exact change to make** when the upstream gate opens, so the future PR is mechanical (no re-design work). The plan also captures the API-uncertainty risk, which is the load-bearing reason this isn't a 5-min change.

## Tied ADRs / plans

- **ADR-0168 D1** — the non-blocking note is the spec for this change
- `docs/plans/port-opencode-re-audit-2026-07-26.md` (archived) — §3.1 documents the session.idle → session.stopping promotion as the non-blocking → blocking path
- `docs/plans/opencode-hook-port-train-2026-07-26.md` — the train summary
- `docs/plans/followup-opencode-port-2026-07-26.md` — the umbrella follow-up plan (F1-F7)
- [sst/opencode#16626](https://github.com/sst/opencode/issues/16626) — the upstream feature request (the actual gate)

## YADGAR findings footer

- F2 is the **smallest** follow-up in scope (one line of code, three supporting doc updates) but the **most async** (waiting on upstream). The right time to do it is the day sst/opencode#16626 merges — there will be no warning, just a "Closed" notification.
- Worth a low-noise watch (subscribe to the issue). When it ships, this plan's P2-P5 should be a 1-hour train: read the upstream API, update the template, run the smoke, PR.
- If upstream ships a different API (no `output.stop`/`output.message` shape), this plan is WRONG and needs a fresh design. The re-audit plan §3.1 was based on the issue's proposed shape, not a shipped contract.