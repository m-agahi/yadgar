# PLAN — Database audit + fix (SKELETON)

Status: **DEFERRED INDEFINITELY — live store re-audited 2026-07-16, healthy (over-archive + dead-consolidation self-healed). Residuals: ~5 dangling edge rows (check_invariants: 1 dangling relationship + 4 memory_transition) + last_decay_at unverifiable read-only. Not worth work.**

theme: data-integrity
priority: high (correctness of the live store)
target: unassigned (assign at ship)

## Goal

Audit the live SurrealDB store for accumulated data-integrity problems and fix
them — both one-off remediation of existing rows and, where a defect produced
the bad data, the upstream code fix + a guard so it can't recur. Pair every
remediation with a behavioral regression test (per the v5.59 lesson:
thousands of unit tests still missed the 6-week-dead consolidation and the
decay-compounding bug — only seed→run→assert E2E tests caught those).

## ⚠️ DISCUSS WITH USER FIRST

The user has thoughts to bring to this. Open the conversation here before scoping
phases. Capture them in this section as they come.

- _(to fill in during discussion)_

## Candidate audit areas (seeded from issues already surfaced — NOT final scope)

1. **Legacy rows missing `last_decay_at` watermark (v5.59 aftermath).** The
   decay-idempotency fix added `last_decay_at`, but rows decayed by pre-5.59 code
   have none → the FIRST 5.59+ decay pass falls back to `last_accessed` and can
   double-decay once before the watermark is set. Audit: how many rows lack
   `last_decay_at`; decide whether to backfill (`last_decay_at = last_accessed`
   or `= now`) as a one-time migration vs let it self-heal.

2. **6-week-dead-consolidation aftermath.** Consolidation was dead ~6 weeks
   (FULLTEXT embedded crash, fixed v5.58). Heats were frozen, then a catch-up
   pass ran. Audit whether the catch-up + archive decisions are correct:
   `archived_count` is high (~2300/3574). Are the right memories archived, or did
   the catch-up over-archive? Is anything wrongly cold/lost?

3. **Entity heat / decay correctness.** Entities are reinforced on access
   (`reinforce_entity`) and now decay idempotently (v5.60 fix shares the
   watermark). Audit entity heat distribution; confirm entities aren't stuck at
   1.0 (the viz observation) due to reinforcement outpacing decay or missing
   watermark.

4. **Wiki ↔ memory disconnect.** v5.58 wiki audit found `source_count = 0` on
   ~100% of wiki pages — wiki and memory corpora effectively unlinked. Audit the
   `wiki_page.source_memory_ids` / `memory_wiki` linkage; decide backfill vs
   accept.

5. **Archive tier sanity.** 2300 archived memories + a `memory_archive` table.
   Audit: are archived rows in the right table/state? Any orphans, any that
   should be reactivated? (Relates to the wiki AWS-inventory-bloat cleanup —
   ~1547 inventory-tier pages flagged in v5.58.)

6. **action_log backlog.** `action_log` ~1629 rows. Audit whether the auto-capture
   action stream is being consolidated into real memories or just accumulating.

7. **Sparse / orphan ids.** Memory integer ids are sparse (3574 total but id 3500
   absent). Audit for orphaned edges (transition/similarity/causal pointing at
   archived/deleted rows), dangling `source_episode_id`, entity edges to gone
   nodes. (Viz orphan-edge filter exists; the DB itself may carry orphans.)

8. **Causal / graph sparsity.** Only ~16 causal_edges, ~1458 entities. Audit
   whether causal discovery + entity extraction are under-producing (real signal
   vs a silent pipeline gap).

## Method (proposed — refine after user input)

- **Read-only audit pass first**: a script (or `consolidate_now`-adjacent
  diagnostic) that reports counts/distributions per area above — never mutates.
  Output a findings report the user reviews before any fix.
- **Per confirmed issue**: (a) one-off remediation migration (idempotent, dry-run
  first), (b) upstream code fix if a defect caused it, (c) a seed→run→assert
  regression test.
- **No silent mutation of the live store.** Every fix is a reviewed migration the
  user runs (per the Apply/Import hard rule), dry-run output first.

## Open questions

- Backfill `last_decay_at` or let it self-heal? (area 1)
- Is the high archive count correct or over-archived? (area 2)
- Wiki↔memory backfill worth it, or accept the disconnect? (area 4)
- _(more after discussion)_

## Related

- `[[viz-data-fidelity]]` — viz must reflect DB reality (separate plan; some
  "DB issues" are actually viz-display issues, not store corruption — keep them
  distinct).
- Decay idempotency fix: v5.59 (`last_decay_at`), v5.60 (entity parity).
