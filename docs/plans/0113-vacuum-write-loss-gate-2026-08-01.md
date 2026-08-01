# Vacuum write-loss gate — engage maintenance mode on EVERY vacuum entry path

**Date:** 2026-08-01
**Task:** #0113 (writes landing during a vacuum are silently lost, or cause a spurious abort)
**Status:** DRAFT — not started.
**Target train:** `feat/v5.172-bug-train`.
**Blocking dependency:** `0111-keep-core-up-during-vacuum.md` — see §8. Without 0111
this car is a **no-op** on the CLI/timer paths.

---

## 0. Verdict up front

There are **two** defects at one seam, and the existing safety gate is blind to the
worse one.

| Window | What happens | Severity |
|---|---|---|
| **(T0, T1]** — between `_capture_table_counts` and `/export` | The write lands in the export but not in `source_counts`. The side DB then has MORE rows than the captured baseline → `side_counts != source_counts` (`__init__.py:625`) → **ABORT**, canonical untouched. | Annoying. Data safe. Reclaims nothing, looks like a verification failure. |
| **(T1, backend-stop]** — between `/export` and `phases.py:151` | The write lands in the canonical only. It is in neither count, so **the exact-count gate PASSES**, the swap is retained, and the canonical (now `surreal_db.old-<ts>`) is `rmtree`'d at `__init__.py:1198`. | **SILENT WRITE LOSS.** The gate that exists cannot see it, by construction. |

The fix for both is the same: quiesce writes **before** T0 and hold the quiesce
through the swap.

`_maintenance_mode` already ships — `yadgar/core/server/routes/control.py:639-669`
(enter/exit handlers), enforced at `yadgar/core/server/_app.py:514-527` (every MCP
tool fast-fails with a structured error before any DB call). `nightly_cycle.py`
already engages it at `:248` and exits it in a `finally` at `:436`. **The viz, CLI
and timer vacuum paths do not.**

---

## 1. Problem statement — with evidence

### 1.1 The two timestamps

```
yadgar/core/vacuum/__init__.py:1661   source_counts = _capture_table_counts(backend_url)   # T0
yadgar/core/vacuum/__init__.py:1671   raw_path, filtered_path = _vacuum_export(...)        # T1
yadgar/core/vacuum/phases.py:151      svc.stop()   /  svc.stop_backend()  (post-0111)      # T2
```

The comment at `:1667` says "real backend still UP — no lost writes vs. count
capture". That is the assumption this car falsifies: the backend being up is
precisely what allows writes to land between T0 and T2.

### 1.2 The exact-count gate cannot detect the loss

`_build_and_verify_side_db` compares `side_counts` (derived from the T1 export)
against `source_counts` (captured at T0) at `__init__.py:625`. A write in
(T1, T2] moves neither number. The gate is a comparison of two pre-stop snapshots;
it is silent about anything that arrived after the later one.

This is not a criticism of the gate — it was built for the 2026-06-16 partial-import
failure mode (1484/3622 rows), which it catches exactly. It was never a
write-quiescence gate, and nothing else is one.

### 1.3 What actually stops writes

- The core `_maintenance_mode` flag stops **new** MCP tool calls from enqueuing
  (`_app.py:514-527`, ahead of the tool body).
- It does **not** stop the queue drainer: the drainer lives in the backend
  (`yadgar/backend/queue_drainer/__init__.py:169-183`, a 30 s-interval loop) and
  keeps applying already-enqueued files to the DB regardless of the core's flag.
- There **is** a synchronous drain nudge: `yadgar/backend/admin_exec/drain.py::drain_now`,
  exposed on the backend `/admin` surface (this is ADR-0139's cross-process nudge).

So a correct quiesce is: **engage the flag, then drain the residual queue, then
capture counts.** Engaging the flag alone leaves the (T0, T1] window half-open.

### 1.4 Entry paths

All four converge on `cmd_vacuum_impl`:

| Entry | Route |
|---|---|
| viz Control tab | `control.py:536` `vacuum_now(force=False)` → trigger file (`ops.py:184`) → host watcher unit → `yadgar vacuum` → `cmd_vacuum_impl` |
| MCP `vacuum_now()` tool | same trigger-file path |
| `yadgar-vacuum.timer` | `yadgar vacuum --service-mode=systemd --yes` → `cmd_vacuum_impl` |
| `yadgar-nightly-cycle` step 4 | `cmd_vacuum_impl(vacuum_args)` **in-process**, with maintenance already engaged (`nightly_cycle.py:248`, `:346`) |
| manual `yadgar vacuum` | `cmd_vacuum_impl` |

**One funnel → one implementation site.** That is the decisive fact for §2.

---

## 2. The decided approach

### 2.1 Where the gate is engaged: inside `cmd_vacuum_impl`

Wrap `_cmd_vacuum_body` in the **same** `try/finally` that already guarantees
`sensitive_lock.release()` on every exit path (`__init__.py:1583-1588`). That block
already survives early returns and exceptions; reusing it means the maintenance
exit inherits a proven exit guarantee instead of a new one.

```
acquire sensitive_lock
try:
    entered = _maintenance_enter()          # returns False if it was ALREADY on
    try:
        return _cmd_vacuum_body(...)
    finally:
        if entered:
            _maintenance_exit()
finally:
    sensitive_lock.release()
```

**Rejected: engaging it at each call site** (viz handler, CLI, timer wrapper,
nightly). Four sites, four chances to miss one, and three of them are in different
processes from the vacuum itself — the flag would have to survive a process hop.
The funnel is the right seam.

**`finally` alone is NOT sufficient — every cleanup step gets its own try/except.**
A `finally` block runs its statements in order and stops at the first raise, so a
raising `_maintenance_exit()` would skip `sensitive_lock.release()` and leave the
host unable to vacuum again. Each cleanup action (maintenance exit, and — once
car 0046 lands — the residue reap) must be individually wrapped so a failure in one
cannot swallow the others. `_restart_services_after_abort` (`__init__.py:687-706`)
already establishes this exact pattern in this file and its docstring explains why;
follow it verbatim rather than inventing a variant. Order within the block:
**maintenance exit first, residue reap second, lock release last** — un-gate the
engine before doing anything that can fail.

### 2.2 Enter-if-not-already / exit-only-if-we-entered — and why it is mandatory

`nightly_cycle` engages at step 1 and exits at step 7 **after** steps 5 (post-backup
snapshot) and 6 (prune). A vacuum that unconditionally exits the gate at the end of
step 4 would un-gate the engine while the nightly still has DB work to do — a new
bug introduced by the fix.

The handler must therefore report the **prior** state. Implementation choice:

- **RECOMMENDED** — `maintenance_enter_handler` returns `{"status": "maintenance",
  "maintenance_mode": true, "previous": <bool>}`. One round trip, no new route, no
  TOCTOU between a read and a write. Additive to the response body, so existing
  callers (`nightly_cycle._maintenance_http` ignores the body) are unaffected.
- Rejected: a new `GET /api/control/maintenance` read followed by a write. Two round
  trips and a race between them.

The vacuum exits the gate **only when `previous` was false**.

### 2.3 Ordering inside the body

Engage → drain → capture → export. Concretely, at the top of `_cmd_vacuum_body`,
**before** `_capture_table_counts` at `:1661`:

1. maintenance enter (done by the caller, §2.1)
2. `POST {backend}/admin` `drain` (`admin_exec/drain.py::drain_now`) — flush the
   residual file queue so nothing lands after T0
3. `_capture_table_counts` (T0)
4. `_vacuum_export` (T1)

The gate is released only after the post-swap backend is healthy — i.e. after
`_vacuum_finalize` returns — which the §2.1 `finally` gives for free.

### 2.4 Failure modes — all three, decided

**(A) The gate cannot be engaged** (core unreachable, 401, timeout).
**Decision: log a WARNING and PROCEED.** Precedent in-repo:
`nightly_cycle.py:248-254` already does exactly this
("step 1 (maintenance enter) unreachable — proceeding without write-gate: %s").
Following an existing precedent beats inventing a policy. Aborting would be worse:
the DB keeps growing, the timer goes red every night, and the operator's remedy
(restart the core) is unrelated to the vacuum. The pre-swap exact-count gate is
still armed, so proceeding is degraded, not unsafe.

**(B) The drain nudge fails.** Same: WARN and proceed. The drain is an optimisation
that narrows (T0, T1]; the exact-count comparison remains the real gate, and a
spurious abort is the bug being fixed, not a safety property to preserve.

**(C) Vacuum dies with the gate ON — a stuck read-only daemon.**
The brief is right that this is worse than the bug it fixes, so it needs a real
answer, not ambient risk.

- The `finally` in §2.1 covers normal returns **and** exceptions **and** `sys.exit`.
- It does **not** cover SIGKILL, OOM-kill, or a host power loss.
- Two candidate backstops:
  - *(i)* **TTL on the flag** — the enter handler records a monotonic deadline
    alongside the bool; `_app.py`'s `_maintenance()` treats an expired deadline as
    "not in maintenance" and clears the flag on the way through. Self-healing,
    process-local, no new unit.
  - *(ii)* **clear-on-core-start** — reset the flag at daemon startup.
- **Decision: (i) TTL.** (ii) is nearly useless here *because of 0111*: with the
  core staying up through the vacuum, a SIGKILL'd vacuum leaves a core that never
  restarts, so a start-time reset never fires. (i) fires unconditionally.
- The TTL must be per-enter, because the two callers have very different windows:
  `yadgar-vacuum.service` has `TimeoutStartSec=30min`, while nightly holds the gate
  across backup + consolidation + vacuum + backup. So the enter body accepts an
  optional `ttl_seconds`; vacuum passes a value derived from the unit timeout
  (default `YADGAR_MAINTENANCE_TTL_SEC = 2400`, comfortably above 1800), and
  `nightly_cycle` passes its own larger value. A missing/blank TTL keeps today's
  behaviour (no expiry) so nothing regresses for a caller that has not been updated.
- The expiry must **log loudly** (WARN naming how long it was held), because a
  fired TTL means a vacuum died without cleanup — an operator needs to know.

---

## 3. Exact files and functions to change

| File | Change |
|---|---|
| `yadgar/core/server/routes/control.py` | `maintenance_enter_handler` (`:638-652`) — capture and return `previous`; accept optional `ttl_seconds` from the JSON body; set the deadline. `maintenance_exit_handler` (`:654-669`) — clear the deadline too. |
| `yadgar/_shared/runtime/state.py` | `:167-172` — add `_maintenance_deadline: float | None = None` next to `_maintenance_mode`, with the same comment block. |
| `yadgar/core/server/_app.py` | `_maintenance()` (`:514-527`) — expire on deadline, clear the flag, WARN; generalise the message string (shared with 0111 part c). |
| `yadgar/core/vacuum/__init__.py` | `cmd_vacuum_impl` `:1583-1588` — nested try/finally per §2.1. New helpers `_maintenance_enter()` / `_maintenance_exit()` (thin httpx POSTs to the core, bearer from `YADGAR_MCP_AUTH_TOKEN`, mirroring `_check_invariants_verified` at `:1035-1048`). New `_drain_backend_queue(backend_url)`. Call the drain before `_capture_table_counts` at `:1661`. |
| `yadgar/core/scripts/nightly_cycle.py` | `_maintenance_http` — pass a `ttl_seconds` body; note in the docstring that step 4's vacuum now nests. Fix the stale step-1 docstring line ("Stop yadgar CORE only") while here. |
| `yadgar/_shared/config/config.py` + `config_registry.py` + `config_yaml.py` | register `MAINTENANCE_TTL_SEC` (three-way sync is pre-commit-enforced — all three or none). |
| `docs/CHANGELOG.md`, `docs/reference/configuration.md` | new knob + behaviour. |

---

## 4. The TDD story

**CI gating asymmetry.** `.forgejo/workflows/ci-pr.yaml` runs by directory:
`test-fast` = `yadgar/tests/{scripts,server,hooks,_meta,clients}/`, `test-shared` =
`yadgar/tests/_shared/`, `test-backend` = `yadgar/tests/backend/`, `test-core` =
`yadgar/tests/core/`. Nothing under `yadgar/tests/integration/` is gated in `ci-pr`.
Vacuum-path tests go in `yadgar/tests/core/`; nightly-interaction tests in
`yadgar/tests/scripts/` (where `test_nightly_maintenance.py` already lives);
route/handler tests in `yadgar/tests/server/`.

### 4.1 RED first — `yadgar/tests/core/test_vacuum_write_gate.py`

1. **`test_maintenance_engaged_before_count_capture`** — a single ordered call
   recorder shared by the maintenance-enter stub and `_capture_table_counts`;
   assert `enter` precedes `capture` precedes `export`. **This is the test that
   encodes the whole car**; RED today (no enter call at all).
2. **`test_drain_nudge_precedes_count_capture`** — same recorder, asserts the drain
   POST lands between enter and capture.
3. **`test_gate_released_after_finalize_on_success`** — exit is called, and *after*
   `_vacuum_finalize` returns.
4. **`test_gate_released_on_every_abort_path`** — parametrized over the same abort
   set `test_vacuum_finalize_verification.py:372` already enumerates
   (snapshot-fail, side-build-fail, promote-fail, quiescence-abort, swap-fail,
   post-swap-unhealthy) plus a raising body; assert exit ran in all of them.
5. **`test_gate_not_released_when_already_engaged`** — enter returns
   `previous=True`; assert **no** exit POST. The nightly-nesting guard.
6. **`test_enter_failure_proceeds_with_warning`** — enter raises `ConnectionError`;
   the vacuum still reaches `_capture_table_counts`, exit is NOT called, and stderr
   names "proceeding without write-gate".
7. **`test_drain_failure_proceeds`** — same shape for the drain nudge.
7b. **`test_exit_failure_does_not_mask_a_successful_vacuum`** — enter **succeeds**,
   the vacuum succeeds, and `_maintenance_exit()` **raises**. Assert: (i)
   `cmd_vacuum_impl` still returns the run's real exit code (0), because an
   un-gating failure must not be reported as a failed compaction; (ii)
   `sensitive_lock.release()` still ran (the per-step try/except of §2.1); (iii) a
   CRITICAL is logged naming the TTL as the backstop that will clear the flag.
   This is failure mode (C)'s *common* case — core reachable at enter, unreachable
   at exit — and it is the only reason the TTL exists. Test 6 covers enter-failed;
   without 7b the exit-failed half is untested.

### 4.2 RED first — `yadgar/tests/server/test_maintenance_gate.py`

8. `test_enter_reports_previous_false_then_true` — two successive enters.
9. `test_expired_ttl_is_treated_as_not_in_maintenance` — set the deadline in the
   past; a decorated tool executes normally, the flag is observably cleared, and a
   WARN was logged.
10. `test_ttl_absent_never_expires` — back-compat for callers that send no TTL.
11. `test_maintenance_short_circuits_before_any_db_call` — pin the existing
    behaviour so the TTL edit cannot accidentally move the check after the tool body.

### 4.3 RED first — `yadgar/tests/scripts/test_nightly_maintenance.py` (extend)

12. `test_nightly_vacuum_does_not_unwedge_the_nightly_gate` — run the nightly with a
    real (in-process) flag; assert the flag is still ON when step 5 begins and only
    OFF after step 7. This is the regression that the naive implementation of this
    car would introduce, so it must exist as a test, not a comment.

### 4.4 Mutation-sensitivity note

Tests 1, 5 and 12 are the three that would survive a "looks right" implementation
being wrong. If any of them can pass with the enter/exit calls deleted, they are
mis-written.

---

## 5. Verification

**Local**

1. `pytest yadgar/tests/core/ yadgar/tests/server/ yadgar/tests/scripts/ -k "vacuum or maintenance or nightly"` green.
2. `pytest yadgar/tests/core/test_vacuum*.py` — the whole vacuum suite, since the
   `cmd_vacuum_impl` control flow changed shape.
3. Pre-commit `check-config-three-way-sync` must pass for the new knob.

**Fresh VM — `192.168.122.101`** (`sshpass -p 'Aa1234.' ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password root@192.168.122.101`).
Needs 0111 landed first, or the core dies mid-run and there is nothing to observe.

4. Seed the DB. Start a background writer loop hammering `memorize()` over MCP.
   Trigger a vacuum. Expected: the writer starts receiving
   `{"error": "maintenance", ...}` **before** the export begins, and resumes after
   the swap. No `ConnectError` in the writer's output during the backend-down window
   (that is 0111 part (c) proven at the same time).
5. Assert exit code 0 and `side_counts == source_counts` — i.e. no spurious abort,
   which is the (T0, T1] half of the bug.
6. **The write-loss half.** Before the run, note `memory_stats()` count. During the
   run's export window, attempt a write; after the run, assert the memory count is
   `pre + (accepted writes)` and that nothing that returned success is missing.
   The gate makes this trivially true by refusing the write — that refusal *is* the
   fix; the point of the check is that no write returns success and then vanishes.
7. **The stuck-gate drill.** `kill -9` the vacuum process mid-run. Assert the core
   is in maintenance, and that after `MAINTENANCE_TTL_SEC` it self-clears with a
   WARN in `yadgar.log`. This is failure mode (C) proven rather than asserted.

---

## 6. Rollback story

Revert in one commit. The flag defaults to `False`, the deadline to `None`, and the
enter/exit helpers are additive — reverting restores exactly today's behaviour
(both windows re-open, no residue).

The one asymmetry worth naming: if the revert happens **while** a host is wedged in
maintenance from failure mode (C), the TTL that would have cleared it disappears
with the revert. Recovery is `POST /api/control/maintenance/exit`, which exists
today and is unchanged. Put that one command in `MIGRATION_NOTES.md`.

---

## 7. ADRs

- **ADR-0139** is the binding prior for the drain nudge: the core-side `drain_now()`
  is a production no-op, so the nudge **must** go to the backend `/admin` drain op.
  A core-side drain call here would silently do nothing — cite 0139 in the code
  comment, not just the plan.
- **ADR-0078** (only the backend touches the DB) is why the drain has to be a
  cross-process POST rather than an in-core call. Cite.
- **New ADR: recommended to FOLD into 0111's ADR** rather than stand alone. The two
  cars are one lifecycle decision — "the core stays up so it can hold the write
  gate" — and splitting them across two ADRs invites a future reader to adopt one
  half. If they land in separate PRs, write two and have 0113's cite 0111's as a
  hard prerequisite.
- Nothing here contradicts ADR-0090 or ADR-0178.

---

## 8. Ordering / dependencies vs the rest of the train

- **HARD: 0111 lands first.** `_maintenance_mode` lives in the core process
  (`yadgar/_shared/runtime/state.py:172`). On today's CLI/timer paths the vacuum
  stops the core at `phases.py:151`, so the flag is destroyed a few seconds after
  it is set and the gate covers nothing across the window that matters. Shipping
  0113 alone would produce a green test suite and zero live effect — the worst kind
  of fix.
- 0111 part (c) ("surface in-window failures as backend restarting") **is** this
  car's mechanism. Land them adjacent.
- **0046** edits `_cmd_vacuum_body`'s exit paths, and this car wraps them in a new
  `try/finally`. Sequence 0046 **after** 0113 (or merge the two `finally` blocks in
  one commit) to avoid a fiddly textual conflict in `yadgar/core/vacuum/__init__.py`.
  If merged: maintenance exit, then residue reap, then lock release — **each in its
  own try/except** (§2.1). A shared bare `finally` re-introduces exactly the
  skip-the-rest failure that helper pattern exists to prevent.
- **0107** is independent.

---

## 9. Explicitly out of scope

- Gating the HTTP/viz surface (not behind the `_instrumented` wrapper).
- Making the backend refuse writes at the DB layer during maintenance — the core
  flag plus the backend stop already bound the window; a second enforcement point
  is a bigger blast radius than the bug.
- Any change to the exact-count verification, the swap, or the finalize gates.
- Pausing (rather than draining) the queue drainer. Draining is sufficient because
  the backend is stopped moments later at T2; a pause mechanism would be a new
  lifecycle to get wrong.
- Persisting maintenance state across a core restart. With 0111 the core no longer
  restarts during a vacuum, so persistence buys nothing and adds a stuck-state
  surface that survives reboots.
