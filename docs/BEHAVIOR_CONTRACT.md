# Yadgar Behavior Contract

The system's designed behavior as **testable SHALL statements**, derived from the
architecture — NOT from the implementation. Each `BC-N` has ≥1 e2e test
(`@pytest.mark.e2e`, real local SurrealDB) that asserts the observable outcome.
If the impl doesn't deliver a SHALL, its test FAILS. **Tests own the contract;
weakening an assertion to go green is a review-rejected violation.**

Status legend: ✅ holds (test green) · ❌ KNOWN-BROKEN (test `xfail(strict)`, links
the fix task) · ⏳ test not yet written.

Run: `make e2e` (local, real `surreal`); pre-push hook; **excluded from CI**
(`-m 'not e2e'`) — CI's embedded SurrealDB can't run these reliably.

> ✅ is reserved for e2e-GREEN only. "I believe it holds" = ⏳ (this whole
> exercise exists to kill belief-without-a-test).

---

## A. Write / memorize
- **BC-A1** A `memorize(content, context=<dir>)` SHALL persist a memory retrievable
  by `recall` from that directory, stamped `directory_context=<dir>`.
- **BC-A2** The surprise/write gate SHALL store a novel memory and SHALL dedup a
  near-identical one (no duplicate row).
- **BC-A3** Every write SHALL receive an embedding (vector search can surface it).

## B. Recall scoping (v5.62/64/65)
- **BC-B1** `recall(directory=A)` SHALL return memories stamped A or {global,''},
  and SHALL NOT return memories stamped another project dir B.
- **BC-B2** `recall(directory=A)` SHALL apply the SAME directory filter to wiki
  results (no cross-project wiki leak).
- **BC-B3** `recall`/`wiki_query` SHALL raise when `directory` is absent/empty
  (no silent unscoped mode).
- **BC-B4** `directory_context='system'` SHALL NOT be eligible (drop-system).
- **BC-B5** Profile-sourced results SHALL surface in recall when a matching
  profile exists. ❌ **#38** (PROFILE_SEARCH_WEIGHT undefined → swallowed).

## C. Consolidation / decay / archive / purge
- **BC-C1** A consolidation cycle SHALL run to completion with 0 invariant
  violations against a seeded real DB.
- **BC-C2** Heat decay SHALL lower heat over elapsed time; a memory below
  `cold_threshold` SHALL become archived.
- **BC-C3** An old AND not-recently-accessed derived/auto-abstracted memory SHALL
  be purged; a recently-accessed one SHALL be spared; protected/_anchor SHALL
  always be spared (v5.66).
- **BC-C4** The nightly's sleep phases (dream/community/cluster/reembed_stale/
  compress/auto_narrate) SHALL run on the nightly schedule. ❌ **#37**
  (`_maybe_sleep_cycle` never called).
- **BC-C5** AstrocytePool domain consolidation SHALL execute (or be removed).
  ❌ **#40** (`_run_domain_consolidation` never called).

## D. Nightly cycle (host job)
- **BC-D1** The nightly cycle SHALL complete with exit code 0 against a seeded DB.
  ❌ **#43** (systemctl/D-Bus stop/start fails; SEGV at exit).
- **BC-D2** The pre-backup snapshot SHALL be created at the real data dir
  (`YADGAR_DATA_DIR`/XDG), not a stale config path (v5.67). ✅
- **BC-D3** Interpreter shutdown SHALL NOT crash (no SEGV / unhandled GC error).
  ❌ **#43**.

## E. Vacuum (DATA-SAFETY — caused the 2026-06-16 data loss)
- **BC-E1** Vacuum SHALL preserve every row: post-vacuum row counts == pre-vacuum
  (per table). ❌ **#44**.
- **BC-E2** Vacuum SHALL be atomic: on ANY failure mid-vacuum, the live DB SHALL
  remain intact + populated (never left empty). ❌ **#44** (renames live DB away
  before rebuild verified → empty on failure).
- **BC-E3** A sensitive job (vacuum/nightly) in progress SHALL block external
  restart/shutdown from corrupting it (defense-in-depth). ❌ **#44**.

## F. Backup / restore
- **BC-F1** A backup snapshot SHALL be a COMPLETE, restorable copy (restoring it
  yields the same row counts as the source). NOTE: a live `cp` mid-write is NOT
  complete (the 2026-06-16 nightly-pre snapshot restored to 1484/3622) — backups
  SHALL be taken from a quiesced/atomic source. ❌ **#45** (pre-backup is a live cp → partial).
- **BC-F2** Restore SHALL bring the daemon back to the snapshot's full state
  (both core + backend reopen the restored DB).

## G. Checkpoint / restore (hippocampal replay)
- **BC-G1** `checkpoint(...)` then `restore(directory)` SHALL return the captured
  task/decisions/next-steps.

## H. reembed_all
- **BC-H1** `reembed_all` SHALL re-embed every memory missing an embedding;
  `reembedded` == count of missing-embedding rows (skips None-content). ⏳ (unit-green; e2e pending).

## I. Hooks directory stamping
- **BC-I1** The tool-usage capture hook SHALL stamp the caller's cwd as
  `directory_context` (not a sentinel). ⏳ (audit-confirmed; e2e pending)
- **BC-I2** The subagent-stop / session-end hook SHALL stamp the caller's cwd. ⏳ (audit-confirmed; e2e pending)
- **BC-I3** The prompt-recall hook's injected context SHALL be directory-scoped
  (no other-project leak). ✅ (v5.65 supplement fix; daemon path post-filter).

---

## Phase plan
- **Phase 1 (v5.68 — this train):** A1–A3, B1–B5, C1–C3, D1–D3, E1–E3, F1–F2,
  G1, H1. Known-broken (B5, C4, C5, D1, D3, E1–E3) ship as `xfail(strict, reason)`
  → each links its fix task; flipping xfail to pass = the fix's acceptance.
- **Phase 2:** C4/C5 deep paths, write-pipeline edge cases, wiki versioning/branch,
  CLS promotion, dream/astrocyte internals.
- **Acceptance for the suite:** re-running Phase-1 against the PRE-fix commits of
  this session's bugs (embedded consolidation, recall wiki leak, reembed_all,
  vacuum data-loss) → each relevant contract goes RED. Proves the net catches rot.
