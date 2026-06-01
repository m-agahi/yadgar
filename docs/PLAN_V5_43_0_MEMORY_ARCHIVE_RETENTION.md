# PLAN — v5.43.0: memory_archive retention (SKELETON)

**Status:** SKELETON — 2026-06-01. Placeholder for discussion. NOT ready for impl.

**Origin:** 2026-06-01 user observation — viz reports total > visible by ~1300 memories. Investigation (caveman investigator, 2026-06-01) confirmed `memory_archive` table has **zero permanent-deletion policy**:

- `prune_old_rows()` allowlist EXCLUDES `memory_archive` (`yadgar/storage/ops.py:110-118`)
- Schemaless table, no TTL, no retention window config (`yadgar/storage/migrations.py:434`)
- Cascade delete only fires when parent memory explicitly DELETEd (`yadgar/storage/memory.py:290-336`)
- v6 LLM curator was planned to handle scope-limited deletion (heat<0.2 + age>30d + max 20/night soft-delete) — not shipped

Current state: ~1300 heat=0 memories accumulating with no auto-prune path. Will grow unbounded.

**Slot:** v5.43.0 — odd-minor, free slot between v5.41 (wiki versioning) and v5.45 (setup foundation). No conflict.

**Effort estimate:** 1.5-2 calendar days.

---

## 1. Problem

`memory_archive` rows accumulate forever once heat hits 0. No retention. SurrealKV grows. Vacuum reclaims dead-version bytes but not row count. Viz total-vs-visible gap reflects this.

## 2. Goal

Auto-purge `memory_archive` rows older than configurable threshold. Default conservative (preserve user data); easy to tune.

## 3. Scope

- New config: `MEMORY_ARCHIVE_RETENTION_DAYS: int = 90` (default 90d — 3 months grace).
- New consolidation phase or extend `_run_retention_tasks()` in `yadgar/consolidation/cleanup.py:184-211`.
- DELETE `memory_archive` WHERE `archived_at < (now - retention_days)` AND `is_protected=false`.
- Soft-delete window? See open-question §5.
- Telemetry: `yadgar_archive_purged_total` counter; CRITICAL log on >N purged in a single cycle (circuit breaker).
- New MCP tool? `archive_purge(dry_run=True)` for manual + audit. Power-gated.

## 4. Non-goals

- Not changing heat decay formula.
- Not changing `COLD_THRESHOLD` (the heat<0.02 → archive transition).
- Not building the v6 LLM curator (separate scope).
- Not retroactively deleting protected/anchored memories regardless of age.

## 5. Open design questions

1. **Default retention.** 30d / 60d / 90d / 180d / 365d? Lean: 90d. Conservative; user-tunable. Considerations: user may genuinely want long-tail recall for "things I discussed once 6 months ago".
2. **Soft-delete vs hard-delete.** Soft (mark `pending_delete_at` + grace window like the v6 plan, then hard-delete N days later) gives recovery option but doubles row count temporarily. Lean: hard-delete with vacuum snapshot as recovery (existing infrastructure).
3. **Circuit breaker.** Max purged per cycle. v6 plan said 20/night. For this purpose probably higher (e.g. 200/cycle) — purpose is bulk historical cleanup. Lean: 500/cycle default, CRITICAL log + tunable.
4. **Anchored memory protection.** `is_protected=true` excluded by default (matches v6 plan). What about memories with `_anchor` tag but `is_protected=false` (legacy state)? Probably exclude any memory carrying `_anchor` tag. Confirm during impl.
5. **Manual override.** New MCP tool `archive_purge(dry_run=True, retention_days=N)` for one-off cleanup. Power-gated. Recommend dry_run default True.
6. **`migration_grace=true` interplay.** v5.21.0 added grace handler for `valid_until` expiry. Memories with grace flag should be excluded until grace deadline. Re-check the PD-23 logic before impl.
7. **What counts as "age"?** `archived_at` is the right anchor (`yadgar/models.py:188`). `created_at` would erase too aggressively (a memory created 5y ago but accessed yesterday is still hot).
8. **Re-archival race.** If a memory archives → purged → another part of system recreates similar memory → re-archived → purged again. Could thrash. Mitigation: rate-limit + dedup-aware (skip purge if memory created within last 7d). Lean: simple cap, observe in prod.
9. **Interaction with v6 curator.** When v6 lands, does it own this scope, or does this stay as the "stupid age-based" backstop and v6 adds smart proposals? Lean: keep this as backstop; v6 LLM proposes earlier deletes within its 20/night cap.

## 6. Acceptance criteria

1. New config `MEMORY_ARCHIVE_RETENTION_DAYS` (default 90).
2. Retention phase (or extended `_run_retention_tasks()`) purges `memory_archive` rows older than threshold during nightly consolidation.
3. Protected + anchored memories never purged.
4. New `yadgar_archive_purged_total` counter + cycle CRITICAL log on >500 purged.
5. New MCP tool `archive_purge(dry_run=True, retention_days=None)` for manual + audit.
6. Tests: TDD failing first. Cover age threshold, protected exclusion, anchor exclusion, dry_run, circuit breaker, telemetry.
7. CHANGELOG + MIGRATION_NOTES + README + config docs updated.
8. Operator validation: dry-run prod 1300-row purge before opt-in; confirm count matches expectation.

## 7. Risks

- Aggressive default deletes user data they wanted. Mitigation: 90d is conservative; opt-out via `MEMORY_ARCHIVE_RETENTION_DAYS=0` (disable).
- Re-archival thrash. See §5 Q8.
- v6 curator collision. See §5 Q9.
- SurrealDB DELETE creates more vlog garbage. Vacuum schedule already covers this (anchor: vacuum every 1-2 weeks).

## 8. Dependencies

- None. Standalone retention work.
- Optional but nice: v5.41.0 wiki versioning (no overlap but ships first in pipeline).

## 9. Decision points to resolve before impl

- DP-A: default retention days (30/60/90/180/365)
- DP-B: soft-delete vs hard-delete
- DP-C: circuit-breaker cap per cycle
- DP-D: include `_anchor`-tagged but `is_protected=false` in exclusion?
- DP-E: re-archival thrash handling

---

## References

- `yadgar/consolidation/heat_decay.py:14-16` — decay constants
- `yadgar/storage/ops.py:110-118` — `prune_old_rows()` allowlist (the gap)
- `yadgar/storage/memory.py:290-336` — `delete_memory()` cascade
- `yadgar/consolidation/cleanup.py:184-211` — `_run_retention_tasks()` (extension point)
- `yadgar/models.py:188` — `archived_at` timestamp
- `yadgar/server/tools/admin_invariants.py:168-188` — dangling-archive detection
- `yadgar/storage/migrations.py:434` — `memory_archive` schemaless table
- Memory 484431 — v6 LLM curator decisions (scope overlap)

## Next steps when picking this up

1. Re-read this skeleton.
2. Confirm investigation findings still hold (`prune_old_rows` allowlist unchanged, `_run_retention_tasks` location unchanged).
3. Resolve DP-A through DP-E with user.
4. Convert skeleton → full plan with §Implementation, §Test plan, §Rollout.
5. Verify no v6 LLM curator landed in between (if it did, scope changes).
