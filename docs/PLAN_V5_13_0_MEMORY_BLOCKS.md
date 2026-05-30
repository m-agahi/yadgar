# PLAN — v5.13.0: In-context Memory Blocks (Letta-style core memory primitive)

**Status:** drafted 2026-05-30 by Adopt-4 parallel agent in response to competitor audit (`docs/competitor-audit-2026-05-30.md` Item 4 — "In-context memory blocks").

**Sequencing:** v5.13.0 slot. After v5.11.0 (anchor cross-project) and v5.12.0 (wiki bookmarks). Independent of both. NOT v6.x — the scan agent found ~30% of this is already shipped via `_active_work` / `_project_init` / anchors; the remaining gap is a single primitive addition, not an architectural rethink. PLAN_V5_2 / PLAN_V5_3 "deferred to v6 (architectural rethink needed)" framing pre-dates the scan finding and is superseded by this plan.

**Master at draft time:** core v5.10.3 shipped. v5.10.4, v5.10.5, v5.10.6, v5.10.7, v5.10.8, v5.10.9, v5.11.0, v5.12.0, v5.20.0 plans queued.

**Related prior plans / wiki:**
- `docs/competitor-audit-2026-05-30.md` Item 4 (primary reference)
- `docs/AUDIT_DECISIONS.md` (decision will be recorded here as ADOPT for Adopt-4)
- `docs/PLAN_V5_2.md:53` "Letta-style agent-self-edit-at-inference (architectural rethink needed)" — SUPERSEDED
- `docs/PLAN_V5_3.md:66` same — SUPERSEDED
- `yadgar-anchor-memory-design-scopes-and-surfacing` (anchor scope precedent)
- `yadgar/server/tools/project.py:1118` (`update_active_work` — existing similar primitive)
- `yadgar/storage/wiki.py:390` (`upsert_active_work` — storage layer)

---

## Why

Yadgar today injects context at three discrete moments:

1. **SessionStart hook** (`session-start-context.py`) — pulls hot memories + checkpoint + anchors into Claude's first user message.
2. **PostCompact hook** (`post-compact-rehydrate.sh`) — re-injects after `/clear` or `/compact`.
3. **Explicit calls** — `restore(directory)`, `project_brief(directory)` invoked by the agent.

None of these reflect mid-session mutations without a round-trip:
- Agent learns a new gotcha at T+5min → `memorize(...)` writes it to DB but the agent's **current** in-context window is unchanged. Next message: gotcha is invisible unless agent calls `recall(...)` again.
- Agent updates `_active_work` via `update_active_work(...)` → DB updated, but in-context view is stale until next SessionStart.

**Letta's core memory blocks** solve exactly this: a small set of always-injected text blocks (typically 1-5, each ≤2000 chars) editable mid-session via tool calls. After every block edit, the system reminder re-emits the block content so the agent sees its own write reflected without a re-read. The block lives in the system prompt — zero retrieval latency, no token cost for "remembering" frequently-needed facts.

The audit (Item 4) called this out as "medium impact, medium effort." The scan agent's 30% overlap finding refines: `_active_work` is one block (per-directory, episodic, single content string, edit via `update_active_work`). The greenfield 70% is: (a) named multi-block support, (b) global scope alongside project scope, (c) PostToolUse-driven re-injection so writes are immediately visible, (d) `replace` / `append` patch semantics not just full overwrite, (e) per-block char limit enforcement at storage.

---

## What Letta does (precise mechanism, since task framing was slightly off)

The task brief said Letta blocks are "edit-in-context without round-trip." That's not literally true — Letta blocks DO require a tool call to write (`core_memory_replace`, `core_memory_append`). The "no round trip" win is:

1. **Block content lives in the system prompt** — re-rendered every turn. Agent never needs to read a block; it's already visible.
2. **Block edits are tool calls but the response includes the updated block** — so the agent's next reasoning step uses the fresh content.
3. **Multiple named blocks** — `persona`, `human`, plus user-defined — each scoped, each char-limited.

For Claude Code via MCP (no system-prompt control), achieve the same by:
- **SessionStart hook injects all blocks** as a fenced markdown section in the first user message.
- **PostToolUse hook on `block_*` tools** emits a system reminder containing the updated block content. Claude treats system reminders as authoritative same-turn context.
- **PreCompact hook re-injects** so blocks survive compaction (mirrors `post-compact-rehydrate.sh`).

This gives the same property: agent reads blocks zero times, writes them through MCP, and never has a stale view of its own writes.

---

## Goals

1. **New primitive `memory_block`** — named, scoped, length-capped, always-injected.
2. **Six MCP tools** — `block_create`, `block_replace`, `block_append`, `block_overwrite`, `block_delete`, `block_list`.
3. **SessionStart + PostToolUse + PreCompact hook integration** — block content visible to agent without explicit read.
4. **Per-scope char budget** — protects against block-pollution context bloat.
5. **Default block set on bootstrap_project** — `current_task` (project scope) + `gotchas` (project scope). Global scope ships empty.
6. **TDD-first** — every primitive lands with a failing test first.
7. **No schema migration trauma** — extend existing `memory` table via tag convention `_block:<name>`. Reuses secret-gate (I26), heat protection, branch-aware queries.

---

## Non-goals

- **No UI surface.** Blocks are an agent-facing primitive. No viz integration in v5.13.0. (Possible v5.13.x: viz panel for human inspection — out of scope here.)
- **No automatic summarization.** Blocks are exactly what the agent writes — no LLM-curated trimming. (v6 LLM curator may add this; out of scope.)
- **No multi-tenancy.** Single-user yadgar — one block namespace per scope.
- **No cross-block dependencies.** Each block is independent text.
- **No bookmark integration.** Wiki Bookmarks (v5.12.0) is a read-only viz pin for wiki pages; blocks are a write-capable agent context primitive. Different beasts — explicitly NOT merged. Only shared concept is "user-curated pin," too thin to bridge.
- **No deprecation of `_active_work` in v5.13.0.** Both coexist initially. Block named `_active_work` (project scope) is a candidate canonical migration in v5.13.1+ once the primitive is proven.
- **No agentic sleep-time block refinement.** Letta has it; yadgar's batch consolidation already covers the consolidation niche. Defer to v6 curator if revisited.

---

## API surface (MCP tools)

Located in `yadgar/server/tools/blocks.py` (new file). All `@_tool(power=True)` with secret-gate via I26 chokepoint.

| Tool | Signature | Semantics |
|---|---|---|
| `block_create` | `(name: str, content: str, scope: str = "project", char_limit: int = 2000, directory: str \| None = None)` | Creates new block. Fails if `(name, scope, directory)` triple exists. `scope` ∈ {`global`, `project`}. `directory` required when `scope='project'`. Returns `{id, name, scope, content, char_limit}`. |
| `block_replace` | `(name: str, old_str: str, new_str: str, scope: str = "project", directory: str \| None = None)` | Substring replace (Letta parity). Errors if `old_str` not found, or appears >1 time. Returns updated block dict. |
| `block_append` | `(name: str, content: str, scope: str = "project", directory: str \| None = None)` | Appends to block. Errors if resulting length > `char_limit`. |
| `block_overwrite` | `(name: str, content: str, scope: str = "project", directory: str \| None = None)` | Full replace. Errors if `len(content) > char_limit`. |
| `block_delete` | `(name: str, scope: str = "project", directory: str \| None = None)` | Removes block. Idempotent. |
| `block_list` | `(scope: str \| None = None, directory: str \| None = None)` | Returns `[{name, scope, content, char_limit, updated_at}, ...]`. `scope=None` returns both scopes for given directory. |

**Error model:** all errors return `{ok: False, error: "..."}` (existing yadgar tool convention). Never raise.

**Hard caps (env-knob configurable, I25 three-way registered):**

| Knob | Default | Type | Purpose |
|---|---|---|---|
| `MEMORY_BLOCK_MAX_PER_SCOPE` | 10 | int | Max blocks per (scope, directory) tuple |
| `MEMORY_BLOCK_DEFAULT_CHAR_LIMIT` | 2000 | int | Default per-block char_limit when not specified |
| `MEMORY_BLOCK_HARD_CHAR_LIMIT` | 8000 | int | Absolute max char_limit any block can be created with |
| `MEMORY_BLOCK_TOTAL_BUDGET_CHARS` | 12000 | int | Hard cap on sum of all blocks injected at SessionStart (global + project for cwd) |

Block creation fails with structured error when caps exceeded — never silently truncates.

---

## Storage

**Decision: extend existing `memory` table via tag convention.** Reuses secret-gate (I26 Layer 1), heat protection, branch-aware queries, registry pattern, restore() ranking infrastructure. Cheapest viable path.

Tag convention:
- `_block` — marker tag (all blocks)
- `_block:<name>` — name-scoped tag (one per block)
- `_block_scope:global` OR `_block_scope:project` — scope marker

Row shape (in `memory` table):
```
id              : <next_id>
content         : <block content>
tags            : ["_block", "_block:<name>", "_block_scope:<scope>", "_anchor"]
directory_context: "global" for global scope, "<resolved abs path>" for project
is_protected    : true   (never decay)
store_type      : "semantic"
heat            : 1.0    (never decay)
embedding       : NONE   (blocks are not retrieval targets)
char_limit      : NEW FIELD — int, see migration below
created_at      : ISO timestamp
last_accessed   : updated on every block_* operation
```

**Schema migration `migration_008_memory_block_char_limit.surql`:**

```surql
DEFINE FIELD char_limit ON memory TYPE option<int> DEFAULT NONE;
```

Single field addition, default NONE. Existing rows unchanged. Only `_block`-tagged rows populate it.

**Uniqueness invariant** (enforced at API layer, NOT DB constraint to avoid migration risk):

```
(name, scope, directory_context) must be unique among _block-tagged rows
```

`block_create` queries before insert; `block_replace`/`append`/`overwrite` query-then-update. Race window is single-session (yadgar = single-writer per directory in practice) — accept eventual consistency.

**Why NOT a new `memory_block` table:**
- Duplicates secret-gate plumbing (I26 mandates single chokepoint at storage layer — adding a parallel table doubles the maintenance surface).
- Duplicates heat protection logic.
- Duplicates branch-aware query helpers.
- Storage layer test infrastructure (`test_storage_*`) all targets `memory` table — tests would need parallel scaffolding.

**Constraint that would force a separate table** (none currently apply, document for future):
- If blocks ever need fields incompatible with `memory` (e.g., binary content, ACL columns, multi-row JSON structure), split then.
- If `MEMORY_BLOCK_MAX_PER_SCOPE` enforcement needs DB-level constraint, add it via SurrealDB `ASSERT count() < N` clause — still possible on shared table.

---

## Hook integration

### 1. SessionStart (modify `session-start-context.py`)

After existing checkpoint/anchors/hot-memories block, append:

```python
# Fetch all blocks for directory (project + global)
block_rows = db.query(
    "SELECT content, tags FROM memory "
    "WHERE '_block' IN tags "
    "AND (directory_context = $dir OR directory_context = 'global') "
    "ORDER BY directory_context, tags",
    {"dir": cwd},
)
```

Render as:

```
## Memory Blocks (always-injected, editable via block_* MCP tools)

### Global blocks
- `persona`: <content>

### Project blocks (/home/max/git/yadgar)
- `current_task`: <content>
- `gotchas`: <content>
```

Total render hard-capped at `MEMORY_BLOCK_TOTAL_BUDGET_CHARS`. Over budget → render with truncation marker + warning.

### 2. PostToolUse hook on `block_*` tools (NEW: `block-reflect.py`)

Located in `dotfiles/common/yadgar-hooks/block-reflect.py`. Matches tools `mcp__yadgar__block_create|block_replace|block_append|block_overwrite|block_delete`.

After successful tool execution, fetches the updated block(s) for the current cwd and emits a system reminder:

```
<system-reminder>
Memory block `current_task` updated. Current content:
---
<block content>
---
</system-reminder>
```

Hook config in user's `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {"matcher": "mcp__yadgar__block_(create|replace|append|overwrite|delete)",
       "hooks": [{"type": "command", "command": "python ~/.claude/hooks/yadgar-block-reflect.py"}]}
    ]
  }
}
```

### 3. PreCompact (extend `post-compact-rehydrate.sh`)

Existing hook re-injects checkpoint + anchors after `/clear` or `/compact`. Add block re-injection using the same DB query as SessionStart.

---

## Default block set on bootstrap_project

`bootstrap_project(directory)` (existing) gains side effect: if no blocks exist for that directory, seed:

- `current_task` (project, char_limit=2000, empty content) — agent-managed running state
- `gotchas` (project, char_limit=2000, empty content) — agent-collected non-obvious facts

Global scope ships empty (user/agent populates as needed).

Idempotent — re-running bootstrap_project does NOT overwrite existing blocks.

---

## Tests (red-first per TDD)

### Storage layer (`yadgar/tests/test_memory_blocks.py`)

1. `test_block_create_inserts_row` — creates block, DB row exists with correct tags + directory_context.
2. `test_block_create_duplicate_errors` — second create with same (name, scope, directory) returns error, no extra row.
3. `test_block_create_exceeds_char_limit_errors` — content > char_limit rejected at create.
4. `test_block_create_exceeds_hard_char_limit_errors` — char_limit > MEMORY_BLOCK_HARD_CHAR_LIMIT rejected.
5. `test_block_create_exceeds_per_scope_cap_errors` — 11th block in same scope rejected.
6. `test_block_replace_substring_unique` — exactly-one-occurrence replace succeeds.
7. `test_block_replace_substring_missing_errors` — old_str not found returns error, content unchanged.
8. `test_block_replace_substring_ambiguous_errors` — old_str appears 2+ times returns error.
9. `test_block_append_under_limit_succeeds` — append fits → success.
10. `test_block_append_over_limit_errors` — append exceeds char_limit → error, unchanged.
11. `test_block_overwrite_replaces_content` — full replace works, char_limit re-checked.
12. `test_block_delete_removes_row` — delete cleans the row.
13. `test_block_delete_nonexistent_idempotent` — delete on missing → no error.
14. `test_block_list_filters_by_scope` — scope='global' returns only global, scope='project' returns only project for cwd.
15. `test_block_list_includes_both_scopes_when_scope_none` — both returned.
16. `test_block_scopes_isolated_by_directory` — project block in /a not visible from /b.
17. `test_block_global_scope_visible_from_any_directory` — global block visible regardless of cwd.
18. `test_block_secret_gate_rejects_creation` — content containing `sk-ant-...` token rejected via I26 chokepoint.
19. `test_block_is_protected_true_never_decays` — heat decay pass leaves blocks untouched.
20. `test_block_persists_across_daemon_restart` — integration: write block, restart, read back.

### Hook integration (`yadgar/tests/test_block_hook_render.py`)

21. `test_session_start_renders_blocks_section` — given seeded blocks, hook output contains "## Memory Blocks" header + block names.
22. `test_session_start_renders_global_above_project` — ordering deterministic.
23. `test_session_start_omits_blocks_when_none_exist` — no header rendered if empty (avoid noise).
24. `test_session_start_truncates_at_total_budget` — sum of block content > budget → truncation marker present.
25. `test_block_reflect_hook_emits_system_reminder_after_create` — PostToolUse mock fires, stdout contains system-reminder fence.
26. `test_block_reflect_hook_fetches_correct_block_post_edit` — after `block_replace`, emitted content is the post-replace state.

### Bootstrap integration (`yadgar/tests/test_bootstrap_seeds_blocks.py`)

27. `test_bootstrap_project_seeds_default_blocks` — first bootstrap creates `current_task` + `gotchas`.
28. `test_bootstrap_project_idempotent_does_not_overwrite_blocks` — re-running with existing content preserves it.

### Env knob registration (existing I25 lint)

29. `python scripts/check_versions.py` exit 0.
30. `python scripts/check_config_three_way.py` — 4 new knobs in yaml + Settings + registry.

---

## Acceptance criteria

- All 30 tests pass.
- I13 + I23 + I24 + I25 + I26 + I27 lints green.
- Schema migration `008_memory_block_char_limit.surql` applies cleanly; pre-v5.13 rows unchanged.
- SessionStart hook output for a directory with 2 seeded blocks contains both block names + content.
- PostToolUse `block-reflect.py` hook tested manually: create a block, observe system reminder in next turn.
- `bootstrap_project` on fresh directory seeds `current_task` + `gotchas`, both empty.
- CHANGELOG + MIGRATION_NOTES updated.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Always-injected blocks blow up context window | `MEMORY_BLOCK_TOTAL_BUDGET_CHARS=12000` hard cap; warning + truncation marker if exceeded; per-block `char_limit` default 2000. |
| Agent over-writes a block losing user content | Blocks tagged `_anchor` + `is_protected=true` → restore() surfaces them; but no version history. Mitigate via `block_replace` patch semantics (forces explicit `old_str` — can't accidentally clobber). |
| Secret leak via block content | I26 chokepoint runs at storage layer — every block write goes through `gate_or_reject`. Tested in `test_block_secret_gate_rejects_creation`. |
| Block fragmentation across scopes confuses agent | Render in SessionStart with clear "### Global blocks" / "### Project blocks (<path>)" headers. Document in CLAUDE.md global guidance. |
| Hook timing — PostToolUse fires before block is committed | Tool call returns synchronously after DB write completes (yadgar's `@_tool` pipeline is sync). Hook reads after tool returns → race-free. Test 26 validates. |
| Conflict with parallel adopt-1 / adopt-5 plans | Plan-only, NEW file path (`docs/PLAN_V5_13_0_MEMORY_BLOCKS.md`). Storage migration number 008 — coordinate with adopt-1/5 if they also propose schema changes. |
| `_active_work` and `current_task` block overlap | Both coexist in v5.13.0. v5.13.1+ decide whether to canonicalize `_active_work` as the block named `_active_work` (project scope). Document in MIGRATION_NOTES as known overlap. |

---

## Estimate

- **Storage + 6 MCP tools + char-limit field:** ~250 LOC + ~350 LOC tests.
- **Hook integration (SessionStart edit + new `block-reflect.py` + PreCompact edit):** ~150 LOC + ~80 LOC tests.
- **Bootstrap seed integration:** ~30 LOC + ~50 LOC tests.
- **Env knobs (I25):** ~60 LOC config registration.
- **Docs (MIGRATION_NOTES + CHANGELOG + CLAUDE.md addition for block conventions):** ~100 lines markdown.

Total: ~640 LOC + ~480 LOC tests + ~100 lines docs. **~2-3 days dedicated work** with hook testing being the long pole (manual smoke + Playwright-free).

---

## Open questions (resolve before dispatch)

1. **Default block set scope.** Plan seeds `current_task` + `gotchas` per project. Alternatives: `persona` (global), `claude_self_review` (global). Lean: ship with empty defaults beyond the two above; let usage patterns drive additions in v5.13.x.
2. **Block name validation.** Plan permits any string. Should we restrict to `[a-z][a-z0-9_]*` to keep parseable? Lean YES (defensive).
3. **Deprecation path for `_active_work`.** v5.13.0 keeps both; v5.13.1 decision: (a) hard-deprecate and migrate, (b) keep both with `_active_work` as canonical "running task" block, (c) automatic alias. Lean (b) initially — let users opt into pure block model.
4. **PostToolUse hook in v5.13.0 or follow-up?** Plan ships it in v5.13.0 because without it, the "no round-trip" property doesn't hold — blocks would only refresh on SessionStart. Lean: SHIP IN v5.13.0. The block primitive without write-reflection delivers half the value.
5. **Block injection in `restore()` output?** Plan adds blocks to SessionStart hook but `restore()` MCP tool currently returns its own context bundle. Should `restore(directory)` also include blocks? Lean YES — keep parity, otherwise post-`/clear` rehydration is incomplete.
6. **Embed blocks (so `recall(query)` can match block content)?** Plan says `embedding=NONE` — blocks aren't retrieval targets, they're always-on. But edge case: agent forgets a block exists, calls `recall("project task")`, and doesn't find it. Lean NO — block always-on means agent doesn't need to recall; if it forgets, that's a SessionStart hook bug. Re-evaluate after first 30 days of use.
7. **Audit integration.** Should `audit_anchors` (v5.9.0+) consider blocks? Blocks are tagged `_anchor` for restore-surfacing, but they don't represent the same "high-value durable fact" concept anchors do. Lean: SKIP blocks in audit. Add scope filter `_block NOT IN tags` to audit queries. Document.
8. **Concurrent block edits across multiple Claude sessions in same directory.** Yadgar today serializes writes via SurrealKV lock. Last-writer-wins. Lean: document; add `updated_at` returned by every block_* tool so caller can detect "block changed under me" if they care.
9. **Char limit telling agent what to do when over budget.** Errors return `{ok: false, error: "..."}` but agent might not know how to recover. Lean: include suggestion in error string: `"block_replace exceeds char_limit (2150 > 2000); consider block_overwrite with summarized content or block_replace with shorter old/new pair"`.
10. **`docs/competitor-audit-2026-05-30.md` references vs current state.** The audit's "30% done" claim came from a scan agent; this plan revises to "specifically `_active_work` is one ad-hoc block, the named multi-block API is greenfield" — record in AUDIT_DECISIONS.md as ADOPT.

---

## Sequencing relative to other plans

- **After v5.11.0 (anchor cross-project)** — to avoid cross-contamination on anchor surfacing logic.
- **After v5.12.0 (bookmarks)** — independent, no overlap, but bookmarks lands first to keep PR pipeline clear.
- **Independent of v5.10.x train** — purely additive primitive.
- **Independent of v5.20.0 (roadmap freshness)** — different surface.

If v5.11/v5.12 slip, v5.13.0 can ship first (no hard dependency).

---

## Files to add / modify

### New
- `yadgar/server/tools/blocks.py` — 6 MCP tools.
- `yadgar/storage/blocks.py` — DB layer (CRUD + uniqueness check + char-limit enforcement).
- `dotfiles/common/yadgar-hooks/block-reflect.py` — PostToolUse hook (mirror to `~/.claude/hooks/yadgar-block-reflect.py` per nix-claude pattern).
- `yadgar/tests/test_memory_blocks.py` — storage + tool unit tests.
- `yadgar/tests/test_block_hook_render.py` — hook integration tests.
- `yadgar/tests/test_bootstrap_seeds_blocks.py` — bootstrap integration.
- `migrations/008_memory_block_char_limit.surql` — schema.

### Modify
- `dotfiles/common/yadgar-hooks/session-start-context.py` — add blocks section to rendered output.
- `dotfiles/common/yadgar-hooks/post-compact-rehydrate.sh` — re-inject blocks on rehydrate.
- `yadgar/server/tools/project.py` — `bootstrap_project` seeds default blocks.
- `yadgar/server/tools/restore.py` (or wherever `restore()` lives) — include blocks in output bundle.
- `yadgar/config_yaml.py` + `yadgar/server/settings.py` + `yadgar/config_registry.py` — 4 new env knobs (I25).
- `pyproject.toml` — 5.12.x → 5.13.0 bump (timing dependent on v5.12.0 ship state).
- `server.json`, `docker-compose.yml`, `uv.lock` — version sync.
- `CHANGELOG.md` — v5.13.0 entry.
- `MIGRATION_NOTES.md` — v5.13.0 section: schema migration step + new env knobs + hook install instructions.
- `docs/AUDIT_DECISIONS.md` — record Adopt-4 as ADOPT, link this plan.
- `docs/ARCHITECTURE_INVARIANTS.md` — note that `_block`-tagged rows are excluded from anchor audit (I9 amendment).

---

## Implementation phasing

If v5.13.0 lands in pieces:

1. **Phase 1 (storage + tools, ~1 day):** schema migration + 6 MCP tools + 20 storage tests. No hooks yet — tools callable via MCP.
2. **Phase 2 (SessionStart hook, ~half day):** modify `session-start-context.py` + render tests (#21-#24).
3. **Phase 3 (block-reflect hook, ~half day):** new `block-reflect.py` + tests (#25-#26).
4. **Phase 4 (bootstrap + restore integration, ~half day):** seed defaults + add to restore() + tests (#27-#28).
5. **Phase 5 (PreCompact + env knobs + docs, ~half day):** PreCompact rehydrate edit + I25 knob registration + CHANGELOG + MIGRATION_NOTES.

Total: ~2-3 days. Phases 2-3 can land separately if needed (tool-only release is useful for early-adopter testing).

---

## Cumulative state after v5.13.0

| Surface | pre-v5.13 | v5.13.0 |
|---|---|---|
| In-context state | hot memories + anchors + checkpoint + `_active_work` (single, ad-hoc) | + named multi-block primitive (global + project scopes) |
| Mid-session write-reflection | none (next SessionStart only) | PostToolUse re-emits updated block as system reminder |
| Default agent state | hook-injected if memorized | seeded `current_task` + `gotchas` per bootstrap |
| Per-context size cap | `restore()` budget only | + per-block char_limit + total block budget |
| Letta parity gap | "no in-context core memory" (audit Item 4) | core memory blocks shipped |

After v5.13.0, the audit's Item 4 gap closes. Yadgar gains a primitive that Letta agents take for granted, without inheriting Letta's runtime lock-in. Existing anchors / `_active_work` / `_project_init` remain — they serve adjacent purposes (durable facts vs running state vs always-on context).
