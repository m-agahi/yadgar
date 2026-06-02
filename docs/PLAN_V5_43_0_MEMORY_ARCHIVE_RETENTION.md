# PLAN — v5.43.0: memory_archive retention

**Status:** drafted 2026-06-02. DPs resolved. REVISED 2026-06-02 post-opus-review. READY for impl.

**Revision notes (opus reviewer):**
- ADDED Phase 0: `audit_anchors` extension to flag "anchored-by-prose-only" memories (no `_anchor` tag + no `is_protected=true` + heat=0 + present in archive table). Required pre-condition before retention can be enabled. Closes the DP-D data-loss-shape concern.
- TIGHTENED DP-D query: also exclude `tags CONTAINS 'anchor'` (no underscore) for legacy state. Cheap, defensive.
- DOCUMENTED `archived_at` re-archival semantics explicitly (not just in test names): a memory archived 91d ago that gets RE-archived 3d ago → `archived_at` updates to 3d ago, so won't be purged. Thrash guard (DP-E `created_at < 7d`) catches the secondary case where created_at is recent.

**Origin:** 2026-06-01 user observation — viz reports total > visible by ~1300 memories. Investigation confirmed `memory_archive` table has zero permanent-deletion policy:
- `prune_old_rows()` allowlist EXCLUDES `memory_archive` (`yadgar/storage/ops.py:110-118`)
- Schemaless table, no TTL (`yadgar/storage/migrations.py:434`)
- Cascade delete only fires when parent memory explicitly DELETEd (`yadgar/storage/memory.py:290-336`)
- v6 LLM curator was planned to handle scope-limited deletion — not shipped

Current state: ~1300 heat=0 memories accumulating with no auto-prune path.

**Slot:** v5.43.0 — between v5.41.x patches and v5.45 setup foundation.

**Effort estimate:** 1.5-2 calendar days.

**Branch:** `feat/v5.43.0-archive-retention` off master.

---

## Resolved decisions (2026-06-02 user-confirmed)

| DP | Decision | Rationale |
|---|---|---|
| **A — default retention** | **90 days** | 3 months grace. Conservative. User-tunable via `MEMORY_ARCHIVE_RETENTION_DAYS`. |
| **B — delete strategy** | **Hard delete + vacuum-snapshot recovery** | DELETE row. Existing vacuum snapshots in `~/.yadgar/archive/` provide recovery path. Simpler code, no soft-delete state machine. |
| **C — circuit breaker** | **500 / cycle (CRITICAL log + tunable)** | Bulk cleanup needs higher cap than v6 curator's 20/night. 500 reasonable for nightly cycle. |
| **D — anchor-tag exclusion** | **Skip `is_protected=true` AND any memory carrying `_anchor` tag** | Both flags treated as "do not touch." Covers legacy anchored memories that lost `is_protected=true`. |
| **E — thrash protection** | **Skip if memory `created_at` <7d ago** | Recent creation = re-archival cycle in progress. Wait until baseline stabilizes. |

Also: `archived_at` is the age anchor (not `created_at`). `migration_grace=true` excluded until grace deadline (PD-23 logic). `_active_work` blocks not affected (they're `memory_block` table, not `memory_archive`).

---

## 1. Problem

`memory_archive` rows accumulate forever once heat hits 0. No retention. SurrealKV grows. Viz total-vs-visible gap reflects this.

## 2. Goal

Auto-purge `memory_archive` rows older than configurable threshold during nightly consolidation. Default conservative. Protected/anchored memories never touched. Thrash-safe.

## 3. Scope

### Config knobs (I25 registered)

- `MEMORY_ARCHIVE_RETENTION_DAYS: int = 90` — 0 disables retention entirely.
- `MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER: int = 500` — max purges per cycle. CRITICAL log if hit.
- `MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS: int = 7` — skip purge if `created_at` younger than this.

### Storage layer

`yadgar/storage/ops.py`:
- New function `purge_expired_archives(dry_run: bool = False) -> dict` — returns `{candidates: N, purged: M, skipped_protected: K, skipped_anchor: L, skipped_recent: P, circuit_breaker_hit: bool}`.
- Query:
  ```surql
  SELECT * FROM memory_archive
  WHERE archived_at < (time::now() - $retention_days * 24h)
    AND is_protected != true
    AND NOT array::contains(tags, '_anchor')
    AND created_at < (time::now() - $thrash_guard_days * 24h)
    AND (migration_grace != true OR valid_until < time::now())
  LIMIT $circuit_breaker
  ```
- DELETE matched rows unless `dry_run=True`.

### Consolidation phase

`yadgar/consolidation/cleanup.py:184-211`:
- Extend `_run_retention_tasks()` to call `purge_expired_archives()` if `MEMORY_ARCHIVE_RETENTION_DAYS > 0`.
- Telemetry: `yadgar_archive_purged_total` counter, `yadgar_archive_retention_skipped` counter (per-reason labels: protected/anchor/recent/grace).

### MCP tool: `archive_purge`

`yadgar/server/tools/admin_archive.py` (new):
- `archive_purge(dry_run: bool = True, retention_days: int | None = None)` — `power=True`, secret-gated.
- `dry_run=True` (default): returns expected purge count + sample of 10 affected slugs. No deletion.
- `dry_run=False`: performs purge. Circuit breaker enforced.
- `retention_days=None`: use configured default. Otherwise override for one-off cleanup.

Return: same dict as storage layer.

## 4. Non-goals

- Not changing heat decay formula.
- Not changing `COLD_THRESHOLD` (heat<0.02 → archive transition).
- Not building v6 LLM curator (separate v6.0 scope).
- Not retroactively deleting protected/anchored memories.
- No soft-delete state machine.
- No retroactive UI for restoring purged memories (vacuum snapshot is the recovery path; manual restore).

## 5. Test plan (TDD — failing tests first)

`yadgar/tests/test_archive_retention.py`:

### Storage layer

1. `test_purge_respects_retention_age` — insert 3 archives at ages 30d/91d/180d; purge with default 90d; assert only 91d + 180d removed.
2. `test_purge_skips_protected` — `is_protected=true` archive at 180d; assert NOT purged.
3. `test_purge_skips_anchor_tag` — `_anchor` tag archive at 180d; assert NOT purged.
4. `test_purge_skips_recent_creation` — archived_at 91d ago BUT `created_at` 3d ago; assert NOT purged (thrash guard).
5. `test_purge_skips_migration_grace` — `migration_grace=true` + `valid_until` future; assert NOT purged.
6. `test_purge_migration_grace_after_expiry` — `migration_grace=true` + `valid_until` past; assert PURGED.
7. `test_circuit_breaker_caps_purge_count` — 600 archives all eligible; assert only 500 purged + CRITICAL log fired.
8. `test_circuit_breaker_returns_indicator` — same; assert return dict has `circuit_breaker_hit: True`.
9. `test_dry_run_no_delete` — eligible archives; `dry_run=True`; assert nothing deleted + count reported.
10. `test_retention_disabled` — `MEMORY_ARCHIVE_RETENTION_DAYS=0`; assert function early-returns with 0 candidates.

### Consolidation integration

11. `test_nightly_cycle_invokes_purge` — run `_run_retention_tasks()`; assert `purge_expired_archives()` called.
12. `test_metrics_emitted` — after purge, assert `yadgar_archive_purged_total` counter increments by expected count, `yadgar_archive_retention_skipped{reason="protected"}` etc.

### MCP tool

13. `test_archive_purge_dry_run_default` — invoke `archive_purge()`; assert no deletion, sample slugs returned.
14. `test_archive_purge_explicit_run` — `archive_purge(dry_run=False)`; assert deletion.
15. `test_archive_purge_retention_override` — `archive_purge(retention_days=30)`; assert 30d threshold used, not config default.
16. `test_archive_purge_power_gated` — without power, returns 403/refusal.
17. `test_archive_purge_secret_gated` — secret-gate runs on payload (I26).

### I25 config

18. `test_three_config_knobs_registered` — assert all 3 knobs in config.py + config_registry.py + config_yaml.py.

## 6. Acceptance criteria

1. 3 new knobs registered three-way (I25).
2. `purge_expired_archives()` storage function with all 5 DPs enforced (90d / hard / 500 cap / `_anchor`+protected skip / 7d thrash guard).
3. Nightly consolidation invokes purge.
4. `archive_purge` MCP tool (power-gated, secret-gated, dry_run default True).
5. 18 tests green; all existing tests still pass.
6. Telemetry: 2 new counters (`yadgar_archive_purged_total`, `yadgar_archive_retention_skipped` w/ reason labels).
7. CHANGELOG + MIGRATION_NOTES + README + config docs updated.
8. Operator dry-run on user prod: confirm ≈1300 candidate count matches earlier viz observation. Document delta in MIGRATION_NOTES.

## 7. Rollout

1. Ship v5.43.0 with retention OFF by default (`MEMORY_ARCHIVE_RETENTION_DAYS=0` ships off; doc says "set to 90 to enable").
2. User runs `archive_purge(dry_run=True)` to validate candidate set.
3. User runs `archive_purge(dry_run=False)` for one-time cleanup of the 1300 backlog.
4. User flips `MEMORY_ARCHIVE_RETENTION_DAYS=90` in config to enable nightly auto-purge going forward.

**Rationale for ship-off-by-default:** auto-purge on first nightly cycle could delete ~1300 rows in one shot, hit circuit breaker, fire CRITICAL log. Better to gate behind explicit user opt-in after dry-run validation.

## 8. Risks

- Aggressive default risks data loss. Mitigation: ships disabled; user opts in after dry-run.
- Re-archival thrash. Mitigation: DP-E 7-day thrash guard.
- v6 curator collision. Mitigation: v5.43 stays as backstop. v6 LLM proposes earlier deletes within its own 20/night cap. Document layering.
- SurrealDB DELETE creates vlog garbage. Existing vacuum schedule covers.
- Anchored memory with neither `_anchor` tag nor `is_protected=true` slips through. Mitigation: audit_anchors finds these; document migration in MIGRATION_NOTES.

## 9. Dependencies

- None hard. v5.41.x patches can ship in any order; v5.43 starts after they're done.
- Soft: v6 LLM curator will compose on top of v5.43 backstop when v6.x lands.

## 10. Phases (agent dispatch)

0. **`audit_anchors` extension — anchored-by-prose detection.** Extend `yadgar/server/tools/anchors.py::audit_anchors` to detect memories with: `_anchor` tag absent + `is_protected=false` + heat=0 + present in `memory_archive`. Returns a candidate-list dict. Add as recommended_action when count > 0. 4 tests (positive/negative/empty/threshold). → COMMIT `feat(anchors): detect anchored-by-prose-only memories at-risk from v5.43 retention`
1. **Storage function + tests RED first.** `purge_expired_archives()` w/ all 5 DPs + the legacy `tags CONTAINS 'anchor'` no-underscore exclusion. 10 storage tests. → COMMIT `feat(storage): purge_expired_archives helper w/ thrash guard + anchor skip`
2. **Config knobs (I25).** 3 knobs three-way. 1 test. → COMMIT `feat(config): I25 env knobs for MEMORY_ARCHIVE_RETENTION_*`
3. **Consolidation integration + telemetry.** Wire into `_run_retention_tasks()`. 2 tests + 2 Prometheus counters. → COMMIT `feat(consolidation): wire archive retention into nightly cycle + metrics`
4. **MCP tool.** `archive_purge` power-gated + secret-gated. 5 tests. → COMMIT `feat(mcp): archive_purge tool (dry_run default True)`
5. **Version bump + docs.** 5.41.4 → 5.43.0 (skip 5.42 per odd-only convention). CHANGELOG + MIGRATION_NOTES + README + config docs. Note: ships OFF (retention_days=0). → COMMIT `chore: bump version 5.41.4 → 5.43.0 + docs (retention ships disabled)`

## 11. References

- `yadgar/consolidation/heat_decay.py:14-16` — decay constants
- `yadgar/storage/ops.py:110-118` — `prune_old_rows()` allowlist (the gap)
- `yadgar/storage/memory.py:290-336` — `delete_memory()` cascade
- `yadgar/consolidation/cleanup.py:184-211` — `_run_retention_tasks()` (extension point)
- `yadgar/models.py:188` — `archived_at` timestamp
- `yadgar/server/tools/admin_invariants.py:168-188` — dangling-archive detection
- `yadgar/storage/migrations.py:434` — `memory_archive` schemaless table
- Memory 484431 — v6 LLM curator decisions (will layer on top)
- v5.21.0 PD-23 — `migration_grace` handler (exclusion logic)
- Investigation 2026-06-01 — caveman investigator report on the gap
