# F1 — Real headless `opencode run` test (opencode plugin e2e infra)

**Date:** 2026-07-26 (filed by the opencode port train, FU of F1 from `docs/plans/followup-opencode-port-2026-07-26.md`)
**Task:** proposed — to be added to yadgar-task-list as a new task item
**Status:** PROPOSED (queued; not shippable in the v5.166.0/v5.166.1 train)
**Builds on:** `docs/plans/opencode-hook-port-train-2026-07-26.md` (the train summary)
**Follows up:** the opencode port train's re-audit §4.5 + §4.6 (the chat.message parts[] gate) and ADR-0168's D1 + D4
**Predecessors:** none within yadgar's control; purely env-side

## Why this is needed

The v5.166.0 opencode hook port delivered a working 4-of-5 functional + 1-of-5 non-blocking layer. The 5th event (UserPromptSubmit / `chat.message` parts[] mutation) is **explicitly deferred** until a real headless test proves the parts[] mutation appears in the same-turn LLM context. Today the opencode plugin template:

- Does NOT include a `chat.message` handler
- Is smoke-tested for static structure (the smoke test in `yadgar/tests/clients/test_hooks_render_opencode_smoke.py` explicitly asserts `chat.message` is absent)
- Cannot be promoted to wire `chat.message` without runtime validation, because the opencode behavior is undocumented and the test would be theater

The proper gate is a headless test that drives a real `opencode run` session end-to-end, then asserts on the plugin's behavior. This requires Bun + the opencode CLI + a (mocked) LLM endpoint.

## What's needed (env + tooling)

Three new dev-env dependencies:

1. **Bun runtime** — the opencode plugin SDK targets Bun (the `PluginInput` `ctx.$` shell helper is Bun-specific). Not installed in the current yadgar dev env. nix shell is the natural path (`flake.nix` already manages similar toolchains). Alternative: a container image (`Dockerfile.dev`?) that ships Bun + the opencode CLI; CI runs the test in that container.
2. **opencode CLI binary** — at least v1.18.4 (the version matching the opencode plugin SDK 1.18.5's API surface). Pin via nix-managed package or a download in the e2e setup.
3. **Mocked LLM endpoint** — the test needs a way to make `opencode run` terminate predictably. Options: (a) `httpmock`/WIREMOCK to intercept the upstream LLM API call, (b) opencode's `--no-llm` flag if it exists (verify), (c) a fake model server. (a) is the lowest-friction if opencode has stable HTTP egress.

## What the test should prove

The headless test (`yadgar/tests/e2e/test_opencode_plugin_e2e.py`, new file, LEGIT-CONDITIONAL skip entry `opencode-plugin-e2e-01`):

1. Spin up `opencode run --prompt <fixture>` with the installed plugin
2. Capture stdout for `yadgar_recent_memories` entries (postToolUse capture)
3. Capture the compacted output (preCompact drain)
4. Assert on shape — not retrieval quality — i.e. "the plugin called the right `yadgar hook` CLI invocation", not "the memory retrieval was good"

Plus a separate scenario for the chat.message gate (F3):

5. Run a prompt that triggers a UserPromptSubmit
6. Assert that the LLM's response (captured from stdout) contains a `<memory-context>` block (or whatever the parts[] injection shape turns out to be) — proves the mutation surfaced

## Phasing

| Phase | Scope | Order |
|---|---|---|
| P1 | Add `Bun + opencode-cli` to the nix dev shell (or a dedicated container image) | Blocked on: env infra |
| P2 | Add `opencode-plugin-e2e-01` LEGIT-CONDITIONAL skip-inventory entry + a stub test that documents the gate | Unblocked by P1 |
| P3 | Implement the 5-scenario headless test (4 status capture + 1 chat.message assertion) | Unblocked by P1 + P2 |
| P4 | Wire chat.message handler in the canonical plugin template (F3) — gated on P3 passing | Unblocked by P3 |
| P5 | Update CAP-INFRA-034 + ADR-0168 to reflect the upgraded coverage (3/5 → 4/5 functional) | Unblocked by P4 |

## Test surface (proposed)

```python
# yadgar/tests/e2e/test_opencode_plugin_e2e.py
#
# SKIPPED unless the e2e infra is available (skip_inventory:
# opencode-plugin-e2e-01 — mirrors the code-graph-e2e-smoke-01 pattern).

import pytest

@pytest.mark.skipif(not shutil.which("opencode") or not shutil.which("bun"),
                reason="opencode-plugin-e2e-01: e2e infra not available (Bun + opencode CLI)")
def test_session_start_signal_captured_by_yadgar_hook_cli(opencode_test_env):
    """opencode run --prompt 'hello' should fire session.created and the
    plugin should call `yadgar hook session-start --directory ... --mode signals`."""
    output = run_opencode_with_plugin(opencode_test_env, prompt="hello")
    assert opencode_test_env.recorded_invocations == [
        ("yadgar", "hook", "--event", "session-start",
         "--directory", str(opencode_test_env.project_dir),
         "--json", JSON_OF_SESSION_START_PAYLOAD),
    ]

def test_post_tool_use_capture(opencode_test_env):
    """A prompt that triggers a tool call should result in a
    `yadgar hook post-tool-capture` invocation recorded in the e2e
    recorder."""
    ...

def test_pre_compact_drain(opencode_test_env):
    """Forcing a compaction mid-session should result in a
    `yadgar hook pre-compact-drain` invocation whose stdout lands in the
    compaction prompt."""
    ...

def test_session_idle_observer(opencode_test_env):
    """Session end should result in a `yadgar hook stop` invocation
    (non-blocking observer; #16626-gated)."""
    ...

@pytest.mark.skipif(...)  # F3 gate
def test_chat_message_parts_injection(opencode_test_env):
    """A UserPromptSubmit prompt should result in the LLM's response
    containing the memory-context injection (proves parts[] mutation
    surfaces in the same-turn context). Only after F3 is wired."""
    ...
```

## Risks + trade-offs

- **Bun is the opencode plugin runtime.** If the opencode plugin SDK ever drops Bun support (adds a Deno/Node adapter), this test infra needs to follow. Low-probability in the next 2 quarters; high-coupling.
- **Mocking the LLM endpoint is fragile.** Any opencode-internal change to how the LLM call is made (e.g. switching from Anthropic-style HTTP to OpenAI-style) breaks the mock. Mitigation: use a real LLM API key with a low-cost model + cost caps; fall back to a recorded-response fixture.
- **The headless test is inherently slow** (~10-60s per scenario, depending on LLM latency). Acceptable as an opt-in e2e tier (per the existing `tests/e2e/` pattern); not suitable for the PR gate.

## Why a separate plan file (not a sub-bullet in the follow-up plan)

F1 is the most complex of the F1-F7 follow-ups. It crosses the env-infra boundary (Bun, opencode CLI, nix-shell, container) AND the test-infra boundary (skip-inventory, e2e conftest). The follow-up plan's bullet format is right for tracking but wrong for designing. This plan file is the design document; the follow-up plan's bullet is the tracking entry.

## Tied ADRs / plans

- **ADR-0168** D1 + D4 — chat.message is OPTIONAL gated on a real headless test; coverage 3/5/1/1 until then
- `docs/plans/opencode-hook-port-train-2026-07-26.md` — the train summary
- `docs/plans/followup-opencode-port-2026-07-26.md` — the umbrella follow-up plan (F1-F7)
- `yadgar/tests/skip_inventory.json` — needs a new `opencode-plugin-e2e-01` entry when the test file is added

## YADGAR findings footer

- F1 is the **most expensive** of the 7 follow-ups (1-2 days of env infra + 1 day of test writing + 1 day of F3 wiring = ~3-4 days total) but the **highest leverage** because it unblocks F3 (the only shippable-after-F1 follow-up).
- The cleanest attack path is: (1) nix shell with Bun + opencode, (2) container image if nix is unavailable on the dev machine, (3) defer to a future train if neither is feasible. Per the existing project discipline, (1) is preferred.
- The smoke test added in CAR 3 (Node-based syntax+structure check, runs in CI) covers everything BELOW the headless-test threshold. F1 fills the gap ABOVE it. The two are complementary, not redundant.
