# PLAN — v5.33.1: Memory blocks follow-ups + `_MEMORY_UPDATABLE_FIELDS` invariant

**Status:** drafted 2026-06-01. Hotfix bundle (same pattern as v5.31.1).

**Why now:** v5.33.0 shipped MVP (storage + MCP tools + restore inject + bootstrap seeds). Six items were explicitly deferred. Slotting before v5.36/v5.37 keeps the deferred-list short. Also folds in the recurring `_MEMORY_UPDATABLE_FIELDS` class.

**Effort estimate:** 1-1.5 calendar days.

**Branch:** `fix/v5.33.1-blocks-followups` off master.

---

## Items

### Item 1 — I25 env knob registration

`yadgar/storage/blocks.py` ships hard-coded:
- `_MAX_PER_SCOPE = 10`
- `_DEFAULT_CHAR_LIMIT = 2000`
- `_HARD_CHAR_LIMIT = 8000`

Per I25 (config three-way-sync invariant), each must register in:
- `yadgar/config.py` — typed knob with default
- `yadgar/config_registry.py` — registration entry
- `yadgar/config_yaml.py` — YAML override path

Add:
- `MEMORY_BLOCK_MAX_PER_SCOPE: int = 10`
- `MEMORY_BLOCK_DEFAULT_CHAR_LIMIT: int = 2000`
- `MEMORY_BLOCK_HARD_CHAR_LIMIT: int = 8000`
- `MEMORY_BLOCK_TOTAL_BUDGET_CHARS: int = 12000` (new — cap total block content at restore time; prevent blocks-bombing context)

`yadgar/storage/blocks.py` reads from `config.settings.MEMORY_BLOCK_*` instead of module constants.

Tests: I25 lint should now pass for blocks; add a regression test that imports config + verifies the four knobs.

### Item 2 — PostToolUse `block-reflect.py` hook

Plan §193 in v5.33 master plan. Real-time re-injection on `block_*` MCP tool writes so updates land in next message context without `/restore`.

`yadgar/hooks/block-reflect.py`:
- PostToolUse matcher: `mcp__yadgar__block_(create|update|delete)`
- On hit: emit a Claude Code hook output that injects updated block contents into next context.
- Idempotent on duplicate calls within same message.

Wire into install_hooks.

### Item 3 — SessionStart automatic injection

Plan §163. Modify `yadgar/hooks/session-start-context.py`:
- Fetch all blocks for current directory (project + global scope)
- Render via the same `_render_blocks_section` helper from `restoration.py`
- Prepend to existing SessionStart output

Same rendering helper — DRY. Move it to `yadgar/blocks_render.py` or similar shared location if needed.

### Item 4 — PreCompact re-injection

Plan §221. Extend `post-compact-rehydrate.sh` (or equivalent PreCompact hook) to re-inject blocks alongside anchored memories.

Reason: blocks edited mid-session should survive `/compact`.

### Item 5 — `block_replace` + `block_append` patch semantics

Add two MCP tools:
- `block_replace(name, old_text, new_text, scope=None)` — string replacement; error if `old_text` not found OR found >1 time (force disambiguation).
- `block_append(name, text, scope=None)` — append with newline; respect HARD_CHAR_LIMIT.

Both `power=True`, secret-gated.

Rationale: full `block_update(name, content=...)` requires the agent to re-emit entire block content. Patch semantics are 10x cheaper for incremental edits (e.g., adding one bullet to `current_task`).

### Item 6 — `_active_work` canonicalization

Current state: `_active_work` is a tagged episodic memory (`update_active_work` tool). Memory blocks could replace it as a named block `active_work` instead.

Decision needed:
- (A) Keep both — different surfaces, parallel infrastructure. Bloat.
- (B) Canonicalize `_active_work` as a memory block — deprecate `update_active_work` MCP tool; restore() reads from block instead.
- (C) Defer — design call, no obvious right answer yet.

Lean: B if block hook integration (Items 2-4) ships smoothly. Otherwise defer C and slot for v5.50+ when block UX is proven.

### Item 7 — `_MEMORY_UPDATABLE_FIELDS` recurring class

Background: v5.17.0 fixed `confidence` missing from `_MEMORY_UPDATABLE_FIELDS` (so `memory_update(confidence=X)` silently no-op'd). `last_accessed` + `access_count` STILL missing — same class.

Two fixes:
- Add the two fields to `_MEMORY_UPDATABLE_FIELDS` in `yadgar/storage/memory.py` (or wherever the list lives).
- **Invariant test** that introspects the SurrealDB `memory` table schema fields + asserts every non-internal field appears in `_MEMORY_UPDATABLE_FIELDS`. Prevents future regressions of this class.

Excluded fields list (internal-only): `id`, `embedding`, `created_at`, `consolidation_state`, etc. Hard-code in the test.

---

## Acceptance criteria

1. Four `MEMORY_BLOCK_*` knobs in config.py + registry + YAML.
2. `yadgar/storage/blocks.py` reads from `config.settings.MEMORY_BLOCK_*`.
3. I25 lint passes for blocks (currently passing — re-verify).
4. PostToolUse `block-reflect.py` hook ships + registered via install_hooks.
5. SessionStart automatic block injection works.
6. PreCompact re-injection works.
7. `block_replace` + `block_append` MCP tools shipped + tested.
8. `_active_work` decision committed (A/B/C) + executed.
9. `last_accessed` + `access_count` updatable via `memory_update`.
10. Schema-vs-updatable-fields invariant test green.
11. Version bumped 5.33.0 → 5.33.1.
12. CHANGELOG + MIGRATION_NOTES updated.
13. All existing tests still pass.

## Non-goals

- No new block storage backend.
- No multi-tenant block scoping (still just project + global).
- No block versioning / history.
- No block-blob semantic search.

## Risks

- PreCompact hook may not exist in current Claude Code 2026 schema (verify via `claude-code-guide` if uncertain — anchor 491682 references the schema).
- Block-reflect hook latency on every block_* call — keep render path FAST (<50ms).
- `_active_work` canonicalization risks data loss if migration buggy. Mitigation: keep parallel path 1 release before deprecating.

## Dependencies

- v5.33.0 must be live (✓).
- No other dependencies.

## Coordination notes

Single agent dispatch. Worktree-isolated. Sonnet. 1-1.5d.
Same phase-commit discipline as v5.33.0 retry (commit after each item or item-pair, not at end).

References:
- Plan v5.33.0 §163, §193, §221, §227
- Anchor 491682 — Claude Code 2026 hook schemas
- v5.17.0 confidence fix — same `_MEMORY_UPDATABLE_FIELDS` class
- I25 — config three-way-sync invariant
