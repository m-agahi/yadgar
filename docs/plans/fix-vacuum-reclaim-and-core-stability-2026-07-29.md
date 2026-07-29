# Fix vacuum reclaim non-persistence + vacuum-path core stability

**Date:** 2026-07-29
**Tasks:** #0045 (vacuum reclaim does not persist) · #0027 (core cascade-dies during consolidation/vacuum)
**Status:** PLANNED — awaiting decisions D1–D4 before implementation.
**Target train:** `feat/v5.169-install-runtime-fixes` (ONE car, two independent defects).
**Sibling plan (same seam family, different defect):** `docs/plans/fix-vacuum-trigger-path-and-watcher-2026-07-29.md` (task:0044).

---

## 0. Verdict up front

| Task | Still real? | Verdict |
|---|---|---|
| **#0045** vacuum reclaim does not persist | **YES — 7 consecutive rollbacks across manual and nightly triggers (07-24 → 07-28)** | Root cause found and proven. Ship the fix. |
| **#0027a** core left stopped on vacuum abort paths | **YES — real defect, but LATENT** | Cited code defect; did **not** cause the reported symptom. Ship the fix anyway (it is a data-availability landmine). |
| **#0027b** core cascade-dies during consolidation | **NOT REPRODUCING** | Zero SIGKILL evidence across 61 core restarts / 15 days. **Recommend closing** (see §5). Do not design a fix. |

**These are two defects at one seam, not one root cause.** Both live in `yadgar/core/vacuum/__init__.py`
and both are exercised by the same nightly run, but their fixes are independent. Do not manufacture a
shared cause — the evidence does not support one.

---

## 1. Bug #0045 — root cause (PROVEN, not hypothesised)

### 1.1 The chain

1. `_vacuum_finalize` verifies the swapped-in compacted DB by POSTing
   **`{core_url}/api/check_invariants`** — `yadgar/core/vacuum/__init__.py:889`.
2. **That route does not exist.** `check_invariants` is an MCP *tool* only
   (`yadgar/core/server/tools/admin_invariants.py:20`, a thin shell that calls
   `_forward_admin("check_invariants", {})` at `:32`). An exhaustive search for the string
   `api/check_invariants` across the repo returns **three hits, all inside `vacuum/__init__.py`
   itself** (`:871`, `:889`, `:962`) — the URL is written nowhere else and served nowhere.
3. `_check_invariants_verified` treats **any** non-2xx as not-verified
   (`__init__.py:895-898`; the docstring at `:871-877` explicitly names "404 while core boots"
   as a not-verified case).
4. Not-verified → `_rollback_swap_on_finalize_failure` (`:996`) → `_restore_db` (`:571-602`):
   `rmtree` the compacted canonical, `os.rename` the retained **original** `surreal_db.old-<ts>`
   back to `surreal_db`, restart the backend.
5. `after_bytes` is captured at **`:1258`, BEFORE `_vacuum_finalize` is called at `:1262`**.
   `_vacuum_report_and_log` (`:1066-1097`) then prints
   `Before / After / Saved (N%)` **unconditionally** — the pre-rollback number — and writes the same
   fabricated numbers into the `consolidation_log` table.

**Net effect: every vacuum swaps in a genuinely compacted DB, then rolls it back ~1 minute later,
and reports a large saving. The exit code (2) is the only truthful output, and nothing surfaces it.**

### 1.2 Live evidence

**The 2026-07-24 observation, reconstructed from `~/.local/share/yadgar/logs/yadgar.log`:**

```
2026-07-24T12:59:04.427Z  POST /api/control/action/vacuum   200   <- the viz-UI trigger
2026-07-24T13:00:19.738Z  POST /api/check_invariants        404   <- 75 seconds later
```

75 s is the "roughly ONE MINUTE later" in the bug report. The 404 is the rollback trigger.

**It has fired on every subsequent run:**

```
07-24T13:00:19  404   (manual, viz UI)
07-24T19:07:29  404   (nightly)
07-25T19:07:23  404   (nightly)
07-26T02:29:33  404   (manual/triggered)
07-26T19:08:35  404   (nightly)
07-27T19:08:42  404   (nightly)
07-28T19:08:05  404   (nightly)
```

**Unit exit codes corroborate:**

```
yadgar-vacuum.service        ExecMainStartTimestamp=Sun 2026-07-26 04:28:20 CEST  ExecMainStatus=2
yadgar-nightly-cycle.service ExecMainStartTimestamp=Tue 2026-07-28 21:00:00 CEST  ExecMainStatus=40
```

`2` is `cmd_vacuum_impl`'s `return 0 if finalize_ok else 2` (`:1280`) — "swap ROLLED BACK". `40` is
`nightly_cycle.py`'s documented "vacuum failed" code (`yadgar/core/scripts/nightly_cycle.py:26`).
Because the 404 *arrived*, `check_invariants` was reached — which rules out the two earlier
finalize early-returns (`:958` core-health, `:985` inode-coherence) and pins the exit to the
`else` branch specifically.

**On-disk state agrees:**

- `~/.local/share/yadgar/surreal_db` is **2.4 GB**; `check_invariants` reports
  `db_size_bytes=2_524_439_043`, `vlog_size_bytes=2_332_629_521` (**92 % vlog**), `size_warning=true`.
- The canonical dir's top-level mtime is **10 jul 13.29** — the ADR-0090 recovery date. A completed
  swap would have replaced it. Every swap since has been rolled back to this same original inode.
- **Six retained export scratch pairs** (07-24 … 07-28, ~200 MB/night, **1.2 GB of scratch**).
  `_delete_export_scratch` (`:851`) runs *only* on the verified branch (`:994`), so their presence
  is a per-run failure receipt.
- No `surreal_db.old-*` dirs remain — consistent with `_restore_db` renaming them back.

### 1.3 How it regressed

- `6da60b49 fix(vacuum): warn-only on post-restart check_invariants 404 (v5.7.0 PR-2)` made a 404
  non-fatal — the endpoint was already missing then.
- `627ec051` (P0 #37, post-07-09 split-brain incident) **deliberately reversed** that to a hard
  rollback. The docstring at `__init__.py:875-877` states the reversal in as many words.

The hardening was correct in intent (an unverified swap must not be retained) and was armed against
a verification endpoint that has never existed. **The fix is to make the verification call real, not
to weaken the guard.**

### 1.4 Why "it completed successfully" was believed

`_vacuum_report_and_log` is unconditional and uses pre-rollback numbers. The `consolidation_log`
rows carry the same fiction. **Every historical `consolidation_log` vacuum row since at least
07-21 is poisoned** — any telemetry baseline must start after this fix lands.

---

## 2. Bug #0027a — core left stopped on vacuum abort paths (real, latent)

`start_yadgar()` is called from **exactly one place in the entire codebase**:
`yadgar/core/vacuum/__init__.py:945`, inside `_vacuum_finalize`. (`grep -rn "start_yadgar" yadgar/`
excluding tests returns that line plus the `ops.py:84` definition.)

`_vacuum_snapshot_and_drop` calls `svc.stop()` (`phases.py:151`), which stops **both** units
(`ops.py:56-63`, `SERVICES = ("yadgar", "yadgar-backend")`). Every abort path between that stop and
finalize restarts **only the backend**:

| Abort path | File:line | Restarts |
|---|---|---|
| snapshot/drop failure | `__init__.py:1242` | `start_backend()` only |
| `_abort_restart` (side-build fail, promote fail, swap fail) | `__init__.py:632-641` | `start_backend()` only |
| quiescence-gate abort | `__init__.py:668-676` | **nothing at all** |
| `_restore_db` (post-swap backend unhealthy) | `__init__.py:591-595` | `stop_backend` + `start_backend` |

`systemctl --user stop` is an *explicit* stop, so `Restart=on-failure`/`always` will **not** bring
core back. Any of these paths leaves the memory engine down until a human notices.

**Honesty note:** this defect did **not** produce the reported symptom. On the runs that actually
failed, finalize calls `start_yadgar()` at `:945` *before* it can roll back, so core came back —
confirmed in `yadgar.log`: paired `_signal_handler` + `_startup.main` at 19:07 on 07-26, 07-27 and
07-28. Ship the fix because it is a live data-availability landmine, not because it explains #0027.

**Related (out of scope, flag only):** `nightly_cycle.py` switched the core to *maintenance mode*
(v5.50.3, core stays UP — see the module docstring vs. the `_maintenance_http` block at `:88-120`),
yet step 4's `cmd_vacuum_impl` still hard-stops core via `svc.stop()`. The two lifecycle models
contradict each other and the module docstring (step 1 "Stop yadgar CORE only") is stale. Worth a
follow-up task; not this car.

---

## 3. Bug #0027b — consolidation kill: NOT REPRODUCING, recommend closing

The hypothesis (sync 1800 s `httpx.post` in `_forward_to_backend`
(`yadgar/core/consolidation/orchestrator.py:73-81`) → loop starvation → `/health/live` timeout →
podman `--health-on-failure kill`) is mechanically plausible and has **zero supporting evidence on
this host**.

**Discriminator run — SIGKILL is uncatchable and leaves no `_signal_handler` line.** Parsing every
`_startup.main` and `_signal_handler` event in the retained `yadgar.log` (2026-07-14 → 2026-07-29):

```
total startups=61   signals=60
UNPAIRED startups (no clean SIGTERM within 120 s before): 0
```

**Every single core restart in 15 days was preceded by a clean signal.** No SIGKILL, no OOM kill, no
crash. The nightly consolidation ran on every one of those days.

The live host already carries the ADR-0019 mitigation: private nix
`/home/max/git/nix/modules/home/yadgar.nix:552` uses
`--health-cmd 'curl -f http://localhost:8765/health/live'` with `--health-on-failure kill`, and
`/health/live` never probes the backend (`yadgar/core/server/http.py:611-650`) — it 503s only on
`pool_saturated()`, which is unconditionally `False` while tool offload is default-off
(`yadgar/_shared/runtime/offload.py:370`).

**Recommendation: close #0027b as fixed-by-ADR-0019, keep #0027a as the surviving defect.**
If it is ever re-reported, the reproduction protocol is in §6, not a code change.

**Separate low-priority finding (NOT a cause of anything observed):** repo `flake.nix:437` still
uses `--health-cmd 'curl -f .../health'` (readiness, probes the backend) instead of `/health/live`
— an unfinished ADR-0019 consequence for repo-flake users. It has **no** `--health-on-failure`, so
it cannot kill anything today; it is a lying probe, not a killer. File as its own task. **Do not
let it drift into the causal story.**

---

## 4. Car scope

**One car** — both surviving defects are in `yadgar/core/vacuum/__init__.py`.

### P0 — RED tests (TDD)

New `yadgar/tests/core/test_vacuum_finalize_verification.py`:

1. `_check_invariants_verified` against a stub core returning 404 → asserts the **new** target
   (per D1) is called, and that a *reachable, ok=true* backend yields `verified=True`.
2. `_cmd_vacuum_body` with a rollback-forcing finalize → asserts the reported `saved_bytes` /
   `saved_pct` are **0**, and that the `consolidation_log` row records `rolled_back=True`.
   *This is the anti-recurrence mechanism for the "it completed successfully" lie.*
3. Parametrized over every phase-3 abort path → asserts `svc.start_yadgar()` was called.
4. Route-existence guard: a test that fails if `vacuum/__init__.py` POSTs a core path that is not
   in the app's registered route table (kills the whole "URL string served nowhere" class).

### P1 — make verification real (#0045)

Per **D1**. Recommended: add a `POST /api/check_invariants` route to the core that delegates to the
existing tool shell (`admin_invariants.check_invariants` → `_forward_admin`). Core already has
`YADGAR_EMBED_URL` in its container env; **the vacuum unit does not** (`systemctl --user show
yadgar-vacuum.service -p Environment` → only `YADGAR_DB_URL` + `YADGAR_DATA_DIR`), so having vacuum
call the backend `/admin` directly would require an **out-of-repo nix unit change**. That constraint
decides the fork.

### P2 — make the report truthful (#0045)

- Re-measure `after_bytes` **after** `_vacuum_finalize` returns (move the `_dir_bytes(db_path)` call
  from `:1258` to after `:1272`), or pass `finalize_ok` into `_vacuum_report_and_log` and report
  `saved=0` on rollback.
- Add `rolled_back: bool` + `exit_code: int` to the `consolidation_log` row (`:1085-1097`).
- Emit a WARN/CRITICAL log line on the rollback path that names the DB size that was *not* reclaimed.

### P3 — restart core on every abort path (#0027a)

Add `svc.start_yadgar()` after `svc.start_backend()` in `_abort_restart` (`:632-641`), the
snapshot-fail path (`:1242`), `_restore_db` (`:595`), and the currently-restart-less quiescence-gate
abort (`:668-676`). Order matters: backend first, then core. **Do it vacuum-side — see D3.**

### P4 — operational cleanup + docs

- The 1.2 GB of retained scratch (`vacuum_export_2026072[4-8]_*.surql`) is correct forensic
  behaviour for failed runs, but there is no retention backstop for the *failure* case (ADR-0076 D2
  only deletes on success). Add an age backstop mirroring `_reap_stale_old_dirs` (`:720`), **or**
  document the manual cleanup in `MIGRATION_NOTES.md`. See **D4**.
- CHANGELOG. Correct the stale `nightly_cycle.py` module docstring (step 1 claims a core stop that
  maintenance mode replaced). Note in `docs/contracts/BEHAVIOR_CONTRACT.md` that vacuum savings are
  now reported post-finalize.

---

## 5. Acceptance criteria

**[unit]**

1. `_check_invariants_verified` returns `verified=True` against a live-shaped core; the 404 path is
   still treated as not-verified (the P0 #37 guard is **preserved**, not weakened).
2. A forced-rollback vacuum reports `saved_bytes == 0` and `saved_pct == 0`. **A rolled-back run can
   never report a positive saving.** *This is the criterion that would have caught the live bug.*
3. `consolidation_log` row for a rolled-back run carries `rolled_back=True` and the non-zero exit code.
4. Every phase-3 abort path calls `svc.start_yadgar()` (parametrized).
5. Route-existence guard: no core URL POSTed by `vacuum/` is absent from the registered route table.

**[e2e]**

6. Full vacuum against a seeded throwaway DB completes with `finalize_ok=True` and exit `0`; the
   canonical dir's inode **differs** from the pre-run canonical (i.e. the swap was *retained*).
7. `yadgar/tests/scripts/test_nightly_cycle*.py` still green.

**[manual] — reclaim must be shown to PERSIST, not merely to complete**

8. **T+0 / T+N size assertion.** Before the run, record `check_invariants().db_size` and
   `du -sb ~/.local/share/yadgar/surreal_db`. Run vacuum. Re-record at **T+5 min**, **T+1 h** and
   **T+24 h**. Pass requires:
   - **T+5 min and T+1 h** — `db_size_bytes` within ~20 % of the immediate post-vacuum value.
     *This is the rollback detector; the 07-24 failure was invisible at T+0 and obvious at T+2 min.*
   - **T+24 h** (post next nightly) — only that size is still **well below the pre-vacuum 2.4 GB**
     and `surreal_db`'s mtime is still from the vacuum run. **Do not assert a tight band here:**
     vlog grows continuously between vacuums by design (mem 360391 — 60 MB → 495 MB in 24 h), so a
     20 % band at T+24 h manufactures a false failure that someone would "fix" by widening it.
   - `vlog_pct_of_total` drops from its current **92 %** immediately post-vacuum.
9. `systemctl --user show yadgar-vacuum.service -p ExecMainStatus` → **0**, and no new
   `vacuum_export_*.surql` scratch is retained after the run.
10. `ls -ld ~/.local/share/yadgar/surreal_db` shows an mtime from the vacuum run, **not** `10 jul`.
11. **Telemetry baseline note:** `consolidation_log` rows before this fix are fabricated (§1.4).
    Any dashboard/regression baseline must be cut from post-fix rows only.

---

## 6. If #0027b is ever re-reported — reproduction protocol (not a fix)

1. Re-run the unpaired-startup discriminator over `yadgar.log` (§3). Non-zero → a real kill happened.
2. Separate the confounds **before** attributing it to the health-kill:
   - core is `--memory 1g`; kernel OOM-kill is indistinguishable from outside. Check `journalctl -k`
     and the cgroup memory events.
   - consolidation compute is **forwarded to the backend** (`orchestrator.py:73`), which is
     `--memory 4g` and does the re-embedding. Confirm **which** process died — check
     `~/.local/share/yadgar/logs/backend.log` for restarts at the same timestamps. Core and backend
     have different watchdogs; this changes the answer entirely.
3. Only then consider making `_forward_to_backend` non-blocking.

---

## 7. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | Adding the route "fixes" the 404 but `check_invariants` legitimately returns `ok=false` → vacuum rolls back forever anyway. **Live check today returns `ok=false`** (`1 relationship rows (non-caused_by) reference non-existent entity IDs`). | **This is the real blocker — see D2.** A pre-existing benign violation makes the verify gate unsatisfiable. Decide D2 before P1, or the fix ships and changes nothing. |
| R2 | `check_invariants` also reports `timeouts: ["memory_transition"]` on the live DB — a partial run. If the new gate treats a timeout as failure, same trap as R1. | D2 must define the pass predicate over `violations` **and** `timeouts`, not just the `ok` bool. |
| R3 | Weakening the guard re-opens the 07-09 split-brain (ADR-0090). | Never revert to warn-only *silently*. If D2 picks a narrowed predicate, the inode-coherence check (`:974`) stays a hard gate — it is the actual split-brain detector. |
| R4 | Restarting core on abort paths could race the backend restart. | Backend first, then core; `_wait_for_health` before `start_yadgar()`. |
| R5 | First successful vacuum reclaims ~2.2 GB and exposes latent surrealkv close bugs (ADR-0090 chronic unclean close). | `.pre-vacuum` snapshot + `.old` retention already cover it; do not shorten retention in this car. |
| R6 | 1.2 GB of retained scratch keeps growing every failed night until the fix lands. | Manual cleanup now via `MIGRATION_NOTES.md` (D4); do not block the car on it. |

---

## 8. File seam vs. the live train

| File | Train status | Verdict |
|---|---|---|
| `yadgar/core/vacuum/__init__.py` | untouched | clean |
| `yadgar/core/vacuum/phases.py` | untouched | clean |
| `yadgar/core/server/routes/` (new route, P1) | untouched | clean |
| `yadgar/core/server/tools/admin_invariants.py` | untouched | clean (read-only reuse) |
| `yadgar/core/scripts/nightly_cycle.py` | untouched | clean (docstring only) |
| **`yadgar/core/ops/ops.py`** | **named as train-owned in the task brief** | **AVOID.** The natural P3 fix is "add `svc.start()` that starts both" — that edits `ops.py`. Do it **vacuum-side** instead: call `start_backend()` then `start_yadgar()` in sequence. **Integration note:** the sibling plan `fix-vacuum-trigger-path-and-watcher-2026-07-29.md` §4.2 lists `yadgar/core/ops/ops.py` as "untouched → clean" and plans to edit it (its P3). That contradicts the seam list this car was given. **Flagging only — integration must sequence it; do not resolve unilaterally.** |
| `flake.nix` | TOUCHED by the train | not needed by this car (§3 finding is a separate task). |
| Private nix (`/home/max/git/nix`) | out of repo | read-only. P1's recommended option requires **no** nix change — that is why it is recommended. |

---

## 9. Open decisions for the user

**D1 — How does vacuum verify the swapped-in DB?**
- (i) **RECOMMENDED** — add `POST /api/check_invariants` to the core, delegating to the existing
  `check_invariants` tool shell (`admin_invariants.py:20` → `_forward_admin`). Core already has
  `YADGAR_EMBED_URL`; **no unit/nix change needed**; the vacuum call site is unchanged.
- (ii) Point vacuum directly at the backend `/admin` op. Requires `YADGAR_EMBED_URL` in
  `yadgar-vacuum.service` — an **out-of-repo nix edit** (`MIGRATION_NOTES.md` hand-off), and the
  non-nix/launchd surfaces would need the same. Rejected on blast radius.
- (iii) Invoke the MCP tool over the MCP transport. Heaviest client; no advantage over (i).
- (iv) Revert to warn-only. **Rejected** — that is undoing the P0 #37 hardening for the second time
  and re-opens ADR-0090.

**D2 — What counts as "verified"? (BLOCKING — decide before P1.)**
`check_invariants` on the live DB **right now** returns `ok=false` with one pre-existing violation
(`1 relationship rows (non-caused_by) reference non-existent entity IDs`) plus
`timeouts: ["memory_transition"]`. Fixing only the 404 therefore changes **nothing** — the gate is
still unsatisfiable.
- (i) **RECOMMENDED** — **demote `check_invariants` to an advisory WARN and rely on the gates that
  already exist.** This is mostly *removing* a gate, not adding one. The swap-correctness question
  ("did every row survive the export→import") is **already answered before the swap**:
  `_build_and_verify_side_db` does the EXACT per-table comparison `side_counts != source_counts` at
  `__init__.py:542` and aborts with the canonical untouched. The split-brain question is already
  answered after the swap by the inode-coherence check (`:974`). `check_invariants` answers a
  *third*, unrelated question — is the data model globally self-consistent — which a vacuum neither
  causes nor fixes, and which is `ok=false` on this host **today** for a reason predating any vacuum.
  Optional cheap addition: one post-swap `_capture_table_counts(backend_url)` against the **real**
  backend (currently never done — `_side_build_swap_and_start` only waits for `/health` at `:692`),
  re-asserted against `source_counts`. That is the only genuinely new check, and it verifies "the
  rename + real-backend reopen didn't lose anything", not global health.
- (ii) Keep `check_invariants` as the gate but pass on `ok=false` when the violation set is
  **unchanged** from a pre-vacuum baseline (regression-only gate). Correct but more moving parts.
- (iii) Keep the strict `ok=true` gate and first repair the standing violation. Couples every future
  vacuum to global DB cleanliness — a single stale edge blocks all reclaim forever. **Rejected.**

**D3 — Where does the "restart core too" fix live?**
- (i) **RECOMMENDED** — vacuum-side sequenced calls (`start_backend()` then `start_yadgar()`),
  avoiding the train-owned `ops.py`.
- (ii) Add `ServiceController.start()` (symmetric with `stop()`) in `ops.py`. Cleaner API, collides
  with the train and with the sibling plan's P3. Defer to a post-train cleanup.

**D4 — The 1.2 GB of retained failed-run export scratch.**
- (i) **RECOMMENDED** — manual one-time delete via `MIGRATION_NOTES.md` (user runs it), **plus** an
  age backstop in this car mirroring `_reap_stale_old_dirs` (`:720`) so failure-path scratch cannot
  grow unbounded again.
- (ii) Manual delete only; file the backstop as a follow-up. Leaves the same unbounded-growth hole
  ADR-0076 D1 already closed for `.old` dirs.

---

## 10. Explicitly out of scope

- The vacuum trigger-path / watcher coherence work — sibling car, `task:0044`.
- `flake.nix:437` `/health` → `/health/live` (ADR-0019 residual) — separate low-priority task (§3).
- The maintenance-mode vs. `svc.stop()` lifecycle contradiction in `nightly_cycle.py` (§2) —
  follow-up task; docstring correction only in this car.
- Any change to `pool_saturated()` / offload / health semantics — §3 shows no defect there.
- SurrealKV's lack of row-version GC (mem 360391) — vacuum exists *because* of it; not fixable here.
