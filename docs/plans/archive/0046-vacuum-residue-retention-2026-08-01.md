# Vacuum residue retention — close the coverage gap, then fix the numbers

**Date:** 2026-08-01
**Task:** #0046 (~1 GB of vacuum residue on a host with a 224 MB live DB)
**Status:** DRAFT — not started.
**Target train:** `feat/v5.172-bug-train`.

---

## 0. CORRECTION UP FRONT — read this before anything else

**The brief's premise is substantially already built.** The task was scoped as
"extend the cleanup lifecycle to reap `vacuum_export_*.surql` + `.filtered`
siblings AND prune `surreal_db.pre-vacuum-*` under the existing retention policy".
Both mechanisms exist on `master` today, shipped in the v5.170 train
(`2df22256`, 2026-07-31) and labelled "Car 0046" in the source:

```
yadgar/core/vacuum/__init__.py:119    _VACUUM_EXPORT_KEEP_RUNS = 2
yadgar/core/vacuum/__init__.py:142    _reap_stale_pre_vacuum_snapshots(yadgar_home, keep_n)
yadgar/core/vacuum/__init__.py:157    _reap_stale_export_scratch(yadgar_home)
```

`git diff --stat master...HEAD -- yadgar/core/vacuum/` is empty, so this is not a
branch-local artefact.

**Therefore the live ~1 GB of residue is the retention policy working as
designed**, not a missing reaper. Rescoping accordingly, the car has four real
pieces — one genuine defect, one decision, one latent correctness bug, one hand-off:

| # | Item | Kind |
|---|---|---|
| A | `_reap_stale_export_scratch` never runs on 8 of the 11 exit paths | **defect** |
| B | The retention *numbers* (2 export pairs + 3 full DB copies) are wrong for the DB size | **decision** |
| C | `_run_cleanup_script` counts FILES, not runs — an orphan half-pair skews the window | **latent bug** |
| D | One-shot purge of the ~1 GB standing today | **hand-off** |

Anyone reviewing this plan against the brief should expect the scope to look
smaller than requested. It is smaller because most of it shipped.

---

## 1. Problem statement — with evidence

### 1.1 Live residue, and why it is "within policy"

| Artefact | Size | Retention rule | Verdict |
|---|---|---|---|
| `vacuum_export_20260729_190709.surql` + `.filtered` | 206 MB (1 pair) | `_VACUUM_EXPORT_KEEP_RUNS = 2` prior pairs = 4 files | kept — under the ceiling |
| 3 × `surreal_db.pre-vacuum-*` | 300 / 286 / 222 MB | `VACUUM_SNAPSHOT_RETENTION = 3` | kept — exactly at the ceiling |
| live `surreal_db` | 224 MB | — | — |

**~1.01 GB of residue guarding a 224 MB database.** The mechanism is fine; the
ceilings were chosen against a 2.4 GB DB (the pre-ADR-0178 era, when a single
`.old` was 2.4 GB and forensics mattered more) and never revisited after vacuum
started actually reclaiming.

This is not cosmetic. `_has_free_space` (`__init__.py:1433-1456`) requires **2.5×
the DB size free** and a shortfall is a `return 0` SKIP. Car 0092's own comment at
`:1290-1295` documents exactly this wedge: accumulated residue eats the headroom,
the free-space preflight starts failing, and vacuum silently becomes a permanent
no-op with a green timer.

### 1.2 (A) The export reap runs on almost no exit path

`_reap_stale_export_scratch` is called from **three** sites, all inside
`_vacuum_finalize` (`__init__.py:1144`, `:1173`, `:1200`). `_vacuum_finalize` is
reached only after Phase 3 succeeds. Every earlier exit skips it:

| Exit | Line | pre-vacuum reaped? | exports reaped? |
|---|---|---|---|
| sensitive-lock held by another job | `:1582` (in `cmd_vacuum_impl`) | no | **no** |
| swap-recovery failed | `:1618` | no | **no** |
| canonical dir missing | `:1627` | no | **no** |
| backend unreachable | `:1629` | no | **no** |
| preflight SKIP (no surreal / low disk) | `:1651` | **yes** | **no** |
| source-count capture failed | `:1665` | no | **no** |
| **export phase raised** | `:1674` | no | **no** — and this path can leave a *partial* `.surql` behind |
| snapshot/drop failed | `:1689` | **yes** | **no** |
| Phase 3 aborted | `:1704` | **yes** | **no** |
| finalize (all 3 branches) | `:1144/:1173/:1200` | yes (`:1207`) | yes |

`_reap_stale_pre_vacuum_snapshots` got the abort-path coverage in Car 0092;
`_reap_stale_export_scratch` never did. **A host that keeps aborting in Phase 3 —
which is exactly what a container-only or low-disk host does — accumulates export
pairs without bound.** That is the true defect behind the observed residue.

### 1.3 (C) Pair-awareness

```python
_run_cleanup_script(yadgar_home, "vacuum_export_*", _VACUUM_EXPORT_KEEP_RUNS * 2)
```
(`__init__.py:162`, helper at `phases.py:32-54`)

The helper globs, sorts by mtime, and deletes everything past index `keep_n`. Two
problems:

- It counts **files**, and "2 prior runs" is expressed as `2 * 2 = 4` files. A run
  that wrote `vacuum_export_TS.surql` and then died before writing
  `.filtered.surql` (the `:1674` path above) leaves an odd file. The window then
  holds 2 full pairs *minus* one, or 1 pair plus 2 orphans — the semantics drift
  silently.
- mtime ordering does not guarantee a pair stays together. The raw and filtered
  files are written seconds apart, so with 4 files retained and an orphan present
  the oldest surviving "pair" can be half-deleted — leaving an unusable artefact
  that still costs 100 MB.

The timestamp is already in the filename (`vacuum_export_%Y%m%d_%H%M%S`,
`phases.py:100-102`). Grouping by that prefix is exact and cheap.

### 1.4 Knob binding — checked, NOT inert

Worth recording because it looks wrong at a glance: `config_registry.py:568`
declares `YADGAR_VACUUM_SNAPSHOT_RETENTION` while `config.py:563` declares the field
as `VACUUM_SNAPSHOT_RETENTION`. **This binds correctly** —
`config.py:1024` sets `model_config = {"env_prefix": "YADGAR_"}`, so the field
resolves from the prefixed env name. The knob is live, not decorative.

(Neighbouring inconsistency, flagged not fixed: `VACUUM_OLD_MAX_AGE_DAYS` is read
via a raw `os.getenv` at `__init__.py:863` and therefore carries **no** prefix —
matching its registry entry at `config_registry.py:567`, but breaking the
convention every other knob follows. Out of scope; do not "fix" it in this car,
it would silently un-bind a live knob on hosts that set it.)

---

## 2. The decided approach

### 2.1 (A) One reap site that every exit path reaches

Introduce `_reap_vacuum_residue(yadgar_home, keep_n)` calling both existing reapers,
and invoke it from **one** place: a `finally` in `cmd_vacuum_impl` around
`_cmd_vacuum_body`, alongside the `sensitive_lock.release()` that already has the
every-exit-path property (`__init__.py:1583-1588`).

Why the single wrapper rather than adding calls to each early return:

- 11 exit paths today; the two previous attempts at this (Car 0092 for snapshots,
  Car 0046 for exports) each missed some. A `finally` cannot be missed.
- It also covers exceptions, which no `return`-site patch does.
- The current-run artefacts are the **newest**, so a keep-newest-N policy retains
  them for forensics on an abort — which is the behaviour ADR-0076 D2 intends. The
  wrapper does not need to know whether the run succeeded.
- On the success path `_vacuum_finalize` has already deleted this run's own pair
  (`_delete_export_scratch`, `:1199`); the wrapper then prunes older ones. No
  double-delete: `_run_cleanup_script` tolerates a missing file and logs.

Leave the existing in-finalize calls in place or delete them — **recommend
deleting** the three in-finalize `_reap_stale_export_scratch` calls and the one
`_reap_stale_pre_vacuum_snapshots` call at `:1207`, plus the abort-path calls at
`:1649/:1688/:1703`, so there is exactly one reap site. Two sites doing the same
work is how the coverage drifted in the first place.

**One of those call sites carries a comment arguing for its position — check it
before deleting, and say why the move is safe.** `:1647-1649` prunes snapshots
*before* `_log_vacuum_skip` on the low-disk branch, with the comment: "A wedged host
reaches the low-disk branch BECAUSE stale `.pre-vacuum-*` dirs ate the headroom;
prune before skipping so a later run can proceed." The intent is that the **next**
run finds headroom, not that this run recovers — so running the prune from the
`finally` (i.e. a few milliseconds later, after the skip row is logged) satisfies it
exactly. Preserve that reasoning as a comment on the new single call site;
otherwise a reviewer reads the deletion as a regression of the Car 0092 wedge fix.

**Each cleanup step in the shared `finally` needs its own try/except** — a raising
reap must not skip `sensitive_lock.release()` (or, once 0113 lands, the maintenance
exit). `_restart_services_after_abort` (`__init__.py:687-706`) is the in-file
precedent and its docstring explains the reasoning; follow it.

**Rejected:** a systemd `.timer`-driven external cleanup unit. It would need its own
knowledge of the retention policy on four install surfaces, and the wedge it must
prevent (low disk → SKIP) is *inside* the vacuum run.

### 2.2 (B) The numbers — and the never-zero floor

| Knob | Today | Proposed | Reason |
|---|---|---|---|
| `VACUUM_SNAPSHOT_RETENTION` | 3 | **2** | The snapshot is the last-resort recovery anchor for ADR-0090's chronic unclean close. The 2026-07-10 recovery used **one** quiesced copy (`cp -a` the latest). Two gives a fallback if the newest snapshot is itself torn — which is the only scenario where a third would help, and by then the `.surql` export path (ADR-0090's stated fallback) applies. Three is one copy of insurance against a scenario nobody has hit. |
| `_VACUUM_EXPORT_KEEP_RUNS` | 2 prior runs | **1 prior run** | The scratch is *diagnostic*, not recovery. The current run's failure plus the immediately preceding one is what a debugging session reads; a third has never been consulted. |
| new `VACUUM_SNAPSHOT_MAX_AGE_DAYS` | — | **14**, exempting the newest | Mirrors ADR-0076 D1's `.old` age backstop (`_reap_stale_old_dirs`, `:867-896`). A host that vacuums rarely should not carry a six-month-old snapshot of a DB that no longer resembles the live one. |

**The never-zero floor is enforced in code, not by documentation:**
`_reap_stale_pre_vacuum_snapshots` clamps `keep_n = max(1, keep_n)`, and the age
backstop always exempts the newest snapshot regardless of age. So the two ways a
user could reach zero — setting `YADGAR_VACUUM_SNAPSHOT_RETENTION=0`, or a host
that has not vacuumed in a year — are both closed. **A vacuum must never leave the
host with no rollback anchor.** Assert this as a dedicated test (§4, test 8), not a
comment.

Expected steady state after the change on this host: 2 snapshots (~520 MB) + at most
1 prior export pair (~206 MB) ≈ 726 MB, and the age backstop trims further on a
regularly-vacuuming host. Against a 224 MB DB that is still generous, which is the
correct bias for a data-safety artefact.

**Rejected:** compressing the export scratch (`gzip` the `.surql`). It changes the
artefact format for a ~5× win, no consumer reads it programmatically, and it makes
the forensic path require a decompress step at exactly the moment someone is
debugging. Scope creep on a retention car.

### 2.3 (C) Pair-aware retention

Add `_reap_export_pairs(yadgar_home, keep_runs)` in `yadgar/core/vacuum/phases.py`
next to `_run_cleanup_script`:

- glob `vacuum_export_*`, extract the `%Y%m%d_%H%M%S` stamp from the filename
- group by stamp; sort groups by stamp descending (the stamp **is** the ordering —
  no mtime dependence, so a `touch` or an rsync cannot reshuffle the window)
- keep the newest `keep_runs` groups, delete every file in every older group
- any file that does not parse into a stamp is treated as its own oldest group and
  deleted — that is the orphan/partial case from `:1674`

`_run_cleanup_script` stays as-is for the snapshot dirs (a snapshot is one dir, so
file-counting is correct there).

### 2.4 (D) One-shot purge of the standing ~1 GB

**Hand-off via `MIGRATION_NOTES.md`, not an automatic purge.** These are the user's
DB snapshots; deleting them is a state mutation on live recovery artefacts and the
repo's standing rule is to hand such commands over. The notes must:

- list the exact paths and sizes observed
- **keep the newest `surreal_db.pre-vacuum-*`** — the command must be written so
  that the newest survives even if the user pastes it blindly
- state that after the code change the first successful vacuum will bring the host
  to the new steady state on its own, so the purge is an accelerator, not a
  prerequisite

---

## 3. Exact files and functions to change

| File | Change |
|---|---|
| `yadgar/core/vacuum/phases.py` | new `_reap_export_pairs(yadgar_home, keep_runs)`; `_run_cleanup_script` unchanged (snapshots only); add the `max(1, keep_n)` clamp where snapshots are pruned. |
| `yadgar/core/vacuum/__init__.py` | `_VACUUM_EXPORT_KEEP_RUNS` 2 → 1; `_reap_stale_export_scratch` delegates to `_reap_export_pairs`; new `_reap_stale_snapshots_by_age`; new `_reap_vacuum_residue(yadgar_home, keep_n)`; single call site in `cmd_vacuum_impl`'s `finally` (`:1583-1588`); remove the seven scattered reap calls (`:1649`, `:1688`, `:1703`, `:1144`, `:1173`, `:1200`, `:1207`). |
| `yadgar/_shared/config/config.py` | `VACUUM_SNAPSHOT_RETENTION` 3 → 2; new `VACUUM_SNAPSHOT_MAX_AGE_DAYS = 14`. |
| `yadgar/_shared/config/config_registry.py` + `config_yaml.py` | matching entries — the three-way sync is pre-commit-enforced, all three or the hook fails. |
| `docs/reference/configuration.md` | rows for the new/changed knobs (`:606-607` is where the vacuum block already is). |
| `docs/CHANGELOG.md` | the ADR-0076 D2/D3 supersession, stated explicitly (§7). |
| `MIGRATION_NOTES.md` (untracked, ADR-0116) | the one-shot purge commands. |

---

## 4. The TDD story

**CI gating asymmetry.** `.forgejo/workflows/ci-pr.yaml` runs by directory:
`test-fast` = `yadgar/tests/{scripts,server,hooks,_meta,clients}/`, `test-shared` =
`yadgar/tests/_shared/`, `test-backend` = `yadgar/tests/backend/`, `test-core` =
`yadgar/tests/core/`. `yadgar/tests/integration/` is **not** gated in `ci-pr`.
Everything below goes in `yadgar/tests/core/`, extending
`yadgar/tests/core/test_vacuum_cleanup.py` (which already owns this seam) except
where noted.

### 4.1 RED first — coverage (item A)

1. **`test_residue_reaped_on_every_exit_path`** — parametrized over all 11 exits
   (lock-held, recovery-fail, missing-canonical, backend-unreachable, skip-no-surreal,
   skip-low-disk, count-capture-fail, export-fail, snapshot-fail, phase3-abort,
   finalize-success). For each: pre-seed 5 export pairs + 5 snapshot dirs, run
   `cmd_vacuum_impl`, assert the window shrank to the configured size. **RED today
   for 8 of the 11 on the export half.** This single parametrized test is the car.
2. **`test_residue_reaped_when_body_raises`** — patch `_cmd_vacuum_body` to raise;
   assert the reap still ran (the `finally` property, unreachable by any
   return-site patch).
3. **`test_current_run_export_survives_an_abort`** — on an abort the newest pair is
   retained for forensics. Guards against "fix the leak, lose the diagnostics".

### 4.2 RED first — pair-awareness (item C)

4. **`test_orphan_raw_export_is_reaped_without_eating_the_window`** — seed 2 complete
   pairs plus 1 orphan `.surql`; assert the orphan is gone and the newest complete
   pair is intact. Under today's file-counting this fails.
5. **`test_pairs_are_never_half_deleted`** — after any reap, every surviving stamp
   has either both files or (for the in-progress case) is the newest.
6. **`test_retention_window_is_stamp_ordered_not_mtime_ordered`** — `os.utime` an old
   pair to "now"; assert the *newest by stamp* still survives and the touched old
   one is still reaped.

### 4.3 RED first — the numbers and the floor (item B)

7. `test_default_snapshot_retention_is_two` / `test_default_export_keep_runs_is_one`
   — plus the three-way-sync assertions the repo already uses
   (`yadgar/tests/server/test_config_three_way_sync.py` is gated in `test-fast`; a
   new knob must appear there too or the pre-commit hook fails locally but CI is
   only half-covered).
8. **`test_snapshot_retention_never_reaches_zero`** — parametrized over
   `keep_n ∈ {0, -1}` **and** over an age-backstop run where every snapshot is
   older than the cutoff; in every case at least one `surreal_db.pre-vacuum-*`
   survives. **This is the criterion that protects the rollback anchor**; if it can
   pass with the clamp deleted, it is mis-written.
9. `test_age_backstop_exempts_the_newest_snapshot`.

---

## 5. Verification

**Local**

1. `pytest yadgar/tests/core/ -k "vacuum"` — the whole vacuum suite, since
   `cmd_vacuum_impl`'s control flow gains a `finally`.
2. `pytest yadgar/tests/server/test_config_three_way_sync.py yadgar/tests/server/test_config_default_values.py`
   — both are separately gated CI steps (`ci-pr.yaml:418-419`) and both will fail on
   a half-registered knob.
3. Pre-commit `check-config-three-way-sync`.

**On the workstation (the host carrying the residue)** — read-only until the user
runs the purge:

4. `du -sh ~/.local/share/yadgar/vacuum_export_* ~/.local/share/yadgar/surreal_db.pre-vacuum-*`
   before and after the first post-change vacuum. Expect the export window to drop
   to ≤1 prior pair and the snapshot window to 2.
5. Confirm at least one `surreal_db.pre-vacuum-*` remains — the floor, observed
   rather than asserted.

**Fresh VM — `192.168.122.101`** is **not** required for this car: every behaviour
is filesystem-level and provable in `tmp_path`. Say so rather than adding a VM step
out of habit; the VM's value is unit-rendering and PATH inheritance (cars 0111 and
0107), neither of which this car touches.

---

## 6. Rollback story

Two independently revertible halves:

- **The numbers** (B) revert by changing three constants back. Reverting does **not**
  restore deleted artefacts — a purged snapshot is gone. That is the irreversible
  part of this car, and it is why (D) is a hand-off with a keep-the-newest guard
  rather than an automatic sweep.
- **The plumbing** (A + C) reverts as a normal code revert; the reap becomes
  finalize-only again and residue re-accumulates on abort paths.

Data-safety framing: the worst case of a bug in this car is deleting a recovery
artefact we meant to keep. That is why the floor test (§4.3 #8) is the acceptance
criterion with the most weight, and why the age backstop exempts the newest
unconditionally rather than "unless it is older than X".

---

## 7. ADRs

- **ADR-0076 (accepted) D2/D3 are partially superseded — state it explicitly.**
  D2's export-scratch policy ("deleted on successful finalize, kept only on failure")
  gains a bounded ceiling **and** whole-lifecycle coverage; the snapshot retention
  number changes. The v5.169 CHANGELOG entry (`docs/CHANGELOG.md:161`) already set
  the house precedent for this: it spells the supersession out in the CHANGELOG
  rather than leaving it to inference. Follow that form exactly.
- **A new ADR is warranted** because the retention *numbers* change — that is a
  standing operational contract, and the reasoning (one quiesced copy is what the
  2026-07-10 recovery actually used; the floor is enforced in code) is the kind of
  thing a future reader will otherwise re-litigate. It should supersede ADR-0076's
  D2 and D3 rows specifically, not the whole ADR.
- **ADR-0090 (open)** is the reason a snapshot floor exists at all — cite it as the
  justification for `max(1, keep_n)`, do not supersede.
- ADR-0178 is untouched (this car changes nothing about verification).

---

## 8. Ordering / dependencies vs the rest of the train

- **Sequence AFTER 0113.** Both cars add a `finally` around `_cmd_vacuum_body` in
  `cmd_vacuum_impl` (`__init__.py:1583-1588`). Landing them in either order works,
  but doing 0113 first means this car nests inside an existing structure rather than
  inventing one that 0113 then has to re-shape. If they land in the same PR, write
  **one** `finally` that does both (maintenance exit, then residue reap, then lock
  release — in that order, so a reap failure cannot leave the engine gated), with
  **each step in its own try/except** so no one failure skips the rest (§2.1).
- **Independent of 0111 and 0107** in behaviour. 0111 touches
  `yadgar/core/vacuum/phases.py` (`_vacuum_snapshot_and_drop`) and this car touches
  the same file (`_run_cleanup_script` / new `_reap_export_pairs`) — different
  functions, textual conflict risk only.
- Worth noting for train sequencing: **0111 + 0113 make aborts rarer**, which
  reduces the residue rate but does not remove the unbounded path. This car is not
  obviated by them.

---

## 9. Explicitly out of scope

- Compressing the export scratch (§2.2).
- Moving the residue under `{DATA_DIR}/backups/` — ADR-0076 D4 deliberately keeps
  `.old` / `.pre-vacuum` DB-adjacent because the atomic-rename requires same-filesystem
  siblings. Do not "tidy" them into the backups tree.
- The `VACUUM_OLD_MAX_AGE_DAYS` prefix inconsistency (§1.4) — flagged, not fixed;
  changing it would un-bind a live knob.
- `surreal_db.old-*` retention (ADR-0076 D1, already has an age backstop at
  `__init__.py:867-896`) and `surreal_db.CORRUPT-*` (ADR-0090 forensics, operator-owned).
- Any change to what the export contains or how the swap works.
