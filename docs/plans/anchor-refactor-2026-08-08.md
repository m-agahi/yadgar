# Anchor refactor — train plan

> Plan doc. Created 2026-08-08. **Runs AFTER the ADR-0215 branch-removal train lands** (see §0.3).
> Sources: wiki `anchor-data-model-audit-2026-07-24-redundancy-time-box-cull-gap` (page 7558) —
> re-verified 2026-08-08, **its §2 central claim is falsified by the live corpus, see §0.2**;
> wiki `yadgar-anchor-memory-design-scopes-and-surfacing` (page 6375).
> Binding: ADR-0081 (plan archives in the completing car), ADR-0083 (signal⇔audit parity),
> ADR-0218 (cars never run the full unit suite).

## 0. Reading this doc

Every car is a checklist. Line numbers were captured **2026-08-08 against `eb2d1be1`** and will rot.
Where a car is a sweep, the entry gives the regenerating command instead.

Standing per-car requirements (not repeated per car):
- version bump (`scripts/check_version_bump.py`) + `CHANGELOG.md` unreleased entry
- backend version bump if the car touches `yadgar/backend/**` (`check_backend_bump.py`, ADR-0080)
- `pre-commit run --all-files` green at the car boundary (measured ~11s, ADR-0218)
- **ADR-0218: no car runs the full unit suite.** Testing obligation = targeted tests derived from
  the symbols changed, plus `make e2e` only when runtime behaviour changed.
- ADR-0081: the car that completes this plan does `git mv docs/plans/anchor-refactor-2026-08-08.md docs/plans/archive/`

### 0.1 Re-verification of the 2026-07-24 audit's coordinates

The audit self-flagged "RE-VERIFY before building — code moves". Five ADR-0215 cars plus an
engine-#2 train have landed since. Every cited coordinate was re-checked on 2026-08-08.

| audit citation | today | verdict |
|---|---|---|
| `validate.py:107-108` (`_anchor` tag append) | `yadgar/_shared/write_exec/validate.py:107-108` | **EXACT** |
| `validate.py:109-110` (`anchor:<reason>`) | same file, 109-110 | **EXACT** |
| `validate.py:59-60` (default `conditional`) | same file, 59-60 | **EXACT** |
| `memory.py:554` (decay exclusion) | `yadgar/_shared/storage/memory.py:554` | **EXACT** |
| `heat_decay.py:130` | `yadgar/backend/consolidation/heat_decay.py:130` | **EXACT** |
| `project.py:1075-1082` (signals count, tag-only) | `yadgar/core/server/tools/project.py:1075-1082` | **EXACT** |
| `project.py:804` (migration_grace reader) | same file, 804 | **EXACT** |
| `project.py:596-622` (catalog top_anchors) | `_build_anchor_rows_catalog` at 590; global query 596-599, project query 615-622 | **EXACT-ish** (span holds) |
| `audit.py:152` / `:220` (grace readers) | `yadgar/core/server/tools/audit.py:152`, `:220` | **EXACT** |
| `audit.py:516` (semantic_immortal forget-guard) | same file, 516 | **EXACT** |
| `admin_exec/audit.py:60` (the DELETE) | `yadgar/backend/admin_exec/audit.py:60` = `storage.delete_memory(mid)` | **EXACT** |
| `server_helpers.py:137-138` (ttl_days) | `yadgar/_shared/server_helpers/server_helpers.py:137-138` | **EXACT** |
| `server_helpers.py:141-146` (TTL defaults) | 142 (conditional/90) and 145 (ephemeral/14) | **EXACT-ish** |
| `thermodynamics.py:190-193` (importance decay rate) | `yadgar/_shared/thermodynamics/thermodynamics.py:190-191` | **EXACT-ish** |
| `audit.py:150` (expiry fetch) | `_fetch_expired_rows` at 143; the grace clause is 152 | **approximate**, substance intact |
| `server_helpers.py:129-147` (`_compute_valid_until`) | function header is at **113**, not 129; body span 129-147 is right | **header DRIFTED −16** |
| `_phase_post_write.py:126-132` (`_zero_gap_2_protection`) | function at **124**; the explicit-anchor branch is 126-132 | span holds, **but the audit stopped one branch short — see §0.2 door 4** |
| `checkpoint_restore.py:408-410` | `yadgar/backend/restoration/checkpoint_restore.py:408-410`. `yadgar/_shared/restoration/checkpoint_restore.py` is now a **29-line PEP-562 shim** (T2 Car B) | line EXACT, **path must be qualified or you edit a shim** |
| `migrations.py:239` (grace write) | **DRIFTED.** `:225` = `DEFINE FIELD ... migration_grace`; `:239` is now `migrated = 0`; the actual `SET ... migration_grace = $grace` is at **:244** | **DRIFTED** |
| `migrations.py:237-239` ("leaves tag WITHOUT is_protected") | span now covers comment + SELECT at 237-238; the proving UPDATE is 242-246 | **DRIFTED — and the claim's premise is FALSIFIED, see §0.2** |

**Verdict on drift:** the coordinates held up better than expected — one real drift
(`migrations.py`), one function-header slip (`server_helpers.py`), one path ambiguity that
would send an editor into a shim (`checkpoint_restore.py`). The audit's *reasoning* is sound
everywhere except §2, where the failure is not line drift but a **factually inverted premise**.

### 0.2 THE HEADLINE — the audit's §2 collapse recommendation is falsified by the corpus

The audit's §2 says: *"`_anchor` tag → COLLAPSE into `is_protected` … reads DIVERGE …
migration_008 leaves tag WITHOUT is_protected … backfill is_protected on legacy tag-only rows;
backfill FIRST."*

Measured on the live DB, 2026-08-08 (`db_inspect`, read-only), and **independently re-verified by
the orchestrator the same day**:

```
SELECT count() FROM memory WHERE '_anchor' INSIDE tags
  AND (is_protected = false OR is_protected IS NONE)   ->   0
SELECT count() FROM memory WHERE is_protected = true
  AND '_anchor' NOTINSIDE tags GROUP ALL               -> 101
```

1. **The backfill the audit ordered as a prerequisite has an empty population.** Zero rows are
   tag-without-protection. That whole step is a no-op today.
2. **The divergence runs the other way, 101 rows deep, and it is BY DESIGN.** Tag census of
   those 101:

   | tags | n | what it is |
   |---|---|---|
   | `_dir_branch_context` | 34 | per-directory git state singleton |
   | `_dispatch_prelude` | 30 | subagent dispatch marker |
   | `_active_work` | 25 | the `update_active_work` singleton |
   | `_prompt_usage` | 1 | prompt-usage counter |
   | `_action_stream` + `_auto` | 1 | action-stream row |
   | assorted content tags | ~10 | `DECISION_AUTO_PROTECT` hits (see door 4) |

   All 101 have `valid_until IS NONE` — they never expire.

   These rows use `is_protected` purely as a **decay-exemption mechanism**. They are not anchors
   and must never surface as anchors.

3. **Consequence — read-unifying onto `is_protected` is a live corruption vector, not a
   theoretical risk.** Every anchor surfacing and audit query keys on `'_anchor' INSIDE tags`.
   Repoint them at `is_protected` and the yadgar anchor set goes **146 → 247**;
   `audit_anchors` starts offering to `forget` `_active_work`; `restore()` surfaces
   `"dispatch_prelude marker"` as a compaction-proof fact.
4. **Do NOT transcribe the audit's proposed `check_invariants` assert.** It says
   "assert (tag ⟺ is_protected)". That assertion fails on 101 legitimate rows on its first run.
   Shipping it ships a broken guard.

**Honest reading:** `is_protected` means *decay-exempt*; `_anchor` means *is an anchor*. The
corpus proves a 101-row by-design difference. **They are not redundant.** Either keep both with
documented distinct meanings, or split into `decay_exempt` + `is_anchor` — a materially larger
migration than the audit scoped. **That is a user decision, taken in Car 7, not here.**

### 0.3 Why this train runs AFTER the ADR-0215 branch-removal train

Three independent reasons, all concrete:

1. **Two one-way migrations with overlapping predicates on the same table.** ADR-0215 Car 8 is a
   user-gated, one-way migration over `memory` rows *split by `is_protected`*
   (67 unprotected/no-tier, 6 unprotected/ephemeral, 18 protected/conditional,
   3 protected/semantic_immortal as of 2026-08-07). This plan's Car 6 and Car 8 mutate the same
   column family on the same table. Interleaving them makes "which train deleted this row"
   unanswerable, and neither backup restores cleanly past the other.
2. **Residue-proof pollution.** ADR-0215 Car 10's completion proof diffs identifier counts
   against Car 0 baselines captured 2026-08-07. Anchor-field churn in `validate.py`,
   `project.py`, `audit.py` and `misc.py` — all files that train edits — moves those counts and
   makes the diff unreadable.
3. **The stop-template byte-pin is mid-edit.** `yadgar/tests/hooks/test_stop_hook_template.py:42`
   pins `stop_checkpoint_prompt.md` byte-for-byte. Car 3 of this plan introduces the *same* pin
   pattern for `anchor_audit_prompt.md`. Two agents editing template-pin tests concurrently is a
   guaranteed conflict.

**Single exception:** Car 1a (the grace-cliff review list) is **zero code — pure `db_inspect` +
tool calls** and touches none of the above. It has a hard external deadline of
**2026-08-26T14:58:02Z** and MAY be run immediately, independently, today. See Car 1.

### 0.4 Measured baseline (live DB, 2026-08-08, read-only)

| measurement | value |
|---|---|
| `_anchor` rows, `/home/max/git/yadgar` | **146** |
| `_anchor` rows, `global` | **9** (155 total) |
| `_anchor` rows across the top-10 directories | ≥344 |
| `_anchor` **without** `is_protected` | **0** |
| `is_protected` **without** `_anchor` | **101** (all `valid_until IS NONE`) |
| yadgar anchors carrying an `anchor:<reason>` tag | **66 / 146** — 80 carry none |
| yadgar anchors with `tier IS NONE` **and** `valid_until IS NONE` | **15** — permanent, never expire |
| yadgar anchors `tier=conditional`, no grace | 67 |
| yadgar anchors `tier=conditional` + `migration_grace` | **61** |
| yadgar anchors `tier=semantic_immortal` | 3 |
| **`heat` distribution across all 146 yadgar anchors** | **one group: `heat = 1.0`, n = 146** |
| `migration_grace = true` rows, all directories | **174** |
| distinct `valid_until` among those 174 | **one value: `2026-08-26T14:58:02.283359+00:00`** |
| yadgar anchors expiring before 2026-08-15 | 0 |

### 0.5 Verdict on wiki page 6375 (unconditional surfacing) — **LIVE, via a different mechanism**

`docs/plans/archive/PLAN_V5_19_0_ANCHOR_SURFACING.md` is archived "SHIPPED v5.19.0". Checked:

- The mechanism 6375 blamed — *relevance* rank-filtering of anchors — **is gone.** Every anchor
  path is now `ORDER BY heat DESC`; no relevance scoring touches them.
- The two-query global/project scope split **shipped**:
  `memory.py::get_anchored_memories_scoped` (`_cap = min(limit, 50)`), wired into
  `checkpoint_restore.py:408`; plus `project.py::_build_anchor_rows_restore` (646) and
  `_build_anchor_rows_catalog` (590).

**But the concern is not closed.** The design said "no rank-filter, hard cap 50 each". What shipped:

- `restore()` calls `get_anchored_memories_scoped(limit=REPLAY_MAX_RESTORE_MEMORIES)`.
  `REPLAY_MAX_RESTORE_MEMORIES = 8` (`config.py:147`). `min(8, 50) = 8`. **The design's 50 is
  never reached — the caller's 8 dominates.** So restore shows **8 of 146** project anchors.
- `project_brief(mode="restore")` truncates `merged[:PROJECT_BRIEF_MAX_ANCHORS]`,
  **`= 12`** (`config.py:707`). **12 of 155.**
- **And the ordering is fully degenerate.** All 146 yadgar anchors have `heat = 1.0` exactly
  (anchors get `REPLAY_ANCHOR_HEAT = 1.0` and `is_protected` excludes them from decay, so heat
  never moves). `ORDER BY heat DESC LIMIT 8` over a 146-way tie returns **an arbitrary 8**,
  chosen by DB row order and not stable across calls.

**Verdict: LIVE.** The original failure — *"I forgot anchor X exists"* — is reproducible today.
The cause changed from *ranked out* to *arbitrarily truncated out of a degenerate tie*.
The **treatment is corpus shrinkage** (Cars 1, 4, 5, 6 here), not raising the cap: 8-of-30 with a
high bar beats 12-of-155 with none. **The redesign is explicitly NOT in scope; a deterministic
tiebreak is Open Question Q3.**

### 0.6 Concern #3 root cause — the hook template's gathering step, root-caused

`yadgar/core/hooks/templates/anchor_audit_prompt.md:18-21` instructs:

```
recall("_audit_anchors sentinel anchor hygiene", directory="{directory}",
       tags=["_anchor"], max_results=25, type="memory")
```

**`tags=` is inert for memory results.** Traced:

- `yadgar/core/server/tools/recall.py:358-360` — the docstring says so outright:
  *"tags: Tag include filter for **wiki** results."*
- `yadgar/backend/retrieval/recall_pipeline.py:436` — `tags` is passed **only** to
  `WikiProvider(...)`.
- `recall_pipeline.py:422-425` — `MemoryProvider(retriever, profile, deadline)` never receives it.
- With `type="memory"`, the wiki provider is not even constructed, so `tags` is used **nowhere**.

So the call is a plain semantic search whose top hits are whatever text resembles
`"_audit_anchors sentinel anchor hygiene"` — which is why the 2026-08-08 pass returned five
auto-abstracted cluster summaries that merely *contain* the string in generated prose, none
tagged, none protected, against 155 real anchors. **Every audit run mis-reports until this is
fixed**, and the retire decisions are made on the wrong rows.

The existing guard does not catch it: `yadgar/tests/hooks/test_v5_158_anchor_audit_scheduler.py:151-165`
asserts only `"de_anchor" in low` and a loose empty/stop substring pair. **It passed with the
broken call in place.** Textbook vacuous pass.

---

## 1. Car list (dependency order)

| # | Car | Type | Blocks / blocked by |
|---|---|---|---|
| 0 | Preflight & baselines (read-only) | — | blocks all |
| 1 | **Grace-cliff defusal** — dated 2026-08-26 | data (user-gated) | 1a runs NOW, independently; blocks 8 |
| 2 | Cull surface — `audit_anchors(enumerate=True)` | **additive** | blocks 3, 6 |
| 3 | Anchor-audit template repoint + byte-pin | fix | needs 2 |
| 4 | **Admission bar** — close the four doors | **additive gate** | independent of 2/3 |
| 5 | Time-box promotion + `anchors_expiring_soon` | **additive** | independent |
| 6 | Corpus review pass (155 anchors) | data (user-gated) | needs 2, 3 |
| 7 | Field-collapse **ADR** — decision only, no code | decision | needs 0.2, 6 |
| 8 | `migration_grace` field drop (migration) | **migration** | needs 1 complete AND date > 2026-08-26 |
| 9 | `tier` → `is_immortal` | **migration** | **CONDITIONAL** on Car 7 |
| 10 | Docs, ADR amendments, residue proof, archive | — | last |

**B-then-A honoured (audit's own recommendation):** Cars 1–6 are additive surfaces, signals and
user-gated data review with zero schema change. The first schema mutation is Car 8, and it is
gated on both a completed review and a calendar date. Car 9 does not start until an ADR says so.

---

## Car 0 — Preflight & baselines

**Scope:** no production code. Capture the numbers Car 10's proof diffs against, and re-check
coordinates one more time (this doc's own §0.1 will be ~2 weeks stale by the time the train runs).

- [ ] Re-run the §0.1 coordinate table. Any row that moved gets a `[CORRECTION]` line in this doc.
- [ ] Re-capture every §0.4 measurement via `db_inspect`. Record the date and the exact queries.
      **Note:** SurrealDB `count()` without `GROUP ALL` returns per-row counts, not an aggregate —
      always write `SELECT count() AS n FROM ... GROUP ALL`. An aggregate-shaped query without it
      silently returns N rows of `1`.
- [ ] Residue baseline:
      ```
      for id in _anchor is_protected migration_grace ANCHOR_CONDITIONAL_TTL_DAYS \
                DECISION_AUTO_PROTECT _DECISION_STRONG_RE semantic_immortal \
                PROJECT_BRIEF_MAX_ANCHORS REPLAY_MAX_RESTORE_MEMORIES; do
        n=$(git grep -c "$id" -- yadgar/ scripts/ | awk -F: '{s+=$NF} END{print s+0}')
        echo "$id: $n"; done
      ```
- [ ] `python scripts/check_test_weakening.py --ci --base origin/master` on a clean tree; record green.
- [ ] Confirm `docs/plans/0115-pre-migration-backup-2026-08-01.md` is executable — Cars 6, 8, 9 need it.
- [ ] Confirm the ADR-0215 train is **merged and its plan archived** (ADR-0081). If not, STOP: §0.3.

**Exit criterion (positive evidence):** a committed table in the CHANGELOG unreleased section
carrying all 9 residue counts, all 14 §0.4 measurements, the ADR-0215 merge commit SHA, and the
date. Car 10's proof is a *diff against these*, not an unanchored claim.

**Could this pass while doing nothing?** Yes — it is inventory, and inventory always "succeeds".
Prevented by making the output a committed artifact of concrete numbers rather than a claim, and
by requiring the ADR-0215 merge SHA, which cannot be fabricated from an unmerged tree.

**Rollback:** nothing to roll back.

---

## Car 1 — Grace-cliff defusal (DATED — 2026-08-26T14:58:02Z)

**Why this is not "sunset a dead field".** The audit says *"`migration_grace` → SUNSET, dead after
2026-08-26"*. It is not dead — it is a **synchronised cliff**. Measured 2026-08-08, re-verified by
the orchestrator the same day:

- **174** rows carry `migration_grace = true`, across all directories. **61 in yadgar.**
- All 174 share **one** `valid_until`: `2026-08-26T14:58:02.283359+00:00` (migration_008 stamped
  `now + ANCHOR_CONDITIONAL_TTL_DAYS` in a single pass). A `GROUP BY valid_until` returns exactly
  one row.

At that instant, three things happen and **none of them is visible**:

1. **They stop surfacing.** Every anchor query filters `(valid_until IS NONE OR valid_until > $now)`
   — `memory.py:1104`, `:1133`, `:1141`; `project.py:600`, `:620`, `:657`, `:668`; `project.py:1079`.
   The yadgar anchor set silently drops 146 → 85.
2. **No signal fires.** `_fetch_expired_anchor_count` (`project.py:788-810`) excludes grace rows at
   `:804`. That exclusion is **deliberate and correct** — ADR-0083 established signal⇔audit parity,
   and grace rows produce always-skipped audit entries. Correct parity, silent cliff.
3. **Nothing deletes them.** `_build_expire_actions` → `_fetch_expired_rows` excludes grace at
   `audit.py:152`. They become invisible, undeleted zombies, permanently.

`_build_verify_grace_actions` (`audit.py:527-554`) *does* surface them — as user-gated,
always-skipped `verify_grace_expired_anchor` entries, capped by
`ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN = 20`. 174 rows through a 20-per-run always-skipped channel is
not a defusal path.

**This date does not wait for the ADR-0215 train.**

### Step 1a — review list (ZERO CODE; may run today, independently)

- [ ] `db_inspect: SELECT count() AS n, directory_context FROM memory WHERE migration_grace = true GROUP BY directory_context ORDER BY n DESC` — re-confirm the 174 split
- [ ] `db_inspect: SELECT id, directory_context, string::slice(content,0,200) AS preview, tags, created_at, access_count, tier FROM memory WHERE migration_grace = true ORDER BY directory_context, created_at LIMIT 200`
- [ ] Present the list to the user grouped by directory, with a per-row suggested fate
      (`renew` / `retire` / `let-expire`). **Do not decide.** These are the user's memories.
- [ ] Record the user's per-row decision in the plan doc as an appendix table.

### Step 1b — apply (only after 1a is signed off; before 2026-08-26)

- [ ] Take the pre-migration backup (`docs/plans/0115-...`).
- [ ] `renew` rows: clear `migration_grace` and set a real `valid_until`, **or** promote to
      `semantic_immortal` with a `reason` — user's choice per row.
- [ ] `retire` rows: `de_anchor(id)` (gentle; the row keeps living and decays). **Not `forget`**
      unless the user explicitly asks per row.
- [ ] `let-expire` rows: clear `migration_grace` only, so they become **normal** expired anchors —
      which means `_fetch_expired_anchor_count` starts counting them, the signal fires, and
      `forget_expired` can reap them through the sanctioned path. This is the one action that
      converts a silent zombie into a visible, actionable row.

**Exit criterion (positive evidence):** two stated numbers.
`SELECT count() FROM memory WHERE migration_grace = true GROUP ALL` returns **174 before** and
**174 − (renewed + retired + let-expire)** after, and that arithmetic is written out in the
CHANGELOG entry. AND: for the yadgar directory,
`SELECT count() FROM memory WHERE '_anchor' INSIDE tags AND directory_context='/home/max/git/yadgar' AND (valid_until IS NONE OR valid_until > '2026-08-27T00:00:00Z') GROUP ALL`
returns the count the user's decisions predict — **stated in advance in 1a, then compared.**
Predicting the post-cliff number before applying is the whole point.

**Could this pass while doing nothing?** Yes, easily — the natural vacuous shape is "the review
list was empty" or "the user kept everything", both of which look like success. Prevented by
requiring the *before* count (174) and the *predicted post-cliff* count to be written down in 1a
and then compared against reality in 1b. A no-op run must state 174 → 174 explicitly and justify it.

**Rollback:** `renew` and `let-expire` are field updates, reversible from the backup. `de_anchor`
is reversible (re-anchor). `forget` is not — which is why it is per-row, explicit, and defaults off.

---

## Car 2 — Cull surface: `audit_anchors(enumerate=True, …)`

**Scope:** additive, no migration, no schema change. Audit §5. Today `audit_anchors` runs six
predicate fetches, **none gated on heat / access / age**, so a stale-but-valid anchor (old,
unaccessed, `valid_until` far future) is invisible to every sanctioned surface. `recall` is top-k;
direct DB access is forbidden by rule #33. There is no sanctioned enumerate-and-cull.

- [ ] `yadgar/core/server/tools/audit.py` — extend `audit_anchors` (currently at `:647`) with
      `enumerate: bool = False`, `sort: str = 'staleness'`, `page: int = 0`, `page_size: int = 50`.
      `enumerate=True` short-circuits the four action builders and returns paged rows.
- [ ] Row shape: `{id, content_preview, tags, heat, last_accessed, age_days, access_count,
      valid_until, expired, has_reason, tier, migration_grace, suggested_fate, fate_reason}`.
      `has_reason` is new relative to the audit's proposal and is load-bearing for Car 4 —
      80 of 146 yadgar anchors carry no `anchor:<reason>` tag.
- [ ] Backing query is a bounded storage-tool query (**not** `db_inspect`):
      ```
      SELECT ... FROM memory WHERE '_anchor' INSIDE tags AND directory_context = $dir
      ORDER BY last_accessed, id LIMIT $n START $offset
      ```
      **`id` tiebreak is mandatory.** `last_accessed` is non-unique; `LIMIT/START` over a
      non-unique sort key silently skips and duplicates rows across pages. The audit's §5 query
      omitted it.
- [ ] Fate heuristic (audit §5, unchanged): `cull` if `heat < 0.05 AND access_count == 0 AND
      age > 90d`; `immortalize` if high-access + high-heat; `renew` if `valid_until < now+14d`
      AND recently accessed; else `keep`.
      **Note the heuristic is near-inert on this corpus today** — all 146 yadgar anchors sit at
      `heat = 1.0` because `is_protected` excludes them from decay. Record that as a
      known limitation in the tool docstring; `access_count` and `age_days` carry the signal.
- [ ] Read-only. Mutation stays with the existing `de_anchor` / `forget` / `anchor` tools.
- [ ] Register in `CAPABILITY_REGISTRY` (`check_capability_coverage.py` will orphan it otherwise).
- [ ] Targeted tests in `yadgar/tests/core/test_audit_anchors.py`.

**Exit criterion (positive evidence):** on the live yadgar directory, summing `len(rows)` across
all pages returned by `audit_anchors(directory='/home/max/git/yadgar', enumerate=True, page_size=50)`
equals **the same-day `db_inspect` count of `'_anchor' INSIDE tags AND directory_context=...`**
(146 as of 2026-08-08 — re-measure, it moves). AND every returned row has a non-null
`suggested_fate` drawn from `{cull, renew, immortalize, keep}`. AND a pagination-integrity test:
the union of `id`s across pages has **no duplicates** and **no gaps** versus a single unpaginated
fetch — this is what the `id` tiebreak buys and the only thing that proves it.

**Could this pass while doing nothing?** Three ways, all closed:
(a) `enumerate=True` returning `[]` — closed by the page-sum-equals-DB-count assertion;
(b) returning rows with `suggested_fate: null` everywhere — closed by the non-null requirement;
(c) returning page 0 correctly and then repeating it — closed by the no-duplicates/no-gaps test.

**Rollback:** delete the parameter. Purely additive; `enumerate` defaults `False` so existing
callers are byte-identical.

---

## Car 3 — Anchor-audit template repoint + byte-pin

**Scope:** fix the broken gathering step root-caused in §0.6, and replace the vacuous guard.

**Why this is not Car 1 despite being the loudest bug:** fixing it standalone would have to
repoint at `project_brief(mode="catalog").top_anchors_project`, which caps at 20
(`project.py:620`) and carries no fate hints — so it would need immediate re-editing once Car 2
lands. And the byte-pin test (below) makes every template edit a two-file change, so a
double-edit doubles the conflict surface. One edit, after Car 2.

- [ ] `yadgar/core/hooks/templates/anchor_audit_prompt.md:18-21` — **delete the
      `recall(..., tags=["_anchor"], type="memory")` call.** Replace with
      `audit_anchors("{directory}", enumerate=True, sort="staleness", page_size=50)`, paging until
      exhausted. State in the template that the returned set is the **complete** anchor list, not
      a top-k sample — the current template's "list this project's `_anchor` memories" reads as
      complete and was not.
- [ ] Fold the Car 4 admission bar (SKIP list) into step 3 (JUDGE) as the retire criterion, so
      admission and retirement use the **same** bar. An anchor that would not be admitted today
      is a retire candidate.
- [ ] Surface `has_reason` in the step-4 table. "No `anchor:<reason>`" is the single strongest
      retire signal available on this corpus — all nine anchors retired in the 2026-08-08 pass
      lacked one, and 80 of 146 lack one today.
- [ ] Keep the empty-list no-nag gate (step 2) intact and keep `de_anchor` as the default action.
- [ ] `yadgar/tests/hooks/test_v5_158_anchor_audit_scheduler.py` — **replace** the substring
      assertions at `:151-165` with a byte-for-byte `_EXPECTED_TEMPLATE` pin, mirroring
      `yadgar/tests/hooks/test_stop_hook_template.py:42` / `:289`.
- [ ] **Add an explicit negative assertion:** `'tags=["_anchor"]' not in content`. A byte-pin alone
      is satisfied by any text, including a reintroduction of the broken call in a later edit.
- [ ] One-line clarification while here: `recall.py:358` docstring → "wiki results **only**; inert
      for `type="memory"`" (see NOT-in-scope item 5).

**Exit criterion (positive evidence):** (1) the byte-pin test fails when a single character of
`anchor_audit_prompt.md` changes — demonstrated by running it against a deliberately mutated copy
and showing red, then reverting. (2) The negative assertion fails when `tags=["_anchor"]` is
re-inserted — same demonstrate-red-then-revert. (3) A live run of the audit protocol against
`/home/max/git/yadgar` returns a candidate count equal to the Car 2 enumerate count, **and zero
auto-abstracted cluster summaries** — record both numbers against the 2026-08-08 baseline of
"5 cluster summaries vs 155 real anchors".

**Could this pass while doing nothing?** Yes — and it already did once. The *existing* test
(`de_anchor` substring + empty/stop substring pair) passed for the entire lifetime of the broken
call. A prose-only template edit satisfies any substring test trivially. Prevented by the
byte-pin (any drift is red), the negative assertion (the specific regression is named), and
the live-count comparison (prose cannot fake a row count).

**Rollback:** `git revert` the two files. No runtime state.

---

## Car 4 — Admission bar (the concern with no prior analysis)

**Scope:** additive write-path gate. The audit did not cover this; it is the user's concern and
the one producing the junk.

### 4.1 The evidence

Of nine anchors retired in the 2026-08-08 audit pass, **five were release-state snapshots** —
"v4.9 fully shipped", "v5.1.7 shipped", "v5.6.7 SHIPPED", "v4.7.0 state / PR #50 open",
"Run-829 CI failure inventory". **None of the nine carried an `anchor:<reason>` tag.**
Corpus-wide: 80 of 146 yadgar anchors carry no reason.

### 4.2 The four doors, precisely

| # | door | file:line | reason required? | tier | expires? | visible to `audit_anchors`? |
|---|---|---|---|---|---|---|
| 1 | `anchor(content, context)` | `misc.py:255-307`; tier default `misc.py:208`; reason check `misc.py:220-229` | **only** for `semantic_immortal` | `conditional` | 90d | yes |
| 2 | `memorize(is_protected=True)` | `validate.py:59-60` auto-promotes tier | **never** | `conditional` | 90d | yes |
| 3 | bare `_anchor` in `memorize(tags=[...])` | `_phase_post_write.py:126-132` | **never** | **none set** | **NEVER** | yes |
| 4 | **`DECISION_AUTO_PROTECT` regex** | `_phase_post_write.py:134-137`; default ON `config.py:160`; regex `server_helpers.py:29-33` | **never** | **none set** | **NEVER** | **NO** |

**Door 3** is the lowest-ceremony path and the only one that produces a **permanent** anchor:
`_zero_gap_2_protection` sets `is_protected=1, importance=1.0` and appends `_anchor`
*post-write*, so `validate.py`'s tier auto-promotion (which runs pre-write) never fires and
`_compute_valid_until` is never called. **15 yadgar anchors are in exactly this state**
(`tier IS NONE AND valid_until IS NONE`, `_anchor` appended last in the tag list) — including
"CPU-BURST INVESTIGATION (10th attempt) — STATE … OPEN/unresolved", an investigation-state note
anchored forever.

**Door 4 is the one nobody found** — not the user, not the audit. `DECISION_AUTO_PROTECT` matches
`chose .+ over | decided to use | switched from .+ to | migrated from | will use .+ instead |
going with | opted for | selected .+ because | choosing .+ approach | picking .+ strategy` and sets
`is_protected=1, importance=1.0` with **no `_anchor` tag, no tier, no `valid_until`, no reason**.
Result: permanently decay-exempt **and** invisible to `audit_anchors`, because every audit fetch
keys on `'_anchor' INSIDE tags`. Roughly 10 of the 101 tag-less protected rows are this — and the
tag census puts `_historical`-tagged release notes like *"yadgar / v4.9 / plan / vacuum / shipped"*
squarely in that set. **Exactly the category the user flagged, arriving through a door with no
gate and no audit visibility.**

Note the asymmetry doors 1–2 have and 3–4 lack: `_compute_valid_until` runs at the API boundary,
so post-write protection paths bypass expiry entirely.

### 4.3 The design

**(a) `reason` becomes mandatory for every tier, not just `semantic_immortal`.**
`misc.py:220-229` currently gates on `_tier == "semantic_immortal" and
settings.ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON`. Widen to all tiers behind a new knob
`ANCHOR_REQUIRES_REASON` (default `true`). Same rejection shape:
`{"stored": False, "reason": "..."}`. `memorize(is_protected=True)` gets the same check in
`validate.py::_validate_tier_and_parity`.

**Rationale:** a reason is a forcing function, not documentation. "v5.6.7 SHIPPED" has no
articulable reason to be compaction-proof forever; being made to type one is where the author
notices.

**(b) A SKIP list mirroring the ADR protocol's own bar.** The repo already has a working
admission bar for durable knowledge, at `stop_checkpoint_prompt.md:37-39`:

> SKIP: routine work (git push, branch cleanup, progress/status checks), in-flux or abandoned
> ideas, pure status ("tests pass"), routine corrections (typos, lint).

Mirror it into the `anchor()` docstring and the anchor-audit template, extended with the
anchor-specific categories the evidence names:

> SKIP: release-state snapshots ("vX.Y shipped", "PR #N open"), CI/run inventories, in-flight
> investigation state, task-scoped notes that die with the task, anything already captured in an
> ADR or wiki page.
> KEEP: facts that stay true and stay useful after the current task ends — workflow rules,
> account IDs, hard constraints, recurring procedures, non-obvious gotchas.

**This is prose and prose is unenforceable.** It goes in the docstring (which the model reads at
call time) and in the template. The *enforceable* half is (a), (c) and (d).

**(c) Flip the default tier `conditional` → `ephemeral`.** `misc.py:208` and `validate.py:59-60`.
90d is long enough that permanence is the effective default; 14d makes the caller opt in to
permanence. Reversible per call: `tier="conditional"` still works.
**This is a behaviour change with a corpus effect — it must be user-approved before the car runs
(Open Question Q1), because it silently shortens the life of every future bare `anchor()` call.**

**(d) Close doors 3 and 4.**
- Door 3: in `_zero_gap_2_protection` (`_phase_post_write.py:126-132`), when protection is applied
  post-write and `tier`/`valid_until` are unset, apply the same default `_compute_valid_until`
  the API boundary would have. No anchor should be born without an expiry.
- Door 4: `DECISION_AUTO_PROTECT` currently protects without tagging. Two options —
  **(i)** tag it `_anchor` + `anchor:auto-decision` and give it an expiry, making it visible to
  `audit_anchors` and reapable; or **(ii)** turn it off (`config.py:160` → `False`).
  **Option (i) is recommended** — the intent (don't decay a decision) is sound; the invisibility
  is the bug. **User decision (Open Question Q2)**, because (i) makes ~10 existing rows appear in
  the audit surface for the first time.

### 4.4 Checklist

- [ ] `yadgar/_shared/config/config.py` + `config_registry.py` — add `ANCHOR_REQUIRES_REASON` (bool, default true)
- [ ] `yadgar/core/server/tools/misc.py:220-229` — widen the reason gate to all tiers
- [ ] `yadgar/_shared/write_exec/validate.py:59-70` — same gate on the `memorize(is_protected=True)` path
- [ ] `yadgar/core/server/tools/misc.py:263-278` — rewrite the `anchor()` docstring with the SKIP/KEEP bar
- [ ] `yadgar/backend/write_exec/_memorize_phases/_phase_post_write.py:126-137` — doors 3 and 4
- [ ] `yadgar/core/server/tools/misc.py:208` + `validate.py:60` — default tier flip (**gated on Q1**)
- [ ] `yadgar/core/hooks/templates/anchor_audit_prompt.md` — the same bar as the retire criterion
      (this is the Car 3 edit; sequence Car 4's template text *into* Car 3 if Car 4 lands first,
      to avoid a second byte-pin churn)
- [ ] Targeted tests: `yadgar/tests/core/test_memorize_anchor_parity.py`, `test_anchor_hygiene_schema.py`

**Exit criterion (positive evidence):** **a rejection, asserted in-process.**
- `anchor(content="x", context="/tmp/p")` with no `reason` returns `{"stored": False, ...}` whose
  `reason` string names the missing argument. Asserted directly against the tool function, not
  through a live MCP call — per wiki `guards-and-tooling-in-this-repo-that-do-not-check-what-their-nam`,
  **FastMCP silently drops unknown kwargs and never raises server-side**, so any exit criterion
  phrased as "a live MCP call must reject" is unsatisfiable in-process.
- `memorize(content="x", context="/tmp/p", tags=[], is_protected=True)` with no `reason`: same.
- `memorize(content="x", context="/tmp/p", tags=["_anchor"])` — the door-3 path — produces a row
  with a **non-null `valid_until`**. Assert the stored row, not the return value.
- A content string matching `_DECISION_STRONG_RE` (e.g. `"we decided to use X"`) written through
  `memorize` produces a row that `audit_anchors(directory, enumerate=True)` **returns** (door 4
  visibility), under whichever of Q2's options the user picks.
- If Q1 is approved: `anchor(content, context, reason="r")` with no `tier` stores
  `valid_until ≈ now + 14d`, asserted as a bounded range, not an equality.

**Could this pass while doing nothing?** Yes, and this is the car most at risk of it: a SKIP list
is prose, and prose satisfies any substring test. A car could edit only the docstring and template,
add a `"SKIP:" in content` assertion, and be green having changed no behaviour whatsoever.
Prevented by making **every** exit criterion a rejection or a stored-field assertion. Prose
carries zero exit weight here by construction.

**Rollback:** `ANCHOR_REQUIRES_REASON=false` reverts (a) at runtime with no deploy. The tier flip
reverts by knob. Doors 3/4 are code and revert by `git revert`. **No existing rows are mutated by
this car** — it is a write-path gate only, so rollback is complete.

---

## Car 5 — Time-box promotion + `anchors_expiring_soon`

**Scope:** additive. Audit §4. `ttl_days` exists and is unused because nothing hints at it,
nothing warns before expiry, and nothing proactively reaps.

- [ ] `yadgar/core/server/tools/misc.py` — `anchor()` **echoes the resolved `valid_until`** in its
      return dict. Today it returns `{queued, status, is_protected, reason, tier}` (`:301-307`) —
      the caller cannot see when their anchor dies. This is the cheapest and highest-leverage item
      in the car.
- [ ] `anchor_temporary(content, context, reason, days)` — thin wrapper over `anchor(ttl_days=...)`.
      A named tool makes time-boxing discoverable in a way a keyword argument does not.
- [ ] `yadgar/core/server/tools/project.py::_compute_anchor_signals` (`:1065`) — add
      `anchors_expiring_soon`: `now < valid_until < now + ANCHOR_EXPIRING_SOON_DAYS` (new knob,
      default 7), **scoped to `directory`** and mirroring `_fetch_expired_anchor_count`'s WHERE
      clause exactly (ADR-0083 parity — a signal that does not correspond to an actionable audit
      entry retrains the instance to ignore signals).
- [ ] `yadgar/core/server/tools/audit.py` — a corresponding `expiring_soon` action type, so the
      signal has a matching action. Non-mutating (advisory), like `verify_grace_expired_anchor`.
- [ ] Register knob + capability entries.

**Exit criterion (positive evidence):** seed a fixture anchor with `ttl_days=3`; assert
`project_brief(dir, mode="signals")["anchors_expiring_soon"] == 1` and
`audit_anchors(dir)["actions"]` contains exactly one `expiring_soon` entry naming that id.
Then seed one with `ttl_days=30`; assert both go back to 0/absent. **The parity assertion is the
load-bearing one:** for a randomised set of TTLs, `anchors_expiring_soon > 0` ⟺
`audit_anchors` emits ≥1 `expiring_soon` action — the property-test shape ADR-0083 established in
`TestSignalAuditParity`. AND: a live `anchor(...)` call returns a dict containing a parseable
ISO-8601 `valid_until`.

**Could this pass while doing nothing?** Yes — a signal hardwired to `0` passes the negative case
and is never exercised by the positive one if the fixture is wrong. Prevented by asserting **both**
directions (3-day → 1, 30-day → 0) and by the ⟺ parity property test, which no constant satisfies.

**Rollback:** additive; remove the signal key and the action type. Knob-disable available.

---

## Car 6 — Corpus review pass (155 anchors) — USER-GATED

**Scope:** data, not code. Produce the review list; **the user decides.** These 155 anchors are
the user's corpus, not the train's to prune.

- [ ] Take the pre-migration backup.
- [ ] Run `audit_anchors('/home/max/git/yadgar', enumerate=True, sort='staleness', page_size=50)`
      to exhaustion; likewise for `directory='global'` (9 rows).
- [ ] Produce a table: `id · one-line preview · age_days · access_count · has_reason ·
      valid_until · suggested_fate · fate_reason`.
- [ ] Pre-sort by the Car 4 bar: rows matching the SKIP categories (release-state snapshot, CI
      inventory, in-flight investigation state, task-scoped note) go top. The 15
      `tier IS NONE AND valid_until IS NONE` rows and the 80 `has_reason = false` rows are called
      out as their own sections.
- [ ] Present. **Do not mutate anything the user has not confirmed row-by-row.**
- [ ] Apply confirmed rows only: `de_anchor(id)` by default; `forget(id)` **only** where the user
      explicitly says delete.
- [ ] Record the decisions as an appendix table in this doc.

**Exit criterion (positive evidence):** the review table exists as a committed appendix covering
**every** row the enumerate surface returned (count matches the Car 2 exit number exactly — no
row silently omitted), each with a user disposition of `keep` / `retire` / `delete`. After apply:
`db_inspect` anchor count equals `before − (retired + deleted)`, with all four numbers stated.
Keep-all is a legitimate outcome and must be recorded as `155 → 155, 0 retired, 0 deleted`.

**Could this pass while doing nothing?** Yes, and "the user kept everything" is indistinguishable
from "the agent never asked". Prevented by requiring a per-row disposition for **every** enumerated
row — a table with 155 rows cannot be produced without actually running the enumeration, and a
table with 12 rows fails the count match.

**Rollback:** `de_anchor` is reversible (re-anchor with reason). `forget` is not, beyond the
backup — which is why it is per-row explicit and never the default.

---

## Car 7 — Field-collapse ADR (DECISION ONLY — no code, no migration)

**Scope:** write the ADR. Ship nothing.

The audit's §2 recommended collapsing `_anchor` → `is_protected` and asserted the divergence ran
tag-without-protection. §0.2 shows the corpus says the opposite, 101 rows deep, by design.
The collapse as specified would take the yadgar anchor set from 146 to 247 and put `_active_work`
rows into the audit's `forget` path. **This needs a decision, not an implementation.**

- [ ] ADR recording: the audit's §2 premise, the 2026-08-08 measurement that falsifies it, and
      the three live options:
      - **(A) Keep both fields, document the distinction.** `is_protected` = decay-exempt
        mechanism; `_anchor` = anchor role. Zero migration. Add a `check_invariants` assertion in
        the **correct** direction — `'_anchor' INSIDE tags ⇒ is_protected = true` (a one-way
        implication, currently satisfied by 0 violations) — **not** the biconditional the audit
        proposed, which fails on 101 legitimate rows on first run.
      - **(B) Split into `decay_exempt` + `is_anchor` booleans.** Semantically clean. Requires
        migrating 101 system rows to `decay_exempt`-only and rewriting ~7 read queries. Materially
        larger than the audit scoped.
      - **(C) The audit's original collapse.** Recommend **against**, with the 146→247 number and
        the `_active_work`-in-`forget_expired` example as the reason.
- [ ] Record the recommendation as **(A)** — the cheapest option that is not wrong — and leave the
      status `open` until the user rules.
- [ ] Amend the audit wiki page (7558) §2 in place with a dated correction block. **An audit
      recommending a falsified action is worse than no audit**, and the next reader will not
      re-verify.
- [ ] Cross-link this plan from page 7558 and from page 6375 (§0.5 verdict).

**Exit criterion (positive evidence):** `adr_get` returns the new ADR with all mandatory fields
populated and the three options named. AND `wiki_read("anchor-data-model-audit-2026-07-24-...")`
returns content containing the dated correction block — verified by reading the page back after
the write, not by the write's return value. AND `wiki_history` shows a version bump on 7558.

**Could this pass while doing nothing?** Yes — an ADR is prose and always "succeeds". Prevented by
the read-back requirement on page 7558: the correction must be present in the *fetched* content,
and `wiki_history` must show the version increment. A write whose queue job was rejected returns
optimistically; the read-back is what catches it.

**Rollback:** ADR status → `rejected`; revert the wiki correction. No code, no data.

---

## Car 8 — `migration_grace` field drop (MIGRATION)

**Preconditions, all three:** Car 1 complete and signed off · **today's date > 2026-08-26** ·
`SELECT count() FROM memory WHERE migration_grace = true GROUP ALL` returns the number Car 1's
arithmetic predicts.

- [ ] `yadgar/_shared/storage/migrations.py` — new **forward** migration
      `REMOVE FIELD IF EXISTS migration_grace ON TABLE memory;`
      Register at the end of the registry. **Do not edit migration_008** — migration 026's
      docstring establishes that historical migrations are immutable for replay; removal is always
      a new forward migration.
- [ ] Remove the three readers: `audit.py:152`, `audit.py:220`, `project.py:804`.
- [ ] Remove `_build_verify_grace_actions` (`audit.py:527-554`) and its call site (`audit.py:697`)
      — the whole action type dies with the field.
- [ ] Sweep tests: `git grep -l migration_grace -- yadgar/tests/`
- [ ] `check_capability_coverage.py` — the new migration needs a registry entry or it orphans.

**Exit criterion (positive evidence):** on a **fresh** DB replaying all migrations 001→N,
`INFO FOR TABLE memory` contains no `migration_grace` field. **And** the same on the **live** DB
after applying. Both, because a forward migration that only works on a fresh DB is a known failure
shape in this repo. AND `git grep -c migration_grace -- yadgar/ scripts/` returns **0** against the
Car 0 baseline. AND `audit_anchors(dir)` on the live DB returns actions containing **zero**
`verify_grace_expired_anchor` entries where Car 0's baseline recorded a nonzero count.

**Could this pass while doing nothing?** Yes — the classic shape: a migration function written but
not registered runs never and fails nothing. Caught by the `INFO FOR TABLE` assertion on **both**
databases. The grep-to-zero catches a partial code sweep, which the migration alone would not.

**Rollback:** restore from the pre-migration backup. The field drop is one-way. This is the
train's first genuine point of no return.

---

## Car 9 — `tier` → `is_immortal` (CONDITIONAL — do not start without Car 7's ADR accepted)

**Scope:** audit §2/§3. `tier` drives nothing at runtime except one guard: `audit.py:516`,
`if tier == "semantic_immortal"`. Decay ignores it entirely; `_compute_valid_until` reads it only
at creation. Three yadgar anchors are `semantic_immortal`.

- [ ] `_compute_valid_until` (`server_helpers.py:113-147`) — replace the tier branch with explicit
      TTL + an `is_immortal` boolean.
- [ ] `audit.py:516` — forget-guard reads `is_immortal`.
- [ ] Forward migration: `is_immortal = (tier == 'semantic_immortal')`, then
      `REMOVE FIELD tier`. **Two migrations, not one** — backfill and drop must be separately
      revertable.
- [ ] Edge case the audit flagged: explicit `valid_until` **plus** `is_immortal=true`. Decide and
      document (recommendation: explicit `valid_until` wins, `is_immortal` becomes advisory).
- [ ] `de_anchor` (`admin_other.py:547-550`) currently demotes `tier` to `"ephemeral"` because
      `memory.tier` is `option<string>` and cannot be JSON-null. With `is_immortal` as a bool this
      becomes a clean `false` — remove the workaround **and its explanatory comment**, which will
      otherwise outlive the constraint it explains.

**Exit criterion (positive evidence):** post-backfill and **before** the drop,
`SELECT count() FROM memory WHERE is_immortal = true GROUP ALL` equals the pre-migration
`SELECT count() FROM memory WHERE tier = 'semantic_immortal' GROUP ALL` — both numbers stated
(3 for yadgar as of 2026-08-08; re-measure globally). AND `audit_anchors` still skips exactly
those rows, proven by id, not by count. AND `INFO FOR TABLE memory` shows no `tier` on both a
fresh and the live DB.

**Could this pass while doing nothing?** Yes — a backfill that sets `is_immortal = false`
everywhere passes any "the field exists" check and silently unprotects every immortal anchor. The
equal-counts assertion catches it; the by-id skip assertion catches the subtler case where counts
match but the wrong rows were flagged.

**Rollback:** backup restore. One-way past the drop.

---

## Car 10 — Docs, ADR amendments, residue proof, archive

- [ ] Residue proof: re-run Car 0's 9-identifier grep; **diff against the committed baseline** and
      state each delta with its cause. Not "grep returns zero" — an unanchored zero proves nothing.
- [ ] Re-run the full §0.4 measurement set; publish before/after.
- [ ] Update `docs/` and `BEHAVIOR_CONTRACT.md` rows touched by Cars 4, 5, 8, 9.
- [ ] Amend wiki 7558 (§2 correction — if Car 7 did not) and 6375 (§0.5 verdict).
- [ ] ADR-0081: `git mv docs/plans/anchor-refactor-2026-08-08.md docs/plans/archive/` with a
      status header, **in this car's first commit**.

**Exit criterion (positive evidence):** a published before/after table for all 9 identifiers and
all 14 measurements, every nonzero delta annotated with the car that caused it. AND
`ls docs/plans/archive/anchor-refactor-2026-08-08.md` succeeds while
`ls docs/plans/anchor-refactor-2026-08-08.md` fails.

**Could this pass while doing nothing?** Yes — "docs updated" is unfalsifiable. Prevented by the
numeric diff (which requires the baseline to exist and be compared) and the file-move assertion
(binary, mechanically checkable).

---

## NOT in scope

1. **Page 6375's surfacing redesign.** §0.5 establishes the concern is LIVE (8-of-146 selected by
   a fully degenerate `heat = 1.0` tie), but the treatment is corpus shrinkage — Cars 1, 4, 5, 6 —
   not raising `REPLAY_MAX_RESTORE_MEMORIES` (8) or `PROJECT_BRIEF_MAX_ANCHORS` (12). Raising the
   caps floods the context window with the same junk this plan exists to remove, and does not fix
   the arbitrariness. **Re-assess after Car 6 with the post-cull number.**
2. **A deterministic tiebreak on the anchor ORDER BY.** Correct and cheap
   (`ORDER BY heat DESC, created_at DESC`), but it touches four surfacing queries across two files
   and changes what every session sees. It belongs with (1). See Q3.
3. **`anchor:<reason>` → a real `reason` column.** The audit deferred it; so does this plan. Car 4
   makes the tag mandatory, which is the behavioural half; normalising the storage is cosmetic.
4. **`importance` and `heat` semantics.** Audit says KEEP; unchanged here. Note `importance` is
   moot for anchors (`is_protected` skips decay) — a documentation matter, not a refactor.
5. **`recall(tags=...)` extension to memory results.** §0.6 shows `tags` is wiki-only. Making it
   work for memories is a retrieval-path change with every-caller blast radius, and Car 3 does not
   need it — `audit_anchors(enumerate=True)` is the right surface for an exhaustive tag-keyed list.
   Worth its own investigation later; the one-line docstring clarification is folded into Car 3.
6. **Cross-project anchor redundancy.** `_fetch_cross_project_candidates` exists and is
   surfaced-only by design (ADR-0083 lineage). Untouched.
7. **Any pruning decision on the 155-anchor corpus.** Car 6 produces the list; the user decides.

---

## Risks

| # | risk | severity | mitigation |
|---|---|---|---|
| R1 | **The 2026-08-26 grace cliff lands before this train starts.** 174 rows silently stop surfacing; 61 of them in yadgar. No signal fires (by ADR-0083 design). | **HIGH — dated, 18 days out, and the ADR-0215 train is mid-flight** | Car 1a is zero-code and explicitly authorised to run immediately and independently (§0.3 exception). If the cliff passes un-defused the rows are recoverable (nothing deletes them) but invisible until Car 8 — call that out to the user now. |
| R2 | Someone implements the audit's §2 verbatim without reading §0.2 — anchor set 146→247, `audit_anchors` offers to `forget` `_active_work`. | **HIGH** | §0.2 is the second section of this doc; Car 7 amends the audit page in place; the audit's proposed biconditional invariant is named as broken. |
| R3 | Car 4's SKIP list ships as prose only and changes nothing. | **MEDIUM-HIGH** — this is the repo's documented failure mode | Every Car 4 exit criterion is a rejection or a stored-field assertion. Prose carries zero exit weight. |
| R4 | Car 4(c) tier flip shortens the life of anchors the user wanted permanent. | MEDIUM | Gated on Q1. Knob-reversible with no deploy. Applies only to future writes; no existing row is touched. |
| R5 | Car 4(d)(i) makes ~10 previously-invisible auto-protected rows appear in the audit surface, and they look like noise. | MEDIUM | That is the *point* — they were unauditable. Gated on Q2. Car 6 reviews them with everything else. |
| R6 | Car 2's `suggested_fate` heuristic is near-inert: it keys on `heat < 0.05` and all 146 anchors sit at `heat = 1.0` by construction. | MEDIUM | Documented as a known limitation in the docstring; `access_count`, `age_days` and `has_reason` carry the real signal. Do **not** paper over it by loosening the threshold — that manufactures false `cull` verdicts. |
| R7 | Concurrent edits to `anchor_audit_prompt.md` between Cars 3 and 4 churn the byte-pin twice. | LOW-MED | Car 3 lands the pin; Car 4's template text is folded into Car 3 if Car 4 sequences first. Stated in both cars. |
| R8 | Cars 8/9 are one-way. A wrong predicate deletes user memories. | **HIGH impact / LOW likelihood** | Pre-migration backup mandatory. Both cars assert counts *before* the destructive step and STOP on mismatch (the ADR-0215 Car 8b pattern). |
| R9 | Line numbers in this doc rot before the train runs. | LOW | Car 0 re-verifies the whole §0.1 table and records `[CORRECTION]` rows. This exact failure mode motivated §0.1. |
| R10 | ADR-0218 targeted testing misses cross-car breakage (it demonstrably did on the ADR-0215 train — Car 1 shipped a live TypeError plus 19 red tests). | MEDIUM, **accepted** | Merge-as-you-go so breakage stays attributable to one car; pre-push `make e2e` gate; Cars 4 and 5 touch the write path and must run `make e2e`. |

---

## Decisions — Q1–Q4 RESOLVED 2026-08-08; only Q5 is the user's

The plan as first drafted marked all five "user decision". That was over-deferral: four are
ordinary engineering judgement that follow from the user's already-stated complaint (permanence
by default lets junk in) and from the measured evidence. They are decided here. **The one thing
that genuinely belongs to the user is row-level disposition of their own memories** — Q5 and
Car 6 — because those decide what gets deleted, and no measurement settles that.

**Q1 — flip the default tier `conditional` (90d) → `ephemeral` (14d): YES, DO IT.**
This is the direct implementation of the stated complaint. Permanence should be opt-in. Risks are
small and covered: fully knob-reversible with no deploy, per-call overridable with
`tier="conditional"`, touches no existing row, and Car 5 makes `anchor()` echo the resolved
`valid_until` so a caller who wanted permanence sees the 14 days immediately rather than
discovering it in a fortnight. Car 4 ships (c).

**Q2 — `DECISION_AUTO_PROTECT` (door 4): option (i), TAG AND EXPIRE.**
The intent — don't let a recorded decision decay — is sound and worth keeping; the bug is that the
rows are invisible to every audit surface. Turning the regex off (option ii) throws away a working
feature to fix a visibility defect. Tag `_anchor` + `anchor:auto-decision`, apply the default
expiry. The ~10 existing rows are not touched retroactively; they surface in Car 6 like everything
else, which is the point.

**Q3 — deterministic tiebreak: DEFER, as the plan already scoped it.**
It stays NOT in scope. `ORDER BY heat DESC, created_at DESC` is correct and cheap, but it changes
what every session sees, and while the corpus is 146-deep a stable-but-arbitrary 8 is barely
better than an unstable arbitrary 8. Re-assess after Car 6 with the post-cull number — if the
corpus lands near 30, the cap stops binding and the tiebreak may be unnecessary. Deciding it now
would be optimising the symptom before the cause is removed.

**Q4 — Car 7: option (A), keep both fields and document the distinction.**
Cheapest option that is not wrong: zero migration, and it adds the invariant in the direction the
corpus actually satisfies (`'_anchor' INSIDE tags ⇒ is_protected = true`, currently 0 violations)
instead of the biconditional that would fail on 101 legitimate rows. (B) — splitting into
`decay_exempt` + `is_anchor` — is semantically cleaner and stays on the table as a future ADR, but
it migrates 101 system rows and rewrites ~7 read queries to buy naming clarity, which is not worth
a one-way migration today. (C), the audit's original collapse, is rejected outright. Car 7 still
writes the ADR — the record of why the audit was wrong is worth having — but it records (A) as
accepted rather than opening the question again.

### Q5 — STILL THE USER'S: disposition of the 174 grace rows

Per-row review (Car 1a) is the default. The alternative is a blanket rule — e.g. "let all 174
expire, they are v5.8 migration residue" — which collapses Car 1 to a single `let-expire` update
and is materially faster.

This one is not delegated because it decides what happens to memories the user wrote. Nothing in
the measurements settles it: the rows are recoverable either way (nothing deletes them at the
cliff), so the question is purely whether the user wants to look before they go dark.

**Same principle governs Car 6**: the plan produces the 155-row review table; the user rules on
each row. An agent pruning someone's memory corpus on its own judgement is not a time saving, it
is a category error.
