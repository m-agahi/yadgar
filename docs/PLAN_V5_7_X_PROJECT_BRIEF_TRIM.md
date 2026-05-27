# PLAN — v5.7.12: `project_brief` catalog-mode trim + two-audience split

**Status:** drafted 2026-05-27 (evening). Triggered by external critique of catalog-mode payload.

**Master at draft time:** core v5.7.11 + backend v5.3.1 (both deployed).

---

## Why

`project_brief(directory, mode="catalog")` serves TWO unrelated audiences with ONE payload, fitting neither well:

1. **Stop-hook caller** — needs 3 binary signals (`init_memory_present`, `active_work_present`, `stale_wiki_count`) to decide which write actions to fire. Anchor lists, hot memories, wiki keys = noise.
2. **`restore()` caller after `/clear`** — needs anchors + hot memories + wiki keys + checkpoint to reconstruct context. Binary signals = noise.

Today's catalog response: ~3KB of structured JSON + a `_render` markdown re-render of the same data. Docstring promises ~500 tokens; actual is several times that. Both audiences pay for content the other needs.

Concrete bugs surfaced by external critique 2026-05-27:

1. **`hot_memories` overlaps with anchors by ~100%** for any anchored memory (anchors are heat-immortal, always at max heat → always in top-N hot list). Architectural duplication.
2. **`top_anchors_global` + `top_anchors_project` split misleads.** Same anchor appearing in both lists isn't duplication — it's tagged both global AND project. Cleaner: single list, `scope: global|project|both` tag.
3. **`stale_checkpoint_hours` + `active_work_age_hours` not surfaced as numerics.** The stop hook rule uses `>24h` thresholds; catalog forces LLM to text-compare content for staleness. Threshold lives in two places (hook script + yadgar).
4. **`_render` field reproduces structured fields as markdown.** Useful for one-shot LLM consumption; pure overhead if consumer reads structured fields.
5. **Catalog token budget unmet.** Docstring `~500 tokens`; observed ~1500-3000 tokens.
6. **`recommended_actions` not pre-computed.** Stop hook LLM derives "refresh active_work" via comparison logic; yadgar has the data to pre-derive.

---

## What ships

### 1. Audience-aware mode parameter

Extend `mode` parameter beyond `catalog` / `full`:

| Mode | Audience | Payload |
|---|---|---|
| `signals` | Stop hook | Pure binary signals + numerics + `recommended_actions` only. <100 tokens. |
| `restore` | Post-/clear, post-/compact | Anchors + hot memories + checkpoint + wiki keys. ~800 tokens. |
| `catalog` (existing, deprecated) | Mixed bag, kept for back-compat | Current behavior. Marked deprecated in docstring. |
| `full` (existing) | Power user / debugging | catalog + everything inlined. |

Stop hook + the restore command both gain explicit mode-target call sites.

### 2. New numeric fields

Add to all relevant modes:

| Field | Type | Source |
|---|---|---|
| `stale_checkpoint_hours` | float \| null | `(now - checkpoint.created_at) / 3600`; null if no checkpoint |
| `active_work_age_hours` | float \| null | `(now - _active_work.created_at) / 3600`; null if missing |
| `init_memory_age_hours` | float \| null | same shape |

Stop hook reads these numerics, no longer text-compares content.

### 3. Pre-computed `recommended_actions`

Yadgar derives from signals + thresholds:

```json
{
  "recommended_actions": [
    {"action": "refresh_active_work", "reason": "age_hours=87.4 > threshold=24"},
    {"action": "bootstrap_project", "reason": "init_memory absent"}
  ]
}
```

Thresholds: configurable via Settings (`ACTIVE_WORK_STALE_HOURS`, `CHECKPOINT_STALE_HOURS`). Default 24. New yaml entries.

Stop hook caller maps action → tool call directly. Coupling between hook script + yadgar daemon collapses to one threshold source.

### 4. Bug fixes

- **`hot_memories` excludes anchored entries.** Filter at query time: `WHERE 'anchor' NOT IN tags AND '_anchor' NOT IN tags`.
- **Merge `top_anchors_global` + `top_anchors_project` into `top_anchors` with `scope` tag per entry.** Stop returning the same anchor twice in different list slots.
- **`_render` only emitted in `full` mode** (and current `catalog` for back-compat until removed).

### 5. Token budget enforcement

- `signals` mode: max 100 tokens, hard-checked in test.
- `restore` mode: max 800 tokens.
- Truncate `top_anchors` list at K entries (configurable env knob `PROJECT_BRIEF_MAX_ANCHORS`, default 12, fits 800-token budget).

---

## What does NOT ship in v5.7.12

| Item | Why deferred |
|---|---|
| Drop `catalog` mode entirely | Back-compat; many callers exist. Mark deprecated in docstring, keep for 1-2 minor releases, then remove in v5.8. |
| Drop `_render` field | Same back-compat reason. Some LLM-driven consumers rely on it. Deprecate in `catalog`, keep in `full`. |
| Audit + reduce wiki keys field | Currently 3-item list of wiki page summaries. Useful for some recall paths. Out of trim scope. |
| Restructure `checkpoint` field shape | Used by `restore()` extensively. Stable surface. Leave alone. |

---

## Implementation order

1. **TDD scaffolding** — `yadgar/tests/test_project_brief_modes.py`:
   - `signals` mode returns only binary signals + age numerics + `recommended_actions`.
   - `restore` mode returns anchors + hot + checkpoint + wiki, no signal dict.
   - `catalog` (deprecated) returns current shape.
   - `full` returns everything.
   - Token budget assertions per mode.
   - `hot_memories` excludes anchored entries.
   - `top_anchors` merged + scope-tagged.
   - `recommended_actions` derived from signal state.
2. **Add age computation helpers** in `yadgar/memory/timing.py` (new module) or extend `yadgar/checkpoint.py`.
3. **Add `recommended_actions` builder** in `yadgar/project_brief.py` (or wherever `project_brief` lives).
4. **Hot memories filter** — query change in the recall path used by `project_brief`.
5. **Merge anchor lists** — simplify the slicing logic; tag each anchor with `scope` field.
6. **Mode switch** in `project_brief` — branch on mode value, build appropriate payload.
7. **Settings + yaml + registry entries** for `ACTIVE_WORK_STALE_HOURS` + `CHECKPOINT_STALE_HOURS` + `PROJECT_BRIEF_MAX_ANCHORS` (3 new knobs, all three-way registered per I25).
8. **Update stop hook script** at `~/.claude/hooks/yadgar-stop-memory-checkpoint.py` to call `mode="signals"` + read `recommended_actions` directly. Drop the text-compare logic.
9. **Update `restore()` MCP tool** to call `mode="restore"` internally.
10. **MIGRATION_NOTES.md** + wiki 6163 update.

---

## Acceptance criteria

- `pytest yadgar/tests/test_project_brief_modes.py` green.
- Token-budget assertions hold: `signals` ≤100, `restore` ≤800.
- `hot_memories` returned never contains an anchored entry.
- `top_anchors` is a single list, each entry has `scope` field.
- Stop hook script + `restore()` updated to use new modes.
- I23 + I24 + I25 lints green.
- `python scripts/check_versions.py` exit 0.

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Breaking external consumers calling `catalog` mode | Keep `catalog` working unchanged; just deprecate in docstring. |
| Stop hook script + yadgar drift on threshold values | New Settings fields are the single source; hook script reads `recommended_actions` instead of recomputing. |
| `signals` mode too lean — missing edge cases | TDD coverage on stop-hook real-world signal patterns. |
| `hot_memories` filter changes recall semantics elsewhere | Filter ONLY applied in `project_brief` call path. Other recall paths unchanged. Verify via grep. |

---

## Estimate

~300 LOC implementation + ~200 LOC tests. Single agent dispatch. No image rebuild needed at first (project_brief lives in yadgar core; but Settings field changes → next yadgar core release v5.7.12).

Stop hook script change is separate from yadgar release — lives in `~/.claude/hooks/`. Could be split into pre-yadgar-release + post-yadgar-release commits or shipped together.

---

## Sequencing vs other trains

| Plan | Status | Order suggestion |
|---|---|---|
| Backend v5.4.0 (recall caching) | drafted, not started | Higher user impact. Ship first. |
| v5.7.12 project_brief trim (this) | drafted | Ergonomic / token-budget win for every session start. Ship second. |
| v5.8 (future) | undefined | post-soak |

User call on ordering.

---

## Open / parked questions

- **Token-budget enforcement strategy** — hard-truncate at K anchors OR rely on K as a soft cap and just trust `len(anchors)` math. Hard-truncate safer; harder to write deterministic tests.
- **`scope: both` value** — does a global anchor that's also project-specific exist today, or is the tag binary? Investigate before merging the two lists.
- **Stop-hook script ownership** — currently in `~/.claude/hooks/` (claude-code config, not yadgar repo). Coordination cost of updating it alongside yadgar release. Possible solution: yadgar ships the recommended `recommended_actions` shape; hook script just maps action → tool call without yadgar coupling.

---

## v5.8 follow-up (deferred)

- Drop `catalog` mode entirely (post-deprecation cycle).
- Drop `_render` from `catalog` (post-deprecation cycle).
- Audit all `project_brief` call sites across yadgar codebase + claude-code hooks to migrate them off `catalog` before drop.
