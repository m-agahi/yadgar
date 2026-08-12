# 0047 spine train — train doc, operator runbook, and rehearsal record

> Created 2026-08-12 by **C16**, the final car of the 0047 spine train (PR #40).
> Status: **code shipped, NOT DEPLOYED.** The cutover in §3 has never run.
> Parent plan: [`task-table-refactor-2026-07-29.md`](../task-table-refactor-2026-07-29.md) —
> deliberately **still live**, because its §6.2 cutover is the work §3 below describes.
> Remediation plan: [`0047-pr40-remediation-2026-08-10.md`](0047-pr40-remediation-2026-08-10.md) — archived.
> Per-car docs: `0047-car-A0` … `0047-car-M` (16 files, all archived).
> Binding ADRs: **ADR-0225** (`project_id` is the sole scoping key) · **ADR-0226** (branch residue
> revoked) · **ADR-0227** (host-side minting only, fail loud) · **ADR-0228** (superseded ADRs
> EXCLUDED, amending ADR-0206) · **ADR-0222** (a tripped migration runs broken but healthy-looking).

**Why this document exists.** The PR body claimed a file at this path that did not exist, and
`MIGRATION_NOTES.md` — where the deploy instructions would otherwise live — is **gitignored**
(`.gitignore:12`). So nothing tracked told an operator what to do with a train that re-keys the
entire read path. §3 is that artefact. It is the load-bearing half of this file; §1–§2 are
context and §4 is the rehearsal record.

---

## 1. What the train did

One change, described three ways by three different reviewers:

1. **The 23 PR-review findings** on PR #40 (5 criticals, the ADR-0202 inversion, the mediums).
2. **The full `directory` → `project_id` retirement** (ADR-0225/0227).
3. **The branch residue** ADR-0226 revoked (`wiki_page_version.branch`, the seeding kwargs).

They are one change because findings #1 and #6–#12 are not eight defects but **one**:
`derive_project_id()` was called from processes that cannot see the host — a container with no
git and no mounted project directory — and never from the one process that can. Such a call
cannot fail; it returns `local/<basename>`, a well-formed key indistinguishable at read time from
a correct one. ADR-0227's answer is host-side minting only, and **no fallbacks**: the mint raises
rather than guessing.

The consequence an operator must internalise: **after this train, a scope that cannot be resolved
is an error, not a default.** Every silent widening the old code performed is now a loud failure.
That is the point, and it is also why the deploy order in §3.2 is not advisory.

## 2. The cars

| group | cars | what landed |
|---|---|---|
| spine (parent plan §7) | A0, A, B, C1, C2, C3, D, E, F, G, H, I, J, K, L, M | ledger tables (`task`, `adr`, `agent_pattern`), the Alembic chain, backend ops + cache, the identity gate, the mutability policy, the nightly archive sweep, the registry |
| remediation (§5) | C0 – C16 | the gates, host-side minting, the `project` parameter surface, the fail-loud flip, the registry guard + backfill, the SQL WHERE clause, superseded-ADR exclusion, the `_shared`/`backend`/`core` sweeps, migrations 032 + 033, the test sweep, the docs sweep, the residue lint, and this document |

Per-car detail lives in the 16 archived `0047-car-*.md` files and in §5 of the archived
remediation plan. Nothing here restates them.

---

## 3. OPERATOR RUNBOOK — the cutover

> **Nothing in this section has ever been executed.** Not on the live corpus, not on a restored
> snapshot, not on a VM. §4 records why the rehearsal that was supposed to precede it did not run.
> Treat every step as first-execution.

### 3.0 Five properties that make this deploy unusual

Read these before the steps. Each one has already caused, or narrowly avoided causing, a defect.

1. **Core and backend images must deploy TOGETHER.** C7 re-keyed `RecallRequest`
   (`yadgar/backend/embed_service/embed_service_models.py:60`): `project_id: str` is **required**
   and `model_config = {"extra": "forbid"}`. An old core sending `directory` gets **HTTP 422 on
   every recall**; a new core against an old backend gets 422 for the same reason from the other
   side. This is deliberate — 422 is the loud version of silently reading the whole corpus — but
   it means a **split or rolling deploy has no working window**. Stop both, deploy both, start both.

2. **"It booted" is not evidence a migration applied.** ADR-0222, measured on a VM: migration 029
   aborted, the backend lifecycle swallowed the exception, and the container reported
   `active (healthy)`, `NRestarts=0`, `Result=success`. The daemon ran broken with a `schema_version`
   table that never advanced. **§3.4's queries are therefore mandatory, not optional** — they are
   the only evidence that exists.

3. **The C6 backfill has never been executed anywhere.** It ships as code with `dry_run` defaulting
   to `True` and a manifest that a human reads. Its four classes each need an explicit decision,
   and **two of them delete rows** (§3.3 step 4).

4. **The nightly sweep has never selected a single row in production.** C15a found the cause:
   `_parse_iso` took a `str` while SQLAlchemy hands back a `datetime`; the resulting `TypeError`
   was swallowed, so every age comparison fell through and the sweep archived nothing, silently,
   for its whole life. Its first post-deploy run is therefore its **first real execution**, and the
   circuit breaker's 500-row default has never been exercised. §3.3 step 8 says measure first.

5. **Grace is forward-only.** `task.completed_at` and `adr.superseded_at` (C15a) are stamped
   going forward. Pre-existing completed tasks have `NULL completed_at` and are **permanently
   un-sweepable** by design (`test_task_with_null_completed_at_is_never_archived` pins it);
   pre-existing superseded ADRs fall back to `created_at`. Neither is a bug. Do not "fix" it with
   a backfill that stamps `completed_at = updated_at` — that is exactly the clock reset
   `test_archive_sweep_ages_off_completed_at_not_updated_at` exists to forbid.

### 3.1 Preconditions

- [ ] **Take a snapshot of both engines before anything.** SurrealDB: stop the daemon and copy
      `~/.local/share/yadgar/surreal_db` (the vacuum path already produces
      `surreal_db.pre-vacuum-<ts>` snapshots; a fresh one is cheaper than reasoning about which
      old one matches). MariaDB: the `mariadb_dump` admin op. **This is the entire rollback plan**
      — see §3.5 for why the Alembic `downgrade()` chain is not one.
- [ ] Record the pre-deploy row counts and keep them: they are the denominators for §3.4.
      ```
      SELECT count() FROM wiki_page GROUP ALL;
      SELECT count() FROM memory GROUP ALL;
      SELECT count() FROM memory WHERE directory_context = 'system' GROUP ALL;
      SELECT count() FROM memory WHERE directory_context = 'global' GROUP ALL;
      ```
- [ ] Have the **host-resolved mapping** ready. The backfill **derives nothing** — it takes a
      `{directory_context: project_id}` dict produced by the C2 mint running **host-side**, where
      git exists. Build it on the host, review it, then pass it in.
- [ ] Confirm the `project` registry seed list. Every mapping target must already be a registered
      project, or the backfill refuses with `unknown_registry_targets` before writing anything.

### 3.2 Deploy order — the constraint

```
1. registry rows          create_project_row, one per project in the mapping
2. operator review        project_id_backfill dry_run=True -> read the manifest
3. backfill               project_id_backfill dry_run=False + the acknowledgement flags
4. guard                  enable registry enforcement
5. C7 read flip           deploy core + backend TOGETHER (see 3.0.1)
6. re-seed agent prompts  seed_agent_prompts
```

**The order is not a preference.** Two of the edges are load-bearing in a way that is invisible
from the code:

- **Guard AFTER rows, never before.** C6 wired the registry guard after the registry rows
  *deliberately*. The guard makes an unregistered `project_id` raise `UnknownProjectError`
  (`yadgar/_shared/storage/sql/registry.py:158`) **before** the FK. Enabling it while the registry
  is empty means every key is unregistered, so **every write in the system fails**. Guard-before-rows
  bricks the instance.
- **Backfill BEFORE the read flip.** After C7 the WHERE clause is keyed on `project_id`. Flip the
  read path while rows still have `project_id = NONE` and every scoped recall returns **zero
  results** — a fast, quiet, total outage of retrieval.

### 3.3 Steps

**Step 1 — snapshot both engines.** §3.1. Do not skip because the backfill "has a dry run": the
dry run protects against a bad *manifest*, not against a bad *apply*.

**Step 2 — seed the registry.** For each project in the mapping:
```
POST /admin  {"op": "create_project_row", "payload": {...}}
```
Verify: `POST /admin {"op": "list_project_rows", "payload": {}}` returns one row per mapping
target. The writer fails loud and never `INSERT OR IGNORE`s, so a duplicate is an error you see.

**Step 3 — backfill, dry run.**
```
POST /admin  {"op": "project_id_backfill",
              "payload": {"mapping": {<directory_context>: <project_id>, ...},
                          "dry_run": true}}
```
Read the manifest. It has **four classes** (`yadgar/backend/admin_exec/project_backfill.py:1-70`),
and each needs an explicit decision:

| class | what the manifest proposes | the decision |
|---|---|---|
| **mapped paths** | stamp `project_id` from your mapping. Subdirectories of one repo **collapse onto one key** — expected and correct. | confirm the collapse is what you meant |
| **`global`** | an owner key **plus** the `global` reach tag. Owner and reach are separate facts; dropping the tag would silently narrow those rows from every-project to one-project visibility. | confirm the owner |
| **D3 — `system`** (604 memory rows at survey) | **DELETE.** Already unreadable since v5.65 removed `'system'` from `_ALWAYS_ELIGIBLE`, so deletion changes no observable behaviour. | low risk |
| **D4 — `_memify_derive` at `global`** (238 rows at survey) | **DELETE**, matched on a four-way producer signature. **Unlike D3 these rows are CURRENTLY READABLE.** This is a real behaviour change. | **the one to think hardest about** |
| everything else | quarantined into `legacy_directory`, never guessed | requires `quarantine_unmapped=true` |

**Ordering inside the op is load-bearing and already handled:** deletes run **before** updates,
because the D4 cohort is a subset of `directory_context='global'`. Do not reorder.

The op **refuses to apply** — writing nothing — on any of five conditions:
`registry_unavailable`, `unknown_registry_targets`, `unconfirmed_deletes`,
`unreviewed_directory_contexts`, and `rows_without_a_directory_context`. The last has **no
acknowledgement flag on purpose**: a row with no `directory_context` has no basis for a mapping,
no cohort and nothing to quarantine. Fix or forget those rows first.

**Step 4 — backfill, apply.** Same call with `"dry_run": false` plus the acknowledgements the
manifest demanded (`"confirm_deletes": true`, `"quarantine_unmapped": true`). Then **re-assert the
distribution against the DB, not against the returned manifest** — the manifest describes an
intent, the DB holds the outcome:
```
SELECT project_id, count() FROM memory    GROUP BY project_id;
SELECT project_id, count() FROM wiki_page GROUP BY project_id;
SELECT count() FROM memory WHERE directory_context = 'system' GROUP ALL;   -- expect 0
```
**Zero rows may carry `project_id = "global"`, `"system"`, `"unresolved"`, or a `local/<basename>`
key for a directory that has a git remote.** Reach is expressed **only** by the `global` tag.
A `local/yadgar` row is the signature of the ADR-0227 defect and means the mapping was built
somewhere without git.

**Step 5 — enable the registry guard.** Only now. §3.2.

**Step 6 — deploy core and backend TOGETHER.** §3.0.1. Both stopped, both replaced, both started.
Expect no working window in between; schedule accordingly (§3.6).

**Step 7 — verify.** §3.4. Every query, with its expected value. Do not substitute `/health`.

**Step 8 — re-seed agent prompts.**
```
seed_agent_prompts()
```
**This step is not optional and it is easy to lose.** `seed_agent_prompts` re-seeds from the
**deployed code**, so C14's corrected agent-facing prose cannot reach the live corpus from a
worktree — it arrives only when the deployed image contains it and the seed re-runs. Skip this
and the live pages keep teaching a removed API to every agent that reads them.

**Step 9 — measure the nightly sweep BEFORE letting it run.** Per §3.0.4 this is a first
execution. Count the candidates first:
```
SELECT count() FROM task WHERE status = 'completed' AND completed_at IS NOT NULL;
SELECT count() FROM adr  WHERE status IN ('superseded','rejected','deprecated');
```
If either approaches the circuit breaker's 500-row default, raise the breaker deliberately or
sweep in batches — do not discover the limit by tripping it on the first run.

### 3.4 Post-deploy verification — one query per migration

**ADR-0222 is why this subsection exists.** A migration that aborted mid-way leaves a healthy
container and an unadvanced `schema_version`. These queries are the evidence.

| # | check | query | expected |
|---|---|---|---|
| 1 | SurrealDB migration head | `SELECT version FROM schema_version;` | contains `030_wiki_mutability_override`, `031_project_id_backfill`, `032_drop_wiki_page_version_branch`, `033_project_id_other_tables` |
| 2 | Alembic head | `SELECT version_num FROM alembic_version;` (MariaDB) | `004_agent_pattern_model_client` |
| 3 | **031** declared the field | `INFO FOR TABLE wiki_page;` / `INFO FOR TABLE memory;` | `project_id` **defined** on both |
| 4 | **031** wrote no data | — | 031 is schema-only by design; **all rows read `project_id = NONE` until step 4 of §3.3 runs.** A populated `project_id` before the backfill means something else stamped it |
| 5 | **032** dropped branch | `INFO FOR TABLE wiki_page_version;` | `branch` **absent** |
| 6 | **032** left no values | `SELECT count() FROM wiki_page_version WHERE branch IS NOT NONE GROUP ALL;` | `0` — the field is SCHEMALESS, so `REMOVE FIELD` alone removes the *definition* while surviving values stay readable. 032 nulls first, then asserts, then removes; this query re-checks the assert |
| 7 | **033** declared nine tables | `INFO FOR TABLE <t>;` for `memory_block`, `episode`, `action_log`, `checkpoint`, `narrative_entry`, `user_profile`, `derived_belief`, `wiki_page_version`, `runtime_config` | `project_id` defined on **all nine** |
| 8 | the sentinel sweep | `SELECT count() FROM memory WHERE project_id IN ['global','system','unresolved'] GROUP ALL;` and the same on `wiki_page` | `0` on both |
| 9 | reach still works | a recall scoped to project `a/b` returns a `global`-tagged memory owned by `c/d`; an **untagged** `c/d` memory is not returned | ownership and reach are separate |
| 10 | the opt-in arm | `recall(type="wiki", tags=["agent-prompt"])` | returns agent-library pages. A WHERE that excludes them unconditionally breaks the documented library lookup |

**If check 1 or 2 fails, stop.** Do not restart hoping it applies — under ADR-0222 the restart
will look successful and change nothing.

### 3.5 Rollback

**Restore the snapshot. That is the rollback path.**

The Alembic `downgrade()` chain is **not** a rollback mechanism here, and this is documented in
the migrations themselves rather than inferred:
`004_agent_pattern_model_client.py:145-153` — *"Reversible only while empty — once rows land …
a downgrade destroys data, which is the ordinary property of a table-creating revision and why
the backup arm exists."* Every table-creating revision in the chain has the same property. Once
the seed and the backfill have run, `downgrade()` deletes the tables holding the result.

Rollback specifics by stage:

- **Before the backfill applies** (registry seeded, dry run only): nothing was written to
  `memory`/`wiki_page`. Roll back by redeploying the previous images. The registry rows are inert.
- **After the backfill applies**: the deletes (D3/D4) are **not reversible in place** — restore
  the snapshot.
- **After the read flip**: restore the snapshot **and** redeploy the previous image pair together
  (§3.0.1 in reverse — an old core against a new backend 422s just as hard).

### 3.6 Surfaces that are unproven, unfixed, or surprising

An operator diagnosing this system in the first week will meet all five of these. None is a
defect introduced by the deploy; all four of the first are things that will look like one.

1. **Four `/admin` ops became reachable for the first time.** C10's admin-dispatch fix made
   `reslug`, `retype_page_type`, `seed_adr_rows` and `seed_task_from_pages` callable — they were
   registered but unreachable before. **`retype_page_type` is D23's sole sanctioned writer for the
   ADR supersede transition, and that transition has never run through `/admin`.** First execution
   is unproven; run it on a snapshot before running it for real.
2. **Superseded ADRs are now recall-invisible** (C8 / ADR-0228, amending ADR-0206's
   "down-weighted, never excluded"). They are excluded in the **stage-1 WHERE**, not ranked last.
   They remain reachable by `adr_get` and by the explicit opt-in tag. Anyone asking *"why can't I
   find ADR-XXXX any more"* has met this, not a bug.
3. **`get_recent_memories(limit)` is corpus-wide with no scope key at all**
   (`yadgar/_shared/storage/memory.py:1290` — the WHERE filters on `heat` and `is_protected`
   only). C10g observed the cross-project leak directly. **This train does not fix it.** It is
   named here so the next person to find it knows it is known.
4. **The pre-backfill read window is UNCHARACTERISED.** Between the deploy and the backfill,
   `recall()` for a project whose rows have `project_id = NONE` either returns **zero results** or
   **raises**. §8 step 5b of the remediation plan existed to determine which, and it did not run
   (§4). **Do not guess.** Until someone measures it, schedule a **maintenance window**, not a
   degraded window — the difference matters to whatever calls recall during the gap.
5. **Legacy columns are still written.** `directory_context` and the four legacy `directory`
   columns are **not** dropped by this train (ADR-0225 carve-out 2) and their writers **dual-write**
   deliberately: the backfill derives from them, and a row written with a `project_id` but no
   legacy value would be invisible to both the backfill and the un-migrated readers. The drop is a
   dedicated migration in a **later** PR, and that PR must kill the writers, not merely issue
   `REMOVE FIELD` — on a SCHEMALESS table a surviving writer re-creates the column untyped and
   `INFO FOR TABLE` still looks clean.

---

## 4. Rehearsal record (remediation plan §8) — **NOT EXECUTED**

§8 required a rehearsal on a restored snapshot before this train touches the live corpus, and
made the requirement sharp: *"the rehearsal must not assert 'the container came up.' That
assertion passes on the current, broken code."* Every step was a state check with a named
expected value.

**It did not run. The environment it names does not exist.** Measured 2026-08-12, not reasoned:

| §8 requirement | measured state |
|---|---|
| VM `192.168.122.101` | **unreachable** — `ping -c 1`: 100% packet loss; TCP/22: closed (`nc -z` non-zero); `virsh list --all`: **no domains defined** |
| snapshot `~/.local/share/yadgar/surreal_db.pre-vacuum-20260808_173247` | **absent.** The oldest surviving snapshot is `…pre-vacuum-20260809_020535`; also present are `…20260809_193051`, `…20260810_190748`, and `surreal_db-202608082020-preupdate` |

**No substitute was attempted, and that is a deliberate choice, not an omission.** The available
move — restore one of the surviving snapshots locally and boot a second engine against it — was
rejected on two grounds. This box runs the **production** yadgar daemon against
`~/.local/share/yadgar/surreal_db`, and a second engine pointed at a restored copy is one path
argument away from the live corpus; and heavy concurrent load on this box has already crashed
that production SurrealDB once (recorded during the v5.56 build train). A rehearsal that risks
the corpus it exists to protect is not a rehearsal.

**Nothing in §3 or §8 was executed against the live corpus. No migration ran. No backfill ran.
No admin op ran.**

### 4.1 What WAS executed, and what it does and does not prove

Everything below runs against **ephemeral engines** that pytest creates and destroys — never the
live corpus. This is the coverage tier **below** the rehearsal, not a substitute for it.

| §8 step | ephemeral-tier coverage | what it does NOT cover |
|---|---|---|
| 2, 3 (migration state) | `yadgar/tests/scripts/test_migration_031_project_id_backfill.py`, `…_032_drop_wiki_page_version_branch.py`, `…_033_project_id_other_tables.py`; `yadgar/tests/e2e/test_migration_032_wiki_page_version_branch_e2e.py` | a **restored real corpus** — the tests build their own rows, so they cannot catch a value shape only the live data has |
| 2 (Alembic head) | `yadgar/tests/_shared/test_mariadb_migrations.py`, `test_migration_fk_ordering_offline.py` | the migration running inside the **real backend image** |
| 4 (boot-failure fatality) | — | **uncovered.** Needs a deliberately broken chain in a real container |
| 5, 6 (backfill) | `yadgar/tests/backend/test_c6_project_id_backfill.py` | the **real mapping** over the **real 31 + 128 distinct `directory_context` values** — the manifest review is the point, and a fixture cannot rehearse a human decision |
| 7 (registry + guard) | `yadgar/tests/backend/test_project_registry.py` | guard behaviour against a populated production registry |
| 8, 9 (e2e ± ) | `make e2e` — the behaviour-contract suite, and `test_c3_enqueue_project_id_stamp.py`, `test_c04b_memorize_anchor_project_id_stamp.py`, `test_car_l_write_paths_stamp_project_id.py` | the **real drainer** against the real corpus |
| 10b (**result-set equivalence — the gate**) | — | **uncovered, and it is the most important gap.** Equivalence must be measured on the *same restored corpus* pre- and post-C7. It cannot be synthesised |
| 10c (latency) | — | uncovered; informational only, never the gate |
| 5b (pre-backfill read window) | — | **uncovered.** §3.6.4 carries the consequence forward |

### 4.2 Consequence

**The §9 exit criterion "§8 rehearsal complete, with steps 5, 8, 9 and 10 recorded verbatim"
remains UNTICKED.** It is recorded as not met rather than reinterpreted into something that
passed, because §8's own opening argument is that a rehearsal which asserts the wrong thing is
worse than none: it converts a loud failure into a silent one.

**The rehearsal is a precondition of §3, not of the merge.** The code is reviewed, gated and
green. Before an operator runs §3 against the live corpus, either the §8 environment is rebuilt
(a VM, a restored snapshot, the real backend image) or §3 is executed first on a restored
snapshot with §3.4's queries as the assertions — which is the same rehearsal under another name.

---

## 5. Plan lifecycle — the three breaks, resolved

C16's §5 listed three verified lifecycle breaks. Each is resolved differently, and the
differences are the point.

| break | resolution |
|---|---|
| `archive/0047-spine-train.md` **claimed in the PR body, did not exist** | **Created** — this file. Note this is a *create*, not a `git mv`: there was no source document to move. |
| `task-table-refactor-2026-07-29.md` — parent plan, unarchived | **Header corrected in place; deliberately NOT archived.** Its §6.2 cutover has never run, so ADR-0081's partial-ship clause applies: the plan stays live with a status header naming exactly what remains. The stale `Status: BUILD-READY … no code exists yet` line is **kept and marked falsified** rather than deleted — it is the record of what was believed, and a reader should be able to see it was overtaken. `ROADMAP.md` updated to match. |
| `archive/0047-car-K-nightly-archive-sweep.md` — `Status: shipped` while asserting untrue behaviour | **Re-verified and annotated.** C15a made the doc's two central claims true (`task.completed_at` in `002`; the RED test at `test_archive_sweep_car_k.py:447`). The gap is *recorded in the doc* rather than quietly closed, because "shipped" asserted it for three days that it was not. A third divergence C16 found while checking — §2 names `consolidation/archive_sweep.py`, but the sweep shipped as an admin op at `admin_exec/nightly_sweep.py` — is recorded in the same table. |

The remediation plan itself was archived by `git mv` as C16's first commit, per ADR-0082.

### 5.1 `[VERIFY]` — why `check-plan-signature-drift` did not catch the Car K divergence

**Verdict: out of remit, twice over. Both exclusions are deliberate and neither should be
widened.**

1. **The file is never read.** `scripts/check_plan_signature_drift.py:400` builds its corpus as
   `plans.rglob("*.md")` filtered by `_ARCHIVE_DIR not in p.relative_to(plans).parts` — so
   `docs/plans/archive/**` is excluded outright. The Car K doc lives there. The guard never
   opened it. The exclusion is reasoned in the module docstring (`:43`): archived plans are
   history and legitimately reference retired signatures, so *"rewriting shipped history to
   satisfy a lint would be the wrong repair."*

2. **Even in-corpus it would not fire.** The guard's whole mechanism is: build a map of
   `def NAME(...)` parameter sets from fenced Python blocks, find call sites of those same
   names, and fail on a kwarg absent from the signature. The Car K divergence is a **prose claim
   about a test's existence** — *"§4 step 3: RED `test_…`"* — which is neither a `def` nor a
   call site with kwargs. It is categorically outside what the guard models.

**No fix is warranted, and widening the hook is the wrong response.** A guard that verified prose
claims about test names against the tree is a different guard with a different failure mode
(every abbreviated or renamed reference in 60-odd plan docs goes red), and the module docstring
already records the design rule this train confirmed: *"a gate that cries wolf gets disabled,
which is strictly worse than no gate."* The class this missed is caught by the C16-style
re-verification pass instead — a human or agent reading a `shipped` claim against the tree.
