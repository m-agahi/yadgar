# v5.55 Refactor Contract Map

Derived from `.complexity-allowlist.json` (86 entries) on `feat/v5.55.0-complexity-governance`.
This is the dispatch contract: each refactor agent reads its row, owns its file(s), removes
its allowlist entries (to satisfy I30 no-stale-entries), and does NOT overlap another agent.
All refactor agents branch off `feat/v5.55.0-complexity-governance` (NOT master — they need
the allowlist so pre-commit doesn't block on other files' pre-existing HARD violations).

## Classification
- **RED (6)** — whole-file LOC > 1000. Splitting moves functions across modules → import
  ripples. SERIALIZE, one file per agent. The "GREEN" functions that live inside these files
  are refactored AS PART OF the split (don't touch them separately — growing an over-LOC file
  trips I30 drift).
- **YELLOW (11)** — params > 8. Fixing means a param-object/dataclass → changes EVERY call
  site. One agent owns the function + all callers. Cannot overlap an agent touching those callers.
- **GREEN (69 funcs / 46 files)** — cyclo/nesting/fn_loc on self-contained functions. Internal
  refactor (extract helpers, guard clauses), public signature FROZEN, zero external-caller
  impact. Safe full parallel — one agent per file.

## RED — serialize (do AFTER v5.56 stabilizes; large blast radius)
| file | LOC | bundle these in-file funcs with the split |
|------|-----|-------------------------------------------|
| server/tools/project.py | 2185 | _render_project_brief, _wiki_refresh_stale_impl, wiki_cleanup_merged_branches |
| server/http.py | 1842 | health_check, hook_auto_capture, hook_session_context, hook_prompt_recall, hook_subagent_stop, _handle_team_inbox, _make_event_stream, api_viz_search |
| config_yaml.py | 1430 | (file-LOC only) |
| server/tools/wiki.py | 1167 | wiki_query (GREEN) + the 3 YELLOW wiki_add* (do split + param-object together) |
| storage/migrations.py | 1153 | _migration_016/_018 nesting — `per_path_override` candidate (append-only ledger; may justify instead of split) |
| wiki.py | 1076 | `add` (YELLOW, 10 params — **52 external callers**, the biggest) |

## YELLOW — coordinated, one agent owns fn + all callers
curation/__init__.py:curate_on_remember(12) · file_queue/__init__.py:__init__(9) ·
retrieval/reranking.py:_apply_rerank_pipeline(11) · retrieval/scoring.py:_collect_fts_scores(9) ·
server/tools/memorize.py:memorize(10, HOT-PATH) · server/tools/misc.py:checkpoint(9) ·
server/tools/wiki.py:_wiki_add_sync_write(11)/_wiki_add_wait_path(13)/wiki_add(14) [with RED split] ·
storage/narrative.py:insert_belief(9) · wiki.py:add(10, 52 callers) [with RED split].

## Hot-path GREEN — benchmark-gated, NOT blind parallel
server/tools/recall.py:recall(cyclo55/loc293) · storage/memory.py:insert_memory(cyclo40/loc165) ·
curation/prune_passes.py:_memify_prune(cyclo56/loc160). Run benchmark baseline → refactor →
re-run; >5–10% regression = revert.

## GREEN parallel — safe wave files (one agent each, non-RED, non-hot-path)
Leaf/utility (wave 1, lowest risk): cls_store/clustering.py · cls_store/patterns.py ·
metacognition/gap_detection.py · metacognition/coverage.py · metacognition/cognitive_load.py ·
sleep_compute/dream.py · sleep_compute/community.py · enrichment/conceptnet.py · staleness.py ·
storage/dbsize.py · hooks/subagent-stop.py · embeddings.py · log_config.py · seed/_analysis.py ·
seed/_generate.py · seed/_scan.py · restoration.py.

Mid (wave 2): consolidation/{cls.py(3),cleanup.py,heat_decay.py,orchestrator.py} ·
retrieval/{query_analysis.py(2),scoring.py(2 GREEN),_reranking_*.py(4)} · rules_engine.py(2) ·
predictive_coding.py(2) · graph_api.py(2) · ml_client.py(4) · cls_store · cli/{stats.py,daemon.py} ·
causal_discovery/pc.py (build_event_matrix GREEN; **pc_algorithm → ALLOWLIST, do not split**) ·
file_queue/__init__.py (_drain_once,_update_dlq_gauges GREEN; __init__ is YELLOW) ·
server/lifecycle.py:init_engines · server/tools/{admin_invariants.py:_run_check_invariants,misc.py:anchor}.

Defer: scripts/check_complexity.py (just modified by v5.55.0 — touch last).

## Integration rule (allowlist conflicts)
Every agent edits `.complexity-allowlist.json` (removes its entries). Integrate agent branches
ONE AT A TIME (rebase onto the growing governance branch) so allowlist conflicts resolve
incrementally. Do NOT merge all in one shot.

## Coordination with v5.56
v5.55 code-moves (esp. RED splits) shift the test-isolation polluter map → v5.56 re-audits.
Do RED splits AFTER v5.56 greens the test job, or expect churn.
