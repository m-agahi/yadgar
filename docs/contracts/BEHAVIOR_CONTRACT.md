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
(`-m 'not e2e'`).

**Surface (recounted v6 T6, self-enforced by `scripts/check_contract_coverage.py`):**
**244 SHALLs / 39 subsystems.** Today: **56 ✅ · 183 ⏳ · 1 ❌.** (+ 2 🗑 RETIRED — BC-CM2/CM3, v5.71.0 #47; + 1 🗑 DELETED — BC-T2 remember tool, v6 T3; + 4 🗑 DELETED — BC-G5/T36/T37/T38 wiki-draft tools, v5.157.0 #76; + 2 🗑 DELETED — BC-T56/T58 container-blind repo-wiki/coverage tools, v5.160.0 #83) Of the 183 ⏳:
**59 `[r]` (real-path coverage exists) · 92 `[u]` (unit-only) · 32 none.**
Goal: every SHALL → ✅ or ❌.

**Lint rules** (`scripts/check_contract_coverage.py`, run as a non-e2e pytest):
1. A ✅ entry MUST cite a resolvable `path::node` test reference; a ✅ without one
   is a rejected claim.
2. A `[r]`/`[u]` tag on any entry, when it carries a `path::node` reference, MUST
   resolve to a real test (validate-if-present — the tag itself is not mandatory,
   but a dangling reference is a lint failure). This extends rule 1 beyond ✅.
3. The header counts (✅/⏳/❌ and `[r]`/`[u]`/none) MUST equal the actual tally
   over all `BC-*` rows. Header drift is a lint failure.

---

## PHASE 1 — critical paths (v5.68; gates pre-push)

### A. Write / memorize
- BC-A1 memorize(content, directory=D) → retrievable, stamped D. ✅ `tests/e2e/test_phase1_db_layer.py::TestBCA1_MemorizeRecallRoundTrip::test_memorize_recall_roundtrip` P1
- BC-A2 write-gate stores novel, dedups near-identical. ✅ `tests/e2e/test_phase2_subsystems.py::TestBCA2_WriteGateSurprise::test_gate_stores_novel_rejects_near_dup` P1
- BC-A3 every write gets an embedding. ✅ `tests/e2e/test_phase1_db_layer.py::TestBCA3_EmbeddingOnWrite::test_memorize_generates_embedding` P1

### B. Recall + directory scoping (v5.62/64/65)
- BC-B1 recall(directory=A) includes A+global, excludes other dir B (memories). ✅ `tests/e2e/test_phase1_db_layer.py::TestBCB1_DirectoryFilter::test_excludes_other_project` P1
- BC-B2 same directory filter on wiki results in recall. ✅ `tests/e2e/test_phase1_db_layer.py::TestBCB2_WikiDirectoryFilter::test_aws_wiki_excluded_from_yadgar_recall` P1
- BC-B3 recall/wiki_query raise on absent/empty directory. ✅ `tests/e2e/test_phase1_db_layer.py::TestBCB3_DirectoryRequired::test_recall_raises_without_directory` P1
- BC-B4 'system' not eligible. ✅ `tests/e2e/test_phase1_db_layer.py::TestBCB4_SystemTagExcluded::test_system_memory_not_returned` P1
- BC-B5 profile-sourced results surface when a profile exists. ✅ `tests/e2e/test_phase1_db_layer.py::TestBCB5_ProfileRecallSurfaces::test_profile_appears_in_recall` P1

### C. Consolidation / decay / archive / purge
- BC-C1 a cycle completes, check_invariants 0 violations (seeded real DB). ✅ `tests/e2e/test_phase1_db_layer.py::TestBCC1_ConsolidationRuns::test_consolidation_completes_no_violations` P1
- BC-C2 heat decay lowers heat; below cold_threshold → archived. ✅ `tests/e2e/test_phase1_db_layer.py::TestBCC2_HeatDecay::test_heat_decay_lowers_heat` P1
- BC-C3 old+not-recently-accessed derived purged; recent spared; protected spared (v5.66). ✅ `tests/e2e/test_phase1_db_layer.py::TestBCC3_PurgeAndSpare::test_old_unaccessed_purged` P1
- BC-C4 nightly sleep phases run (dream/community/cluster/reembed_stale/compress/auto_narrate). ✅ `tests/e2e/test_phase2_subsystems.py::TestBCC4_NightlySleepCycleRuns::test_nightly_runs_sleep_cycle_produces_dream_insight` P1
- BC-C5a AstrocytePool domain consolidation executes (assign→consolidate per domain produces a summary). ✅ `tests/e2e/test_phase2_subsystems.py::TestBCAC2_AstrocyteDomainConsolidation::test_consolidate_domain_produces_summary` P2
- BC-C5b if astrocyte pool is disabled, config reports it disabled + emits exactly one startup warning. ⏳ #40 P2
- BC-CSW1 one consolidation cycle issues exactly ONE storage.batch_writes call for all heat mutations (memories + entities combined); no phase other than HeatWriter.apply_heat_intents writes heat. ✅ `tests/e2e/test_heat_single_writer_e2e.py::TestBCCSW1_HeatSingleWriterE2E::test_single_batch_writes_for_heat_real_cycle` P1

### D. Nightly cycle (host job; tests use TEMP data dir + stubbed service control)
- BC-D1 nightly completes exit 0 against seeded temp DB. ✅ `tests/e2e/test_vacuum_backup_safety.py::TestBCD1_NightlyCompletesExitZero::test_real_nightly_main_exits_zero_no_contention` P1
- BC-D2 pre-backup snapshot at real YADGAR_DATA_DIR/XDG, not stale config (v5.67). ⏳ P1
- BC-D3 interpreter shutdown clean (no SEGV / unhandled GC). ✅ `tests/e2e/test_vacuum_backup_safety.py::TestBCD3_CleanShutdown::test_restore_exits_zero_no_segv` P1 (SEGV resolved via CPython 3.14.4; e2e asserts `yadgar restore` exits 0, no SIGSEGV/-11/139)

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
- BC-G6 similarity gate blocks near-duplicate page. ⏳[r] P2
- BC-G7 wiki bookmarks CRUD. ⏳[r] P2
- BC-G8 wiki_cleanup_merged_branches removes merged-branch pages. ⏳[u] P2
- BC-G9 wiki edit primitives (set_metadata/anchor-text/positional/structural) mutate as specified, versioned. ⏳[u] P2
- BC-G10 wiki_set_metadata reaches ALL rows of a slug across branches (the migration found dup-row stragglers it couldn't touch). ✅ `tests/e2e/test_wiki_set_metadata_allrows.py::TestWikiSetMetadataAllRows::test_set_metadata_updates_all_rows_for_slug` P2
- BC-G11 fan-out recall (UNIFIED_RECALL_ENABLED=True) scopes wiki results to caller directory (same eligible-set rule as legacy). ✅ `tests/e2e/test_scope_filter_e2e.py::TestScopeFilterE2E::test_db_clause_excludes_other_dir` P1

### U. Unified recall fan-out (v6 T6 — UNIFIED_RECALL_ENABLED flag-ON only)
- BC-U1 recall(type="all") returns BOTH mem:<id> and wiki:<slug> when both exist in scope. ✅ `tests/e2e/test_fusion_e2e.py::TestFusionE2E::test_fanout_returns_memory_and_wiki` P1
- BC-U2 recall(type="memory") returns memories ONLY — zero wiki results. ✅ `tests/e2e/test_type_param_e2e.py::TestTypeParamE2E::test_type_memory_returns_only_memories` P1
- BC-U3 recall(type="wiki") returns wiki pages ONLY — zero memory results. ✅ `tests/e2e/test_type_param_e2e.py::TestTypeParamE2E::test_type_wiki_returns_only_wiki` P1
- BC-U4 a high-CE wiki page is retrievable even when irrelevant high-heat memories flood the memory pool (quota gate). ✅ `tests/e2e/test_fusion_e2e.py::TestFusionE2E::test_quota_prevents_source_starvation` P1
- BC-U5 recall(type=<invalid>) raises ValueError immediately, before any retrieval work. ✅ `tests/e2e/test_type_param_e2e.py::TestTypeParamE2E::test_type_invalid_raises_before_retrieval` P1
- BC-U6 recall(type="memory") preserves the retriever's native order (single-provider bypass; no CE double-rerank). ✅ `tests/e2e/test_type_param_e2e.py::TestTypeParamE2E::test_type_memory_order_matches_legacy` P1
- BC-U7 recall(type="all") preserves memory relative order when a relevant wiki is present (fuse interleaves wiki by CE, never reorders memories). ✅ `tests/e2e/test_type_param_e2e.py::TestTypeParamE2E::test_type_all_memory_order_parity_with_relevant_wiki` P1
- BC-U8 recall(type="all") with an empty wiki pool preserves memory native order via the single-provider bypass (no fuse on a memory-only pool). ✅ `tests/e2e/test_type_param_e2e.py::TestTypeParamE2E::test_type_all_wiki_pool_empty_preserves_memory_order` P1

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
- BC-CK1 checkpoint(dir,...) then restore(dir) returns task/decisions/next-steps. ✅ `tests/e2e/test_phase1_db_layer.py::TestBCCK1_CheckpointRestore::test_checkpoint_restore_roundtrip` P1

### H2. reembed_all / admin (P1 subset)
- BC-ADM1 reembed_all re-embeds every missing-embedding row (v5.67). ✅ `tests/e2e/test_phase1_db_layer.py::TestBCADM1_ReembedAll::test_reembed_fills_missing_embeddings` P1

### DB-CONTRACT (directory/branch, v5.42–v5.65, PD-46..49)
- BC-DC1 eligible set = {caller_dir, global, '', None}; single is_directory_eligible predicate (I31). ⏳[r] P1
- BC-DC2 hard-require directory on reads; no os.getcwd() container fallback. ⏳[r] P1

### IC. In-context blocks (block_* tools — `yadgar/server/tools/blocks.py`)
- BC-IC1 block_create(dir,label,value) then block_get returns the value, stamped dir. ⏳[u] P2
- BC-IC2 block_list(dir) returns only blocks for dir (no cross-project leak). ⏳[u] P2
- BC-IC3 block_append/block_replace mutate the stored value as specified. ⏳[u] P2
- BC-IC4 block_update patches label/value; block_delete removes it (subsequent get → not-found). ⏳[u] P2

### AS. Action stream (tool-call capture → episodic action records)
- BC-AS1 a captured tool-usage action becomes a retrievable action-stream record stamped caller cwd. ⏳[u] P2
- BC-AS2 action records feed recall (an action surfaces in directory-scoped recall, not in another dir). ⏳ P2

### AN. Anchors as first-class (anchor / audit_anchors — `yadgar/server/tools/misc.py`, `audit.py`)
- BC-AN1 anchor(dir,title,...) creates a pinned `_anchor`-tagged memory that project_brief surfaces in top_anchors for dir. ⏳[u] P2
- BC-AN2 anchors are exempt from heat decay / archival (an aged anchor stays in top_anchors). ⏳ P2
- BC-AN3 audit_anchors(dir) reports anchor count + flags malformed/duplicate anchors. ⏳[u] P2

### HK. Hook install / sync (install_hooks / sync_instructions — `yadgar/server/tools/misc.py`)
- BC-HK1 install_hooks(dir) writes the Claude Code hook config to dir and is idempotent (re-run = no duplicate entries). ⏳[u] P2
- BC-HK2 sync_instructions(dir) writes the agent-instruction block to dir; a stale block is replaced, not duplicated. ⏳[u] P2

### RU. Rules engine (add_rule / get_rules — `yadgar/server/tools/admin_other.py`)
- BC-RU1 add_rule(dir,rule) stores a rule that get_rules(dir) returns, stamped dir. ⏳[u] P2
- BC-RU2 get_rules is directory-scoped (a rule added for dir A absent from get_rules(dir B)). ⏳ P2
- BC-RU3 stored rules are applied at retrieval time (BC-RR4 rules-rerank consumes them). ⏳ P2

### AP. Agent-prompt library (agent_prompt_save / agent_dispatch_prelude — `yadgar/server/tools/agent_prompts.py`, `dispatch_helper.py`)
- BC-AP1 agent_prompt_save(name,body) then an exact-key read (internal _read_agent_prompt slug-read; semantic lookup is recall(type="wiki", tags=["agent-prompt"])) returns the saved body. ⏳[u] P2
- BC-AP2 an exact-key read of an unknown name returns not-found (not a stale/other prompt). ⏳ P2
- BC-AP3 agent_dispatch_prelude(dir) returns the standard dispatch prelude with dir-scoped context injected. ⏳[u] P2

---

## PHASE 2 — broader subsystems

### RR. Retrieval scoring / reranking (`yadgar/retrieval/`)
- BC-RR1 cross-encoder rerank reorders candidates by query-document relevance (top-1 changes vs raw vector order on a designed query). ⏳[u] P1 (`_reranking_cross_encoder.cross_encoder_rerank`)
- BC-RR2 NLI entailment demotes a contradicting passage below an entailing one for the same query. ⏳[u] P1 (`_reranking_nli.nli_rerank`)
- BC-RR3 adversarial-candidate filter drops/penalises a planted adversarial (prompt-injection / contradictory) candidate. ⏳[u] P2 (`stages/adversarial.AdversarialStage`, `_reranking_confidence.detect_adversarial`)
- BC-RR4 stored rules rerank: a matching rule boosts/penalises a candidate's rank. ⏳[u] P2 (`reranking._rerank_rules`)
- BC-RR5 confidence gate / quality floor drops low-confidence (CE≈0) results below the floor. ✅ `tests/e2e/test_phase2_subsystems.py::TestBCRR5_ConfidenceGate::test_zero_confidence_for_empty_signal` (also test_positive_confidence_for_populated_signal, test_abstain_on_near_zero_score, test_no_abstain_on_high_score) P1
- BC-RR6 multi-passage aggregation: a multi-chunk memory is scored by aggregated passage scores, not a single chunk. ⏳[u] P2 (`_reranking_multi_passage.multi_passage_rerank`)
- BC-RR7 MMR diversification reduces near-duplicate results in the top-k. ✅ `tests/e2e/test_phase2_subsystems.py::TestBCRR7_MMRDiversification::test_mmr_selects_diverse_over_near_dup` P2
- BC-RR8 query routing: analyze_query classifies query type and selects the matching retrieval path (factoid vs comparison vs open-domain). ⏳[u] P2 (`query_analysis.analyze_query`, `_classify_query_type`)
- BC-RR9 query expansion: pseudo-HyDE / semantic-expansion adds boosted FTS subqueries that retrieve an item the raw query misses. ⏳[u] P2 (`query_analysis._pseudo_hyde_expand`, `_collect_semantic_expansions`, `_build_boosted_fts_query`)
- BC-RR10 fusion default = convex combination of signal scores. ✅ `tests/e2e/test_phase2_subsystems.py::TestBCRR10_ConvexFusion::test_convex_fuse_combines_signals` P1
- BC-RR11 WRRF fusion (weighted reciprocal-rank fusion) available + selected by config, produces a rank-based fused order. ⏳[u] P2 (`fusion._wrrf_fuse`)
- BC-RR12 temporal retrieval: a temporal expression ("yesterday", "last week") parses to a window that scores time-matching memories higher. ⏳[r] P2 (`temporal.parse_temporal_expression`, `scoring._collect_temporal_scores`)
- BC-RR13 comparison-merge rerank merges the option candidates for a comparison query into a single ranked answer set. ⏳[u] P2 (`reranking._rerank_comparison_merge`)

### Sleep compute (gated behind #37 wiring)
- BC-SC1a dream replay surfaces latent memory pairs (a co-activated pair becomes a derived link). ✅ `tests/e2e/test_phase2_subsystems.py::TestBCC4_NightlySleepCycleRuns::test_nightly_runs_sleep_cycle_produces_dream_insight` P2
- BC-SC1b if dream replay is disabled, config reports it disabled + emits exactly one startup warning. ⏳ #37 P2
- BC-SC2 community detection clusters the memory graph. ⏳[u] P2
- BC-SC3 cluster summarization writes semantic summaries. ⏳[u] P2
- BC-SC4 reembed_stale fixes stale embeddings after a model change. ✅ `tests/e2e/test_phase2_subsystems.py::TestBCSC4_ReembedStale::test_reembed_stale_updates_embedding_model` P2
- BC-SC5 compress_old_memories gists aged memories. ⏳[u] P2
- BC-SC6 auto_narrate writes a project story. ✅ `tests/e2e/test_phase2_subsystems.py::TestBCSC6_AutoNarrateWritesProjectStory::test_auto_narrate_inserts_narrative_for_active_directory` P2

### Astrocyte pool
- BC-AC1 assign_memory routes a memory to a domain. ✅ `tests/_shared/test_astrocyte_pool.py::TestMemoryAssignment` P2
- BC-AC2 domain consolidation runs per domain. ✅ `tests/e2e/test_phase2_subsystems.py::TestBCAC2_AstrocyteDomainConsolidation::test_consolidate_domain_produces_summary` P2
- BC-AC3a consensus_retrieve merges results across domains into one ranked set. ✅ `tests/e2e/test_landscape_recall_e2e.py::TestLandscapeRecallE2E` P2
- BC-AC3b if consensus_retrieve is disabled/removed, config reports it absent + emits exactly one startup warning. ⏳ #41 P2

### Knowledge graph / entities
- BC-KG1 entities extracted from episodes. ⏳[r] P2
- BC-KG2 relationships (co-occurrence/causal/subtype) edged. ⏳[r] P2
- BC-KG3 neighborhood traversal returns adjacent entities. ⏳[u] P2
- BC-KG4 PPR walk from seeds ranks related memories. ⏳[r] P2
- BC-KG5 spreading activation propagates through edges. ⏳[r] P2

### CLS (episodic→semantic)
- BC-CLS1 episodic grouped from episodes. ✅ `tests/e2e/test_phase2_subsystems.py::TestBCCLS1_2_3_EpisodicToSemantic::test_consolidation_cycle_promotes_semantic_and_stamps_directory` P2
- BC-CLS2 repeated patterns abstracted to semantic. ✅ `tests/e2e/test_phase2_subsystems.py::TestBCCLS1_2_3_EpisodicToSemantic::test_consolidation_cycle_promotes_semantic_and_stamps_directory` P2
- BC-CLS3 promoted memory derives directory from sources (v5.64 PD-48). ✅ `tests/e2e/test_phase2_subsystems.py::TestBCCLS1_2_3_EpisodicToSemantic::test_consolidation_cycle_promotes_semantic_and_stamps_directory` P2

### Causal discovery
- BC-CA1 co-occurring entities create edges. ⏳[r] P2
- BC-CA2 PC algorithm (Meek R1/R2/R3) decomposes correctly. ⏳[r] P2
- BC-CA3 causal DAG inferred from dependency patterns. ⏳[u] P3

### Enrichment
- BC-EN1a ConceptNet expansion adds related terms to a query/memory. ⏳ #64 P2 (HTTP path wired — ConceptNetExpander(http_enabled=True); e2e `tests/e2e/test_phase2_subsystems.py::TestBCEN1a_ConceptNetHTTP` is network-gated to api.conceptnet.io → skips offline, provable only on a networked runner; lite-DB ~9GB not bundled)
- BC-EN1b if ConceptNet expansion is disabled, config reports it disabled + emits exactly one startup warning. ⏳ #39 P2
- BC-EN2a COMET commonsense expansion adds inferred commonsense triples. ❌ WON'T-IMPLEMENT — COMET retired to dormant per ADR-0004 (en2a ablation `benchmarks/reports/en2a_comet_ablation_2026-06-24.md` decided un-FPA'd COMET does NOT help recall: multi-session R@5 −4.2pt, ~17h/10-core → net-negative). COMET DOES infer (verified yadgar-ci:5.72.0) but the FPA filter (FPA_SIMILARITY_THRESHOLD=0.25) drops its abstract traits → enrichment_comet empty. Code retained dormant (flag off by default); test stays xfail/skip to guard the dormant path (`tests/e2e/test_phase2_subsystems.py::TestBCEN2a_CometEnrichment`). #64 P2
- BC-EN2b if COMET expansion is disabled, config reports it disabled + emits exactly one startup warning. ✅ `tests/_shared/test_comet_dormant_warning.py::TestBCEN2bStartupWarning::test_disabled_emits_exactly_one_warning` P2 — `config_registry.warn_comet_dormant()` called once at startup (`server/lifecycle.py` `main()`) emits exactly one WARNING when disabled, none when enabled; `YADGAR_COMET_ENRICHMENT_ENABLED` registered in `_REGISTRY` so `/admin/config` + startup.config report it disabled. #39
- BC-EN3a doc2query generates synthetic queries for a stored memory. ✅ `tests/e2e/test_phase2_subsystems.py::TestBCEN3a_Doc2QueryEnrichment::test_stored_memory_has_synthetic_queries` P2 (proven in yadgar-ci:5.72.0; model-skip-guarded so host make-e2e skips)
- BC-EN3b if doc2query is disabled, config reports it disabled + emits exactly one startup warning. ⏳ #39 P2

### Metacognition
- BC-MC1 coverage scored by entity/topic distribution. ⏳[r] P2
- BC-MC2 gap detection flags missing topics. ⏳[u] P3
- BC-MC3 belief search returns high-confidence inferred statements. ⏳[u] P2 (ties B5)
- BC-MC4 quality floor drops low-confidence/CE≈0 results. ⏳[u] P1 (recall; ties RR5)

### Predictive coding / surprise
- BC-PCd1 novel memory triggers surprise heat boost. ⏳[r] P2
- BC-PCd2 should_store gates redundant writes. ✅ `tests/e2e/test_phase2_subsystems.py::TestBCA2_WriteGateSurprise::test_gate_stores_novel_rejects_near_dup` P2

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

### INV. Architecture invariants (I1–I31; from `docs/contracts/ARCHITECTURE_INVARIANTS.md`)
> One falsifiable SHALL per defined invariant. CI-enforced ones (a pre-commit
> checker script already proves them) are marked `[ci]` + the script. Runtime ones
> need an e2e. I16/I17/I18 are DEFERRED in the invariants doc (codify on violation)
> — no SHALL minted; listed for completeness only.
- BC-I1 request path stays THIN: no ML/heavy compute on the request thread; offloaded to drainer/to_thread. ⏳ P2 (runtime)
- BC-I2 the drainer is the SINGLE catch-up lane (no second background processor competes). ⏳[u] P2 (runtime)
- BC-I3 opt-in features short-circuit BEFORE expensive setup when disabled. ⏳[u] P2 (runtime)
- BC-I4 ML compute runs via asyncio.to_thread or drainer-thread ONLY (never inline on the event loop). ⏳ P2 (runtime)
- BC-I5 module decomposition never moves work across boundaries (refactor preserves where work runs). ⏳ P3 (review-time; no runtime probe)
- BC-I6 no double-pay: a given compute (embedding/rerank) is not performed twice per request. ⏳[u] P2 (runtime)
- BC-I7 the queue is the durability boundary (an accepted write survives a crash before processing). ⏳[r] P1 (runtime)
- BC-I8 backpressure is observable: queue depth / backpressure exposed as a metric. ⏳ P2 (runtime; metric assert)
- BC-I9 new write-path code budget ≤5ms p50. ⏳ P2 (runtime; latency assert)
- BC-I10 overrides are explicit (a config override is logged/visible, never silent). ⏳ P3
- BC-I11 heavy stable artifacts live in backend image, not core (size-ratchet CI-enforced). ⏳[ci] P2 (`scripts/check_image_size.py`)
- BC-I12 measure before optimize (perf claims backed by a recorded measurement). ⏳ P3 (process)
- BC-I13 bounded file + function complexity. ⏳[ci] P2 (`scripts/check_complexity.py`)
- BC-I14 structured logging contract (SCOPED) holds for in-scope log sites. ⏳ P3
- BC-I15 boundary-property fuzz tests (SCOPED) exist for in-scope boundaries. ⏳[u] P3
- BC-I19 file handler installed before tracing init. ⏳[ci] P2 (`scripts/check_trace_spans.py`)
- BC-I20 FastAPI/Starlette apps instrumented with FastAPIInstrumentor. ⏳ P3 (runtime)
- BC-I21 background threads create an OTel root span per work unit. ⏳ P3 (runtime)
- BC-I22 trust boundary: single-user single-host (no multi-tenant auth assumed). ⏳ P3 (design)
- BC-I23 every declared metric has ≥1 writer. ⏳[ci] P2 (`scripts/check_metric_writers.py`)
- BC-I24 every public HTTP-handler has @trace_span. ⏳[ci] P2 (`scripts/check_trace_spans.py`)
- BC-I25 every config knob defaults to a yaml-backed value. ⏳[u] P2
- BC-I26 secret-gate is a single chokepoint at the storage layer. ⏳[ci+r] P1 (`scripts/check_secret_gate.py`; ties S1)
- BC-I27 plan-first: every reproducible bug / >10 LOC fix lands in a plan doc same-session. ⏳ P3 (process)
- BC-I28 every pre-commit allowlist bypass is audited. ⏳[ci+r] P2 (`scripts/check_allowlist_audit.py`; ties S3)
- BC-I29 no dead capability: stored ≡ used ≡ shown (edge types). ⏳[ci] P2 (`scripts/check_dead_capability.py`)
- BC-I30 complexity-cap integrity: caps configurable, allowlist gated, no silent baselining. ⏳[ci] P2 (`scripts/check_complexity.py`)
- BC-I31 directory scoping: single is_directory_eligible predicate, hard-require, no 'system'. ⏳[r] P1 (ties DC1)
- BC-I32 capability-registry coverage: every Settings field, MCP tool, migration, and BC-* is catalogued in CAPABILITY_REGISTRY. ⏳[u] P2 (`scripts/check_capability_coverage.py`; test `yadgar/tests/test_capability_coverage.py`)

### MCP tool surface (72 registered tools — `yadgar/server/tools/*`)
> Replaces the old single BC-MCP umbrella. Each registered `@_tool` gets a row;
> status ⏳ until a contract e2e exercises its real path. Many overlap an existing
> BC-* above (cross-ref in the Note column) — those inherit that contract's e2e.

| BC id | Tool | Status | Note |
|-------|------|--------|------|
| BC-T1 | memorize | ⏳[r] | =A1 |
| BC-T2 | remember | 🗑 DELETED | stub deleted v6 T3 — use memorize |
| BC-T3 | recall | ⏳[r] | =B1..B4 |
| BC-T4 | project_brief | ⏳[r] | =PC1 |
| BC-T5 | seed_project | ⏳[u] | =PC2 |
| BC-T6 | bootstrap_project | ⏳[u] | initial project scaffold |
| BC-T7 | update_active_work | ⏳[r] | =PC3 |
| BC-T8 | checkpoint | ⏳[u] | =CK1 |
| BC-T9 | restore | ⏳[u] | =CK1 |
| BC-T10 | anchor | ⏳[u] | =AN1 |
| BC-T11 | audit_anchors | ⏳[u] | =AN3 |
| BC-T12 | install_hooks | ⏳[u] | =HK1 |
| BC-T13 | sync_instructions | ⏳[u] | =HK2 |
| BC-T14 | add_rule | ⏳[u] | =RU1 |
| BC-T15 | get_rules | ⏳[u] | =RU1/RU2 |
| BC-T16 | agent_prompt_save | ⏳[u] | =AP1 |
| BC-T17 | recall(type=wiki, tags=[agent-prompt]) / _read_agent_prompt | ⏳[u] | =AP1/AP2 |
| BC-T18 | agent_dispatch_prelude | ⏳[u] | =AP3 |
| BC-T19 | block_create | ⏳[u] | =IC1 |
| BC-T20 | block_get | ⏳[u] | =IC1 |
| BC-T21 | block_list | ⏳[u] | =IC2 |
| BC-T22 | block_append | ⏳[u] | =IC3 |
| BC-T23 | block_replace | ⏳[u] | =IC3 |
| BC-T24 | block_update | ⏳[u] | =IC4 |
| BC-T25 | block_delete | ⏳[u] | =IC4 |
| BC-T26 | bookmark_add | ⏳[r] | =G7 |
| BC-T27 | bookmark_list | ⏳[r] | =G7 |
| BC-T28 | bookmark_remove | ⏳[r] | =G7 |
| BC-T29 | bookmark_reorder | ⏳[r] | =G7 |
| BC-T30 | wiki_add | ⏳[r] | =G1 |
| BC-T31 | wiki_get | ⏳[r] | =G1 |
| BC-T32 | wiki_read | ⏳[r] | =G3 |
| BC-T33 | wiki_query | ⏳[r] | =G2 |
| BC-T34 | wiki_list | ⏳[u] | catalog listing |
| BC-T35 | wiki_update | ⏳[r] | =G4 |
| BC-T39 | wiki_check_duplicate | ⏳[r] | =G6 |
| BC-T40 | wiki_history | ⏳[u] | =G4 |
| BC-T41 | wiki_read_version | ⏳[u] | =G4 |
| BC-T42 | wiki_diff | ⏳[u] | version diff |
| BC-T43 | wiki_restore | ⏳[u] | restore old version |
| BC-T44 | wiki_set_metadata | ⏳[u] | =G9/G10 |
| BC-T45 | wiki_lint | ⏳[u] | lint a page |
| BC-T46 | wiki_append_section | ⏳[u] | =G9 |
| BC-T47 | wiki_insert_at | ⏳[u] | =G9 |
| BC-T48 | wiki_insert_after | ⏳[u] | =G9 |
| BC-T49 | wiki_insert_before | ⏳[u] | =G9 |
| BC-T50 | wiki_replace_at | ⏳[u] | =G9 |
| BC-T51 | wiki_replace_text | ⏳[u] | =G9 |
| BC-T52 | wiki_replace_markdown_block | ⏳[u] | =G9 |
| BC-T53 | wiki_delete | ⏳[u] | delete a page |
| BC-T54 | wiki_delete_at | ⏳[u] | =G9 |
| BC-T55 | wiki_delete_text | ⏳[u] | =G9 |
| BC-T56 | ~~wiki_coverage~~ | removed | removed #83 Car C (ADR-0157) |
| BC-T57 | wiki_cleanup_merged_branches | ⏳[u] | =G8 |
| BC-T58 | ~~wiki_refresh_stale~~ | removed | removed #83 Car C (ADR-0157) |
| BC-T59 | consolidate_now | ⏳[r] | =C1 |
| BC-T60 | reembed_all | ⏳[u] | =ADM1 |
| BC-T61 | vacuum_now | ⏳[r] | =E1..E3 |
| BC-T62 | vacuum_checkpoints | ⏳[u] | checkpoint vacuum |
| BC-T63 | archive_purge | ⏳[u] | =ADM5 |
| BC-T64 | forget | ⏳[r] | =ADM2 |
| BC-T65 | memory_get | ⏳[r] | fetch by id |
| BC-T66 | memory_update | ⏳[r] | =ADM6 |
| BC-T67 | memory_stats | ⏳[u] | counts/heat stats |
| BC-T68 | validate_memory | ⏳[u] | =ADM3 |
| BC-T69 | check_invariants | ⏳[r] | =C1 |
| BC-T70 | dlq_inspect | ⏳[u] | DLQ list |
| BC-T71 | dlq_requeue | ⏳[r] | =ADM4 |
| BC-T72 | dlq_dismiss | ⏳[u] | DLQ dismiss |

### Viz / graph API
- BC-VZ1 graph REST returns entity neighborhood + scores. ✅ `tests/e2e/test_viz_graph_fidelity_e2e.py::TestBCVZ1_GraphRESTEntityNeighborhoodScores::test_co_occurrence_edge_endpoints_match_seeded_entity_ids` P2
- BC-VZ2 viz_search returns matching node ids from ALL directories — whole-DB by design for the god's-eye overlay; dir-scoping intentionally bypassed (not a BC-B3 violation: scoping lives at the MCP-tool layer, not the in-process method; localhost auth-gated). ✅ `tests/e2e/test_viz_graph_fidelity_e2e.py::TestBCVZ2_VizSearchWholeDB::test_viz_search_returns_nodes_from_all_directories` P2
- BC-VZ-R1 every edge in the default /api/graph payload SHALL carry role ∈ {retrieval, informational}; transition + entity typed-relation edges SHALL have role=retrieval; temporal/causal/wiki_crossref/memory_wiki/semantic/memory_similarity_link SHALL have role=informational. ✅ `tests/e2e/test_viz_fidelity_v2_e2e.py::TestBCVZR1_EdgeRoleVocabulary::test_every_edge_has_valid_role_in_retrieval_informational` P2
- BC-VZ-R2 the default /api/graph payload SHALL NOT include any edge with type=semantic (lazy-path only; available on-demand via /api/graph/edges?type=semantic). ✅ `tests/e2e/test_viz_fidelity_v2_e2e.py::TestBCVZR2_NoSemanticInDefaultPayload::test_no_semantic_edges_in_default_payload` P2
- BC-VZ-R3 /api/graph payload SHALL include clusters[] sourced from real memory_cluster rows; each entry SHALL have id, source=memory_cluster, label, level, member_node_ids listing the mem:{id} nodes assigned to that cluster. ✅ `tests/e2e/test_viz_fidelity_v2_e2e.py::TestBCVZR3_ClusterPayload::test_seeded_cluster_appears_in_payload_with_correct_member_ids` P2
- BC-VZ-R4 memory_similarity_link rows from the CLS phase SHALL appear as edges in the default /api/graph payload with type=memory_similarity_link and role=informational. ✅ `tests/e2e/test_viz_fidelity_v2_e2e.py::TestBCVZR4_SimilarityLinkEdges::test_seeded_similarity_link_appears_as_edge` P2
- BC-VZ-F2 the yadgar daemon SHALL emit an SSE heat_updated event whenever a memory or entity heat score changes (real-time frontend patch without full reload). ⏳ viz-train Car C wired all three layers with unit coverage (`tests/backend/test_viz_f2_heat_sse.py`): (1) `_apply_decay` emits ONE `heat_updated` event with typed ids (`mem:N`+`entity:N`) built from the reconciled heat intents, skipping the push when nothing changed; (2) backend `_op_events` viz op returns ring-buffer entries with `seq>since`; (3) core `_poll_backend_events` re-stamps backend events onto core's queue each SSE loop tick (fixes the process-split bug where backend-pushed `memory_added`/`wiki_added`/`heat_updated` events landed in a buffer no core SSE client could read). Stays ⏳ because the real browser-SSE end-to-end path (HTTP `/api/graph/events` stream → live browser patch) is a user smoke-check, not driven in-harness (no-browser-harness convention; viz-fidelity-v2 pre-authorized ⏳-with-note when SSE e2e is infeasible).

---

## PHASE 3 — closure (dead-or-decide + remaining gaps)

### Cognitive map / SR (mostly dead since v5.0 — wire or remove, #41)
- BC-CM1 SR transition matrix built. ✅ `tests/e2e/test_phase3_closure.py::TestBCCM1_SRTransitionMatrixBuilt` P3
- BC-CM2 topological/spatial layout (extract_coordinates/update_memory_coordinates — DEAD #41). 🗑 RETIRED v5.71.0 (#47) — methods deleted; capability removed, not a failing spec. P3
- BC-CM3 get_neighborhood/get_sr_scores/is_dirty (DEAD #41). 🗑 RETIRED v5.71.0 (#47) — methods deleted; capability removed, not a failing spec. P3

### Remaining
- Drive every UNIT-ONLY + NONE SHALL to a real e2e or an explicit ⏳-with-reason.
- Dead config (15) + dead functions (10) removed (#41) — contract lints them out.

---

## Roadmap / acceptance
- **v5.68 (P1 build now):** harness + isolation fixture (temp data dir, stub service
  control) + P1 DB-layer tests green + #38, #37 fixed real red→green; the
  data-safety P1 contracts (D1/D3/E1-3/F1) ship `xfail(strict)` linking #43/#44/#45.
- **v5.69:** vacuum/backup data-safety contracts BC-E1/E2/E3/F1/F2/F3 flipped ✅
  (real e2e in `test_vacuum_backup_safety.py`). BC-D1 flipped ✅ in v5.70.1 (#51 — nightly
  moved to HTTP/server mode, eliminating the embedded surrealkv SDK/server skew).
- **v5.71:** contract hardened — header recounted + self-enforced, retrieval (RR),
  in-context/action-stream/anchors/hook/rules/agent-prompt subsystems added,
  BC-MCP exploded to a per-tool table (72), BC-INV exploded per-invariant,
  disjunctive-escape SHALLs split. Coverage-lint extended to validate `[r]`/`[u]`
  references + header counts.
- **P2:** sleep/astrocyte/KG/CLS/causal/enrichment/hooks/admin/RR/MCP-surface e2e +
  the #39/#40/#41 fixes flip their xfails.
- **P3:** cognitive-map decide-or-remove, full coverage closure, dead-code lint.
- **Suite acceptance:** re-run vs PRE-fix commits of this session's bugs (embedded
  consolidation, recall wiki leak, reembed_all, vacuum data-loss, partial backup)
  → each relevant contract goes RED. Proves the net catches rot.
- **Coverage lint (CI-able):** every BC-* references an e2e test OR is ⏳/❌ with a
  reason; ✅ without a test ref fails the lint; a dangling `[r]`/`[u]` reference
  fails the lint; header counts must equal the actual tally.
