# PLAN — Agent-Prompt Passive Library (REWORKED, ADR-0007)

Status: PLANNED (rework). Lands on **`feat/v5.85-train` BEFORE PR #125 merges** (PR #125
shipped the original #6 design — commit `fabab7e` on this branch). Source decision:
**ADR-0007** ([[yadgar-adr-log]]) — "Agent-prompt library rework — one-page + tag-aware
recall, close loop in-PR". Supersedes the v5.71 Tier-1 hook/semantic-tool design (kept in
git history) and the as-shipped Phase 1 (`agent_prompt_search` + slug-vN + bespoke tools).

> **GROUND-TRUTH AUDIT 2026-06-26 (verified against `feat/v5.85-train`, two
> Explore investigations + advisor feasibility vet).** Every file:line below was
> confirmed in current code. The previous #6 design shipped avoidable flaws
> (redundant slug-vN versioning forcing a cyclomatic-16 dedup; bespoke
> tools duplicating the recall path; **agent-prompt pages leaking into normal
> recall with no exclusion**). This rework fixes all three with ONE capability.

---

## 0. What is already shipped on this branch (the thing we rework)

Commit `fabab7e` ("feat(agent-prompt): Tier-1 passive library Phase 1") added, on
`feat/v5.85-train`:

| Artifact | Location | Fate in rework |
|---|---|---|
| `agent_prompt_save(pattern, content, …)` | `yadgar/server/tools/agent_prompts.py:69-163` | **KEEP** (retarget: one page per pattern, typed) |
| `agent_prompt_get(pattern)` exact-key, highest-vN | `agent_prompts.py:167-227` | **REMOVE as MCP tool**; logic folded into an internal slug-read helper |
| `agent_prompt_search(query, …)` semantic | `agent_prompts.py:264-334` | **REMOVE as MCP tool**; collapsed into `recall(type=wiki, tags=["agent-prompt"])` |
| `_best_prompts_by_pattern` (vN dedup, "extracted to keep cyclomatic ≤ I13") | `agent_prompts.py:230-260` | **DELETE** (no vN → no dedup) |
| `_slug_for` / `_VERSION_SUFFIX_RE` / `_PATTERN_FROM_SLUG_RE` | `agent_prompts.py:33,35,38` | **DELETE** (slug is `agent-prompt-<pattern>`, deterministic) |
| `_next_version` | `agent_prompts.py:43-65` | **DELETE** (wiki versioning carries history) |
| `storage.search_agent_prompt_vectors` (SQL tag PRE-filter, brute-force cosine) | `yadgar/storage/wiki.py:691-715` | **GENERALIZE** to arbitrary include-tag; becomes the include path |
| `AGENT_PROMPT_LIBRARY_ENABLED` knob (I25) | `config.py:810`, `config_registry.py:327`, `config_yaml.py:~1019` | **KEEP** (kill-gate carries over; no new knob) |
| `agent_dispatch_prelude` calling `agent_prompt_get(pattern)` exact-key | `dispatch_helper.py:114-116` | **REWIRE** to internal slug-read helper |
| Tests `test_agent_prompt_search.py`, `test_dispatch_helper.py`, `test_agent_prompts.py` | `yadgar/tests/` | **REWRITE RED-first** (slug + version asserts invert) |

---

## 1. One wiki page per pattern (drop `-vN`)

**Design.** Slug = `agent-prompt-<pattern>` (no version suffix). History/improvement is
carried by the **existing wiki versioning** (`WikiStore` snapshots every content change to
`wiki_page_version`; `get_max_version_for_page` at `storage/wiki.py:609`,
`list_wiki_page_versions` at `:620`). A second `agent_prompt_save(pattern, …)` on an
existing slug becomes a wiki **update** (new version row), not a new page.

**Why.** The shipped scheme writes a fresh page `agent-prompt-<pattern>-v<N>` per save
(`_slug_for` `:38-40`, `_next_version` `:43-65`), which forced `_best_prompts_by_pattern`
(`:230-260`) to re-derive "latest per pattern" on every read — the function whose own
docstring says it exists "to keep its cyclomatic complexity within I13 caps". Wiki versioning
already does exactly this, for free. Deleting vN deletes the dedup.

**Touches.** `agent_prompt_save` must switch from "insert new page" to "upsert by slug":
`WikiStore` has `get_wiki_page_by_slug` + an update path (`wiki_update` / `WikiStore.add`
already detects existing slug and versions). Confirm `wiki.add()` upserts-on-existing-slug
vs. needing an explicit `wiki_update` call — **OPEN verification for the builder** (test S-save-versions below pins it).

---

## 2. Typed `AgentPrompt` model + `page_type="agent_prompt"`

**Design.** Mirror the `ADR` model (`yadgar/models.py:226-271`). Add:

```python
# yadgar/models.py
class AgentPrompt(BaseModel):
    pattern: str            # slug stem: agent-prompt-<pattern>
    purpose: str            # one-line; feeds the TOC
    content: str            # the dispatch-prompt body
    # directory is a routing arg in agent_prompt_save, not part of the record
```

Register the page type in the existing registry (`yadgar/wiki_meta.py:24` `PAGE_TYPES`):

```python
"agent_prompt": ["Purpose", "Prompt"],   # required ## headings for wiki_lint
```

`agent_prompt_save` then passes `page_type="agent_prompt"` to `WikiStore.add` (the field is
already threaded: `wiki.py:595`, `wiki_add`/`wiki_update` accept it —
`server/tools/wiki.py:81`). `WIKI_SCHEMA_VERSION=1` (`wiki_meta.py:19`) is already stamped on
write — no new schema-version plumbing.

**Why.** Typed + page-typed pages become lintable (`wiki_lint` checks PAGE_TYPES headings,
`wiki_meta.py:56 check_page_type_format`) and consistent — the same discipline ADR-0003 P3
applied to ADRs. The `AgentPrompt` model is used as shape/post-validation **inside**
`agent_prompt_save`, NOT as the tool signature (FastMCP derives the schema from flat kwargs —
same note as `ADR` at `models.py:234-237`).

**Effort note.** Heat is NOT set on agent-prompt pages (the as-shipped save sets none, and
wiki pages have no heat field — heat is memory-only; corrected vs. the old plan's "set base
heat"). Reference content must NOT decay → wiki is the correct store (ADR-0007 rejected the
decaying-memory option). No heat work in this plan.

---

## 3. THE LOAD-BEARING ONE — tag-aware wiki retrieval (one capability, two uses)

### 3a. The problem, confirmed in code

Unified recall fuses wiki + memory:
`recall()` (`yadgar/server/tools/recall.py:371`) → `_fanout_recall` (`:179-295`) →
`WikiProvider.candidates` (`yadgar/retrieval/providers/wiki.py:56`) →
`WikiStore.query` (`yadgar/wiki.py:812`) → `fuse_candidates`
(`yadgar/retrieval/providers/fusion.py:171`).

- `WikiProvider` calls `self._wiki.query(query, max_results=limit)` **with NO tags**
  (`providers/wiki.py:56`). So **every agent-prompt page is a candidate in normal recall.**
  Because agent-prompt pages are `directory_context='global'` (saved with `directory="global"`),
  the leak hits **every project's** recall — real, currently-unmitigated noise.
- `recall()` has a `type` filter (`recall.py:380`, enum `{all,memory,wiki}`,
  `_VALID_RECALL_TYPES` `:23`) that selects **provider source**, but **NO tag filter**
  (signature `:371-382` has no `tags`).
- `WikiStore.query` (`wiki.py:812`) ranks the **GLOBAL** corpus first:
  `_collect_wiki_fts_scores` (`:840`→`search_wiki_fts_scored`, candidate pool `max_results*3`)
  + `_collect_wiki_vector_scores` (`:843`→`search_wiki_vectors` HNSW `<|K,40|>`, pool
  `max_results*3`), THEN applies a **POST-rank** tag filter on the already-ranked list
  (`:855-862`). For `max_results=5` the pool is ~15 globally-top pages; a rare agent-prompt
  page rarely enters it, so the post-rank tag filter sees a list that already excluded it →
  **dilution**. (`search_wiki_vectors` itself has NO tag param — `wiki.py:669-689`.)
- This dilution is exactly why the bespoke `search_agent_prompt_vectors` exists
  (`storage/wiki.py:691-715`): a **SQL PRE-filter** brute-force cosine over
  `WHERE tags CONTAINS 'agent-prompt'` — its own docstring cites avoiding "the global-pool
  dilution that the generic search_wiki_vectors HNSW path would suffer".

### 3b. Design — ONE capability (tag include/exclude on the wiki path), two uses

**Use A — general recall EXCLUDES `agent-prompt` (kills the leak).** There is no
exclude-from-recall field today. Add an **exclude** path; it is **clean and HNSW-free**:

- `agent-prompt` is a **rare** tag, so a **post-rank exclude** in `WikiStore.query` is
  dilution-safe (you drop at most a handful of slots from the ~15-candidate pool). Extend the
  existing post-rank filter loop (`wiki.py:855-862`) with an `exclude_tags` check.
- `WikiProvider` defaults `exclude_tags=["agent-prompt"]` when no include-tag is requested.
- `recall()` general path → WikiProvider excludes agent-prompt → leak gone.
- **Do NOT** add `AND tags CONTAINSNOT 'x'` to the HNSW `<|K,EF|>` query — SurrealDB's
  HNSW+predicate pre/post-filter ordering is the same unverified semantics that drove the
  brute-force workaround. Post-rank is cleaner here and sidesteps it. (Advisor's call.)

**Use B — targeted lookup `recall(type=wiki, tags=["agent-prompt"])` (collapses the bespoke
tools).** This MUST be a **SQL PRE-filter** (HNSW + a tag predicate does not compose cleanly
in SurrealDB — that's why `search_agent_prompt_vectors` is brute-force, comment at
`storage/wiki.py:697-701`):

- **Generalize** `search_agent_prompt_vectors` → `search_wiki_vectors_tagged(query_embedding,
  include_tag, top_k)` doing `WHERE tags CONTAINS $tag` SQL pre-filter (the mechanism already
  proven; just parametrize the literal `'agent-prompt'`).
- Thread a `tags: list[str] | None` param: `recall()` → `_fanout_recall` → `WikiProvider` →
  `WikiStore.query`. When `tags` is set, `query` routes the vector collector to the
  pre-filtered SQL path instead of the global HNSW path. **Requesting `tags=["agent-prompt"]`
  SUPPRESSES the default exclude** (include wins — explicit precedence, see test below).
- **FTS-include fork (pick explicitly): KEEP tagged wiki recall VECTOR-ONLY** — matches the
  as-shipped `agent_prompt_search` (vector-only via `search_agent_prompt_vectors`), avoids a
  second SQL-filter on `search_wiki_fts_scored`, and is the cheaper, lower-risk path. (If
  hybrid is later wanted, FTS has no HNSW constraint — `AND tags CONTAINS $tag` is trivial —
  but that is OUT of this plan.)

### 3c. FEASIBILITY VERDICT — **CLEAN COLLAPSE. No thin-search fallback needed.**

| Use | Mechanism | Feasibility | Evidence |
|---|---|---|---|
| **A — exclude** (general recall) | POST-rank in `WikiStore.query` (rare tag → dilution-safe) | **CLEAN** | extends existing post-rank loop `wiki.py:855-862`; no HNSW change |
| **B — include** (targeted) | SQL PRE-filter, generalize `search_agent_prompt_vectors` | **CLEAN** | mechanism already shipped (`storage/wiki.py:691-715`), just parametrize the tag |

Both `agent_prompt_get` and `agent_prompt_search` MCP tools are therefore **removed** (I32);
targeted lookup is `recall(type=wiki, tags=["agent-prompt"])`. The bespoke surface collapses.

**One thing that does NOT collapse into recall (advisor catch):** `agent_dispatch_prelude`'s
`agent_prompt_get(pattern)` is an **exact-key** lookup, and `recall` is **semantic** — not a
drop-in. With one-page-per-pattern the slug is deterministic (`agent-prompt-<pattern>`), so the
replacement is a **direct slug read** (`WikiStore.get_wiki_page_by_slug`), kept as an
**internal helper** in `dispatch_helper.py` / `agent_prompts.py` — NOT an MCP tool, NOT recall.
"Remove the tool surface" ≠ "remove the lookup mechanism".

**Advisor exchange (verbatim summary).** Advisor confirmed the verdict and split the mechanism
by direction: *INCLUDE must be SQL pre-filter (HNSW+predicate doesn't compose — that's why the
brute-force path exists); EXCLUDE should be post-rank in `query()` because agent-prompt is rare
and that sidesteps the unverifiable HNSW+AND ordering — cleaner than the SQL-exclude I first
proposed.* It flagged three gaps I'd missed: (1) `agent_dispatch_prelude` exact-key ≠ semantic
recall → keep an internal slug read; (2) **default-exclude precedence** (requesting the tag must
suppress the default exclude) needs its own RED test; (3) the **FTS-include fork** must be picked
explicitly (chose vector-only). All folded above. No blocker; the 4-file tags thread (recall →
`_fanout_recall` → WikiProvider → `query`) is effort **M**.

---

## 4. Discovery surface (passive)

Mirror the ADR-log surface pattern exactly.

1. **`agent-prompt-toc` wiki page** — global slug, auto-maintained. On every
   `agent_prompt_save`, upsert a one-line `pattern → purpose` row into the TOC page
   (purpose from the `AgentPrompt.purpose` field / first heading). Pattern: same as
   `adr_add` appending to `<project>-adr-log` (`server/tools/adr.py:140-284`,
   header-scan + `wiki_append_section`).
2. **Global memory anchor** pointing at the TOC — `anchor(content="Agent-prompt library TOC:
   [[agent-prompt-toc]] — reusable subagent dispatch prompts; recall(type=wiki,
   tags=['agent-prompt']) to pull one", context="global", reason="agent-prompt-library")`.
   Stored `directory_context='global'` (`misc.py:382-467`), surfaces in **every** project's
   `project_brief(mode="restore")` via `top_anchors_global`
   (`server/tools/project.py:249-252`, `_build_anchor_rows_restore` `:651-703` with `scope`
   field).
3. **project_brief surfaces the TOC** — add `_build_agent_prompt_toc(resolved)` mirroring
   `_build_adr_log` (`project.py:1650-1676`): return `{slug:"agent-prompt-toc",
   patterns:[…]}` into the restore dict (`_project_brief_restore`, key alongside `"adr_log"`).
   Catalog mode omits it (deprecated, like ADR). Surfacing the global TOC in restore is the
   passive-discovery hook.
4. **`agent_dispatch_prelude` pulls the matching prompt** — already wired; rewire the
   exact-key `agent_prompt_get(pattern)` (`dispatch_helper.py:114-116`) to the internal
   slug-read helper (point 3c). Behavior preserved; budget/cap logic untouched
   (`dispatch_helper.py:121-125`).

---

## 5. Capture loop in-PR (no SubagentStart hook)

- **Manual `save`** — `agent_prompt_save` is the capture surface (kept, retargeted).
- **project_brief NUDGE** — add `_apply_agent_prompt_signal(resolved, storage, actions)`
  mirroring `_apply_adr_signal` (`project.py:1428-1494`, wired at `:1569-1570`): when session
  activity exists but the library is empty/stale for this dir, append a
  `{"action": "save_agent_prompt", "suggested_call": "agent_prompt_save(directory=…,
  pattern='…', content='…')"}` recommended action. This closes the loop in-PR.
- **SubagentStart auto-capture stays explicitly OUT** (future polish; the original plan's
  Phase-2 hook is deferred — surface needs no hook, capture is manual+nudge per ADR-0007).

---

## 6. Migration — collapse `agent-prompt-<pattern>-v<N>` → one page per pattern

Any pages already written by the as-shipped save on this branch carry the `-vN` slug. A
one-shot migration (a `migration_0NN` under the existing migration framework, or a guarded
startup step):

1. `SELECT slug, content, directory_context, updated_at FROM wiki_page WHERE tags CONTAINS
   'agent-prompt'`.
2. Group by pattern (strip `-v\d+$`). For each pattern, take the **highest-version** content
   as canonical.
3. Write a single `agent-prompt-<pattern>` page (typed, `page_type='agent_prompt'`) with that
   content; (optionally seed `wiki_page_version` rows from the old vN pages to preserve
   history — OPTIONAL, low value).
4. Delete the old `-vN` pages + their `wiki_page_version`/`wiki_crossref` rows.

**Risk LOW** in practice — this branch is pre-merge and dogfood-only; the population of `-vN`
pages is small/empty. Migration is mostly defensive. Idempotent (no-op when no `-vN` slugs).

---

## 7. SEEDING — ship STARTER content so a fresh install isn't empty

**Key finding (investigator-2):** `seed_project` (`server/tools/misc.py:668-700` →
`yadgar/seed/_generate.py:339`) and `bootstrap_project` (`project.py:1844-1880`) write
**MEMORIES only** (`_seed`-tagged, directory-scoped), **never wiki pages or anchors**. The
install script (`scripts/install/yadgar-setup.sh:641-663` step 10) seeds **global anchors**
from `seeds/anchors.yaml` via the CLI. **There is no starter-wiki path today.** So the library
seed is a NEW, GLOBAL, one-time path — it must NOT ride the per-project file scanner.

**Design — a global library-seed step:**

1. **Starter content module** — `yadgar/seed/agent_prompts.py` holding 2-4 genuinely useful
   GENERIC starter patterns as `(pattern, purpose, content)` tuples. Proposed starters
   (provider-agnostic, broadly reusable):
   - `investigate-codebase` — read-only locator/mapper: "find where X lives, return file:line,
     no fixes."
   - `review-diff` — diff/PR reviewer: severity-tagged one-line findings, no scope creep.
   - `surgical-edit` — bounded 1-2 file change with test-first + loop-until-clean.
   - `plan-feature` — architect: failing-tests-first plan, files-to-change, risks, sequencing.
2. **Seed entry point** — a `seed_agent_prompt_library(storage, force=False)` function that,
   when the library is empty: writes each starter via the (retargeted) `agent_prompt_save`
   path with `directory="global"`, `page_type="agent_prompt"`; builds the `agent-prompt-toc`
   page; creates the global anchor (point 4). Idempotent — skip patterns that already exist.
3. **Where it runs** — wire into the install path **alongside** the existing anchor seed
   (`yadgar-setup.sh` step 10 `_step_seed_anchors`): add a `_step_seed_agent_prompts` calling
   a CLI subcommand (`yadgar seed --agent-prompts`) or a `seed_project`-sibling MCP tool.
   Prefer the **install-script step** (one-time global) over `seed_project` (per-project,
   memory-only, runs on every project). A fresh user then has library + TOC + discovery anchor
   from first launch.

**Open decision (flag to user):** put the starter seed in (a) the install script step
[recommended — global, one-time, matches anchor-seed precedent], or (b) a new `power=True` MCP
tool `seed_agent_prompt_library()` the user calls once, or (c) both (tool used by the script).

---

## TDD plan (RED-first — write failing tests before implementation)

Test files are under `yadgar/tests/`. **Several existing assertions INVERT** — list them as
RED-first rewrites, not "updates":

### Tests to REWRITE (existing asserts now wrong)
- `test_agent_prompt_search.py::S6` asserts slug `agent-prompt-dispatch-fix-bug-v2`
  (`:166`) and `version==2` (`:164`) → **invert**: slug `agent-prompt-dispatch-fix-bug`
  (no vN), second save = wiki **version 2 of the same page**, `agent_prompt_get` tool gone →
  rewrite as internal-slug-read / `recall(tags=)`.
- `test_agent_prompt_search.py::S1/S7` (`agent_prompt_search` semantic + dilution) → **port**
  to `recall(type="wiki", tags=["agent-prompt"])`; S7 dilution-guard becomes the **include
  pre-filter** test (relevant prompt survives 40 unrelated pages).
- `test_dispatch_helper.py::test_includes_version_label` asserts `"v2" in prelude` (`:84`) →
  **invert**: prelude references `agent-prompt-<pattern>` (no version label) or the wiki
  version count; rewire to the slug-read helper.

### NEW failing tests (the rework contract)
1. **Save = one page, wiki-versioned.** Two `agent_prompt_save("p", …)` → ONE page
   `agent-prompt-p`, `wiki_page_version` count == 2, latest content wins. (Pins point 1.)
2. **Typed page.** Saved page has `page_type=="agent_prompt"`, `wiki_schema_version==1`,
   and `wiki_lint` passes its PAGE_TYPES headings. (Point 2.)
3. **Use A — general recall EXCLUDES agent-prompt.** With an agent-prompt page present,
   `recall("…anything…", type="all")` and `type="wiki"` return NO agent-prompt page. (Point 3a/b.)
4. **Use B — targeted include.** `recall("audit this PR for vulns", type="wiki",
   tags=["agent-prompt"])` returns the security-review prompt above the corpus; every result
   carries the `agent-prompt` tag. (Point 3b.)
5. **PRECEDENCE (advisor).** Single test, two asserts: general `recall()` does NOT return an
   agent-prompt page; `recall(tags=["agent-prompt"])` DOES — the requested tag suppresses the
   default exclude. (Point 3b, the actual contract of #3.)
6. **Dilution-safe include.** 40 unrelated wiki pages present → the one relevant agent-prompt
   still surfaces via the SQL pre-filter. (Ports S7.)
7. **dispatch_prelude slug-read.** `agent_dispatch_prelude("p", …)` embeds the
   `agent-prompt-p` content via the internal slug read (no `agent_prompt_get` tool, no
   semantic recall). Pattern-not-found → graceful. (Point 4.4.)
8. **Discovery surface.** After `agent_prompt_save`, `agent-prompt-toc` page lists
   `pattern → purpose`; the global anchor exists (`directory_context='global'`);
   `project_brief(mode="restore")` surfaces both TOC and the global anchor in an unrelated
   project dir. (Point 4.)
9. **Nudge.** `project_brief(mode="signals")` emits a `save_agent_prompt` action when the
   library is empty + session-active; silent when populated. (Point 5.)
10. **Migration.** Given `agent-prompt-p-v1`/`-v2` pages, migration collapses to one
    `agent-prompt-p` with v2's content; old pages gone; idempotent re-run no-ops. (Point 6.)
11. **Seed.** `seed_agent_prompt_library` on an empty store creates N starter pages + TOC +
    global anchor; re-run skips existing (idempotent). (Point 7.)
12. **I25 three-way sync** (`test_config_three_way_sync`) still green — no NEW knob added (the
    `AGENT_PROMPT_LIBRARY_ENABLED` gate carries over). Assert the kill-gate still makes the
    tagged recall path inert when False.
13. **I32 tool-surface** — `agent_prompt_get` and `agent_prompt_search` are NOT in the MCP
    schema / `tools.__all__`; `agent_prompt_save` IS. (See I32 below.)

### I25 (config knobs)
- **No new knob.** Reuse `AGENT_PROMPT_LIBRARY_ENABLED` (`config.py:810`,
  `config_registry.py:327`, `config_yaml.py FIELD_META ~:1019`). When False, the tagged-recall
  include path returns nothing and the nudge stays silent. Three-way sync test unchanged.

### I32 (tool surface — tools being REMOVED; there is NO separate CAP manifest)
Tools register via `@_tool()` side-effect + `__init__.py`. To **remove** `agent_prompt_get`
and `agent_prompt_search`:
1. Delete the two functions + their `@_tool()` decorators in `agent_prompts.py` (keep
   `agent_prompt_save`; move `agent_prompt_get`'s lookup logic into a private
   `_read_agent_prompt(slug)` helper, no decorator).
2. Remove their named imports `yadgar/server/tools/__init__.py:107-108` (`agent_prompt_get`,
   `agent_prompt_search`).
3. Remove their entries from `__all__` `:188-189`.
4. FastMCP regenerates the `/api/mcp` schema from live `@_tool()` registrations — removal is
   immediate, no cache. Test 13 asserts the schema no longer exposes them.

### Contracts touched
- `recall()` MCP signature gains `tags: list[str] | None = None` (additive, back-compat).
- `WikiStore.query`, `WikiProvider.candidates`, `_fanout_recall` gain `tags` / `exclude_tags`
  threading (internal).
- `storage.search_agent_prompt_vectors` → renamed/generalized `search_wiki_vectors_tagged`.
- `agent_prompt_save` output: `slug` no longer ends in `-vN`; `version` reflects the wiki page
  version. **MCP output-shape change** — note in changelog.
- New `page_type` value `agent_prompt` in PAGE_TYPES (additive).
- `project_brief(restore)` dict gains an `agent_prompt_toc` key; `signals` gains a
  `save_agent_prompt` action.

---

## Sequencing, effort & risk

| Step | Scope | Effort | Risk | Notes |
|---|---|---|---|---|
| **S1. Model + page_type** | `models.py` AgentPrompt, `wiki_meta.py` PAGE_TYPES | S | LOW | pure additive, mirrors ADR |
| **S2. Save → one page, typed** | `agent_prompt_save` retarget (slug, upsert, page_type); delete vN helpers + dedup | M | MED | confirm `wiki.add` upserts-on-existing-slug vs needs `wiki_update`; RED tests 1-2 |
| **S3. Tag-aware recall** (load-bearing) | thread `tags`/`exclude_tags`: `recall`→`_fanout_recall`→`WikiProvider`→`query`; generalize SQL pre-filter; post-rank exclude | M | **MED-HIGH** | 4 files; precedence + exclude correctness; RED tests 3-6 the gate |
| **S4. Remove bespoke tools** (I32) | delete get/search tools + imports + `__all__`; internal slug-read helper | S | LOW | RED test 13 |
| **S5. dispatch_prelude rewire** | `dispatch_helper.py` → slug-read helper | S | LOW | RED test 7; invert `test_includes_version_label` |
| **S6. Discovery surface** | TOC page upsert, global anchor, `project_brief` restore + nudge | M | MED | mirror `_build_adr_log` + `_apply_adr_signal`; RED tests 8-9 |
| **S7. Migration** | `-vN` → one page collapse | S | LOW | idempotent; small/empty population; RED test 10 |
| **S8. Seed starter content** | `yadgar/seed/agent_prompts.py` + seed entry + install wiring | M | MED | RED test 11; **user decision on placement** |
| **S9. Loop-until-clean** | full suite, lint, I13/I25/I32 invariants | S | LOW | inverted tests must go green |

**Recommended order:** S1 → S2 → S3 → S4 → S5 → S6 → S7 → S8 → S9. S3 is the keystone —
land and prove it (tests 3-6) before S4 removes the bespoke tools, so the collapse target
exists first.

**Overall: effort M (one car + contingency), risk MED.** The HNSW-uncertainty risk is retired
by the include=SQL-pre-filter / exclude=post-rank split (advisor-vetted). Residual risk is
wiring correctness across the 4-file tags thread and the precedence rule.

---

## Decisions needing the user

1. **Seed placement (point 7).** Install-script step [recommended] vs. a one-shot
   `power=True` MCP tool vs. both. Affects whether a fresh install auto-seeds or requires one
   user call.
2. **Starter set (point 7).** Confirm the 4 proposed generic patterns
   (`investigate-codebase`, `review-diff`, `surgical-edit`, `plan-feature`) or substitute.
3. **Migration history (point 6).** Preserve old `-vN` content as `wiki_page_version` history
   on the collapsed page, or just keep latest + drop the rest? (Recommend: keep latest only —
   the `-vN` population is dogfood-only.)
4. **`recall` output for tagged wiki hits.** Confirm tagged agent-prompt recall returns the
   same candidate shape as normal wiki recall (so no special-casing downstream).

---

## What this rework explicitly does NOT do (carried from ADR-0007)

- SubagentStart auto-capture hook (deferred future polish).
- Tier-2 Claude-in-loop auto-improve (separate, gated behind dogfood).
- Heat on agent-prompt pages (reference must not decay → wiki, no heat).
- The 2-3 week dogfood kill-gate still governs: if the library is never reached for, rip the
  surface. (ADR-0007 revisit_trigger.)
