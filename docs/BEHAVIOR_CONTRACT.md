# Yadgar Behavior Contract (full surface)

The system's designed behavior as **testable SHALL statements**, derived from the
architecture — NOT from the implementation. Each `BC-*` gets ≥1 e2e test
(`@pytest.mark.e2e`, real local SurrealDB) asserting the observable outcome.
If the impl doesn't deliver a SHALL, its test FAILS. **Tests own the contract;
weakening an assertion to pass is a review-rejected violation.**

**Status:** ✅ = e2e-GREEN (a real e2e test proves it) · ⏳ = e2e-pending (may have
unit/real coverage today — noted `[u]`=unit-only `[r]`=real-path existing — but no
contract e2e yet) · ❌ = KNOWN-BROKEN (ships `xfail(strict)`, links fix task).
> ✅ is reserved for e2e-green. "I believe it holds" = ⏳. This whole exercise
> exists to kill belief-without-a-test.

Run: `make e2e` (local, real `surreal`) + pre-push hook; **excluded from CI**
(`-m 'not e2e'`). Surface: **290 SHALLs / 28 subsystems.** Today: ~107 real-path,
~124 unit-only, ~59 none, 41 known-broken. Goal: every SHALL → ✅ or ❌.

Lint rule: a ✅ without an e2e test reference is a rejected claim.

---

## PHASE 1 — critical paths (v5.68; gates pre-push)

### A. Write / memorize
- BC-A1 memorize(content, directory=D) → retrievable, stamped D. ⏳[r] P1
- BC-A2 write-gate stores novel, dedups near-identical. ⏳[r] P1
- BC-A3 every write gets an embedding. ⏳[r] P1

### B. Recall + directory scoping (v5.62/64/65)
- BC-B1 recall(directory=A) includes A+global, excludes other dir B (memories). ⏳[r] P1
- BC-B2 same directory filter on wiki results in recall. ⏳[r] P1
- BC-B3 recall/wiki_query raise on absent/empty directory. ⏳[r] P1
- BC-B4 'system' not eligible. ⏳[r] P1
- BC-B5 profile-sourced results surface when a profile exists. ❌ #38 P1

### C. Consolidation / decay / archive / purge
- BC-C1 a cycle completes, check_invariants 0 violations (seeded real DB). ⏳[r] P1
- BC-C2 heat decay lowers heat; below cold_threshold → archived. ⏳[r] P1
- BC-C3 old+not-recently-accessed derived purged; recent spared; protected spared (v5.66). ⏳[u] P1
- BC-C4 nightly sleep phases run (dream/community/cluster/reembed_stale/compress/auto_narrate). ❌ #37 P1
- BC-C5 AstrocytePool domain consolidation executes (or removed). ❌ #40 P2

### D. Nightly cycle (host job; tests use TEMP data dir + stubbed service control)
- BC-D1 nightly completes exit 0 against seeded temp DB. ❌ #43 P1 (skew-blocked: surrealdb SDK 2.0.0 cannot embedded-open a surreal-3.0.5 surrealkv DB; e2e ships skipped, see SDK/server-alignment follow-up)
- BC-D2 pre-backup snapshot at real YADGAR_DATA_DIR/XDG, not stale config (v5.67). ⏳ P1
- BC-D3 interpreter shutdown clean (no SEGV / unhandled GC). ❌ #43 P1 (resolved via CPython 3.14.4 — the `_asyncio` finalize SEGV was a 3.14.3 bug fixed in 3.14.4; `.venv` now 3.14.4; no dedicated e2e asserts clean exit, so status stays ❌ until a test proves it)

### E. Vacuum (DATA-SAFETY — caused 2026-06-16 data loss)
- BC-E1 post-vacuum row counts == pre-vacuum, per table. ✅ `tests/e2e/test_vacuum_backup_safety.py::TestBCE1_RowCountsPreserved::test_memory_count_unchanged` P1
- BC-E2 atomic: any mid-vacuum failure leaves live DB intact+populated (never empty). ✅ `tests/e2e/test_vacuum_backup_safety.py::TestBCE2_VacuumAtomicity` (test_a_import_failure_leaves_canonical_untouched, test_b_verification_failure_blocks_swap, test_c_happy_path_swapped_dir_opens_complete, test_d_crash_mid_swap_recovery, test_e_recovery_runs_before_preflight_in_cmd_vacuum_impl) P1
- BC-E3 sensitive job in progress blocks external restart/shutdown. ✅ `tests/e2e/test_vacuum_backup_safety.py::TestBCE3_SensitiveJobLock::test_external_shutdown_refused_while_locked` P1

### F. Backup / restore
- BC-F1 a backup is a COMPLETE restorable copy (restore == source row counts). ✅ `tests/e2e/test_vacuum_backup_safety.py::TestBCF1_BackupRoundTrip::test_snapshot_restore_same_count` P1
- BC-F2 restore brings daemon to full state (core+backend reopen restored DB). ✅ `tests/e2e/test_vacuum_backup_safety.py::TestBCF2_RestoreToFullState::test_export_restore_brings_full_state` P1
- BC-F3 a backup taken under concurrent writes restores to a consistent committed-prefix state. ✅ `tests/e2e/test_vacuum_backup_safety.py::TestBCF3_QuiescedBackup::test_concurrent_write_backup_is_consistent` P1

### G. Wiki
- BC-G1 wiki_add(slug, content, directory=D) stamps D. ⏳[r] P1
- BC-G2 wiki_query(term, directory=A) excludes other-project pages. ⏳[r] P1
- BC-G3 wiki_read §25 resolution: dir+branch → dir+null → global → not-found. ⏳[r] P1
- BC-G4 every wiki_add/update creates an immutable wiki_page_version. ⏳[r] P1
- BC-G5 draft → approve workflow end-to-end. ⏳[r] P2
- BC-G6 similarity gate blocks near-duplicate page. ⏳[r] P2
- BC-G7 wiki bookmarks CRUD. ⏳[r] P2
- BC-G8 wiki_cleanup_merged_branches removes merged-branch pages. ⏳[u] P2
- BC-G9 wiki edit primitives (set_metadata/anchor-text/positional/structural) mutate as specified, versioned. ⏳[u] P2
- BC-G10 wiki_set_metadata reaches ALL rows of a slug across branches (the migration found dup-row stragglers it couldn't touch). ⏳ P2

### H. Hooks (directory stamping)
- BC-H1 tool-usage capture hook stamps caller cwd. ⏳[u] P2
- BC-H2 subagent-stop/session-end hook stamps caller cwd. ⏳[u] P2
- BC-H3 prompt-recall injected context is directory-scoped (no other-project leak). ⏳[r] P1

### S. Rules / secret gate
- BC-S1 secret patterns blocked at API + storage. ⏳[r] P1
- BC-S2 allowlist bypass permits legitimate content. ⏳[r] P1
- BC-S3 every allowlist bypass audited (I28). ⏳[r] P2

### ST. Storage
- BC-ST1 embedded-mode consolidation succeeds (no FULLTEXT/type::record error, v5.53/63). ⏳[r] P1
- BC-ST2 migrations run forward deterministically, no data loss. ⏳[r] P1
- BC-ST3 embedded batch_writes use inline record ids. ⏳[r] P1
- BC-ST4 server vs embedded mode selected correctly by env. ⏳[u] P2

### PC. Project context
- BC-PC1 project_brief(dir) returns anchors+hot memories+wiki for dir (all modes), no cross-project leak. ⏳[r] P1
- BC-PC2 seed_project(dir) creates initial anchors/bootstrap context. ⏳[u] P2
- BC-PC3 update_active_work(dir) stamps task context; project_brief signals reflect it. ⏳[r] P1
- BC-PC4 _build_wiki_pages scoped to dir (was leaking all wikis, v5.65). ⏳[r] P1

### G2. Checkpoint / restore
- BC-CK1 checkpoint(dir,...) then restore(dir) returns task/decisions/next-steps. ⏳[u] P1

### H2. reembed_all / admin (P1 subset)
- BC-ADM1 reembed_all re-embeds every missing-embedding row (v5.67). ⏳[u] P1

### DB-CONTRACT (directory/branch, v5.42–v5.65, PD-46..49)
- BC-DC1 eligible set = {caller_dir, global, '', None}; single is_directory_eligible predicate (I31). ⏳[r] P1
- BC-DC2 hard-require directory on reads; no os.getcwd() container fallback. ⏳[r] P1

---

## PHASE 2 — broader subsystems

### Sleep compute (gated behind #37 wiring)
- BC-SC1 dream replay surfaces latent memory pairs. ❌/⏳ #37 P2
- BC-SC2 community detection clusters the memory graph. ⏳[u] P2
- BC-SC3 cluster summarization writes semantic summaries. ⏳[u] P2
- BC-SC4 reembed_stale fixes stale embeddings after a model change. ❌ #37 P2
- BC-SC5 compress_old_memories gists aged memories. ⏳[u] P2
- BC-SC6 auto_narrate writes a project story. ❌ #41 (get_project_story dead) P2

### Astrocyte pool
- BC-AC1 assign_memory routes a memory to a domain. ⏳[u] P2
- BC-AC2 domain consolidation runs per domain. ❌ #40 P2
- BC-AC3 consensus_retrieve merges across domains (or removed). ❌ #41 P2

### Knowledge graph / entities
- BC-KG1 entities extracted from episodes. ⏳[r] P2
- BC-KG2 relationships (co-occurrence/causal/subtype) edged. ⏳[r] P2
- BC-KG3 neighborhood traversal returns adjacent entities. ⏳[u] P2
- BC-KG4 PPR walk from seeds ranks related memories. ⏳[r] P2
- BC-KG5 spreading activation propagates through edges. ⏳[r] P2

### CLS (episodic→semantic)
- BC-CLS1 episodic grouped from episodes. ⏳[u] P2
- BC-CLS2 repeated patterns abstracted to semantic. ⏳[u] P2
- BC-CLS3 promoted memory derives directory from sources (v5.64 PD-48). ⏳[u] P2

### Causal discovery
- BC-CA1 co-occurring entities create edges. ⏳[r] P2
- BC-CA2 PC algorithm (Meek R1/R2/R3) decomposes correctly. ⏳[r] P2
- BC-CA3 causal DAG inferred from dependency patterns. ⏳[u] P3

### Enrichment
- BC-EN1 ConceptNet expansion adds related terms (or config reflects off + warns). ❌ #39 P2
- BC-EN2 COMET commonsense expansion (or off+warn). ❌ #39 P2
- BC-EN3 doc2query synthetic queries (or off+warn). ❌ #39 P2

### Metacognition
- BC-MC1 coverage scored by entity/topic distribution. ⏳[r] P2
- BC-MC2 gap detection flags missing topics. ⏳[u] P3
- BC-MC3 belief search returns high-confidence inferred statements. ⏳[u] P2 (ties B5)
- BC-MC4 quality floor drops low-confidence/CE≈0 results. ⏳[u] P1 (recall)

### Predictive coding / surprise
- BC-PCd1 novel memory triggers surprise heat boost. ⏳[r] P2
- BC-PCd2 should_store gates redundant writes. ⏳[u] P2

### Engram / Hopfield
- BC-EG1 slot allocation sets excitability/plasticity/stability. ⏳[r] P2
- BC-EG2 patterns recalled via hopfield dynamics. ⏳[r] P2

### Heat / thermodynamics
- BC-HT1 decay = heat*factor^hours from max(last_accessed,last_decay_at). ⏳[r] P1
- BC-HT2 recall bumps access_count + last_accessed + heat. ⏳[r] P1
- BC-HT3 emotional salience slows decay. ⏳[u] P2

### Curation
- BC-CU1 co-occurrence memify strengthens memories, stamps originating dir (v5.64). ⏳[u] P2
- BC-CU2 prune recency gate (v5.66). ⏳[u] P1 (=C3)
- BC-CU3 near-duplicate merge keeps highest-heat. ⏳[u] P2

### Admin ops
- BC-ADM2 forget(id) permanently deletes. ⏳[r] P2
- BC-ADM3 validate_memory detects stale file-backed memories (fallback bug). ⏳[u] P2
- BC-ADM4 dlq_requeue re-sends failed writes. ⏳[r] P2
- BC-ADM5 archive_purge(older_than) purges archived. ⏳[u] P2
- BC-ADM6 memory_update patches allowed fields. ⏳[r] P2

### Tracing / metrics / invariants (I1–I31)
- BC-INV-* the I1–I31 invariants hold (most enforced by CI pre-commit checkers I13/I23/I24/I25/I28/I29/I30; add e2e for the runtime ones: I1 latency, I8 backpressure metrics, I2/I4 drainer/ML-async). ⏳ mixed P2

### MCP tool surface (52 tools)
- BC-MCP every registered MCP tool has ≥1 e2e exercising its real path + a contract. 18 real today, 22 unit-only, 12 none → drive all to ⏳→✅. P2

### Viz / graph API
- BC-VZ1 graph REST returns entity neighborhood + scores. ⏳[u] P2
- BC-VZ2 viz_search returns matching node ids (note: bypasses dir scoping — decide if intended). ⏳[u] P2

---

## PHASE 3 — closure (dead-or-decide + remaining gaps)

### Cognitive map / SR (mostly dead since v5.0 — wire or remove, #41)
- BC-CM1 SR transition matrix built. ⏳[u] P3
- BC-CM2 topological/spatial layout (extract_coordinates/update_memory_coordinates — DEAD #41). ❌ #41 P3
- BC-CM3 get_neighborhood/get_sr_scores/is_dirty (DEAD #41). ❌ #41 P3

### Remaining
- Drive every UNIT-ONLY + NONE SHALL to a real e2e or an explicit ⏳-with-reason.
- Dead config (15) + dead functions (10) removed (#41) — contract lints them out.

---

## Roadmap / acceptance
- **v5.68 (P1 build now):** harness + isolation fixture (temp data dir, stub service
  control) + P1 DB-layer tests green + #38, #37 fixed real red→green; the
  data-safety P1 contracts (D1/D3/E1-3/F1) ship `xfail(strict)` linking #43/#44/#45.
- **P2:** sleep/astrocyte/KG/CLS/causal/enrichment/hooks/admin/MCP-surface e2e +
  the #39/#40/#41 fixes flip their xfails.
- **P3:** cognitive-map decide-or-remove, full coverage closure, dead-code lint.
- **Suite acceptance:** re-run vs PRE-fix commits of this session's bugs (embedded
  consolidation, recall wiki leak, reembed_all, vacuum data-loss, partial backup)
  → each relevant contract goes RED. Proves the net catches rot.
- **Coverage lint (CI-able):** every BC-* references an e2e test OR is ⏳/❌ with a
  reason; ✅ without a test ref fails the lint.
