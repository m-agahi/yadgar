# PLAN — v5.41.4: Roadmap-update gap signal + wiki_append_section convention

**Status:** drafted 2026-06-02. Tiny patch (hotfix tier).

**Origin:** 2026-06-02 user observation — "you keep missing the roadmap update after ships." Root-caused this session:
1. Stop hook signals don't detect "version bumped on master since last roadmap update" — no hard gate.
2. Full RMW pattern on the ~9k-token roadmap wiki is expensive — subconsciously deferred.
3. Concurrent ships (v5.41.0/1/2/3 + v5.50 + v5.39 + v5.37 cluster) compound the gap.

v5.41.0 shipped `wiki_append_section` MCP tool. Convention can shift from full RMW per ship → targeted section append. Drops per-ship cost ~9k → ~500 tokens.

**Effort estimate:** 0.5 day.

**Branch:** `fix/v5.41.4-roadmap-signal` off master (after v5.41.2 and v5.41.3 ship).

---

## Problem

Workflow rule (anchored in wiki + CLAUDE.md): "After EACH ship: read-modify-write the roadmap wiki." Routinely missed. No mechanism to detect the gap.

## Fix scope

### 1. New signal: `roadmap_update_lag`

`yadgar/server/tools/project.py::project_brief(mode="signals")`:

Add to returned signals dict:
- `roadmap_update_lag_hours: float` — hours between roadmap wiki `updated_at` and most recent master HEAD commit timestamp. 0 if roadmap is newer; positive if master has moved since roadmap last refreshed.

Add to `recommended_actions`:
- When `roadmap_update_lag_hours > 0` AND most recent master commit looks like a ship commit (heuristic: matches `^merge: v\d+\.\d+\.\d+` OR contains "chore: bump version") → emit:
  ```
  {
    "action": "update_roadmap",
    "reason": "master moved Xh ago; roadmap not updated since",
    "suggested_call": "wiki_append_section('yadgar-roadmap-future-improvements', ...)"
  }
  ```

This becomes a Stop-hook recommended action — main thread is prompted explicitly when shipping without updating roadmap.

### 2. Convention shift: `wiki_append_section` for ship entries

Update workflow rules anchored in roadmap wiki + CLAUDE.md:

OLD:
> "After EACH ship: read-modify-write the roadmap wiki."

NEW:
> "After EACH ship: use `wiki_append_section(slug='yadgar-roadmap-future-improvements', section='Recently shipped', content='- vX.Y.Z (date): ...', position='start_of_section')`. Reserve full RMW for restructures."

Document the pattern with example. Add `yadgar/docs/WORKFLOW_ROADMAP_UPDATE.md` (new file) with templated section-append snippet.

### 3. Tests

`yadgar/tests/test_roadmap_update_signal.py`:

1. `test_signal_present_when_master_newer` — patch master HEAD commit timestamp to be 2h ago, set roadmap `updated_at` to 24h ago, assert `roadmap_update_lag_hours == 22` (2h ago - 24h ago = 22h positive lag).
2. `test_signal_zero_when_roadmap_newer` — roadmap updated_at newer than master HEAD → lag = 0.
3. `test_recommended_action_fires_on_ship_commit` — master HEAD message matches `merge: vX.Y.Z` pattern → `update_roadmap` action present.
4. `test_recommended_action_skips_non_ship_commit` — master HEAD message is `docs(plan): ...` → action NOT present (docs commit isn't a ship).
5. `test_signal_no_master_branch` — feature branch checkout: lag computed against master HEAD, not current branch.
6. `test_roadmap_wiki_not_found` — wiki slug missing → signal returns sentinel value (e.g., -1) + skipped action with explanation.

## Acceptance criteria

1. `project_brief(mode="signals")` returns `roadmap_update_lag_hours`.
2. `recommended_actions` includes `update_roadmap` when conditions met.
3. Workflow rule updated in roadmap wiki + CLAUDE.md anchored block.
4. New `docs/WORKFLOW_ROADMAP_UPDATE.md` with templated `wiki_append_section` snippet.
5. 6 tests green; all existing tests still pass.
6. Version bumped 5.41.3 → 5.41.4 (assumes 5.41.3 ships first).
7. CHANGELOG + MIGRATION_NOTES entry.

## Non-goals

- No automated wiki update (that's a v5.99-class agent).
- No retroactive enforcement on past missed updates.
- No deletion of the existing "after EACH ship: full RMW" rule — replaced, not removed.
- No restructuring of the roadmap wiki itself.

## Risks

- False positives on non-ship commits that match the heuristic. Mitigation: heuristic is conservative (must match `merge: vX.Y.Z` OR `chore: bump version`); tunable.
- Heuristic misses unusual ship commit messages. Mitigation: log + adjust heuristic when observed.
- Master HEAD commit timestamp doesn't equal ship time (could be old commit if rebased). Mitigation: also check `git log -1 --format=%at` for committer date, not author date.

## Phases (3 commits)

1. **Signal + recommended action.** Add `roadmap_update_lag_hours` to `project_brief(mode="signals")` + emit `update_roadmap` recommended action. Tests. → COMMIT `feat(project_brief): roadmap_update_lag signal + update_roadmap recommended action`
2. **Workflow doc.** Update anchored workflow rule in roadmap wiki (via `wiki_append_section` — dogfood the new pattern). Create `docs/WORKFLOW_ROADMAP_UPDATE.md`. → COMMIT `docs: wiki_append_section convention for roadmap ship entries`
3. **Version bump + CHANGELOG + MIGRATION_NOTES.** → COMMIT `chore: bump version 5.41.3 → 5.41.4 + docs`

## References

- v5.41.0 — `wiki_append_section` MCP tool (enables the convention shift)
- `yadgar/server/tools/project.py::project_brief` — signal addition point
- Anchored workflow rule in `yadgar-roadmap-future-improvements` wiki
- 2026-06-02 session — root-cause analysis of the gap

## Coordination

Ship AFTER v5.41.2 (wait flag) + v5.41.3 (I9 attribution fix). Tiny patch — single agent dispatch, NO isolation, sonnet, ~0.5d.

After ship: dogfood — first ship under new convention exercises the `wiki_append_section` path. If it works smoothly, propagate. If clunky, iterate.
