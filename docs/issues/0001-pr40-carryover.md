# PR #40 carryover — findings not fixed in this PR

**Status:** open · **Raised:** 2026-08-12 · **Source:** PR #40 (`feat/spine-0047-train`, core `5.181.43` / backend `5.72.33`)

Everything here was found while taking PR #40's CI from **49 failures to 1**. Each item is
either deliberately out of scope, blocked on an operator action, or a follow-up the fix
itself created. Nothing in this file is fixed.

The single remaining CI failure is `test_daemon_obs_gauges.py::test_loop_lag_monitor_spikes_when_loop_blocked`
— a timing flake that predates this PR.

---

## 0. BLOCKING FOR DEPLOY — ~42% of the memory corpus goes dark

Measured on the live corpus 2026-08-12 via read-only `db_inspect`, **3070 memory rows total**:

| bucket | rows | fate after this PR |
|---|---:|---|
| real `owner/repo` (`m-agahi/yadgar` 1022, `quinyx/*`, …) | 1774 | reachable |
| `local/*` phantoms (incl. `local/system` 604) | 885 | **unreachable** |
| `project_id = 'global'` **without** the `global` reach tag | 343 | **unreachable** |
| unstamped (`project_id` NONE/NULL) | 68 | **unreachable** |

**1296 rows (42%) become unreachable the moment the read flip lands.** Three independent causes:

1. **`local/*` phantoms** — written by `derive_project_id`'s `_local_fallback`, which was
   `os.path.basename` on a string and therefore could not fail. It returned `local/<basename>`
   silently inside both containers (neither installs git; no host project dirs are mounted).
   ADR-0227 deleted it. A caller in `/home/max/aws-work` now resolves to a real `owner/repo`,
   so the 139 `local/aws-work` rows no longer match anything.
2. **`project_id = 'global'` as a VALUE** — the design says `project_id` is always a real
   registered project and cross-project reach is a **tag**. The stage-1 clause is
   `project_id = $sc_pid OR $sc_reach IN tags`. Only **3 of 346** such rows carry the tag;
   the other 343 match neither arm.
3. **Unstamped rows** — `is_project_eligible` no longer admits `project_id IS NONE`. The old
   permissive `{'global', '', None}` sentinel set is gone by design (ADR-0227).

**Consequence:** the system boots and is fully usable *for correctly-stamped projects*, but a
large slice of history is invisible until the C6 backfill re-keys it. **The backfill is not
optional — it is the difference between "works" and "works and remembers".**

Known intent: the 604 `local/system` rows are `_memify_derive` junk already marked for deletion
(decision D3), so the true re-key target is ~692 rows.

### Deploy order (from `docs/plans/archive/0047-spine-train.md` §3)

`rows → review → backfill → guard → read flip → re-seed agent prompts`

- **Core and backend images must ship together** — C7 re-keyed `RecallRequest`, which is `extra="forbid"`.
- **Migration aborts are SILENT** (ADR-0222): the lifecycle swallows the exception, the container
  reports `active (healthy)` with `NRestarts=0`. §3.4 gives one verification query per migration.
  "It booted" proves nothing.
- The backfill has **never executed**. It returns a manifest and refuses to write without `dry_run=False`.

---

## 1. Left alone — production defects, real but out of scope

### 1.1 `recent_episode_count` has the identical bug F7 just fixed
`_project_brief_catalog_full`'s `recent_episode_count` query still binds `directory_context = $dir`
on a **raw filesystem path**, while writers stamp that column from the resolved `project_id`
(C10f). It returns nothing. No test asserts on it positively, so it is invisible — the same
condition that hid the anchor/hot-memory breakage for the whole train.

### 1.2 `memorize(wait=False)` reports `queued` before the stamp is validated
The backend re-validates `project_id` at INSERT (`_ensure_project_exists_sync`) and can DLQ a job
this path already reported accepted. **Car F9 made this window reachable for the first time** —
previously every sentinel write was refused synchronously. `wait=True` is the wrong fix (5s
`WIKI_WRITE_WAIT_TIMEOUT_SECONDS` behind a 2s SessionStart `urlopen` timeout). Recovery channel is
the DLQ plus `project_brief`'s `pending_rejections_count` / `review_rejections`, correlated by
`queue_id` (logged at INFO on consume).

### 1.3 The scoping guarantee rests on one resolver never returning empty
`build_project_scope_clause` returns `("", {})` on a falsy `project_id`, and `is_project_eligible`
returns `True` on a falsy caller. **An empty resolved project silently unscopes everything, on both
the SQL and the residual arm.** This is by design — the resolver is supposed to raise upstream —
which makes C5's resolver a single point of failure with no independent guard behind it.

### 1.4 Pre-existing NULL embeddings were never swept
Car F1 stopped the writers producing them but did not clean history. Reads are now safe either way,
but `stats.py:439`'s `dq_null_embedding_count` filters on `IS NONE` alone and therefore
**under-counts exactly these rows**.

### 1.5 One-time sentinel burst on first SessionStart after deploy
Sentinel files already in `~/.local/state/yadgar/session-ends/` predate Car F9 and carry no
`project_id`. They are retired to `failed/` at ERROR, one line each — correct (they name no identity
and nothing may invent one), but visibly noisy, and their `last_human_turns` / `pending_findings`
are not imported.

---

## 2. Left alone — test/infrastructure debt

### 2.1 Ten test files raw-`CREATE` into `memory`, bypassing the C5b chokepoint
The AST guard only scans `yadgar/_shared/storage`, so test helpers writing raw SQL pin the retired
directory convention invisibly. Car F11 audited all ten and found **none currently latent** — each
either reads by direct id, threads `project_id` correctly, or is deliberate (`test_vector_null_embedding_guard.py`
raw-inserts precisely to bypass F1's writer fix). Inventory taken; no sweep performed. Worth a
guard that reaches test helpers, not a one-off cleanup.

### 2.2 HF-hub SSL finalizer reds slow recall tests nondeterministically
A GC finalizer raises `PytestUnraisableExceptionWarning`; under the repo's `filterwarnings = ["error"]`
it lands on whichever slow recall test is running. Reproduced at train-head, so not caused by any car.
Did not fire in the final full run (0 occurrences), which makes it *intermittent*, not gone.

### 2.3 `make ci-local` runs its four legs serially and idles 85% of the box
Measured: **211 MB peak pytest RSS against 28 GB free**, load 2.3 on 24 cores, 2h49m wall
(fast 1:01, shared 0:14, backend 0:10, core 1:04). The 20G OOM that motivated serializing came from
accumulating 10,492 tests in **one** process for 1h32m — not from any leg being heavy. Legs could run
concurrently with enormous headroom and finish in ~64 min. Car F10 fixed the OOM correctly but
overcorrected on throughput.

### 2.4 There is no master CI baseline to A/B against
`ci-pr.yml` triggers on `pull_request` only; `ci-release.yml` (which runs on master pushes) contains
zero pytest invocations. Every "introduced by this PR" verdict in this train rested on
`git diff master...HEAD` plus several failing test files being provably untouched by the branch —
good evidence, not proof.

---

## 3. Carried from before this round

### 3.1 The §8 rehearsal never ran — exit criterion UNTICKED, not waived
The VM is unreachable (`virsh list --all` defines no domains) and the named snapshot is gone.
Improvising on the dev box was rejected: it runs the production daemon, and a second engine on a
restored copy is one path argument away from the live corpus. **Unverified as a result:** step 10b
(C7's result-set equivalence gate) and step 5b (the pre-backfill read window — hence *maintenance*
window, not degraded window, in the runbook).

### 3.2 `directory` is not gone, by design
C5 removed its ability to *resolve*, not its existence. It remains a parameter on **46 tools** in two
classes: `resolve_effective_project` (recall, memorize, anchor, restore, wiki_add, adr_*) **raises**
without `project=`; `accept_project_param` (project_brief, block_*, checkpoint, wiki_list) is still
directory-keyed with `project=` validated only. **A blanket `directory=` sweep breaks the second class.**
Zero-directory is the next PR's bar.

### 3.3 `directory_context` now carries two meanings
New writes stamp it from the resolved `project_id` (`owner/repo`); legacy rows hold filesystem paths.
The C6 backfill still works because it targets legacy rows — but **the PR #40 body's claim that
"`directory_context` survives as a written column because the backfill derives from it" is now only
half true**, and any other reader of that column must know which era a row is from. The PR body needs
this correction.

### 3.4 `fix/prompt-recall-latency-and-visibility` is complete but unpushed
Sits off master, separate from this train. ~310 ms removed; `TIME_BUDGET` untouched at 0.5.
`yadgar/core/hooks/prompt-recall.py` was OFF LIMITS to every car this train because that branch
rewrites it (241 → 127 lines).

### 3.5 Three DLQ pages still parked
Left in place deliberately. Cause is the similarity gate rejecting cross-referencing syntheses
(follow-up task #44).

---

## 4. Fixed in PR #40 — recorded so nobody re-opens them

- **`cosine()` NULL crash** — SurrealDB `NONE ≠ NULL`; `IS NOT NONE` admits a NULL and *vice versa*.
  Both guards now on all three brute-force arms, plus the four writers producing NULL. One bad row
  killed the **entire** scoped query, not just that row. KNN arms were always immune.
- **`project_id` NULL vs `option<string>`** — `project_id_set_fragment()` at `insert_profile` /
  `insert_belief`; all nine migration-033 tables swept.
- **`project_brief` anchors + hot memories returned nothing** (session-start context injection).
- **Session-end capture never wrote a sentinel row** — two ordered defects; fixing either alone left
  it dead.
- **Four vacuous guards** — the stage-1 WHERE clause could be deleted entirely with every scoping test
  still green; two negative assertions passed *because* their bucket was empty; a Makefile invariant
  was about to go blind through one layer of delegation; an expiry test was proven to pass with the
  expiry filter removed.
- **`make e2e` was never the gate** — it runs `yadgar/tests/e2e/ -m e2e`, disjoint from CI's four
  subsystem jobs. `make ci-local` now reproduces them, with a drift guard pinning leg *structure*.

### Two claimed leaks were DISPROVED, not fixed
`test_other_project_excluded` and `test_agent_in_proj_A_does_not_see_proj_B_writes` both looked like
cross-project leaks. Both were tests seeding every row under **one** `project_id` and varying only
the directory. Car F6 captured every emitted query during a live scoped recall: all six arms bind
`(project_id = $sc_pid OR $sc_reach IN tags)`, **zero mention `directory_context`**, and a genuinely
foreign row was excluded. The PR's central promise holds.

---

## Cross-references

- Plan: `docs/plans/archive/0047-pr40-remediation-2026-08-10.md`
- Runbook: `docs/plans/archive/0047-spine-train.md` §3
- ADRs: 0225 (`directory` retired), 0227 (host-side minting, no fallbacks), 0228 (superseded-ADR
  exclusion), 0218 (a car never runs the full unit suite), 0222 (silent migration aborts),
  0206 (stage-1 push-down, not post-filter)
- Tasks: #40 (gate the `global` reach tag), #41 (`_memify_derive` junk), #43 (prompt-recall dedup),
  #44 (similarity gate eats syntheses)
