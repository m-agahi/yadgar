# Silent-breakage audit — 2026-06-16

Motivated by repeated "looked working, was broken for weeks" failures. Static
audit of yadgar/ (excludes existing CI checkers I23/I29 which pass clean — these
are what they MISS). 35 suspected issues. Main-thread VERIFIED items marked ✅.

## TOP RISKS (confirmed silent failures in production)

1. **Sleep cycle never runs on the nightly** ✅ VERIFIED — `_maybe_sleep_cycle`
   (consolidation/orchestrator.py:51) has ZERO call sites (only def + comments
   "by the nightly cron (PR-1)"; PR-1 never wired it). dream replay, community
   detection, cluster summarization, reembed_stale, compress_old_memories,
   auto_narrate DEAD since v5.7.0. nightly_cycle calls force_consolidate() which
   never reaches _maybe_sleep_cycle.
2. **Profile recall no-ops every query** ✅ VERIFIED — `PROFILE_SEARCH_WEIGHT`
   used at retrieval/fusion.py:413 but NOT defined in config.py → AttributeError
   → swallowed by `except Exception: pass` (fusion.py:416). Profile results never
   surface despite PROFILE_EXTRACTION_ENABLED=True.
3. **COMET/doc2query enrichment permanently off** — config enabled, but
   enrichment/_seq2seq.py:17 catches model-load failure silently → infer()/expand()
   return [] forever. Models not in image; no warning.
4. **AstrocytePool domain consolidation never fires** — _run_domain_consolidation
   (consolidation/__init__.py:176) uncalled → consolidate_domain (astrocyte_pool.py:181)
   dead. assign_memory runs, but domain consolidation (the point) never executes.
5. **reembed_stale (auto re-embed) dead** — gated behind the never-called sleep
   cycle (#1). Stale embeddings from model upgrades never auto-fixed.

## CAT 1 — dead production functions (10)
consolidation/__init__.py:176 _run_domain_consolidation; orchestrator.py:51
_maybe_sleep_cycle; astrocyte_pool.py:181 consolidate_domain, :272 consensus_retrieve;
cognitive_map.py:132 extract_coordinates, :167 update_memory_coordinates, :222
get_neighborhood, :269 get_sr_scores, :315 is_dirty; narrative.py:119 get_project_story.
Bonus: server/lifecycle.py:306 _st._narrative instance set+cleared, never read.

## CAT 3 — silent no-op capabilities (5)
3a sleep-cycle-never-nightly (=#1). 3b COMET/doc2query (=#3). 3c profile-search
(=#2). 3d ConceptNet HARDCODED_EXPANSIONS = hobby terms only (camping/yoga/…);
HTTP perm-disabled, conceptnet_lite not a dep → returns hobby/nothing for code
memories. 3e validate_memory fallback (admin_other.py:67) _file_hash on a DIRECTORY
→ always None → "file no longer exists" (only fires when _staleness uninit).

## CAT 4 — dead config fields (15)
FRACTAL_LEVELS, HOPFIELD_BETA, COMPRESSION_GIST_AGE_HOURS, COMPRESSION_TAG_AGE_HOURS,
RECONSOLIDATION_LOW/HIGH_THRESHOLD, PLASTICITY_SPIKE, PLASTICITY_HALF_LIFE_HOURS,
STABILITY_INCREMENT, ADVERSARIAL_SCORE_GAP_THRESHOLD,
BELIEF_SEARCH_PRIORITY_FOR_OPEN_DOMAIN, WIKI_SIM_TITLE_THRESHOLD ("currently unused"),
PROFILE_SUMMARY_ENABLED, PROFILE_CONFIDENCE_DIRECT, PROFILE_CONFIDENCE_INFERRED.

## CAT 5 — stale metric label
metrics.py:638 consolidation_daemon heartbeat label — _daemon_loop removed v5.7.0,
never written (I23 misses it: metric var has other writers).

## CAT 6 — background paths with no real-DB test (4)
6a nightly cycle: all 37 test_nightly_cycle.py mock storage+scheduler — real
consolidate chain never exercised in the nightly lifecycle. 6b sleep phases tested
standalone, not via nightly trigger (so #1 uncaught). 6c astrocyte domain
consolidation path doesn't exist to test. 6d convex fusion (FUSION_METHOD=convex
default) — all retrieval tests override to wrrf → default path undertested.

## DISPOSITION
- Confirmed bugs to FIX: #1 wire sleep into nightly, #2 PROFILE_SEARCH_WEIGHT,
  #3 enrichment-silent-off (warn or honor config), #4 domain consolidation, #5
  reembed_stale (follows #1), 3d ConceptNet, 3e validate_memory fallback.
- Dead code to REMOVE or WIRE: Cat 1 (10) + Cat 4 (15 config) + Cat 5 label.
- Test gaps → behavior-spec-e2e plan (each TOP RISK = a SHALL contract that
  currently fails silently; the suite must go RED on these pre-fix).
