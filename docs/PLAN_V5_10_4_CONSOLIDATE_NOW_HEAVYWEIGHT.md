# PLAN — v5.10.4: consolidate_now heavyweight fix + mode parameter

**Status:** drafted 2026-05-30. Inserted as v5.10.4; prior v5.10.4 (nightly cycle remaining bugs) renumbered to v5.10.5. Plan-first per I27.

**Master at draft time:** v5.10.3 shipped + tagged (26011d6).

**Sequencing:** v5.10.4 hotfix. Slots immediately after v5.10.3. No dependency on v5.10.5 (nightly cycle bugs), v5.10.6 (session-end), v5.10.7 (viz), v5.10.8 (secret-gate). Ships before those unless main thread decides otherwise.

---

## Problem Statement

### Empirical Observation

Two sequential `consolidate_now()` calls in the 2026-05-29/30 session each consumed approximately 13 minutes. This is the full sleep cycle running twice back-to-back in a live interactive session. The tool is supposed to be an on-demand "flush my recent memories" action. 13 minutes of blocking work defeats that purpose and saturates the container's 1-CPU cap during the user's active session.

### Code Path

`consolidate_now()` in `yadgar/server/tools/admin_other.py:79` executes unconditionally:

```python
stats = _st._consolidation.force_consolidate()     # step 1: full consolidation cycle
if _st._sleep is not None:
    sleep_stats = _st._sleep.run_sleep_cycle()     # step 2: FULL sleep cycle — no gate
    stats["sleep_cycle"] = sleep_stats
# step 3 (conditional): anchor audit pass
if cfg.ANCHOR_AUDIT_CONSOLIDATION_ENABLED:
    anchor_pass_stats = _run_anchor_audit_pass(...)
```

`force_consolidate()` in `yadgar/consolidation/__init__.py:151` calls `_consolidation_cycle()`, which has 12 phases and its own `phase_start`/`phase_end` log instrumentation. This portion is CPU- and DB-bound but completes in tens of seconds normally.

`run_sleep_cycle()` in `yadgar/sleep_compute/__init__.py:48` executes 6 phases sequentially:

| Phase | Implementation | Cost type | Estimated cost at ~52 active dirs / ~500+ memories |
|-------|---------------|-----------|-----------------------------------------------------|
| 1. dream_replay | `sleep_compute/dream.py` | CPU (numpy matmul) + DB reads + embedding encodes for "insights" at sim>0.7 | Moderate: O(DREAM_REPLAY_PAIRS) dot products + batch encodes. Not LLM. |
| 2. detect_communities | `sleep_compute/community.py` | CPU (networkx Louvain) + bulk DB reads (all entities + all relationships) | Moderate: scales with entity graph size. Not LLM. |
| 3. generate_cluster_summaries | `sleep_compute/community.py:125` | CPU (regex + Counter) + bulk DB reads + embedding encodes (centroid per cluster) | Moderate: one `encode_batch()` call per cluster. Not LLM. Heavy DB read (`get_all_memories_with_embeddings()` called PER CLUSTER — potential N+1 bug). |
| 4. reembed_stale | `sleep_compute/embed_compress.py:28` | embedding model (yadgar-backend, CPU-bound) | Low when embeddings current; high after model upgrades (batch=50). |
| 5. compress_old_memories | `sleep_compute/embed_compress.py:51` | DB reads + one `encode()` per compressed memory | Low: only >30-day memories >1000 chars. |
| 6. auto_narrate | `yadgar/narrative.py:135` | DB reads (get_memories_for_directory per active dir) + DB writes (insert_narrative_entry) | **Long pole.** One `get_memories_for_directory()` + `get_narratives_for_directory()` call PER ACTIVE DIRECTORY. At 52 active dirs this is 104 DB round-trips. NO LLM calls — pure DB + regex. |

**Correction of prior framing:** `auto_narrate` is NOT an LLM call per directory. It is regex-based extraction from memory content + keyword matching. The long pole is 52× DB round-trips, not LLM inference. `reembed_stale` calls the embedding model but is batch-based. The dominant cost is likely:
1. Embedding model round-trips in `dream_replay` (one encode per "dream insight" memory) and `reembed_stale` (up to N/50 batches).
2. DB call amplification in `generate_cluster_summaries` (`get_all_memories_with_embeddings()` called once per cluster — likely O(clusters × total_memories) read volume).
3. `auto_narrate` at 52 active directories × 2 DB round-trips each = 104 sequential HTTP calls.

### Gating Gap: consolidate_now Bypasses the 6-Hour Gate

`_maybe_sleep_cycle()` in `yadgar/consolidation/orchestrator.py:21` gates the sleep cycle behind a 6-hour cooldown:

```python
def _maybe_sleep_cycle(self) -> None:
    now = datetime.now(UTC)
    if self._last_sleep_cycle is not None:
        hours_since = (now - self._last_sleep_cycle).total_seconds() / 3600.0
        if hours_since < 6.0:
            return
    ...
```

`consolidate_now` calls `_st._sleep.run_sleep_cycle()` DIRECTLY — completely bypassing `_maybe_sleep_cycle()` and its 6-hour gate. This means:
- Calling `consolidate_now` twice in rapid succession runs the full sleep cycle TWICE.
- The nightly cron (`yadgar/scripts/nightly_cycle.py:155`) calls `force_consolidate()` ONLY — no sleep cycle. The nightly cron is lighter than on-demand MCP `consolidate_now`. This is backwards from intuition.
- The sleep cycle gate timestamp (`_last_sleep_cycle`) is never set by `consolidate_now`, so even after `consolidate_now` runs the full sleep cycle, a nightly-cycle trigger still fires one again.

### Historical Context

Git log for the relevant path:

```
e3d0a20  feat(consolidate): anchor audit pass in consolidate_now() + _audit_anchors sentinel
bac9540  feat(consolidation): remove daemon 30-min trigger (v5.7.0 PR-0)
378de08  feat(observability): wire @trace_span across core + backend (v5.6.3)
7c29a33  feat: v5.1 — ops fixes + retrieval polish + module decomposition
```

Commit `bac9540` preserved `_maybe_sleep_cycle()` for PR-1 (nightly cron wiring) but `consolidate_now` bypassed it from the beginning. There is no commit that intentionally wired the sleep cycle into `consolidate_now` with a gate — the sleep cycle was added to `consolidate_now` at an earlier point (not visible in the post-v5.1 log) and `_maybe_sleep_cycle` was added for the cron but never applied to MCP path. **This is an oversight, not an intentional design choice.**

---

## Why It Matters

Use cases where `consolidate_now` is invoked:

1. **Pre-shutdown flush** — user is about to end a session and wants recent memories processed. Needs: consolidation cycle (process episodes, decay, merge). Does NOT need: full sleep cycle (sleep cycle is non-urgent offline work, 13+ minutes is unacceptable here).
2. **Debug / "what's in memory"** — developer calls `consolidate_now` after creating a few test memories. Needs: consolidation cycle. The sleep cycle adds no value and burns 13 minutes.
3. **Queue-fill scenarios** — action_log has backed up. User forces flush. Same as above.
4. **Deliberate deep maintenance** — user explicitly wants the full sleep cycle now (e.g., before a multi-day break). This is the ONLY case where full behavior is correct. Currently this is always on.

In cases 1-3 (the majority), the 13-minute sleep cycle is pure overhead.

---

## Goals

1. Make `consolidate_now` complete in under 30 seconds for typical use (cases 1-3 above).
2. Preserve the ability to request the full sleep cycle explicitly (case 4).
3. Update `_last_sleep_cycle` timestamp when the sleep cycle IS run via `consolidate_now`, so the 6-hour gate is respected going forward.
4. Add a `mode` parameter to the MCP tool so callers can distinguish light vs full intent.
5. Ensure the nightly cron remains unaffected (it calls `force_consolidate()` directly, not the MCP tool).
6. Write failing tests first (TDD).

---

## Non-Goals

- Changing the nightly cron behavior. It already runs consolidation-only (correct).
- Adding LLM inference to `consolidate_now` (no LLM phases exist currently — keep it that way).
- Changing the 6-hour gate threshold for scheduled sleep cycles.
- Changing the anchor audit pass gating (it already has its own config gate `ANCHOR_AUDIT_CONSOLIDATION_ENABLED`).
- Fixing the `generate_cluster_summaries` N+1 DB read bug (surfaces here but different fix, different version).

---

## Approach Options

### Option A — Gate Sleep Cycle Behind `_maybe_sleep_cycle()` (minimal fix)

**Change:** In `consolidate_now()`, replace `_st._sleep.run_sleep_cycle()` with `_st._consolidation._maybe_sleep_cycle()`. This applies the existing 6-hour gate.

**Pros:** Smallest possible change. Prevents the rapid back-to-back full cycle case.

**Cons:** User cannot request the full sleep cycle even when they want it. The 6-hour gate is also wrong for pre-shutdown use — user wants consolidation-only, not gated sleep. Does not fix the core problem: even with gate, first call of the day runs the full 13-minute cycle with no way to opt out.

### Option B — Remove Sleep Cycle from consolidate_now Entirely

**Change:** Delete the `run_sleep_cycle()` call from `consolidate_now()`. The sleep cycle would then only be triggered by the nightly cron (when PR-1 wires it via `_maybe_sleep_cycle()`).

**Pros:** Simple. `consolidate_now` becomes fast. Sleep cycle moves to nightly-only (which is the intended architecture post-v5.7.0 PR-0).

**Cons:** No manual way for the user to trigger the sleep cycle. If the nightly cron is broken (see v5.10.5 plan), sleep cycle never runs. Loses the "deliberate deep maintenance" use case.

### Option C — `mode` Parameter (recommended)

**Change:** Add `mode: str = "light"` parameter to `consolidate_now()` with values:
- `"light"` (default): `force_consolidate()` only. Anchor audit pass if enabled. No sleep cycle. Fast (< 30s typical).
- `"full"`: `force_consolidate()` + `run_sleep_cycle()` + anchor audit. Sets `_last_sleep_cycle` timestamp. Full behavior, callers opt in.

**Pros:**
- Default is fast. All current callers that call `consolidate_now()` without args get the fast path.
- Explicit opt-in for the sleep cycle. Power user can request `consolidate_now(mode="full")`.
- Sets `_last_sleep_cycle` after a full run — fixes the double-fire bug.
- Backward compatible at the MCP protocol level (new optional param, existing calls unaffected).
- Preserves the nightly cron design intent (sleep cycle is optional offline work, not required on every flush).

**Cons:** One new parameter. Callers need to know the distinction to opt into the full cycle.

---

## Recommended Approach: Option C (`mode` parameter)

Reasoning:

- `force_consolidate()` docstring explicitly says "Ignores CONSOLIDATION_COOLDOWN_SECONDS — an explicit user/MCP request beats throttling." This tells us the design intent is user-controlled, not auto-throttled. Option A fighting this intent.
- Option B is too restrictive — loses the deliberate maintenance path entirely.
- Option C matches the "explicit user/MCP request" design intent. The user gets what they ask for: light flush by default, heavy maintenance on demand.
- The mode parameter also serves as documentation at the call site — `consolidate_now(mode="full")` is self-explanatory.

### Implementation Plan

**Files to modify:**

1. `yadgar/server/tools/admin_other.py` — add `mode: str = "light"` param, branch on value.
2. `yadgar/sleep_compute/__init__.py` — no change needed (sleep cycle logic unchanged).
3. `yadgar/consolidation/__init__.py` or orchestrator — no change needed (force_consolidate unchanged).
4. `yadgar/tests/test_consolidate_now.py` — new test file (TDD: write failing tests first).

**Code change (admin_other.py):**

```python
@_tool(power=True)
def consolidate_now(mode: str = "light") -> dict:
    """Trigger an immediate consolidation cycle.

    mode="light" (default): consolidation cycle only (decay, episodes, merge,
        CLS, causal). Fast — typically < 30 seconds. Correct for pre-shutdown
        flushes, debug runs, and queue-fill scenarios.

    mode="full": consolidation cycle + full sleep cycle (dream replay,
        community detection, cluster summaries, re-embedding, compression,
        auto-narrate). Takes 5-15 minutes. Use for deliberate maintenance
        before a multi-day break or after large memory import.
    """
    if _st._consolidation is None:
        return {"status": "error", "message": "Consolidation engine not initialized"}

    if mode not in ("light", "full"):
        return {"status": "error", "message": f"Invalid mode {mode!r}. Use 'light' or 'full'."}

    stats = _st._consolidation.force_consolidate()

    if mode == "full" and _st._sleep is not None:
        try:
            sleep_stats = _st._sleep.run_sleep_cycle()
            stats["sleep_cycle"] = sleep_stats
            # Update the 6-hour gate timestamp so nightly cron sees the cycle ran
            _st._consolidation._last_sleep_cycle = datetime.now(UTC)
        except Exception:
            logger.exception("Sleep cycle failed during consolidate_now(mode='full')")

    # Anchor audit pass remains gated on its own config flag (unchanged)
    cfg = get_settings()
    if cfg.ANCHOR_AUDIT_CONSOLIDATION_ENABLED:
        try:
            from yadgar.server.tools.audit import _run_anchor_audit_pass  # noqa: PLC0415
            anchor_pass_stats = _run_anchor_audit_pass(_get_storage())
            stats["anchor_audit_pass"] = anchor_pass_stats
        except Exception:
            logger.exception("Anchor audit pass failed during consolidate_now (non-fatal)")

    return {"status": "completed", "mode": mode, **stats}
```

Note: import `datetime` and `UTC` at the top of the function or from consolidation module.

---

## Tests (Red-First TDD)

Write these tests in `yadgar/tests/test_consolidate_now.py` BEFORE implementing:

```python
# Test 1: light mode (default) does NOT call run_sleep_cycle
def test_consolidate_now_light_no_sleep_cycle(mock_state):
    # GIVEN: state with initialized consolidation and sleep
    # WHEN: consolidate_now() called with default args
    # THEN: force_consolidate() called, run_sleep_cycle() NOT called
    # AND: result contains no "sleep_cycle" key

# Test 2: full mode calls run_sleep_cycle
def test_consolidate_now_full_calls_sleep_cycle(mock_state):
    # GIVEN: state with initialized consolidation and sleep
    # WHEN: consolidate_now(mode="full")
    # THEN: force_consolidate() AND run_sleep_cycle() both called
    # AND: result contains "sleep_cycle" key

# Test 3: full mode sets _last_sleep_cycle timestamp
def test_consolidate_now_full_sets_last_sleep_cycle_timestamp(mock_state):
    # GIVEN: _last_sleep_cycle is None on consolidation
    # WHEN: consolidate_now(mode="full")
    # THEN: _st._consolidation._last_sleep_cycle is not None
    # AND: it's approximately datetime.now(UTC)

# Test 4: light mode does NOT set _last_sleep_cycle
def test_consolidate_now_light_does_not_set_timestamp(mock_state):
    # GIVEN: _last_sleep_cycle is None
    # WHEN: consolidate_now() [default light]
    # THEN: _last_sleep_cycle remains None

# Test 5: invalid mode returns error
def test_consolidate_now_invalid_mode(mock_state):
    # WHEN: consolidate_now(mode="invalid")
    # THEN: returns {"status": "error", "message": contains "Invalid mode"}
    # AND: force_consolidate() NOT called

# Test 6: sleep cycle exception is caught in full mode
def test_consolidate_now_full_sleep_cycle_exception(mock_state):
    # GIVEN: run_sleep_cycle() raises an exception
    # WHEN: consolidate_now(mode="full")
    # THEN: does not propagate exception
    # AND: result still has status "completed"
    # AND: no "sleep_cycle" key (or error key instead)

# Test 7: anchor audit pass still fires when enabled (light mode)
def test_consolidate_now_light_anchor_audit_fires(mock_state, settings_with_audit_enabled):
    # WHEN: consolidate_now() [light] with ANCHOR_AUDIT_CONSOLIDATION_ENABLED=True
    # THEN: _run_anchor_audit_pass() called
    # AND: result has "anchor_audit_pass" key

# Test 8: result includes mode field
def test_consolidate_now_result_includes_mode(mock_state):
    # WHEN: consolidate_now(mode="light")
    # THEN: result["mode"] == "light"
    # WHEN: consolidate_now(mode="full")
    # THEN: result["mode"] == "full"
```

---

## Acceptance Criteria

1. `consolidate_now()` (no args) completes in < 30 seconds on a live yadgar instance with normal memory load.
2. `consolidate_now(mode="full")` runs the sleep cycle and sets `_last_sleep_cycle`.
3. Two rapid successive calls to `consolidate_now()` do NOT run the sleep cycle on either call.
4. All 8 tests above pass.
5. No existing tests broken.
6. MCP schema documentation updated (docstring is the source of truth for MCP tools).

---

## Open Questions

1. **Should `mode="full"` also respect the 6-hour gate?** Currently the design is: user requests full = full runs unconditionally. But one could argue that even a full request should be gated within the same session. Main thread to decide. Recommended: no gate (explicit request beats throttling, consistent with `force_consolidate` docstring intent).

2. **Should the nightly cron PR-1 ever wire `_maybe_sleep_cycle()`?** The v5.7.0 PR-0 commit said `_maybe_sleep_cycle` is "preserved for PR-1 to wire". The nightly cycle at `yadgar/scripts/nightly_cycle.py:155` calls `force_consolidate()` only — no sleep cycle. Is the sleep cycle intended to run nightly? If yes, it should be added to the cron script (not `consolidate_now`). This plan does not change the cron.

3. **`generate_cluster_summaries` N+1 bug:** `get_all_memories_with_embeddings()` is called once PER CLUSTER inside a loop (line 134 of `community.py`). This means if there are 20 clusters, 20 full table scans occur. This amplifies the DB load significantly. Should this be fixed in the same version or a separate issue?

4. **Nightly cron sleep cycle gap:** If sleep cycle is removed from default `consolidate_now`, and the nightly cron does NOT call `_maybe_sleep_cycle()`, then sleep cycle NEVER runs in the current deployed state. Is this acceptable while v5.10.5 nightly cycle bugs are unresolved? Probably yes — it means less CPU load while nightly cycle is being fixed.

---

## Dependencies

- Does NOT depend on v5.10.5, v5.10.6, v5.10.7, or v5.10.8.
- Does NOT require backend changes (no embedding model calls in the modified path).
- Does NOT require DB schema changes.
- Pre-commit hooks must pass (ruff, mypy, test suite).

## Risk and Rollback

**Risk:** Callers who relied on `consolidate_now` to run the sleep cycle (e.g., a script that calls it as a maintenance action) will now get light mode. Risk: low. Calling scripts can be updated to pass `mode="full"`. The user is the primary caller of this tool.

**Rollback:** Revert `admin_other.py` change. One-file change = trivial rollback.

## Files to Modify

| File | Change |
|------|--------|
| `yadgar/server/tools/admin_other.py` | Add `mode` param, branch on light vs full |
| `yadgar/tests/test_consolidate_now.py` | New test file (8 tests) |

No other files require changes.

---

## Related: PreToolUse:Bash Hook Error — `(root): Invalid input`

### Investigation

The user reports recurring `PreToolUse:Bash hook error — (root): Invalid input` in tool call paths.

**Hook configuration found:**

- `~/.claude/settings.json` has NO `PreToolUse` section — it is in the project-local `.claude/settings.json`.
- `/home/max/git/yadgar/.claude/settings.json` line 71-84: `PreToolUse` hook with `matcher: "Bash"` runs `hook_runner.py db-lockdown-check`.
- The hook runner outputs: `{"decision": "allow"}` or `{"decision": "block", "reason": "..."}`.

**Schema discrepancy found:**

The Claude Code official hook examples (found in `/home/max/.claude/plugins/marketplaces/claude-plugins-official/plugins/plugin-dev/skills/hook-development/`) use:
```json
{"hookSpecificOutput": {"permissionDecision": "deny"}, "systemMessage": "..."}
```

The hookify plugin (`rule_engine.py`) outputs BOTH schemas depending on hook type:
- For non-`PreToolUse`/`PostToolUse`: `{"decision": "block", "reason": "..."}`
- For `PreToolUse`/`PostToolUse`: `{"hookSpecificOutput": {"permissionDecision": "deny"}}`

The yadgar `hook_runner.py:db-lockdown-check` outputs `{"decision": "allow"}` and `{"decision": "block"}` — the OLD schema. The `yadgar-stop-memory-checkpoint.py` also uses `{"decision": "block"}`.

**Verdict: Not a yadgar bug in the classical sense, but a schema mismatch.**

The old `{"decision": "allow"}` schema appears to still be processed by Claude Code (the allow path silently passes through; the `Invalid input` error appears on SOME Bash invocations but not all). The `(root): Invalid input` JSON Schema error suggests Claude Code validates the hook output against a schema and the old `decision` field fails validation.

**Why it's intermittent:** Only the BLOCK path returns `{"decision": "block"}`. The allow path returns `{"decision": "allow"}`. If `Invalid input` only fires on certain Bash commands, the block path is not being hit for those commands — the error may instead come from a different hook or from an edge case in how the hook reads stdin (e.g., if the hook is called with a tool that doesn't pass `tool_input.command`).

**Root cause (most likely):** Claude Code's `PreToolUse` hook now expects the new schema (`hookSpecificOutput.permissionDecision`) for structured decisions. The old `decision` field is accepted for `Stop` hooks but may be invalid for `PreToolUse`. When yadgar outputs `{"decision": "allow"}`, Claude Code validates it and emits `(root): Invalid input` but falls through to allowing the command (fail-open). This is not data-corrupting but generates noise in the UI.

**Fix (2-line, should be in v5.10.4 or standalone):**

In `yadgar/scripts/hook_runner.py`, update `hook_db_lockdown_check()`:

```python
def hook_db_lockdown_check() -> None:
    """PreToolUse (Bash) — block direct docker exec into yadgar containers."""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}}))
        return

    cmd = data.get("tool_input", {}).get("command", "")
    if "docker exec yadgar-backend" in cmd or "docker exec yadgar-db" in cmd:
        print(json.dumps({
            "hookSpecificOutput": {"permissionDecision": "deny"},
            "systemMessage": (
                "Direct docker exec into yadgar DB/backend containers is blocked "
                "to prevent data corruption. Use yadgar MCP tools instead."
            ),
        }))
    else:
        print(json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}}))
```

Also update `install_hooks_lib.py` if it generates this hook (it references it but doesn't generate the response body). The installed version at `.claude/hooks/hook_runner.py` also needs updating — it's a copy that was installed.

**Scope of fix:** `hook_runner.py` is the source; the installed copy at `.claude/hooks/hook_runner.py` is a duplicate maintained separately. Both need updating, or `install_hooks` needs to copy fresh from source.

**Note:** The `yadgar-stop-memory-checkpoint.py` also uses `{"decision": "block"}` — this is for the `Stop` hook, not `PreToolUse`. Stop hook schema may be different; confirm before changing.

**Recommendation:** Include this 2-line fix in the v5.10.4 commit to eliminate the noise. It is low-risk (the allow path is unchanged in behavior, only schema compliance improves).
