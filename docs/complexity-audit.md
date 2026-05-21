# Complexity Audit — yadgar v5.4.2 P12

**Invariants:** I13 (bounded complexity caps) + I5 (no topology-breaking decomp).
**Source of truth:** `docs/ARCHITECTURE_INVARIANTS.md`.
**Scope:** static analysis only. No runtime data. Catalog only — no decompositions performed.

## Summary

- **Total functions audited:** 3426 (999 non-test, 2427 test)
- **Hard violations:** 90 functions (80 non-test, 10 test)
- **Soft violations (any cap, no hard):** 100 functions
- **Any cap exceeded (all):** 190 / 3426 = **5.5%**
- **Any cap exceeded (non-test only):** 167 / 999 = **16.7%** — under the 20% critical threshold
- **Any cap exceeded (test only):** 23 / 2427 = **0.9%** (cyclomatic + nesting only; LOC + params exempt)
- **Files exceeding LOC hard cap (>1000):** 1
- **Files exceeding LOC soft cap (>500):** 13

### Top 10 Hard Violations (by cyclomatic)

| file:line | function | cyclo | LOC | params | nesting |
|---|---|---|---|---|---|
| server/tools/admin_invariants.py:18 | `_run_check_invariants` | 98 | 569 | 1 | 7 |
| server/tools/memorize.py:36 | `memorize` | 84 | 484 | 5 | 6 |
| cli/stats.py:15 | `cmd_stats` | 68 | 506 | 1 | 4 |
| graph_api.py:19 | `get_full_graph` | 56 | 194 | 4 | 3 |
| curation/prune_passes.py:13 | `_memify_prune` | 56 | 160 | 3 | 3 |
| causal_discovery/pc.py:105 | `pc_algorithm` | 53 | 175 | 4 | 6 |
| install_hooks_lib.py:48 | `install_hooks_impl` | 47 | 308 | 4 | 4 |
| retrieval/query_analysis.py:285 | `_derive_implied_fact_passages` | 46 | 107 | 1 | 4 |
| server/tools/recall.py:20 | `recall` | 44 | 203 | 3 | 4 |
| restoration.py:319 | `_format_restoration` | 37 | 113 | 8 | 4 |

### Files Exceeding Hard LOC Cap (>1000 lines)

- `server/http.py` — 1040 lines

### v5.5 Bundle Plan

- LOW-risk decompositions identified: 25
- Recommended bundle: 5 functions per PR = **5 PRs**
- Each PR must include before/after test parity (per I13 enforcement spec).
- HIGH-risk and topology-crossing functions: defer until P11 metrics available.

---

## Function Violations Table

Sorted: HARD violations first, then by cyclomatic descending.
Test files exempt from LOC + params caps; cyclomatic + nesting still enforced.

| file:line | function | cyclomatic | LOC | params | nesting | hard/soft | risk | proposed action |
|---|---|---|---|---|---|---|---|---|
| server/tools/admin_invariants.py:18 | `_run_check_invariants` | 98 | 569 | 1 | 7 | **HARD** `cyclo=98>15 LOC=569>150 nesting=7>4` | HIGH | justify-cohesion (noqa) |
| server/tools/memorize.py:36 | `memorize` | 84 | 484 | 5 | 6 | **HARD** `cyclo=84>15 LOC=484>150 nesting=6>4` | HIGH | decompose-with-topology-proof |
| cli/stats.py:15 | `cmd_stats` | 68 | 506 | 1 | 4 | **HARD** `cyclo=68>15 LOC=506>150` | MEDIUM | decompose-with-topology-proof |
| graph_api.py:19 | `get_full_graph` | 56 | 194 | 4 | 3 | **HARD** `cyclo=56>15 LOC=194>150` | MEDIUM | decompose-with-topology-proof |
| curation/prune_passes.py:13 | `_memify_prune` | 56 | 160 | 3 | 3 | **HARD** `cyclo=56>15 LOC=160>150` | MEDIUM | decompose-with-topology-proof |
| causal_discovery/pc.py:105 | `pc_algorithm` | 53 | 175 | 4 | 6 | **HARD** `cyclo=53>15 LOC=175>150 nesting=6>4` | MEDIUM | decompose-with-topology-proof |
| install_hooks_lib.py:48 | `install_hooks_impl` | 47 | 308 | 4 | 4 | **HARD** `cyclo=47>15 LOC=308>150` | MEDIUM | decompose-with-topology-proof |
| retrieval/query_analysis.py:285 | `_derive_implied_fact_passages` | 46 | 107 | 1 | 4 | **HARD** `cyclo=46>15 LOC=107>80` | MEDIUM | decompose-with-topology-proof |
| server/tools/recall.py:20 | `recall` | 44 | 203 | 3 | 4 | **HARD** `cyclo=44>15 LOC=203>150` | HIGH | decompose-with-topology-proof |
| restoration.py:319 | `_format_restoration` | 37 | 113 | 8 | 4 | **HARD** `cyclo=37>15 LOC=113>80 params=8>5` | MEDIUM | justify-cohesion (noqa) |
| metacognition/gap_detection.py:9 | `detect_gaps` | 35 | 160 | 2 | 3 | **HARD** `cyclo=35>15 LOC=160>150` | MEDIUM | decompose-with-topology-proof |
| cli/daemon.py:8 | `cmd_daemon` | 35 | 138 | 1 | 12 | **HARD** `cyclo=35>15 LOC=138>80 nesting=12>4` | MEDIUM | decompose-with-topology-proof |
| retrieval/reranking.py:56 | `_apply_rerank_pipeline` | 35 | 137 | 11 | 4 | **HARD** `cyclo=35>15 LOC=137>80 params=11>8` | HIGH | decompose-with-topology-proof |
| retrieval/fusion.py:104 | `_fuse_scores` | 35 | 109 | 4 | 6 | **HARD** `cyclo=35>15 LOC=109>80 nesting=6>4` | MEDIUM | decompose-with-topology-proof |
| seed/_generate.py:33 | `generate_memories` | 33 | 154 | 1 | 4 | **HARD** `cyclo=33>15 LOC=154>150` | MEDIUM | decompose-with-topology-proof |
| server/tools/admin_other.py:129 | `memory_stats` | 32 | 155 | 0 | 5 | **HARD** `cyclo=32>15 LOC=155>150 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| server/tools/project.py:208 | `_render_project_brief` | 32 | 108 | 1 | 3 | **HARD** `cyclo=32>15 LOC=108>80` | MEDIUM | decompose-with-topology-proof |
| metacognition/coverage.py:16 | `assess_coverage` | 31 | 123 | 3 | 6 | **HARD** `cyclo=31>15 LOC=123>80 nesting=6>4` | MEDIUM | decompose-with-topology-proof |
| consolidation/cls.py:58 | `_process_new_episodes` | 30 | 108 | 2 | 7 | **HARD** `cyclo=30>15 LOC=108>80 nesting=7>4` | HIGH | decompose-with-topology-proof |
| retrieval/query_analysis.py:394 | `analyze_query` | 29 | 103 | 2 | 7 | **HARD** `cyclo=29>15 LOC=103>80 nesting=7>4` | MEDIUM | decompose-with-topology-proof |
| ml_client.py:171 | `score_cross_encoder` | 28 | 104 | 3 | 3 | **HARD** `cyclo=28>15 LOC=104>80` | HIGH | decompose-with-topology-proof |
| retrieval/scoring.py:23 | `_collect_fts_scores` | 28 | 102 | 9 | 5 | **HARD** `cyclo=28>15 LOC=102>80 params=9>8 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| tests/test_integration.py:986 | `[test] test_clean_startup_and_shutdown` | 28 | 41 | 2 | 0 | **HARD** `cyclo=28>15` | MEDIUM | decompose-with-topology-proof |
| retrieval/_reranking_heuristic.py:9 | `heuristic_rerank` | 27 | 150 | 4 | 3 | **HARD** `cyclo=27>15 LOC=150>80` | MEDIUM | decompose-with-topology-proof |
| cls_store/clustering.py:15 | `find_recurring_patterns` | 26 | 118 | 3 | 4 | **HARD** `cyclo=26>15 LOC=118>80` | MEDIUM | decompose-with-topology-proof |
| rules_engine.py:322 | `evaluate_condition` | 25 | 82 | 3 | 5 | **HARD** `cyclo=25>15 LOC=82>80 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| server/tools/project.py:319 | `project_brief` | 24 | 217 | 3 | 2 | **HARD** `cyclo=24>15 LOC=217>150` | MEDIUM | decompose-with-topology-proof |
| seed/_scan.py:252 | `scan_project` | 24 | 123 | 1 | 6 | **HARD** `cyclo=24>15 LOC=123>80 nesting=6>4` | MEDIUM | decompose-with-topology-proof |
| storage/memory.py:87 | `insert_memory` | 24 | 116 | 5 | 6 | **HARD** `cyclo=24>15 LOC=116>80 nesting=6>4` | MEDIUM | decompose-with-topology-proof |
| graph_api.py:393 | `sample_system_metrics` | 24 | 110 | 3 | 5 | **HARD** `cyclo=24>15 LOC=110>80 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| restoration.py:219 | `restore` | 24 | 99 | 2 | 6 | **HARD** `cyclo=24>15 LOC=99>80 nesting=6>4` | MEDIUM | decompose-with-topology-proof |
| server/tools/project.py:619 | `_wiki_refresh_stale_impl` | 23 | 100 | 3 | 3 | **HARD** `cyclo=23>15 LOC=100>80` | MEDIUM | decompose-with-topology-proof |
| consolidation/cls.py:345 | `_merge_duplicates` | 23 | 85 | 2 | 3 | **HARD** `cyclo=23>15 LOC=85>80` | HIGH | decompose-with-topology-proof |
| consolidation/cls.py:217 | `_link_similar_memories` | 22 | 127 | 2 | 4 | **HARD** `cyclo=22>15 LOC=127>80` | HIGH | decompose-with-topology-proof |
| retrieval/_reranking_cross_encoder.py:20 | `cross_encoder_rerank` | 22 | 87 | 4 | 3 | **HARD** `cyclo=22>15 LOC=87>80` | HIGH | decompose-with-topology-proof |
| storage/client.py:364 | `_q` | 21 | 65 | 3 | 5 | **HARD** `cyclo=21>15 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| file_queue/__init__.py:120 | `_drain_once` | 20 | 117 | 1 | 5 | **HARD** `cyclo=20>15 LOC=117>80 nesting=5>4` | HIGH | decompose-with-topology-proof |
| server/tools/wiki.py:19 | `wiki_add` | 20 | 106 | 9 | 3 | **HARD** `cyclo=20>15 LOC=106>80 params=9>8` | MEDIUM | decompose-with-topology-proof |
| retrieval/_reranking_mmr.py:9 | `mmr_rerank` | 20 | 76 | 5 | 5 | **HARD** `cyclo=20>15 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| sleep_compute/dream.py:11 | `dream_replay` | 20 | 74 | 1 | 3 | **HARD** `cyclo=20>15` | HIGH | decompose-with-topology-proof |
| tests/integration/test_vacuum_e2e.py:232 | `[test] test_vacuum_e2e_happy_path` | 19 | 174 | 1 | 2 | **HARD** `cyclo=19>15` | MEDIUM | decompose-with-topology-proof |
| server/tools/project.py:760 | `wiki_cleanup_merged_branches` | 19 | 105 | 2 | 5 | **HARD** `cyclo=19>15 LOC=105>80 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| causal_discovery/pc.py:18 | `build_event_matrix` | 19 | 85 | 4 | 4 | **HARD** `cyclo=19>15 LOC=85>80` | MEDIUM | decompose-with-topology-proof |
| metacognition/cognitive_load.py:85 | `chunk_memories` | 19 | 73 | 2 | 4 | **HARD** `cyclo=19>15` | MEDIUM | decompose-with-topology-proof |
| consolidation/heat_decay.py:12 | `_apply_decay` | 19 | 66 | 2 | 3 | **HARD** `cyclo=19>15` | HIGH | decompose-with-topology-proof |
| wiki.py:127 | `query` | 19 | 66 | 5 | 4 | **HARD** `cyclo=19>15` | MEDIUM | decompose-with-topology-proof |
| sleep_compute/community.py:121 | `generate_cluster_summaries` | 18 | 89 | 1 | 3 | **HARD** `cyclo=18>15 LOC=89>80` | HIGH | decompose-with-topology-proof |
| predictive_coding.py:462 | `get_directory_model` | 18 | 85 | 2 | 4 | **HARD** `cyclo=18>15 LOC=85>80` | MEDIUM | decompose-with-topology-proof |
| server/tools/misc.py:34 | `checkpoint` | 18 | 83 | 8 | 3 | **HARD** `cyclo=18>15 LOC=83>80 params=8>5` | MEDIUM | decompose-with-topology-proof |
| storage/dbsize.py:20 | `get_db_size` | 17 | 128 | 1 | 5 | **HARD** `cyclo=17>15 LOC=128>80 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| cls_store/patterns.py:252 | `abstract_to_schema` | 17 | 109 | 2 | 3 | **HARD** `cyclo=17>15 LOC=109>80` | MEDIUM | decompose-with-topology-proof |
| consolidation/cleanup.py:12 | `_process_action_log` | 17 | 90 | 1 | 3 | **HARD** `cyclo=17>15 LOC=90>80` | HIGH | decompose-with-topology-proof |
| server/http.py:977 | `api_viz_search` | 17 | 57 | 1 | 6 | **HARD** `cyclo=17>15 nesting=6>4` | MEDIUM | decompose-with-topology-proof |
| server/lifecycle.py:167 | `init_engines` | 16 | 130 | 4 | 4 | **HARD** `cyclo=16>15 LOC=130>80` | MEDIUM | decompose-with-topology-proof |
| retrieval/core.py:211 | `spreading_activation` | 16 | 66 | 4 | 6 | **HARD** `cyclo=16>15 nesting=6>4` | HIGH | decompose-with-topology-proof |
| predictive_coding.py:248 | `_compute_temporal_novelty` | 16 | 55 | 3 | 5 | **HARD** `cyclo=16>15 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| retrieval/fusion.py:214 | `_build_initial_results` | 16 | 54 | 8 | 5 | **HARD** `cyclo=16>15 params=8>5 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| hooks/subagent_stop.py:84 | `_get_report_text` | 16 | 51 | 1 | 5 | **HARD** `cyclo=16>15 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| hooks/subagent-stop.py:68 | `_get_report_text` | 16 | 37 | 1 | 5 | **HARD** `cyclo=16>15 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| tests/test_frontier_integration.py:391 | `[test] test_all_globals_initialized` | 16 | 17 | 1 | 0 | **HARD** `cyclo=16>15` | MEDIUM | decompose-with-topology-proof |
| tests/test_vacuum.py:1180 | `[test] test_check_invariants_passes_bearer` | 14 | 79 | 2 | 6 | **HARD** `cyclo=14>10 nesting=6>4` | MEDIUM | justify-cohesion (noqa) |
| tests/test_vacuum.py:1260 | `[test] test_check_invariants_env_missing_warns` | 14 | 78 | 3 | 6 | **HARD** `cyclo=14>10 nesting=6>4` | MEDIUM | justify-cohesion (noqa) |
| storage/client.py:431 | `_build_chunk_body` | 14 | 62 | 2 | 5 | **HARD** `cyclo=14>10 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| retrieval/_reranking_confidence.py:70 | `compute_signal_confidence` | 14 | 42 | 3 | 5 | **HARD** `cyclo=14>10 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| embeddings.py:93 | `_ensure_model` | 13 | 69 | 1 | 6 | **HARD** `cyclo=13>10 nesting=6>4` | HIGH | decompose-with-topology-proof |
| graph_api.py:292 | `_compute_semantic_edges` | 13 | 63 | 4 | 5 | **HARD** `cyclo=13>10 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| consolidation/orchestrator.py:72 | `_consolidation_cycle` | 12 | 152 | 1 | 3 | **HARD** `cyclo=12>10 LOC=152>150` | HIGH | decompose-with-topology-proof |
| tests/test_vacuum.py:554 | `[test] test_wiki_crossref_preserved_in_filtered_surql` | 12 | 62 | 5 | 5 | **HARD** `cyclo=12>10 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| retrieval/scoring.py:217 | `_collect_temporal_scores` | 12 | 48 | 6 | 5 | **HARD** `cyclo=12>10 params=6>5 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| staleness.py:129 | `scan_directory` | 12 | 43 | 2 | 5 | **HARD** `cyclo=12>10 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| file_queue/queue.py:81 | `cleanup_archive` | 12 | 32 | 1 | 5 | **HARD** `cyclo=12>10 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| tests/test_vacuum.py:745 | `[test] test_import_success_leaves_no_surreal_db_at_bloated_path` | 11 | 51 | 5 | 5 | **HARD** `cyclo=11>10 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| tests/test_vacuum.py:453 | `[test] test_characterization_produces_filtered_surql_and_log_row` | 11 | 47 | 5 | 5 | **HARD** `cyclo=11>10 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| tests/test_retrieval_branch_polish.py:274 | `[test] test_c4_no_boost_when_branch_is_none` | 11 | 45 | 1 | 5 | **HARD** `cyclo=11>10 nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| consolidation/orchestrator.py:13 | `_daemon_loop` | 11 | 44 | 1 | 6 | **HARD** `cyclo=11>10 nesting=6>4` | HIGH | decompose-with-topology-proof |
| rules_engine.py:281 | `apply_rules` | 10 | 40 | 3 | 7 | **HARD** `nesting=7>4` | HIGH | decompose-with-topology-proof |
| embed_service.py:225 | `admin_dbsize` | 9 | 45 | 1 | 5 | **HARD** `nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| enrichment/conceptnet.py:174 | `_try_http` | 9 | 34 | 4 | 6 | **HARD** `nesting=6>4` | MEDIUM | decompose-with-topology-proof |
| seed/_analysis.py:219 | `_summarize_config` | 9 | 27 | 1 | 6 | **HARD** `nesting=6>4` | MEDIUM | decompose-with-topology-proof |
| daemon.py:59 | `_host_memory_bytes` | 9 | 26 | 0 | 5 | **HARD** `nesting=5>4` | MEDIUM | decompose-with-topology-proof |
| tests/test_vacuum.py:501 | `[test] test_cmd_vacuum_delegates_to_impl` | 8 | 43 | 5 | 7 | **HARD** `nesting=7>4` | LOW | decompose-low-risk |
| storage/migrations.py:246 | `_init_schema` | 7 | 170 | 1 | 1 | **HARD** `LOC=170>150` | MEDIUM | decompose-with-topology-proof |
| curation/__init__.py:58 | `curate_on_remember` | 7 | 65 | 12 | 3 | **HARD** `params=12>8` | MEDIUM | decompose-with-topology-proof |
| cli/config.py:4 | `cmd_config` | 7 | 23 | 2 | 6 | **HARD** `nesting=6>4` | LOW | decompose-low-risk |
| storage/entity.py:210 | `insert_typed_relationship` | 6 | 49 | 10 | 1 | **HARD** `params=10>8` | LOW | decompose-low-risk |
| restoration.py:64 | `create_checkpoint` | 6 | 34 | 10 | 0 | **HARD** `params=10>8` | LOW | decompose-low-risk |
| storage/narrative.py:153 | `insert_belief` | 3 | 35 | 9 | 0 | **HARD** `params=9>8` | LOW | decompose-low-risk |
| curation/ingestion.py:106 | `insert_new_memory` | 2 | 39 | 12 | 1 | **HARD** `params=12>8` | LOW | decompose-low-risk |
| curation/__init__.py:154 | `_insert_new_memory` | 1 | 29 | 12 | 0 | **HARD** `params=12>8` | LOW | decompose-low-risk |
| file_queue/__init__.py:73 | `__init__` | 1 | 26 | 9 | 0 | **HARD** `params=9>8` | LOW | decompose-low-risk |
| cls_store/promotion.py:13 | `_promote_pattern` | 15 | 101 | 2 | 4 | **soft** `cyclo=15>10 LOC=101>80` | MEDIUM | decompose-low-risk |
| server/http.py:145 | `hook_auto_capture` | 15 | 79 | 1 | 2 | **soft** `cyclo=15>10` | MEDIUM | decompose-low-risk |
| server/http.py:327 | `hook_subagent_stop` | 15 | 78 | 1 | 3 | **soft** `cyclo=15>10` | MEDIUM | decompose-low-risk |
| server/http.py:470 | `_handle_team_inbox` | 15 | 77 | 3 | 3 | **soft** `cyclo=15>10` | HIGH | defer |
| sleep_compute/community.py:24 | `detect_communities` | 15 | 77 | 1 | 3 | **soft** `cyclo=15>10` | HIGH | defer |
| retrieval/temporal.py:44 | `parse_temporal_expression` | 15 | 72 | 1 | 2 | **soft** `cyclo=15>10` | MEDIUM | decompose-low-risk |
| astrocyte_pool.py:257 | `consensus_retrieve` | 15 | 66 | 3 | 3 | **soft** `cyclo=15>10` | MEDIUM | decompose-low-risk |
| retrieval/query_analysis.py:166 | `_extract_comparison_options` | 15 | 59 | 1 | 3 | **soft** `cyclo=15>10` | MEDIUM | decompose-low-risk |
| tests/conftest.py:301 | `[test] memorize_sync` | 15 | 53 | 4 | 3 | **soft** `cyclo=15>10` | HIGH | defer |
| hooks/stop-memory-checkpoint.py:53 | `_count_human_messages` | 15 | 44 | 1 | 3 | **soft** `cyclo=15>10` | MEDIUM | decompose-low-risk |
| curation/strengthen.py:90 | `_memify_derive` | 14 | 124 | 3 | 2 | **soft** `cyclo=14>10 LOC=124>80` | MEDIUM | decompose-low-risk |
| wiki.py:255 | `lint` | 14 | 94 | 1 | 4 | **soft** `cyclo=14>10 LOC=94>80` | MEDIUM | decompose-low-risk |
| __main__.py:41 | `cli` | 14 | 93 | 0 | 3 | **soft** `cyclo=14>10 LOC=93>80` | MEDIUM | decompose-low-risk |
| astrocyte_pool.py:166 | `consolidate_domain` | 14 | 88 | 2 | 4 | **soft** `cyclo=14>10 LOC=88>80` | MEDIUM | decompose-low-risk |
| tests/test_curation.py:150 | `[test] test_curate_link_moderate` | 14 | 67 | 3 | 3 | **soft** `cyclo=14>10` | MEDIUM | decompose-low-risk |
| metacognition/cognitive_load.py:159 | `summarize_overflow` | 14 | 64 | 3 | 4 | **soft** `cyclo=14>10` | MEDIUM | decompose-low-risk |
| causal_discovery/independence.py:12 | `conditional_independence_test` | 14 | 62 | 4 | 3 | **soft** `cyclo=14>10` | MEDIUM | decompose-low-risk |
| server/http.py:265 | `hook_prompt_recall` | 14 | 59 | 1 | 3 | **soft** `cyclo=14>10` | HIGH | defer |
| knowledge_graph.py:171 | `extract_entities_typed` | 14 | 56 | 3 | 3 | **soft** `cyclo=14>10` | MEDIUM | decompose-low-risk |
| retrieval/scoring.py:126 | `_collect_vector_scores` | 14 | 53 | 8 | 3 | **soft** `cyclo=14>10 params=8>5` | MEDIUM | decompose-low-risk |
| enrichment/comet.py:48 | `infer` | 14 | 51 | 3 | 4 | **soft** `cyclo=14>10` | MEDIUM | decompose-low-risk |
| retrieval/query_analysis.py:77 | `_pseudo_hyde_expand` | 14 | 50 | 1 | 3 | **soft** `cyclo=14>10` | MEDIUM | decompose-low-risk |
| consolidation/cls.py:168 | `_extract_entities` | 14 | 48 | 1 | 2 | **soft** `cyclo=14>10` | HIGH | defer |
| storage/client.py:316 | `_q_timeout` | 14 | 47 | 4 | 3 | **soft** `cyclo=14>10` | MEDIUM | decompose-low-risk |
| seed/_analysis.py:112 | `_summarize_pyproject` | 14 | 44 | 1 | 2 | **soft** `cyclo=14>10` | MEDIUM | decompose-low-risk |
| predictive_coding.py:304 | `_compute_structural_novelty` | 14 | 43 | 3 | 3 | **soft** `cyclo=14>10` | MEDIUM | decompose-low-risk |
| daemon.py:170 | `start` | 13 | 110 | 2 | 2 | **soft** `cyclo=13>10 LOC=110>80` | MEDIUM | decompose-low-risk |
| tests/test_check_invariants_repair.py:361 | `[test] test_caused_by_row_count_ceiling_prunes_oldest` | 13 | 73 | 1 | 1 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| metacognition/cognitive_load.py:16 | `manage_context` | 13 | 68 | 3 | 2 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| retrieval/core.py:119 | `ppr_retrieve` | 13 | 65 | 3 | 2 | **soft** `cyclo=13>10` | HIGH | defer |
| tests/test_vacuum.py:1032 | `[test] test_issues_define_user_sql_for_rw_and_ro` | 13 | 60 | 2 | 1 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| enrichment/__init__.py:79 | `enrich` | 13 | 59 | 4 | 3 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| cli/context.py:6 | `cmd_context` | 13 | 55 | 1 | 3 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| server/tools/wiki.py:128 | `wiki_query` | 13 | 52 | 4 | 3 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| server/_helpers.py:92 | `_file_hash` | 13 | 50 | 1 | 4 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| curation/strengthen.py:40 | `_memify_reweight` | 13 | 48 | 2 | 4 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| tests/test_causal_discovery.py:476 | `[test] test_edges_persisted` | 13 | 47 | 3 | 2 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| tests/test_check_invariants_repair.py:242 | `[test] test_caused_by_dangling_edges_auto_repaired` | 13 | 47 | 0 | 1 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| config_yaml.py:576 | `coerce_value` | 13 | 41 | 2 | 2 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| tests/test_frontier_schema.py:93 | `[test] test_all_frontier_fields_settable` | 13 | 29 | 1 | 0 | **soft** `cyclo=13>10` | MEDIUM | decompose-low-risk |
| vacuum/__init__.py:446 | `cmd_vacuum_impl` | 12 | 116 | 1 | 2 | **soft** `cyclo=12>10 LOC=116>80` | MEDIUM | decompose-low-risk |
| server/http.py:675 | `hook_subagent_start` | 12 | 70 | 1 | 3 | **soft** `cyclo=12>10` | MEDIUM | decompose-low-risk |
| rules_engine.py:420 | `check_write_policy` | 12 | 66 | 4 | 4 | **soft** `cyclo=12>10` | MEDIUM | decompose-low-risk |
| hooks/prompt-recall.py:67 | `_fts_search` | 12 | 58 | 3 | 3 | **soft** `cyclo=12>10` | MEDIUM | decompose-low-risk |
| config_yaml.py:710 | `cmd_config_list` | 12 | 47 | 1 | 3 | **soft** `cyclo=12>10` | MEDIUM | decompose-low-risk |
| server/http.py:44 | `health_check` | 12 | 45 | 1 | 4 | **soft** `cyclo=12>10` | MEDIUM | decompose-low-risk |
| embeddings.py:53 | `_is_model_cached` | 12 | 39 | 1 | 4 | **soft** `cyclo=12>10` | HIGH | defer |
| seed/_analysis.py:75 | `_summarize_package_json` | 12 | 35 | 1 | 3 | **soft** `cyclo=12>10` | MEDIUM | decompose-low-risk |
| retrieval/query_analysis.py:244 | `_build_open_domain_subqueries` | 12 | 32 | 2 | 2 | **soft** `cyclo=12>10` | MEDIUM | decompose-low-risk |
| tests/test_vacuum_now.py:164 | `[test] test_no_service_manager` | 12 | 26 | 1 | 1 | **soft** `cyclo=12>10` | MEDIUM | decompose-low-risk |
| tests/test_frontier_schema.py:663 | `[test] test_default_values_persist` | 12 | 14 | 2 | 0 | **soft** `cyclo=12>10` | MEDIUM | decompose-low-risk |
| tests/integration/test_vacuum_e2e.py:414 | `[test] test_vacuum_e2e_import_failure_restores_original` | 11 | 105 | 1 | 3 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| config_yaml.py:619 | `cmd_config_init` | 11 | 89 | 1 | 4 | **soft** `cyclo=11>10 LOC=89>80` | MEDIUM | decompose-low-risk |
| server/http.py:549 | `_handle_plan_file` | 11 | 71 | 3 | 2 | **soft** `cyclo=11>10` | HIGH | defer |
| hooks/post-tool-capture.py:41 | `main` | 11 | 62 | 0 | 3 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| knowledge_graph.py:266 | `get_subgraph` | 11 | 59 | 3 | 4 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| astrocyte_pool.py:113 | `assign_memory` | 11 | 50 | 2 | 3 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| tests/test_subagent_stop_hook.py:176 | `[test] test_endpoint_stores_findings_with_provenance` | 11 | 41 | 1 | 1 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| storage/client.py:267 | `_row_to_dict` | 11 | 40 | 2 | 2 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| cognitive_map.py:269 | `get_sr_scores` | 11 | 38 | 4 | 2 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| retrieval/fusion.py:59 | `_convex_fuse` | 11 | 36 | 2 | 2 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| server/http.py:841 | `api_consolidation_log` | 11 | 33 | 1 | 2 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| storage/__init__.py:276 | `close` | 11 | 31 | 1 | 4 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| tests/test_cls_store.py:343 | `[test] test_consolidation_cycle_stats` | 11 | 31 | 4 | 0 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| seed/_analysis.py:248 | `_find_subproject_boundaries` | 11 | 28 | 2 | 2 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| curation/strengthen.py:11 | `_memify_strengthen` | 11 | 27 | 2 | 2 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| tests/test_sse.py:132 | `[test] test_partial_disconnect_connected_clients_receive_event` | 11 | 24 | 1 | 1 | **soft** `cyclo=11>10` | MEDIUM | decompose-low-risk |
| server/tools/admin_vacuum.py:13 | `vacuum_now` | 10 | 106 | 1 | 2 | **soft** `LOC=106>80` | MEDIUM | decompose-low-risk |
| seed/_generate.py:279 | `seed_project` | 9 | 138 | 7 | 3 | **soft** `LOC=138>80 params=7>5` | MEDIUM | decompose-low-risk |
| server/tools/misc.py:231 | `sync_instructions` | 9 | 104 | 1 | 4 | **soft** `LOC=104>80` | MEDIUM | decompose-low-risk |
| storage/__init__.py:172 | `__init__` | 9 | 101 | 3 | 3 | **soft** `LOC=101>80` | MEDIUM | decompose-low-risk |
| prospective.py:140 | `_matches` | 9 | 29 | 6 | 4 | **soft** `params=6>5` | MEDIUM | decompose-low-risk |
| predictive_coding.py:350 | `should_store` | 8 | 81 | 4 | 2 | **soft** `LOC=81>80` | MEDIUM | decompose-low-risk |
| vacuum/__init__.py:372 | `_vacuum_finalize` | 8 | 67 | 6 | 2 | **soft** `params=6>5` | MEDIUM | decompose-low-risk |
| rules_engine.py:175 | `add_rule` | 8 | 63 | 7 | 3 | **soft** `params=7>5` | MEDIUM | decompose-low-risk |
| wiki.py:35 | `add` | 7 | 72 | 8 | 1 | **soft** `params=8>5` | MEDIUM | decompose-low-risk |
| storage/memory.py:383 | `search_memories_by_content_date` | 7 | 38 | 7 | 2 | **soft** `params=7>5` | LOW | decompose-low-risk |
| retrieval/core.py:280 | `recall` | 6 | 127 | 6 | 1 | **soft** `LOC=127>80 params=6>5` | HIGH | defer |
| cli/setup.py:6 | `cmd_setup` | 6 | 112 | 1 | 3 | **soft** `LOC=112>80` | MEDIUM | decompose-low-risk |
| storage/user.py:90 | `insert_profile` | 6 | 74 | 8 | 2 | **soft** `params=8>5` | MEDIUM | decompose-low-risk |
| curation/ingestion.py:55 | `merge_memory` | 6 | 49 | 7 | 3 | **soft** `params=7>5` | LOW | decompose-low-risk |
| vacuum/__init__.py:228 | `_vacuum_restart_and_import` | 5 | 105 | 5 | 2 | **soft** `LOC=105>80` | MEDIUM | decompose-low-risk |
| viz_server.py:32 | `_proxy_request` | 5 | 45 | 6 | 1 | **soft** `params=6>5` | LOW | decompose-low-risk |
| daemon.py:455 | `install_systemd_service` | 4 | 103 | 2 | 0 | **soft** `LOC=103>80` | MEDIUM | decompose-low-risk |
| server/tools/admin_other.py:287 | `add_rule` | 4 | 31 | 6 | 2 | **soft** `params=6>5` | LOW | decompose-low-risk |
| knowledge_graph.py:57 | `add_relationship` | 4 | 29 | 6 | 1 | **soft** `params=6>5` | LOW | decompose-low-risk |
| storage/memory.py:493 | `update_memory_compression` | 3 | 24 | 6 | 1 | **soft** `params=6>5` | LOW | decompose-low-risk |
| restoration.py:99 | `anchor_memory` | 2 | 37 | 6 | 0 | **soft** `params=6>5` | LOW | decompose-low-risk |
| storage/user.py:47 | `update_memory_metamemory` | 2 | 22 | 6 | 1 | **soft** `params=6>5` | LOW | decompose-low-risk |
| restoration.py:32 | `__init__` | 2 | 16 | 7 | 0 | **soft** `params=7>5` | LOW | decompose-low-risk |
| storage/queue.py:60 | `insert_action_log` | 1 | 23 | 6 | 0 | **soft** `params=6>5` | LOW | decompose-low-risk |
| knowledge_graph.py:347 | `_insert_typed_relationship` | 1 | 19 | 7 | 0 | **soft** `params=7>5` | LOW | decompose-low-risk |
| curation/__init__.py:135 | `_merge_memory` | 1 | 18 | 6 | 0 | **soft** `params=6>5` | LOW | decompose-low-risk |
| sleep_compute/__init__.py:27 | `__init__` | 1 | 18 | 7 | 0 | **soft** `params=7>5` | HIGH | defer |
| retrieval/core.py:32 | `__init__` | 1 | 17 | 6 | 0 | **soft** `params=6>5` | HIGH | defer |
| astrocyte_pool.py:66 | `__init__` | 1 | 14 | 6 | 0 | **soft** `params=6>5` | LOW | decompose-low-risk |
| config.py:456 | `settings_customise_sources` | 1 | 14 | 6 | 0 | **soft** `params=6>5` | LOW | decompose-low-risk |
| causal_discovery/__init__.py:76 | `_meek_r1` | 1 | 13 | 6 | 0 | **soft** `params=6>5` | LOW | decompose-low-risk |
| causal_discovery/__init__.py:90 | `_meek_r2` | 1 | 13 | 6 | 0 | **soft** `params=6>5` | LOW | decompose-low-risk |
| causal_discovery/__init__.py:104 | `_meek_r3` | 1 | 13 | 6 | 0 | **soft** `params=6>5` | LOW | decompose-low-risk |

---

## File Metrics

Non-test files only. Sorted by LOC descending.

| file | LOC | public symbols | cap |
|---|---|---|---|
| `server/http.py` | 1040 | 23 | LOC HARD>1000 |
| `config_yaml.py` | 871 | 10 | LOC soft>500 |
| `server/tools/project.py` | 864 | 7 | LOC soft>500 |
| `daemon.py` | 714 | 7 | LOC soft>500 |
| `storage/memory.py` | 690 | 0 | LOC soft>500 |
| `storage/client.py` | 617 | 0 | LOC soft>500 |
| `server/tools/admin_invariants.py` | 600 | 3 | LOC soft>500 |
| `vacuum/__init__.py` | 561 | 1 | LOC soft>500 |
| `predictive_coding.py` | 546 | 2 | LOC soft>500 |
| `cli/stats.py` | 535 | 2 | LOC soft>500 |
| `server/tools/memorize.py` | 533 | 4 | LOC soft>500 |
| `rules_engine.py` | 517 | 4 | LOC soft>500 |
| `graph_api.py` | 514 | 4 | LOC soft>500 |
| `ml_client.py` | 505 | 4 | LOC soft>500 |
| `retrieval/query_analysis.py` | 496 | 1 | - |
| `metrics.py` | 490 | 47 | symbols soft>30 |
| `config.py` | 478 | 3 | - |
| `server/lifecycle.py` | 434 | 5 | - |
| `restoration.py` | 431 | 2 | - |
| `consolidation/cls.py` | 429 | 1 | - |
| `storage/wiki.py` | 429 | 0 | - |
| `server/tools/admin_other.py` | 423 | 13 | - |
| `knowledge_graph.py` | 418 | 3 | - |
| `seed/_generate.py` | 416 | 3 | - |
| `storage/migrations.py` | 415 | 0 | - |
| `server/tools/misc.py` | 414 | 12 | - |
| `retrieval/core.py` | 406 | 2 | - |
| `wiki.py` | 387 | 3 | - |
| `seed/_scan.py` | 374 | 2 | - |
| `install_hooks_lib.py` | 371 | 3 | - |
| `retrieval/fusion.py` | 369 | 1 | - |
| `cls_store/patterns.py` | 360 | 0 | - |
| `astrocyte_pool.py` | 355 | 3 | - |
| `server/tools/wiki.py` | 331 | 10 | - |
| `embeddings.py` | 317 | 5 | - |
| `cognitive_map.py` | 316 | 2 | - |
| `storage/__init__.py` | 312 | 1 | - |
| `storage/ops.py` | 284 | 0 | - |
| `thermodynamics.py` | 282 | 2 | - |
| `causal_discovery/pc.py` | 279 | 3 | - |
| `seed/_analysis.py` | 275 | 0 | - |
| `storage/entity.py` | 275 | 0 | - |
| `embed_service.py` | 269 | 11 | - |
| `retrieval/scoring.py` | 264 | 1 | - |
| `consolidation/__init__.py` | 257 | 2 | - |
| `sleep_compute/community.py` | 255 | 0 | - |
| `curation/__init__.py` | 254 | 2 | - |
| `metacognition/cognitive_load.py` | 254 | 0 | - |
| `file_queue/__init__.py` | 249 | 3 | - |
| `server/__init__.py` | 244 | 0 | - |
| `scripts/hook_runner.py` | 240 | 7 | - |
| `enrichment/conceptnet.py` | 238 | 2 | - |
| `causal_discovery/__init__.py` | 233 | 2 | - |
| `narrative.py` | 233 | 2 | - |
| `models.py` | 228 | 16 | - |
| `consolidation/orchestrator.py` | 223 | 1 | - |
| `server/tools/recall.py` | 222 | 3 | - |
| `hooks/prompt-recall.py` | 221 | 5 | - |
| `curation/strengthen.py` | 213 | 1 | - |
| `server/_app.py` | 210 | 2 | - |
| `storage/narrative.py` | 210 | 0 | - |
| `storage/rules.py` | 210 | 0 | - |
| `hooks/subagent_stop.py` | 206 | 2 | - |
| `retrieval/reranking.py` | 205 | 2 | - |
| `engram.py` | 203 | 2 | - |
| `storage/vector.py` | 199 | 0 | - |
| `conflict_resolver.py` | 198 | 1 | - |
| `log_config.py` | 191 | 3 | - |
| `viz_server.py` | 189 | 3 | - |
| `staleness.py` | 188 | 4 | - |
| `storage/user.py` | 187 | 0 | - |
| `cls_store/__init__.py` | 186 | 2 | - |
| `cli/daemon.py` | 183 | 2 | - |
| `prospective.py` | 183 | 3 | - |
| `file_queue/queue.py` | 179 | 2 | - |
| `retrieval/entities.py` | 176 | 0 | - |
| `server/_helpers.py` | 176 | 2 | - |
| `ops.py` | 175 | 3 | - |
| `server/tools/agent_prompts.py` | 175 | 3 | - |
| `observability/timing.py` | 174 | 3 | - |
| `curation/prune_passes.py` | 172 | 1 | - |
| `curation/ingestion.py` | 171 | 6 | - |
| `cls_store/clustering.py` | 170 | 1 | - |
| `metacognition/gap_detection.py` | 168 | 0 | - |
| `hooks/stop-memory-checkpoint.py` | 167 | 2 | - |
| `server/tools/wiki_coverage.py` | 162 | 2 | - |
| `retrieval/_reranking_heuristic.py` | 158 | 0 | - |
| `server/tools/__init__.py` | 152 | 0 | - |
| `vacuum/phases.py` | 150 | 0 | - |
| `retrieval/_reranking_cross_encoder.py` | 148 | 1 | - |
| `storage/dbsize.py` | 147 | 0 | - |
| `__main__.py` | 146 | 4 | - |
| `vacuum/strip.py` | 143 | 2 | - |
| `consolidation/cleanup.py` | 140 | 1 | - |
| `remote_embeddings.py` | 139 | 2 | - |
| `server/_state.py` | 139 | 0 | - |
| `hooks/subagent-stop.py` | 138 | 0 | - |
| `metacognition/coverage.py` | 138 | 0 | - |
| `enrichment/__init__.py` | 137 | 3 | - |
| `storage/cluster.py` | 133 | 0 | - |
| `sleep_compute/dream.py` | 130 | 0 | - |
| `auth_middleware.py` | 128 | 2 | - |
| `file_queue/dlq.py` | 125 | 1 | - |
| `cli/setup.py` | 122 | 2 | - |
| `sensory_buffer.py` | 122 | 1 | - |
| `server/tools/admin_vacuum.py` | 118 | 2 | - |
| `storage/queue.py` | 117 | 0 | - |
| `retrieval/temporal.py` | 115 | 1 | - |
| `cls_store/promotion.py` | 113 | 1 | - |
| `retrieval/_reranking_confidence.py` | 111 | 0 | - |
| `enrichment/logic.py` | 109 | 1 | - |
| `hooks/post-tool-capture.py` | 106 | 1 | - |
| `secrets.py` | 103 | 1 | - |
| `server/tools/dispatch_helper.py` | 103 | 2 | - |
| `hooks/file-changed.py` | 100 | 0 | - |
| `storage/causal.py` | 99 | 0 | - |
| `enrichment/comet.py` | 98 | 2 | - |
| `cli/rules.py` | 97 | 4 | - |
| `hooks/instructions_loaded.py` | 96 | 1 | - |
| `causal_discovery/dag_io.py` | 95 | 3 | - |
| `server/tools/admin_dlq.py` | 92 | 3 | - |
| `sleep_compute/embed_compress.py` | 92 | 0 | - |
| `storage/episode.py` | 92 | 0 | - |
| `hooks/subagent_start.py` | 91 | 1 | - |
| `retrieval/_reranking_mmr.py` | 84 | 0 | - |
| `file_queue/apply.py` | 83 | 1 | - |
| `curation/contradiction.py` | 82 | 2 | - |
| `hooks/subagent-start.py` | 79 | 0 | - |
| `retrieval/graph_helpers.py` | 79 | 0 | - |
| `enrichment/doc2query.py` | 78 | 2 | - |
| `hooks/instructions-loaded.py` | 78 | 0 | - |
| `consolidation/heat_decay.py` | 77 | 1 | - |
| `causal_discovery/independence.py` | 73 | 2 | - |
| `hooks/file_changed.py` | 73 | 3 | - |
| `cli/vacuum.py` | 71 | 2 | - |
| `sleep_compute/__init__.py` | 70 | 2 | - |
| `scripts/wiki_snapshot.py` | 68 | 2 | - |
| `cli/context.py` | 67 | 2 | - |
| `causal_discovery/meek.py` | 62 | 4 | - |
| `hooks/session-start-context.py` | 61 | 1 | - |
| `cli/install_hooks.py` | 60 | 2 | - |
| `sanitize.py` | 60 | 1 | - |
| `rate_limit.py` | 58 | 1 | - |
| `server/tools/admin.py` | 57 | 0 | - |
| `storage/bitemporal.py` | 56 | 1 | - |
| `cli/config.py` | 49 | 2 | - |
| `cli/seed.py` | 49 | 2 | - |
| `storage/branch.py` | 49 | 1 | - |
| `metacognition/__init__.py` | 48 | 2 | - |
| `enrichment/fpa.py` | 46 | 2 | - |
| `consolidation/causal.py` | 44 | 1 | - |
| `retrieval/_reranking_multi_passage.py` | 44 | 0 | - |
| `retrieval/_reranking_nli.py` | 41 | 1 | - |
| `cli/capture.py` | 39 | 2 | - |
| `cli/_shared.py` | 36 | 1 | - |
| `enrichment/_seq2seq.py` | 28 | 1 | - |
| `cli/viz.py` | 27 | 2 | - |
| `retrieval/quality.py` | 26 | 0 | - |
| `cli/drain.py` | 24 | 2 | - |
| `cli/restore.py` | 23 | 2 | - |
| `retrieval/__init__.py` | 19 | 0 | - |
| `seed/__init__.py` | 14 | 0 | - |
| `file_queue/_locals.py` | 9 | 0 | - |
| `__init__.py` | 6 | 0 | - |
| `cli/__init__.py` | 1 | 0 | - |
| `hooks/__init__.py` | 1 | 0 | - |
| `observability/__init__.py` | 1 | 0 | - |
| `scripts/__init__.py` | 1 | 0 | - |

---

## Class Violations

| file:line | class | methods | attrs | inh_depth | cap |
|---|---|---|---|---|---|
| consolidation/__init__.py:78 | `ConsolidationScheduler` | 10 | 20 | 1 | attrs=20>15 |

---

## Methodology

- **Cyclomatic complexity:** McCabe (1 + branches). Branch nodes: `if`, `for`, `while`,
  `ExceptHandler`, `with`, `assert`, `comprehension`, `BoolOp` operands, `IfExp`, `match_case`.
- **LOC:** `end_lineno - lineno + 1` from AST (includes docstrings, blank lines in body).
- **Params:** `args + kwonlyargs + posonlyargs + (*args if present) + (**kwargs if present)`.
- **Nesting:** max depth of control-flow nodes (`if/for/while/with/try/ExceptHandler`) from body root.
- **Public symbols (file):** top-level defs/classes/assignments not prefixed with `_`.
- **Instance attrs:** `self.X` assignments in `__init__` (direct body only).
- **Inheritance depth:** recursive base-class walk within the same codebase.
- **Test exemption:** `yadgar/tests/` files exempt from LOC + params caps; cyclo + nesting enforced.
- **Risk classification:** HIGH = file/function crosses async/thread/queue boundary or
  is in a known topology-sensitive module; MEDIUM = multi-step pipeline, single thread;
  LOW = mechanical independent branches.
