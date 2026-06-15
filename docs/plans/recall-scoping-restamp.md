# PLAN — Recall scoping + corpus re-stamp (near-term noise cut)

Status: **PLANNED 2026-06-15.** Ships AFTER wiki edit-primitives (v5.61) — uses
`wiki_set_metadata` as the re-stamp tool. This is the immediate-relief release
before the unified-recall rebuild (`[[unified-scoped-recall]]`). Lower-risk:
policy + data fixes in the existing two-path design, no architecture rebuild. Its
policy parts carry into unified unchanged.

theme: retrieval / data-integrity
priority: high (recall is 37.5% noise from within yadgar today)

## Problem (measured)

Recall from within `/home/max/git/yadgar` is **62.5% signal / 37.5% noise**
(80-sample). Cross-project knowledge leaks into every project's results, and
`directory=` does **nothing** (identical results with/without). Same disease in
both corpora:

| | Memories | Wikis |
|---|---|---|
| Always-eligible sink | `directory_context="system"` | `directory_context="global"` |
| Leak size | co-occurrence + cls-promotions stamped `system` | **612 of 616 `global` pages mis-stamped (~97%)** |
| Worst culprit | derived "X and Y frequently modified together" (`curation/strengthen.py:179`) | **364 yadgar `fn-`/`mod-`/`api-` doc pages** → leak into every project |

Clean buckets: `aws-work` (1404 wikis), `nix`. The AWS data was never bloat —
the `global`/`system` *sink* is the problem.

## Three-layer root cause (re-stamp alone won't hold)

1. **Write-time defaults** stamp `system`/`global` when no directory is supplied
   → new pages keep re-polluting. MUST fix first or backfill is Sisyphean.
   - Wikis: `wiki_add` + the repo-wiki generator don't pass `directory_context`
     for `fn-`/`mod-` reference pages → default `global`.
   - Memories: `curation/strengthen.py:179` (co-occurrence), `cls_store/promotion.py:53`
     (cls fallback when no dominant dir), `sleep_compute/dream.py:129` (dreams)
     stamp `system`.
2. **`system`/`global`/`""`/`None` are always-eligible** in the recall filter
   (`server/tools/recall.py:189`) → mis-stamps get global reach.
3. **Directory filter is a soft post-fetch no-op** — Python crop after scoring,
   only when `directory=` passed, and it lets the always-eligible buckets through.
   No DB-level directory filter (cf. `storage/branch.py` BranchFilter has no
   directory equivalent).

## Scope

### A. Stop the bleeding — write-time directory stamping
- **Wikis:** `wiki_add` + repo-wiki generator must stamp the originating
  `directory_context` (the repo being documented), not default `global`. Only
  genuinely cross-cutting pages get `global`.
- **Memories:** `strengthen.py:179` + `promotion.py:53` stamp the originating
  directory (the dir the co-occurring files / cluster members belong to), not
  `system`. `dream.py` dreams may stay `system` IF (B) makes `system` non-global.
- Acceptance: a fresh `wiki_add`/co-occurrence write from a project lands in that
  project's bucket, not `global`/`system`.

### B. Tighten recall scoping (existing path, cheap)
- `recall.py:189`: stop treating other-project dirs as eligible. Default = caller
  dir + `global` only. `system` no longer always-eligible (either drop it from the
  pass-list or reclassify `system`→`global` for the few legit daemon-internal rows).
- **Quality floor:** drop results the cross-encoder scored ~0 (keyword-only
  matches that survive despite `_rerank_score=0`).
- **Dedup:** collapse repeated identical co-occurrence rows (same pair, multiple
  creation dates) — currently resurface 2-4×.
- NOTE: this is the *quick* filter. The *heavy* DB-level `DirectoryFilter` (pushed
  into SurrealQL WHERE) is deferred to `[[unified-scoped-recall]]` so we don't
  build it twice.

### C. Backfill (migration — dry-run first, user-run per Apply/Import rule)
- **Wikis (~612):** re-stamp `global`→correct bucket via `wiki_set_metadata` +
  a slug-prefix driver:
  - `fn-`/`mod-`/`api-`/`yadgar-`/`ccpm-`/`code-review-plugin-*` → `/home/max/git/yadgar` (~364)
  - `services-`/`tests-`/`shared-`/`ir-`/`ui-`/`meridian-*`/… → `/home/max/quinyx/meridian` (~130) — **new bucket**
  - `aws-*`/`rds-*`/`digger-*`/`aws-org-migration-*`/`github-team-*`/… → `/home/max/aws-work` (~110)
  - `flux-*`/`nixos-*` → `/home/max/git/nix` (~8)
  - leave ~21 genuinely-global pages
- **Memories:** backfill `system`-stamped derived rows to originating dir where
  recoverable, else `global`. Scope by store_type/tags.
- All migrations: dry-run output → user reviews → user runs (no auto-apply).

### D. Meridian bucket
`/home/max/quinyx/meridian` has 130 pages but no bucket — all in `global`.
Re-stamp creates it.

### E. CENTRALIZE directory scoping across ALL context surfaces
`recall` is not the only leaky surface. The same directory-scope rule is
copy-pasted (and independently broken — `system`/`global` always-eligible) across
every tool that reads memories/wikis/anchors by directory:
- `recall` (`server/tools/recall.py:189`)
- `project_brief` (anchors + hot_memories + key_wiki_pages — `project.py:599,654`)
- `restore` (post-compaction context: anchors + hot context)
- `agent_dispatch_prelude(include_context=True)` — **worst**: injects recall +
  wiki_query context into EVERY subagent (observed leaking AWS co-occurrence +
  meridian wiki into a dispatched agent). Poisons all agents, not just main.
- SessionStart hook context injection (inherits project_brief/recall)
- anchor queries (`storage/memory.py:768`: `directory_context IN ('','global','system')`)

Fix: extract ONE directory-scope predicate (caller-dir + truly-global only; not
`system`) used by all surfaces — single source of truth, not N copies. The
write-time stamp fixes (A) + the system-reclassify (B) then benefit every surface
automatically. Acceptance: project_brief / restore / prelude from within yadgar
return NO cross-project results (same bar as recall).

**Enumeration (audit 2026-06-15): 27 directory-scope sites, ~9 distinct
predicates, wildly inconsistent.** Build `storage/directory.py` as the exact twin
of `storage/branch.py`: `DirectoryFilter` + `_build_directory_clause(df) ->
(sql, params)` + `is_directory_eligible(dc, caller_dir)` for Python post-filters.
Migrate the 9 predicates to it. **Eligible set after reclassify = `{caller_dir,
'global', ''}` — drop `'system'`** (it's the mis-stamp sink; A/B remove it).

Pure leaks to fix (no directory scoping at all today):
- `server/tools/project.py:432` `_build_wiki_pages` calls `list_wiki_pages()`
  with NO directory arg → **every `project_brief` mode leaks ALL wiki pages**
  (active structural leak since v5.42.5). Add a `directory` arg. ← highest-impact
- `hooks/prompt-recall.py:90-96` supplement query is `directory_context != $dir`
  → returns EVERY non-project memory (all other projects). Replace with explicit
  `IN ('global','')`.
- `storage/vector.py:67` `search_vectors` + `yadgar/wiki.py:437` `WikiStore.query()`
  — unscoped at storage, rely on tool-layer post-filter; push the directory clause
  down (or document + guarantee the post-filter always runs).
- `restoration.py:271` empty-dir branch falls through to `get_memories_by_heat`
  (no dir filter).

Inconsistency to normalize (sentinel eligibility differs per site):
- `'system'` eligible in `recall`(#1)/`project_brief`(#6,#8)/anchors(#16) but
  MISSING from `wiki_query`(#2) + all `storage/wiki.py` predicates (#3,#4,#5) →
  `system` wiki pages currently invisible to wiki surfaces. After reclassify this
  becomes moot (no `system` rows), but normalize via the one helper.

Full site table: see audit output (27 rows). Representative migration targets:
`recall.py:189`, `wiki.py:625`, `storage/wiki.py:418,449,344`, `project.py:599,654,432`,
`storage/memory.py:781,370`, `prompt-recall.py:90`, `dispatch_helper.py:176`.

## Behavior preservation — DO NOT break the tuned context machinery (2026-06-15)

`project_brief` + the stop/SessionStart hooks are the most-iterated surfaces in
the system (catalog mode v5.7.12, active_work v5.10.1, session-end-capture
v5.10.6, KB-usability v5.53, checkpoint/restore, the `recommended_actions`/signals
+ anchor-audit machinery). Changing their scoping must be planned from all angles
— a blunt directory filter wrecks tuned behavior. User directive: heavy planning.

**`global` ≠ `system` — the central rule:**
- `global` = INTENTIONAL cross-cutting (the 114 anchors, key decisions, cross-
  project knowledge the hooks deliberately surface). **STAYS eligible everywhere.**
- `system` = mis-stamp sink. FIX, non-uniformly: dreams (`dream.py`) → `global`;
  project-derived (co-occurrence, cls promotion) → originating dir.
- Cross-project leak (other projects' dirs) = the ONLY thing removed.
The fix never touches `global` cross-cutting. Dropping `system` from the eligible
set is safe ONLY after its rows are reclassified (A/B) — order matters.

**Phase split by risk:**
- **E1 — retrieval surfaces (safer, do first):** `recall`, `wiki_query`,
  `agent_dispatch_prelude` recall/wiki calls. Agent-facing; leak removal is
  low-risk to tuned UX. Ship + measure SNR.
- **E2 — tuned context machinery (heavy review, separate):** `project_brief` (all
  modes), `restore`, SessionStart injection, the stop-hook
  checkpoint/anchor/active_work paths. Each surface gets a behavior-preservation
  pass before any predicate change.

**Behavior-preservation gate for E2 (required before merge):**
1. **Per-surface purpose map:** for each E2 surface, document what it MUST keep
   surfacing (global anchors, catalog, stale signals, checkpoint, recommended_actions)
   vs what is leak. (The 27-site audit gives the predicates; this adds intent.)
2. **Golden baselines:** capture CURRENT project_brief (catalog/restore/signals
   modes) + restore + a stop-hook signals run output as fixtures BEFORE any change.
3. **Assert preservation:** after the predicate change, golden outputs differ ONLY
   by removed cross-project leak — anchors/catalog/signals/recommended_actions
   otherwise unchanged. Any other diff = regression, block.
4. **Map signal dependencies:** `recommended_actions`, `stale_wiki_count`,
   anchor-audit triggers, redundancy pairs all derive from these queries — verify
   the scoping change doesn't perturb the thresholds/counts that drive hook actions.
5. **`_build_wiki_pages` (project.py:432) leak fix** is in E2 (it feeds every
   project_brief mode) — must pass the golden-baseline gate.

## Tests (behavioral — seed→recall→assert, per v5.59 lesson)
- Seed a mixed-directory corpus (yadgar + aws + system/global) → `recall(directory=yadgar)`
  → assert: no other-project results, no `system` co-occurrence, noise < threshold.
- Filter unit tests: `directory=` excludes other dirs; quality floor drops ce~0;
  dedup collapses duplicate co-occurrence.
- Write-time: `wiki_add` from a dir stamps that dir; co-occurrence write stamps
  originating dir.

## Acceptance
- `directory=` measurably scopes results (the no-op is fixed).
- Recall-from-yadgar noise drops from 37.5% to a target (set after A+B land; aim <10%).
- ~612 wikis re-stamped (verified via wiki_list histogram: `global` shrinks to ~21).
- New writes land in correct buckets (no re-pollution).

## Out of scope (→ unified-scoped-recall)
- DB-level `DirectoryFilter` in SurrealQL (heavy; unified builds it once)
- One unified recall tool / multi-source / `type=` param
- Cross-encoder fusion across types

## Related
- `[[unified-scoped-recall]]` — the architecture this feeds into
- `[[wiki-edit-primitives]]` — provides `wiki_set_metadata` (the re-stamp tool)
- `[[wiki-kb-usefulness-snr]]` — the investigation + decisions behind this
- Code: `server/tools/recall.py:189`, `storage/branch.py`, `curation/strengthen.py:179`, `cls_store/promotion.py:53`, `sleep_compute/dream.py:129`
