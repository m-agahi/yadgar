> ARCHIVED 2026-07-09 — SHIPPED: #175 (db36e1e5) "fix: data-dir hygiene — backups/ layout, retention backstops, wiki cadence (ADR-0076)".

# Plan: Data-dir hygiene — backups layout, retention, wiki-snapshot cadence

**Status:** AGREED (user blessed all six decision points 2026-07-09). ADR-0076.
**Date:** 2026-07-09
**Trigger:** `~/.local/share/yadgar` at 13 GB: ~20 `surreal_db.old-*` rollback dirs (200–600 MB each), unbounded `vacuum_export_*.surql` scratch (~200 MB/night), 6-hourly 22 MB `wiki_*.jsonl` snapshots, nightly surql dumps at the volume root, June-incident debris.

## Root causes (scoped 2026-07-09)

1. **`.old` accumulation = the check_invariants timeout bug** (fixed in PR #173): `_vacuum_finalize()` (vacuum/__init__.py:730) reaps `.old` only on a check_invariants PASS; the 34 s compute vs 30 s client timeout meant it never passed → one orphaned `.old` per nightly since June. #173's 120 s per-op timeout should already stop the growth; this plan adds the backstop.
2. **`vacuum_export_*.surql` have NO retention anywhere** — mid-vacuum scratch (raw + filtered, ~100 MB each) written by `_vacuum_export()` (vacuum/phases.py:92–124), never consumed after finalize, never deleted.
3. **Wiki jsonl volume = cadence × retention**, not broken pruning: the backend-container loop (`entrypoint-backend.sh:133–158`) prunes at 14 d mtime correctly (bind mount — container deletes ARE host deletes); 6-hourly cadence × 22 MB × 14 d ≈ 1.9 GB. Separately, `wiki_snapshot.py:49 prune_old_snapshots()` is dead code — never called by anything.
4. **Nightly surql retention works** (`YADGAR_BACKUP_RETENTION`, default 3, `nightly_cycle.py:391 _step_prune`) — only the LOCATION (volume root) is wrong.

## Decisions (all accepted)

| # | Area | Decision |
|---|---|---|
| D1 | `.old` rollback dirs | Hybrid reap: on check_invariants pass (status quo) OR age > 7 days, whichever first. New knob `VACUUM_OLD_MAX_AGE_DAYS=7`. |
| D2 | `vacuum_export_*.surql` | Delete both (raw + filtered) on successful `_vacuum_finalize()`; retain ONLY when the vacuum run fails (diagnostic). No knob — behavior, not policy. |
| D3 | Wiki snapshots | Cadence 6 h → 24 h (entrypoint-backend.sh loop). Retention stays 14 d. Output moves to `backups/wiki/`. Dead `prune_old_snapshots()` in wiki_snapshot.py: DELETE (container loop owns pruning; one owner). |
| D4 | Layout | New `{DATA_DIR}/backups/` home: `backups/surql/` (nightly pre/post dumps), `backups/wiki/` (jsonl). `.old-*` and `.pre-vacuum-*` STAY siblings of `surreal_db` — atomic-rename artifacts must remain same-filesystem-adjacent to the DB dir. `queue/`, `dlq/`, `archive/` untouched (FileQueue-owned, ADR-0075). |
| D5 | Container/host split | Unchanged: backend container owns the wiki loop; host systemd nightly owns surql + vacuum. Only paths/cadence change. |
| D6 | One-time migration | Move existing surql dumps + jsonl into `backups/`; DELETE June debris (`.bloated-*` ×2, `.EMPTY-postvacuum-*`, `.bak`), excess `.old-*` beyond the newest 3, all orphaned `vacuum_export_*`. ≈10 GB freed. Delete commands go to MIGRATION_NOTES (user executes); moves may be scripted. |

## Build spec (single PR)

1. **`yadgar/core/export/backup.py`**: `create_snapshot()` default `snapshot_dir` → `{DATA_DIR}/backups/surql/` (mkdir parents); `prune_snapshots()` glob paths follow. Keep filename scheme.
2. **`yadgar/core/vacuum/phases.py` + `__init__.py`**: D2 delete-on-success for both export files (in `_vacuum_finalize`, after CI pass, alongside `.old` reap); D1 age-backstop reap of `surreal_db.old-*` older than `VACUUM_OLD_MAX_AGE_DAYS` (runs every vacuum finalize regardless of CI outcome; never touches the newest `.old` from the CURRENT run).
3. **`entrypoint-backend.sh`**: wiki loop sleep 6 h → 24 h; output + find-prune path → `/data/backups/wiki/` (mkdir -p).
4. **`yadgar/core/scripts/wiki_snapshot.py`**: delete dead `prune_old_snapshots()`; module docstring updated (container loop owns pruning).
5. **Knobs (I25 three-way sync + I32 registry)**: add `VACUUM_OLD_MAX_AGE_DAYS=7`; registry/docs rows for it; existing `YADGAR_BACKUP_RETENTION` docs updated with new path.
6. **Migration**: MIGRATION_NOTES section — mkdir `backups/{surql,wiki}`, `mv` existing artifacts, delete list (June debris, excess `.old`, orphaned exports) with sizes; note first post-deploy nightly validates the new paths.
7. **Tests (TDD)**: backup path defaults + prune globs (test_backup.py seam); finalize deletes exports on success and keeps them on failure; `.old` age-backstop (older-than-N reaped, current-run survivor kept); entrypoint change is shell — assert via the e2e/vacuum integration seam if covered, else document as deploy-verified.
8. **Versions**: core bump (backup/vacuum are core); backend bump only if entrypoint-backend.sh counts as backend build input (check_backend_bump hook will arbitrate).

## Verify after deploy
- Nightly run writes to `backups/surql/`, prunes to 3.
- Vacuum finalize: exports gone, `.old` reaped (CI pass) — and tonight's run confirms the #173 timeout fix ended the accumulation.
- Wiki: one new jsonl per day under `backups/wiki/`, count trends to ≤14.
- Disk: ~13 GB → ~2.5 GB after migration.

## References
ADR-0075 (queue base /data contract), ADR-0076 (this plan's decisions), PR #173 (check_invariants timeout — the `.old` root cause), scoping report 2026-07-09 (producer table, session agent a5db9092).
