# Car C2 — implement `downweight` recall disposition

> Parent plan: docs/plans/task-table-refactor-2026-07-29.md (task 0047, §7 + §16)
> Status: build-ready (spec extracted from audited master plan)
> Depends on: —
> Lifecycle: ADR-0081/0082 — archive this doc as the first commit of the completing branch; mark partial scope in the status header if shipped incomplete.

## 1. Scope

Implement the `"downweight"` `recall_disposition` — the third disposition beside `"include"` and
`"exclude"` (D22). Today it is a docstring placeholder with zero code path: `policy.py:37` says
*"reserved for future tuning (treat as include for now)"*, and `is_recall_visible`
(`policy.py:186`) returns `True` for every disposition ≠ `"exclude"`, so a `"downweight"` page
passes the visibility gate at full score and is indistinguishable from `"include"`. §1.4 flags
this: *"`downweight` is documented, zero implementation. Gates the task cars."*

This car makes `"downweight"` a real ranking penalty: a page whose `page_type` resolves to
`recall_disposition="downweight"` is still VISIBLE in search (unlike `"exclude"`, it is never
filtered out), but its ranking score is multiplied by a configurable factor < 1.0 so it sinks
below `"include"` pages of comparable relevance. D22 maps `task → downweight`; this car adds the
`task_list` page-type policy entry that realizes that mapping. The mechanism is generic — any
future page type declared `"downweight"` inherits the penalty for free.

This is sub-decision **3b** of the three-part retrieval/gate redesign (3a/3b/3c = C1/C2/C3):
C1 tightens the tag-override opt-in, C2 implements `downweight` (D22's third disposition), C3
reimagines the identity gate (D21). The three share the `WikiPolicy` / `policy.py` resolver
surface but are independent (all depend on nothing).

## 2. Touched files

| file | change | verified |
|------|--------|----------|
| `yadgar/_shared/wiki/policy.py:37` | Replace the docstring *"reserved for future tuning (treat as include for now)"* with the implemented semantics: a downweighted page passes `is_recall_visible` but receives a ranking-score multiplier < 1.0 at the scoring stage. | `:37` `""downweight"`` — reserved for future tuning (treat as include for now)` — confirmed via Read |
| `yadgar/_shared/wiki/policy.py:123` | Add `PAGE_TYPE_TASK_LIST` entry to `POLICY_BY_TYPE`: `WikiPolicy(gate_mode="similarity", recall_disposition="downweight", dir_scope="strict", merge="allow", storage_scope="project")`. D22's `task → downweight` mapping. | `:123-134` `POLICY_BY_TYPE` dict — confirmed; no `task_list` entry today (falls through to `DEFAULT_POLICY` include) |
| `yadgar/_shared/wiki/policy.py:163` | Add `downweight_multiplier(page: dict, factor: float) -> float` helper: returns `factor` if `get_policy(page.get("page_type")).recall_disposition == "downweight"`, else `1.0`. Single source of truth for the penalty, shared by both search paths. | `:163` `def is_recall_visible` — confirmed; new function appended after `is_recall_visible` (`:191`) |
| `yadgar/_shared/wiki/wiki_meta.py:57` | Add `PAGE_TYPE_TASK_LIST = "task_list"` constant (after `PAGE_TYPE_AGENT_PROMPT_LEGACY`, before the blank line at `:58`). The `task_list` page type exists in `wiki_page_types.yaml:76` but has no Python constant — `POLICY_BY_TYPE` needs one. | `:57` `PAGE_TYPE_AGENT_PROMPT_LEGACY = "agent_prompt"` — confirmed; `:58` blank, `:76` yaml `task_list:` — confirmed |
| `yadgar/_shared/config/config.py:404` | Add `RECALL_DOWNWEIGHT_FACTOR: float = 0.5` after `RECALL_WIKI_PRIOR_WEIGHT` (`:404`). The ranking-score multiplier applied to downweighted wiki candidates. 0.5 = sink to half score; tunable. | `:403-404` `RECALL_MEMORY_PRIOR_WEIGHT` / `RECALL_WIKI_PRIOR_WEIGHT` — confirmed; `:396` section comment "v6 T6 Step 4" |
| `yadgar/_shared/config/config_registry.py:445` | Add `ConfigEntry("YADGAR_RECALL_DOWNWEIGHT_FACTOR", "0.5", "float")` after the `RECALL_WIKI_PRIOR_WEIGHT` entry (`:445`). I25 three-way registration (env → registry → Settings field). | `:444-445` `RECALL_MEMORY_PRIOR_WEIGHT` / `RECALL_WIKI_PRIOR_WEIGHT` entries — confirmed |
| `yadgar/backend/retrieval/providers/fusion.py:270` | Apply `downweight_multiplier` to `placement_score` for wiki candidates: `placement_score *= downweight_multiplier(wiki_cand.raw, float(settings.RECALL_DOWNWEIGHT_FACTOR))`. The `raw` dict carries `page_type` (set at `providers/wiki.py:104`). The penalty flows through interleaving (`:281`), dedup (`:285`), and the final trim (`:288`) — all use `placement_score` / `wiki_score`. | `:268-271` `for j, wiki_cand in enumerate(wiki_pool): ce = ...; placement_score = ce + wiki_prior_weight * wiki_cand.native_score` — confirmed; `:47` `from ...providers.base import Candidate` — need to add `from yadgar._shared.wiki.policy import downweight_multiplier` |
| `yadgar/core/server/tools/wiki.py:590` | After the `is_recall_visible` filter (`:590`) and the `is_directory_eligible` filter (`:598-600`), BEFORE the truncate (`:602`): apply `r["_retrieval_score"] *= downweight_multiplier(r, factor)` for each result, re-sort by `_retrieval_score` descending, then truncate. The `wiki_query` path has no fusion/CE — `_retrieval_score` IS the ranking key, so the penalty must be applied + re-sorted here. `factor` read via `get_settings().RECALL_DOWNWEIGHT_FACTOR` (the `get_settings` import pattern already used at `:250,823`). | `:586` `results = _st._wiki.query(...)`, `:590` `is_recall_visible` filter, `:598-600` directory filter, `:602` `results = results[:max_results]` — confirmed; `:250` `from yadgar._shared.config import get_settings as _get_settings` — confirmed |
| `yadgar/tests/_shared/test_wiki_policy.py` | Add `TestDownweightDisposition` class: `task_list` resolves to `recall_disposition="downweight"`; `downweight_multiplier` returns `factor` for a `task_list` page and `1.0` for an `include`/`exclude` page; `is_recall_visible` returns `True` for a `downweight` page (it is visible, just penalized). | `:84` `TestPolicyByType`, `:97` `TestGetPolicy` — confirmed; test file uses `PAGE_TYPE_*` from `wiki_meta` (`:22-26`) |
| `yadgar/tests/backend/test_wiki_provider_policy_exclusion.py` | Add `TestDownweightPenalty` class: a `task_list` page SURVIVES `is_recall_visible` (not dropped), but its `Candidate.native_score` is unchanged (the penalty is applied downstream in fusion, not in the provider — see §3). This pins that the provider stays score-agnostic. | `:28` `_page(...)` helper, `:46` `_slugs(provider)` helper — confirmed; pattern reusable for downweight tests |
| `yadgar/tests/backend/test_fusion_tiebreak.py` | Add test: two wiki candidates with equal CE + native_score, one `task_list` (downweight) one `None` (include); the include candidate ranks ABOVE the downweight one in `fuse_candidates` output. RED before the fusion.py edit. | `yadgar/tests/backend/test_fusion_tiebreak.py` exists — confirmed; `fuse_candidates` signature at `fusion.py:179` |
| `yadgar/tests/core/test_wiki_query_policy_exclusion.py` | Add `test_downweight_page_visible_but_ranked_lower`: a `task_list` page and a plain page both match the query; both survive the visibility filter; the plain page ranks above the task_list page in `wiki_query` output (the penalty re-orders them). RED before the wiki.py edit. | `:66` `_slugs(**kwargs)` helper, `:36` `_corpus` fixture pattern — confirmed |
| `pyproject.toml:7` + `server.json:10` | Core `version` bump per WORKFLOW RULE (C2 touches `yadgar/core/server/tools/wiki.py` → core bump). | `pyproject.toml:7` `version = "5.181.0"`, `server.json:10` `"version": "5.181.0"` — confirmed |
| `server.json:11` + `yadgar/__init__.py:21` | `backend_version` bump — C2 touches `yadgar/backend/retrieval/providers/fusion.py` → `scripts/check_backend_bump.py:44` `BACKEND_BUILD_DIRS = ("backend",)` triggers. | `server.json:11` `"backend_version": "5.71.0"`, `yadgar/__init__.py:21` `BACKEND_VERSION = "5.71.0"` — confirmed; `check_backend_bump.py:44,69` "backend" in `p.parts` → True for `yadgar/backend/...` — confirmed |

## 3. Functions / symbols

**New:**

- `downweight_multiplier(page: dict, factor: float) -> float`
  (`yadgar/_shared/wiki/policy.py`, appended after `is_recall_visible` at `:191`):
  ```python
  @observe(tier="hot")
  def downweight_multiplier(page: dict, factor: float) -> float:
      """Return the ranking-score multiplier for *page*.

      Returns *factor* (a value in (0, 1)) when the page's ``page_type`` resolves
      to ``recall_disposition="downweight"``; ``1.0`` otherwise.  A downweighted
      page is VISIBLE in search (``is_recall_visible`` returns True — it only
      drops ``"exclude"``) but its ranking score is scaled by *factor* so it sinks
      below ``"include"`` pages of comparable relevance.
      """
      if get_policy(page.get("page_type")).recall_disposition == "downweight":
          return factor
      return 1.0
  ```
  Single source of truth for the penalty; called from `fusion.py` (unified recall)
  and `wiki.py` (wiki_query). The `factor` is passed in (not read from settings
  inside the helper) so the helper is testable without a Settings instance and
  stays in `_shared` (no config import — `_shared` policy stays config-agnostic).

- `PAGE_TYPE_TASK_LIST = "task_list"`
  (`yadgar/_shared/wiki/wiki_meta.py:58`, after `PAGE_TYPE_AGENT_PROMPT_LEGACY`):
  Python constant for the `task_list` page type already declared in
  `wiki_page_types.yaml:76`. Used in `POLICY_BY_TYPE` and in tests.

- `RECALL_DOWNWEIGHT_FACTOR: float = 0.5`
  (`yadgar/_shared/config/config.py:404`, after `RECALL_WIKI_PRIOR_WEIGHT`):
  The multiplier applied to downweighted candidates' ranking scores. 0.5 = half
  score. Tunable via `YADGAR_RECALL_DOWNWEIGHT_FACTOR` env (registered in
  `config_registry.py:445`).

**Modified:**

- `POLICY_BY_TYPE` (`yadgar/_shared/wiki/policy.py:123`) — add entry:
  ```python
  PAGE_TYPE_TASK_LIST: WikiPolicy(
      gate_mode="similarity",
      recall_disposition="downweight",
      dir_scope="strict",
      merge="allow",
      storage_scope="project",
  ),
  ```
  D22's `task → downweight`. The task-list page stays recallable (you might
  genuinely ask "what tasks are open?") but sinks below knowledge pages of
  comparable relevance. `gate_mode`/`dir_scope`/`merge`/`storage_scope` match
  `DEFAULT_POLICY` — only `recall_disposition` differs.

- `fuse_candidates` (`yadgar/backend/retrieval/providers/fusion.py:179`) — at
  `:270`, after computing `placement_score`:
  ```python
  ce = wiki_ce_scores.get(j, wiki_cand.native_score)
  placement_score = ce + wiki_prior_weight * wiki_cand.native_score
  # Car C2 (0047 §7 3b): downweight penalty — sink downweighted wiki pages
  # below include-disposition pages of comparable CE relevance.
  placement_score *= downweight_multiplier(
      wiki_cand.raw, float(settings.RECALL_DOWNWEIGHT_FACTOR)
  )
  wiki_with_placement.append((wiki_cand, placement_score))
  ```
  The penalty hits the actual ranking key, so it propagates through
  interleaving (`_interleave_wiki_into_memories` reads `wiki_score`),
  dedup (`_cross_type_dedup` compares `wiki_score`), and the final trim.
  `wiki_cand.raw` carries `page_type` (set at `providers/wiki.py:104`:
  `raw = dict(page)`). New import at `fusion.py:48`:
  `from yadgar._shared.wiki.policy import downweight_multiplier`.

- `wiki_query` (`yadgar/core/server/tools/wiki.py:523`) — between the directory
  filter (`:600`) and the truncate (`:602`), insert:
  ```python
  # Car C2 (0047 §7 3b): downweight penalty for wiki_query (no fusion/CE
  # here — _retrieval_score IS the ranking key, so penalize + re-sort).
  from yadgar._shared.config import get_settings as _get_settings  # noqa: PLC0415
  from yadgar._shared.wiki.policy import downweight_multiplier  # noqa: PLC0415
  _dw_factor = float(_get_settings().RECALL_DOWNWEIGHT_FACTOR)
  if _dw_factor < 1.0:
      for r in results:
          r["_retrieval_score"] = float(r.get("_retrieval_score", 0.0)) * downweight_multiplier(r, _dw_factor)
      results.sort(key=lambda r: r.get("_retrieval_score", 0.0), reverse=True)
  ```
  Guarded on `_dw_factor < 1.0` so a factor of 1.0 is a no-op (no re-sort cost).
  The `_retrieval_score` mutation is on the result dicts BEFORE caching
  (`:610` `_wiki_query_cache.put`) — the cached copy holds the penalized scores,
  which is correct (the penalty is deterministic per page_type, not per call).

**Existing signatures to preserve (verified):**

- `is_recall_visible(page, opt_in_tags) -> bool` (`policy.py:163`) — UNCHANGED.
  It already returns `True` for `recall_disposition="downweight"` (the `!= "exclude"`
  check at `:186` passes). No edit needed; the downweight path goes AROUND
  `is_recall_visible`, not through it.
- `WikiProvider.candidates(self, query, scope, limit) -> list[Candidate]`
  (`providers/wiki.py:60`) — UNCHANGED. The provider's `native_score`
  (`:102`) stays the RAW retrieval score (unmodified). The penalty is applied
  downstream in fusion, not in the provider — this keeps the provider
  score-agnostic and avoids double-penalization (provider + fusion).
- `get_policy(page_type: str | None) -> WikiPolicy` (`policy.py:147`) — UNCHANGED
  signature; returns a `WikiPolicy` that now includes a `task_list` entry.
- `fuse_candidates(memory_candidates, wiki_candidates, query, retriever,
  max_results, settings, profile) -> list[Candidate]` (`fusion.py:179`) —
  UNCHANGED signature; `settings` already passed (used for quotas/priors at
  `:228-231`); `settings.RECALL_DOWNWEIGHT_FACTOR` read at the new `:270b`.

## 4. Build steps (TDD)

1. **RED** — `tests/_shared/test_wiki_policy.py`: add `TestDownweightDisposition`:
   - `test_task_list_resolves_to_downweight`: `get_policy(PAGE_TYPE_TASK_LIST).recall_disposition == "downweight"`.
   - `test_task_list_differs_from_default`: `get_policy(PAGE_TYPE_TASK_LIST) != DEFAULT_POLICY`.
   - `test_downweight_multiplier_returns_factor_for_task_list`: `downweight_multiplier({"page_type": PAGE_TYPE_TASK_LIST}, 0.5) == 0.5`.
   - `test_downweight_multiplier_returns_one_for_include`: `downweight_multiplier({"page_type": None}, 0.5) == 1.0`.
   - `test_downweight_multiplier_returns_one_for_exclude`: `downweight_multiplier({"page_type": PAGE_TYPE_AGENT_PATTERN}, 0.5) == 1.0` (exclude is NOT downweighted — it is filtered by `is_recall_visible`, never reaches the scorer).
   - `test_is_recall_visible_passes_downweight`: `is_recall_visible({"page_type": PAGE_TYPE_TASK_LIST}) is True` (downweight is visible, not filtered).
   All RED before any policy.py edit.

2. **GREEN** — add `PAGE_TYPE_TASK_LIST` to `wiki_meta.py`; add the `POLICY_BY_TYPE` entry in `policy.py`; add `downweight_multiplier` in `policy.py`; update the `:37` docstring. Policy tests go green.

3. **RED** — `tests/backend/test_fusion_tiebreak.py`: add `test_downweight_page_ranks_below_include_page`:
   Two wiki candidates, equal `native_score` and equal CE (mock retriever returning
   identical CE scores), one `page_type="task_list"` and one `page_type=None`.
   Assert the `None` (include) candidate appears BEFORE the `task_list` (downweight)
   candidate in `fuse_candidates` output. RED before the fusion.py edit (currently
   they tie and the tie-break by id desc decides — the task_list slug may or may not
   win, but the PENALTY is absent).

4. **GREEN** — add `RECALL_DOWNWEIGHT_FACTOR` to `config.py` + `config_registry.py`; add the `downweight_multiplier` call in `fusion.py:270`; add the import. Fusion tiebreak test goes green.

5. **RED** — `tests/core/test_wiki_query_policy_exclusion.py`: add `test_downweight_page_visible_but_ranked_lower`:
   Insert a `task_list` page and a plain page with identical content (both match the
   query). Assert BOTH survive `wiki_query` (downweight is visible), and the plain
   page's slug appears before the `task_list` page's slug in the result order. RED
   before the `wiki.py` edit (currently they return in `_retrieval_score` order, and
   with identical content the task_list page may rank equally or first).

6. **GREEN** — add the downweight + re-sort block in `wiki.py` between `:600` and `:602`. wiki_query test goes green.

7. **REFACTOR** — verify the `_retrieval_score` mutation does not corrupt the `WikiStore.query` result list (the results are fresh dicts from the query, not shared with the store's internal state — confirmed at `providers/wiki.py:104` `raw = dict(page)` pattern; `wiki_query` receives a new list from `_st._wiki.query` at `:586`). If the store returns shared references, deep-copy before mutating; otherwise no change.

8. **GATE** — run `ruff`, `import-linter`, `check_versions`, `check_backend_bump` (expect backend bump REQUIRED — `yadgar/backend/retrieval/providers/fusion.py` is staged), and the three test modules.

## 5. Acceptance gates

- [ ] `PAGE_TYPE_TASK_LIST` constant exists in `wiki_meta.py`; `get_policy("task_list").recall_disposition == "downweight"`.
- [ ] `downweight_multiplier(page, factor)` returns `factor` for a downweight-disposition page, `1.0` otherwise; is importable from `yadgar._shared.wiki.policy`.
- [ ] `is_recall_visible` STILL returns `True` for a `downweight` page — it is visible, not filtered. No change to `is_recall_visible`'s exclusion arm.
- [ ] In `fuse_candidates`, a downweighted wiki candidate with CE + native_score equal to an include candidate ranks BELOW the include candidate (penalty on `placement_score`).
- [ ] In `wiki_query`, a downweighted page survives the visibility filter but ranks below an include page of equal `_retrieval_score` (penalty + re-sort before truncate).
- [ ] `RECALL_DOWNWEIGHT_FACTOR` is registered three-way (env `YADGAR_RECALL_DOWNWEIGHT_FACTOR` → `config_registry.py` → `config.py` Settings field); default 0.5; overridable.
- [ ] `WikiProvider.candidates` `native_score` is UNMODIFIED — the penalty is applied in fusion, not the provider (avoids double-penalization and keeps the provider score-agnostic).
- [ ] core version bumped (`pyproject.toml:7` + `server.json:10` via `scripts/sync_version.py`; `scripts/check_versions.py` enforces consistency across pyproject/server.json/docker-compose/uv.lock/flake.nix).
- [ ] backend_version bumped (`server.json:11` + `yadgar/__init__.py:21`; `scripts/check_backend_bump.py` requires it because `yadgar/backend/retrieval/providers/fusion.py` is staged — `BACKEND_BUILD_DIRS = ("backend",)` at `:44`).
- [ ] pre-commit green (ruff, import-linter, I32, I33, `check_versions`, `check_backend_bump`)
- [ ] tests pass (`tests/_shared/test_wiki_policy.py`, `tests/backend/test_fusion_tiebreak.py`, `tests/core/test_wiki_query_policy_exclusion.py`, `tests/backend/test_wiki_provider_policy_exclusion.py`)

## 6. Sequencing

- **Depends on: —** (§7 row C2). C2 is rootless — it touches only `_shared` policy/config, `backend` fusion, `core` wiki_query, and tests. No ledger/migration/registry prerequisite.
- **WikiPolicy field-order collision with C1 and J.** C1 appends `opt_in_tag` (field #6) to `WikiPolicy`; Car J appends `mutability` (field #6 or #7). C2 does NOT append a field — it uses the existing `recall_disposition` string field (just adds a new value + a new `POLICY_BY_TYPE` entry). So C2 has NO field-order collision with C1 or J. They can land in any order.
- **Coordinates with C1 (tag-override) and C3 (identity gate).** All three live on `WikiPolicy` / `policy.py`. C1 changes `is_recall_visible`; C2 adds `downweight_multiplier` (a new function, no overlap); C3 changes `gate_mode`. They do not block each other. C1's §6 notes: "C2 implements the `recall_disposition='downweight'` code path (currently a docstring only, `policy.py:34,67`)". This car delivers that.
- **Gates the task cars (D, E).** §1.4: *"`downweight` is documented, zero implementation. Gates the task cars."* Once C2 ships, the `task_list` page type is downweighted in recall. Car D (task tools) and Car E (task seed + SessionStart rewire) move tasks to SQL and delete the task-list page; the downweight disposition remains relevant for any surviving `task_list` pages and for D38's archived-task-body handling (archived bodies get a separate retype to an EXCLUDED type per D38, not downweight — downweight is for the LIVE task list).
- **D22's status-driven end state is NOT all in C2.** The ADR status→disposition mapping (`accepted`/`open` → include; `superseded`/`rejected`/`deprecated` → exclude) is realized by Car G's `adr → adr_superseded` retype (D23), which makes the status flip a `page_type` flip, so `get_policy` resolves the disposition. C2 only delivers the `task → downweight` mapping + the mechanism. The ADR exclusion arm is Car G.

## 7. ADRs / decisions

- **D22** — `recall_disposition` becomes status-driven: `accepted`/`open` → include; `superseded`/`rejected`/`deprecated` → exclude; `task` → downweight; `agent_prompt` → exclude unconditional. C2 delivers the `task → downweight` mapping (the `POLICY_BY_TYPE` entry) AND the downweight mechanism (the scoring penalty). The `task` row of D22 is fully satisfied by C2; the ADR-status rows are satisfied by Car G's retype.
- **D38** — on archive, the body page persists, retained, never deleted, and EXCLUDED from recall by the same status-driven `recall_disposition` as D22, plus a `page_type` retype on the D23 model. C2 does NOT implement the archived-body exclusion (that is Car G's retype to an excluded type). C2's `downweight` is for the LIVE `task_list` page; D38's archived bodies get a DIFFERENT disposition (`exclude`) via a different `page_type`. The two are complementary, not overlapping: live task list → downweight; archived task body → exclude.
- **D23** — supersede = retype `adr → adr_superseded`, atomic with the status flip. C2 does not touch `adr` types or the retype mutator. C2's scope is the `task_list` type only.
- **ADR-0209** — `page_type` is the policy lever, not tags. C2 aligns with this: the downweight disposition is keyed by `page_type` (`task_list`), resolved by `get_policy`, not by tag inspection.

## 8. Out of scope

- **The ADR status-driven exclusion** (D22's `superseded`/`rejected`/`deprecated → exclude`) — that is Car G (`adr → adr_superseded` retype + `adr_superseded` policy entry). C2 only delivers `task → downweight`.
- **D38's archived-task-body exclusion** — archived bodies get a `page_type` retype to an EXCLUDED type (Car G's retype mechanism), not downweight. C2's `downweight` is for the live `task_list` page.
- **Reimagining the identity gate** — that is C3 (3c, D21). C2 does not touch `gate_mode`.
- **Changing `is_recall_visible`** — no change. `is_recall_visible` already returns `True` for `downweight` (the `!= "exclude"` check at `:186` passes). The downweight path goes through a NEW function (`downweight_multiplier`), not through the visibility filter.
- **Penalizing `native_score` in `WikiProvider.candidates`** — deliberately NOT done. The provider's `native_score` is the raw retrieval score (an observation); the penalty is applied at the ranking-decision point (`placement_score` in fusion). This avoids double-penalization and keeps the provider score-agnostic. If a future caller needs the penalized score at the provider level, that is a separate change.
- **Making `RECALL_DOWNWEIGHT_FACTOR` page-type-specific** — the factor is a single global setting. If different downweighted types need different factors, that is a follow-up (add a `downweight_factor` field to `WikiPolicy`). One factor is sufficient for C2's scope (one downweighted type: `task_list`).
- **The `wiki_query` cache interaction** — the penalized `_retrieval_score` is stored in the cache (`:610`). This is correct (the penalty is deterministic per `page_type`, not per call), but if the factor is changed at runtime via config, stale cached entries hold the OLD factor's scores. The cache is epoch-folded (`:574` `_current_wiki_epoch()`), and a config change does NOT bump the wiki epoch — so stale downweight scores persist until a wiki write bumps the epoch. [VERIFY: acceptable, or bump epoch on config change? — see §9].

## 9. Risks / open questions

- **[VERIFY: penalty on `placement_score` vs `native_score` only]** — the chosen design multiplies the FULL `placement_score` (`ce + wiki_prior_weight * native_score`) by the factor, which penalizes the CE relevance term too. An alternative is to penalize only the `wiki_prior_weight * native_score` prior term, leaving CE untouched. The full-placement penalty is stronger (a downweighted page with high CE still sinks below an include page with moderate CE), which matches D22's intent (tasks shouldn't dominate recall). The prior-only penalty would be too weak (`wiki_prior_weight=0.1` makes the prior a small fraction of the score, so even a 0.0 factor barely moves the ranking). Confirm the full-placement penalty is intended; if CE relevance should be preserved for downweighted pages, switch to prior-only and accept the weaker effect.
- **[VERIFY: `wiki_query` cache staleness on factor change]** — the `wiki_query` cache (`:560-610`) is keyed on `(query, dir, category, tags, max_results, wiki_epoch)` — NOT on `RECALL_DOWNWEIGHT_FACTOR`. If the factor is changed at runtime via `YADGAR_RECALL_DOWNWEIGHT_FACTOR`, cached entries hold the old factor's penalized scores until a wiki write bumps the epoch. This is likely acceptable (config changes are rare; the cache is warm-only and a restart clears it), but if it is not, add `RECALL_DOWNWEIGHT_FACTOR` to the cache key or bump the wiki epoch on config change. The unified-recall path (fusion) is NOT affected — it reads the factor live from `settings` on every call, no caching.
- **[VERIFY: `task_list` page type after the spine]** — Car D (task tools) + Car E (task seed) move tasks to SQL and delete the task-list markdown page. After those cars ship, there may be no live `task_list` pages in the wiki. D38 says archived task bodies persist with a retype to an EXCLUDED type (not `task_list`). So the `task_list → downweight` mapping may be vestigial post-spine. This is acceptable: (a) C2 ships before D/E (C2 is rootless, D depends on B/C, E depends on D), so the downweight is live and useful in the interim; (b) keeping the mapping costs nothing (an unused `POLICY_BY_TYPE` entry); (c) if a future workflow re-creates `task_list` pages, the downweight is already correct. Confirm the mapping should stay rather than be removed in Car E.
- **[VERIFY: fusion import of `downweight_multiplier`]** — `fusion.py` is in `yadgar/backend/retrieval/providers/`; `downweight_multiplier` is in `yadgar/_shared/wiki/policy.py`. The import-linter contract (`pyproject.toml:303`) forbids `backend → core` edges but permits `backend → _shared` (both core and backend may import `_shared`, per `wiki_meta.py:36-38`). The existing `providers/wiki.py:17` already imports from `yadgar._shared.wiki.policy`, confirming the edge is allowed. No contract violation.
- **[VERIFY: `downweight_multiplier` in `_shared` importing nothing from `config`]** — the helper takes `factor` as a parameter (not reading `settings` internally), so `policy.py` stays config-agnostic. This preserves the `_shared` layering invariant (policy is pure routing logic; config is a separate concern). Confirm this is preferred over having the helper read the setting internally (which would couple `_shared/wiki` to `_shared/config`).
