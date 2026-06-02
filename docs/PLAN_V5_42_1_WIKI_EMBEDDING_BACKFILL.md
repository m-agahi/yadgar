# PLAN — v5.42.1: wiki_page embedding backfill + silent-failure surfacing

**Status:** drafted 2026-06-02 evening. Critical hotfix — v5.39 + v5.42 similarity gate effectively non-functional in live env.

**Origin:** v5.42.0 E2E smoke test 2026-06-02 evening — created base page → wrote near-clone with `wait=False` → expected DLQ rejection entry. Got clean insert. Investigation (cavecrew-investigator) confirmed:

- `wiki_page` table shipped v5.1 with `embedding` column → defaults NULL
- `_compute_embedding()` only added in v5.39 commit `a1d05c0` (2026-06-01, 2 days ago)
- **No migration to backfill embeddings on pre-v5.39 rows** — all ~1.9k existing wiki pages have `embedding=NULL`
- SurrealDB KNN operator `<|fetch_k,40|>` silently excludes NULL rows
- `find_similar_wiki_pages` returns 0 candidates against any pre-v5.39 page
- v5.39 similarity gate + v5.41.5 drainer move + v5.42 DLQ tracking ALL inherit this — gate ships but never fires in production

**Secondary symptom:** even a freshly-created post-v5.39 page didn't trigger the gate. Means `encode_document` may also be silently failing at write time (wiki.py:880-882 catches all exceptions). Both must be fixed.

**Effort estimate:** 0.5-1 calendar day.

**Branch:** `fix/v5.42.1-wiki-embedding-backfill` off master.

---

## 1. Problem

**Two failure modes compounded:**

### A. Pre-v5.39 NULL embeddings (~1.9k rows)

No backfill migration. KNN excludes NULL → find_similar returns 0 candidates. Gate has nothing to compare against. Always passes.

### B. New writes may silently skip embedding too

`encode_document()` call in wiki.py:880-882 wraps in try/except, returns None on any failure → embedding stays NULL → same KNN exclusion → same gate-always-passes.

Smoke test created a brand-new page; check_duplicate against identical content still returned 0 candidates. Suggests new writes ALSO failing embed silently.

## 2. Goal

1. Backfill embeddings for all NULL wiki_page rows (migration 014).
2. Surface embed-compute failures loudly (log + metric + optionally block write).
3. Verify gate fires on real near-duplicate after backfill.

## 3. Scope

### 3.1 Migration 014: `_migration_014_wiki_page_embedding_backfill`

`yadgar/storage/migrations.py`:
- Find all wiki_page rows where `embedding IS NULL`
- Batch encode (batch size 50; encode_document supports batching or call per-row)
- Update row with computed embedding
- Idempotent: re-run finds 0 rows
- Transactional per batch (BEGIN/COMMIT)
- Logged: `migration_014: backfilling {count} embeddings...`

Risks:
- Embed service must be reachable during migration. If not → migration logs warning + skips (don't block startup). Re-run when service available.
- Batch failure rolls back batch, continues with next batch — incremental progress preserved.

### 3.2 Embed-failure surfacing

`yadgar/wiki.py:875-885` (`_compute_embedding`):
- Replace bare `except Exception:` with explicit logging at WARN
- New Prometheus counter: `yadgar_wiki_embedding_compute_failed_total{reason}`
- Add toggle env knob `WIKI_EMBED_FAILURE_BLOCKS_WRITE: bool = False` (I25 three-way registered):
  - Default `False`: log + counter, write proceeds with NULL embedding (legacy behavior — backward compat)
  - `True`: write fails with explicit error "embed unavailable; retry or use wait=True; check yadgar-backend"
- Operator can flip to `True` to enforce embedding-on-write once confident

### 3.3 Operational verification

- Re-run v5.42 E2E smoke after migration: create base page + near-clone with wait=False → expect DLQ rejection entry visible via `dlq_inspect(filter="rejections")`
- Document the test in `docs/V5_42_1_GATE_VERIFICATION.md`

## 4. Non-goals

- No change to KNN operator semantics (NULL exclusion is correct SurrealDB behavior).
- No change to v5.42 DLQ tracking design (the bug is upstream of the gate).
- No retroactive DLQ entries for rejections that should have fired pre-v5.42.1.
- No migration of memory table embeddings (separate concern; memory rows shouldn't have similar gap).

## 5. Acceptance criteria

1. Migration 014 ships idempotent + transactional + safe-to-re-run.
2. Migration 014 logs progress; reports final count.
3. All wiki_page rows have non-NULL embedding after first successful run.
4. `_compute_embedding` failure emits WARN log + Prometheus counter `yadgar_wiki_embedding_compute_failed_total{reason}`.
5. New `WIKI_EMBED_FAILURE_BLOCKS_WRITE` knob registered three-way (I25). Default False.
6. Smoke test passes: near-clone via `wait=False` → DLQ entry appears within 30s → `pending_rejections_count` signal fires → `dlq_inspect(filter="rejections")` shows the entry with candidates.
7. v5.39 calibration tests still pass.
8. v5.42 DLQ rejection tests still pass.
9. Version bumped 5.42.0 → 5.42.1.
10. CHANGELOG entry documents the gap + fix. MIGRATION_NOTES: explain the migration cost + duration estimate.

## 6. Risks

- **Embed service unreachable at migration time:** migration logs warning + skips. Re-run when service available. Document in MIGRATION_NOTES.
- **Backfill cost on large wiki tables (~1.9k rows × embed latency):** ~50-150ms per row × 1.9k = 1.5-5 min total. Acceptable for one-time migration. Logged progress.
- **Breaking change risk with `WIKI_EMBED_FAILURE_BLOCKS_WRITE=True`:** would fail wiki_add when embed service down. Default False preserves backward compat. Flip to True only after operator confidence.
- **Silent fallback to NULL still possible** under DEFAULT setting — that's intentional for compat. Real fix is operator awareness via counter/log, then opt-in.
- **Tests that mock embed return None** may need adjustment if WARN log changes test fixtures. Audit.

## 7. Dependencies

- v5.39 wiki similarity gate (✓ shipped)
- v5.41.5 drainer placement (✓ shipped)
- v5.42.0 DLQ tracking (✓ shipped)
- Live embed service reachable from migration runner (verify pre-flight)

## 8. Phases (4 commits)

1. **Investigation reproduction + test setup.** Add failing test that asserts find_similar returns candidates against an identical-content page after backfill. Currently fails (returns 0). Will pass post-fix. → COMMIT `test(wiki): RED test reproducing find_similar returns 0 on NULL embeddings`

2. **Migration 014 + idempotency.** New migration in `yadgar/storage/migrations.py`. Tests: fresh DB (0 rows → 0 backfill), populated DB (N rows → N backfilled), re-run (idempotent). → COMMIT `feat(storage): migration 014 — backfill wiki_page embeddings on NULL rows`

3. **Embed-failure surfacing + I25 knob.** Replace silent catch with WARN log + Prometheus counter + new `WIKI_EMBED_FAILURE_BLOCKS_WRITE` knob. Tests cover all 3 paths (success, failure-log-warn, failure-block-write). → COMMIT `feat(wiki): surface embed failures via WARN + counter + opt-in block knob`

4. **Version bump + smoke verification + docs.** Run E2E smoke (create base + near-clone + verify DLQ entry). Document in `docs/V5_42_1_GATE_VERIFICATION.md`. Version 5.42.0 → 5.42.1. CHANGELOG + MIGRATION_NOTES. → COMMIT `chore: bump version 5.42.0 → 5.42.1 + gate verification report`

## 9. References

- Investigator report (cavecrew-investigator, 2026-06-02 evening)
- `yadgar/wiki.py:428` — `find_similar_wiki_pages`
- `yadgar/wiki.py:875` — `_compute_embedding`
- `yadgar/storage/wiki.py:456` — `search_wiki_vectors` (NULL-excluding KNN)
- `yadgar/storage/migrations.py:671` — `wiki_embedding_idx` (HNSW since v5.1)
- `yadgar/storage/wiki.py:113-114` — embedding input path (allows None to pass through)
- v5.39 commit `a1d05c0` — added `_compute_embedding`
- v5.41.5 commit `c26df69` — moved gate to drainer (inherits same NULL problem)
- v5.42.0 commit `dd01b6d` — DLQ rejection tracking (also inherits same NULL problem)

## 10. Open questions

1. Should migration 014 run automatically at startup OR be opt-in CLI (`yadgar migrate --backfill-embeddings`)? Lean: auto-run via standard migration framework (matches 010-013 pattern). Embed-unavailable case logs + skips, doesn't block startup.
2. Should existing pages with NULL embedding emit a single startup-time CRITICAL log "{N} wiki_page rows lack embeddings — similarity gate ineffective" until migration 014 lands? Lean: yes, add as part of Phase 3.
3. Should the smoke test be added as an integration test that runs against a real spawned daemon (vs the unit test that mocks)? Lean: yes — `pytest.mark.integration` so it doesn't block CI but ops can run before nix-apply.

## 11. Coordination

Single agent dispatch. Sonnet. NO worktree isolation. Main thread parks on master.

After ship:
- Roadmap pipeline update: v5.42.1 row marked SHIPPED
- v5.42.0 retrospectively confirmed functional (gate fires) — close the loop
- Slot v5.42.2 if `WIKI_EMBED_FAILURE_BLOCKS_WRITE=True` becomes default after operator confidence (rollout follow-up)

## 12. Rollout note

After v5.42.1 ships and nix-applies:
1. Migration 014 runs automatically at startup, backfills ~1.9k rows (~1.5-5 min).
2. User runs E2E smoke from `docs/V5_42_1_GATE_VERIFICATION.md` to confirm gate fires.
3. If confirmed: similarity gate is finally functional. Old undetected duplicates may now surface on next write attempt to similar slugs.
4. Operator considers flipping `WIKI_EMBED_FAILURE_BLOCKS_WRITE=True` once monitoring shows no silent failures.
