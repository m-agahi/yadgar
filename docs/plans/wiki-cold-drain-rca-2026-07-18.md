# Wiki `wait=True` Cold-Drain Timeout — RCA + Fix Plan

- **Status:** PROPOSED
- **Date:** 2026-07-18
- **Task:** #29
- **Scope:** RCA of `wiki_add(wait=True)` / `wiki_write_task_list(wait=True)` returning
  `{"stored": false, "reason": "wait_timeout", "queued": true}` even after task #26
  bumped the wait timeout 5s → 15s. Write is NOT lost — it commits on the next
  background drain. This is a WAIT/LATENCY defect.
- **Follow-up to:** task #26 (raised `WIKI_WRITE_WAIT_TIMEOUT_SECONDS` 5→15).

---

## 1. Symptom (reproduced live 2026-07-18)

`wiki_write_task_list(wait=True)` observed returning `wait_timeout` twice this
session; `wiki_add(wait=True)` same. The queued write DID land later (background
drain), so the row is never lost — the `wait=True` read-your-writes contract just
times out before the drainer commits. Task #26 already raised the timeout 5→15 and
it STILL times out.

---

## 2. Topology premise (the RCA hinges on this)

**Production runs core and backend as two SEPARATE processes / containers**
(ADR-0078 split; `docs/reference/architecture.md:231-233`). The diagnosis below is
**topology-scoped**: in a hypothetical single-process deployment the in-process
`drain_now()` nudge would work and the bug would not manifest. Everything that
follows assumes the two-container production topology, which is the shipped default.

Evidence the drainer is a *backend-only* object:
- `yadgar/core/lifecycle/lifecycle.py:47-51` — core owns ONLY the `FileQueue`
  (enqueue endpoint). Explicit comment: *"The QueueDrainer (drain-replay) is a
  backend concern started by the backend lifecycle half … Core no longer constructs
  or starts the drainer here."*
- `yadgar/backend/embed_service/embed_service_lifecycle.py:139` — only the **backend**
  process assigns `_st._queue_drainer = drainer`.
- `yadgar/_shared/file_queue/queue.py:80` (`wait_for_job` docstring) — *"Cross-process
  safe: the drainer runs in the backend process and archives (success) or DLQs
  (failure) the queue file."* i.e. the two sides communicate ONLY through the shared
  filesystem queue dir — no shared memory.
- Parallel already documented: `docs/CHANGELOG.md:54` — *"`_st._retriever` is `None`
  in core; retrieval is fully sunk to backend (ADR-0078 clean)."* The exact same
  None-in-core pattern applies to `_st._queue_drainer`.

---

## 3. The write path, end to end (file:line)

### 3.1 Enqueue + wait (CORE process)

`yadgar/core/server/tools/wiki.py`, `_wiki_add_wait_path()` (~lines 288-337):

1. `fq = _get_file_queue()` → `fq.enqueue("wiki_add", payload)` drops a JSON file into
   the shared queue dir and returns a `job_id` (`_get_file_queue` in
   `core/lifecycle/lifecycle.py:44`; queue base = `YADGAR_DATA_DIR`).
2. **The nudge (the dead line):**
   ```python
   _drainer = _st._queue_drainer          # wiki.py:294
   if _drainer is not None:               # wiki.py:295  ← FALSE in core
       _drainer.drain_now()               # wiki.py:297  ← never reached
   ```
3. `timeout = WIKI_WRITE_WAIT_TIMEOUT_SECONDS` (default `15.0`) — `wiki.py:304`.
4. `outcome = fq.wait_for_job(job_id, timeout=timeout)` — `wiki.py:308`.
5. On `status == "timeout"` returns
   `{"stored": False, "reason": "wait_timeout", "queued": True, …}` — `wiki.py:310-318`.

`wiki_write_task_list(wait=True)` routes through the same `_wiki_add_wait_path()`
(canonical task-list writer), so it inherits the identical behavior.

`wait_for_job` (`yadgar/_shared/file_queue/queue.py:80-110`) is a **pure filesystem
poll** — no DB read. Terminal states:
- success → queue file moved to `archive/memories/<date>/*_<job_id>.json`,
- rejection → `dlq/*_<job_id>.json` (+ `.error.json` sidecar).
- Poll interval `_WAIT_POLL_INTERVAL` (50 ms). No `threading.Event`; archiving IS the
  signal (cross-process safe).

### 3.2 Drain (BACKEND process)

`yadgar/backend/queue_drainer/__init__.py`:

- `run()` loop (155-179): `_drain_once()` then
  **`self._stop_event.wait(timeout=self._drain_interval)`** — a **passive interval
  sleep** with no early-wake mechanism (line 179). A write enqueued just after a pass
  waits up to a FULL interval.
- `drain_interval` default `_DRAIN_INTERVAL = 30.0` (line 46);
  settings `QUEUE_DRAIN_INTERVAL: int = 30` (`config.py:421`).
- `drain_now()` (206) → `_drain_once()` → `_drain_once_locked()` under
  `self._drain_lock` (188-204). **`drain_now()` IS synchronous and durable** — it
  applies files and returns only after the writes hit storage (the lock exists
  specifically so `drain_now()` cannot return before durability; CI flake #53 comment
  at 154-169). So *if it were reachable from core*, the wait would resolve in one call.

### 3.3 Commit cost (per wiki write)

Drain-side apply: `yadgar/backend/queue_drainer/apply.py:124-132` (`op == "wiki_add"`
→ `run_wiki_add_replay`), preceded by the v5.41.5 **similarity gate** in the drainer
(`queue_drainer/dlq.py:259+`, embedding compute) + branch/dir validation. Cost:
- **Cold first drain ≈ 12s** — `config.py:319-320` comment (task #26):
  *"post-deploy cold drain measured ~12s; 15s covers that plus margin."* Cold = model
  load + first embed + first DB insert.
- Warm drains are much cheaper (model already resident).
- The per-`_drain_once` cost is instrumented: `@observe(metric="drainer.drain_cycle")`
  on `_drain_once` (`queue_drainer/__init__.py`). **The fix plan MUST pull this metric
  from a warm production backend to size the timeout** — see §5 open measurement.

---

## 4. Root-cause verdict

**Primary cause = (a) + (d-variant): the `drain_now()` nudge is a silent no-op across
the core→backend process boundary, so `wait=True` degrades to passively polling for
the backend's PASSIVE 30s-interval drainer — and 30s (worst-case wait) > 15s (timeout).**

Decisive lines:
- `core/server/tools/wiki.py:294-297` — nudge guarded by `_st._queue_drainer is not
  None`, which is `None` in the core process.
- `backend/embed_service/embed_service_lifecycle.py:139` — only backend sets it.
- `backend/queue_drainer/__init__.py:179` — passive 30s interval sleep, no early wake.
- `config.py:421` = 30s interval vs `config.py:321` = 15s timeout → **structural gap:
  worst-case wait (≈ one drain interval, 30s) exceeds the timeout (15s).**

Ruled out / clarified:
- **(b) per-write commit cost > timeout** — NOT the dominant cause. Warm drain is well
  under 15s; only the *cold first* drain (~12s, task #26 comment) approaches it, and
  even that fits 15s. The failure is the drainer *not running at all* inside the 15s
  window, not a single slow write. **Caveat:** this rests on the task-#26 ~12s figure,
  not a fresh measurement — §5 requires confirming warm `drainer.drain_cycle` p95
  before finalizing the timeout knob. If warm drain turns out ~10s+, the timeout also
  needs headroom on top of the event-driven fix.
- **(c) read-your-writes race / cache** — NOT the cause. `wait_for_job` polls the
  archive/dlq dirs the drainer writes; archiving IS the commit signal; no stale cache.
- **(d) drainer down/backpressured** — NOT the steady-state cause (the write DOES
  commit ~30s later, proving the drainer is alive). But note: **any core-side code that
  guards `if _st._queue_drainer is not None` before `drain_now()` is dead in
  production** — a cross-cutting trap, not unique to wiki (also `memorize.py:211`).

### Why task #26 missed it
Task #26 raised the *wait budget* (5→15) but left the *drain frequency* (30s) and the
dead cross-process nudge untouched. 15s only covers the "cold first drain ~12s" case
where the drainer happens to already be mid/near a pass. It does nothing for a write
that arrives just after a pass — that still waits ~30s. **Wrong knob.**

---

## 5. Open measurement (do before finalizing the timeout)

Pull from a **warm** production backend:
- `drainer.drain_cycle` p50/p95 for a single wiki write (sizes the timeout floor).
- Distribution of `time_since_last_pass` at enqueue (confirms the ~half-interval mean
  wait).

This decides whether the event-driven fix (§6.A) alone suffices (if warm drain is
sub-second/low-single-digit) or must be paired with a modest timeout bump.

---

## 6. Fix options (ranked)

### A. **[RECOMMENDED] Event-driven cross-process drain nudge**
Make `wait=True` (and any enqueue that wants low latency) actively wake the backend
drainer instead of waiting for its 30s tick.

Because core cannot call backend's in-process `drain_now()` (no shared memory), the
nudge must cross the process boundary. Two implementation shapes:

1. **Backend HTTP drain endpoint (preferred).** Add `POST /admin` op (or a dedicated
   `POST /drain`) on the backend embed_service — it already exposes an `/admin`
   write-surface (`embed_service_routes.py:294`, `@app.post("/admin")`). The op calls
   the live backend `_st._queue_drainer.drain_now()` (synchronous + durable per §3.2)
   and returns items-processed. Core's `_wiki_add_wait_path` POSTs it right after
   `enqueue`, then falls through to the existing `wait_for_job` poll (which now
   resolves near-immediately). Backpressure-safe: `drain_now()` is `_drain_lock`-
   serialized, so a nudge landing mid-interval-pass just waits for the lock, never
   double-drains.
2. **Sentinel-file wake.** Core `touch`es a sentinel in the shared queue dir; the
   backend `run()` loop waits on the file (inotify) OR uses a short `wait()` that the
   sentinel interrupts. Lower-latency than HTTP but requires the backend loop to watch
   the FS — more moving parts than reusing `/admin`.

- **Trade-offs:** satisfies BOTH the `wait=True` latency need AND the deliberate 30s
  observability interval (see §7 — the interval was raised on purpose). Adds a
  core→backend HTTP call on the wait path (one localhost round-trip; the wait path is
  already slow-by-design). Endpoint (1) is the clean reuse; must be debug/health-safe
  and idempotent.
- **Verdict:** top choice. Only option that fixes the structure without regressing the
  observability intent or making `wait=True` block ~30s.

### B. Shorten `QUEUE_DRAIN_INTERVAL` (30s → e.g. 5-10s)
- **Trade-off:** fights a DELIBERATE prior decision — the interval was raised 5→30s in
  v4.1.3 *specifically "to make queue entries observable before drain"* (see §7). Also
  raises steady-state drain CPU (more passes) and still leaves a worst-case wait ≈ the
  new interval. Caveated **fallback**, not primary.

### C. Raise `WIKI_WRITE_WAIT_TIMEOUT_SECONDS` above one full interval (>30s)
- **Trade-off:** pure band-aid. Makes `wait=True` block up to ~30s+ on every call —
  bad UX for the task-list mirror and adr-index writes that use `wait=True`. Worst
  option alone; only acceptable as headroom ON TOP of (A) if §5 shows warm drain is
  slow.

### D. Drain SYNCHRONOUSLY in the core handler
- Rejected. Core has no drainer, no storage engine, no embedding model (ADR-0078). Re-
  homing drain into core reverses the split. Non-starter.

### E. Improve the return contract (secondary, do alongside A)
`wait_timeout` currently reads as a failure to naive callers. `adr_add` already treats
it as success-still-converging (`tests/core/test_adr.py:369-430`). Consider returning
a clearer `{"stored": true, "committed": false, "converging": true}` shape so callers
don't misclassify a queued-but-not-yet-committed write as an error. Cheap, orthogonal
to the latency fix.

**Recommendation:** ship **A (endpoint variant)**, measure per §5, and only add a
small timeout bump (C-as-headroom) if warm drain p95 warrants. Fold in E for contract
clarity.

---

## 7. The 30s interval is deliberate — do not "just lower it"

Memory (yadgar v4.1.3, id=10) records `QUEUE_DRAIN_INTERVAL` was introduced at **30s,
raised from a hardcoded 5s, expressly "to make queue entries observable before
drain."** So Option B directly regresses an intentional observability property.
Option A is the ONLY fix that keeps the 30s steady-state interval (observability
intact) while giving `wait=True` immediate convergence.

---

## 8. Core-only vs backend + version implications

**This is a BACKEND change (not core-only)** for the recommended fix:
- Core cannot reach the drainer in-process; the drain trigger must run in the backend.
- Option A(1) adds/extends a backend route (`embed_service_routes.py`) → **backend
  image rebuild + backend version bump**, plus a core change (the POST call in
  `_wiki_add_wait_path`) → core version bump. Both halves ship together.
- Queue/data location is the shared volume (`YADGAR_DATA_DIR`, ADR-0075 backend `/data`
  queue) — unchanged.
- Options B/C are config-only (core `config.py` defaults + `config_registry.py`), no
  image change — which is exactly why they're tempting and why they don't actually fix
  the structure.

CI has a backend-bump gate (`ci-release.yaml`, #83) — confirm the backend route change
trips `backend_changed=true` so the backend image actually rebuilds.

---

## 9. Build-car breakdown

- **Car 0 — measurement (blocks sizing).** Pull warm `drainer.drain_cycle` p50/p95 +
  enqueue-to-pass latency from a live backend (§5). Output: the timeout floor. No code.
- **Car 1 — backend drain endpoint.** Add `POST /admin` drain op (or `/drain`) in
  `yadgar/backend/embed_service/embed_service_routes.py` → live
  `_st._queue_drainer.drain_now()`; return items-processed. RED test first (endpoint
  drains a pending file synchronously). Backend version bump.
- **Car 2 — core wait-path nudge.** In `_wiki_add_wait_path` (and the parallel
  `memorize.py:211` path if in scope), replace the dead
  `if _st._queue_drainer is not None: drain_now()` with a POST to Car-1's endpoint
  (best-effort, non-fatal on failure — fall through to the existing poll). Core version
  bump. RED test: cross-process repro (see §10).
- **Car 3 — contract clarity (Option E, optional).** Reshape the `wait_timeout` return
  and/or align callers. Update `wiki_add`/`wiki_write_task_list` docstrings.
- **Car 4 (fallback only) — config nudge.** If §5 shows warm drain is slow, add a
  small headroom bump to `WIKI_WRITE_WAIT_TIMEOUT_SECONDS` (config-only). Do NOT lower
  `QUEUE_DRAIN_INTERVAL` (§7).

Cars 1+2 are the load-bearing pair and must land together (endpoint before caller).

---

## 10. Test approach

**Reproduction test (the crux — must fail before the fix):**
Existing drainer tests use `drain_interval=9999` and *call `drain_now()` directly*
(e.g. `tests/backend/test_wiki_add_wait_phase1.py:41`,
`tests/core/test_queue_drainer_validation.py:98`) — that exercises the **WORKING**
in-process path and will NOT reproduce the bug. The failing repro must model the
**core process where `_st._queue_drainer is None`**:

1. Build a `FileQueue` on a temp `YADGAR_DATA_DIR`.
2. Start a `QueueDrainer` with a realistic interval (e.g. 30s) on that queue in a
   background thread — but ensure the *enqueue-side* `_st._queue_drainer` is `None`
   (simulating the separate core process; monkeypatch the core-side state to `None`).
3. `enqueue` a `wiki_add` job, then invoke the core wait path with `timeout < interval`.
4. **Assert current behavior: `wait_timeout`** (RED — reproduces the bug).
5. After the fix: the core wait path fires the cross-process nudge (mock/loopback the
   backend endpoint to call the real `drain_now()`), and **assert the job commits
   within `timeout`** (GREEN).

Additional coverage:
- Endpoint test (Car 1): pending file → `POST /admin` drain op → file archived, row in
  DB, synchronous return.
- Idempotency / backpressure: two concurrent nudges do not double-apply (relies on
  `_drain_lock`); assert single archive.
- Regression: `wait=False` path unchanged (returns `queued` immediately, no nudge
  needed).
- Contract test (Car 3): `wait_timeout` classification matches the `adr_add`
  success-still-converging semantics (`tests/core/test_adr.py:369-430`).

---

## 11. Risks

- **Nudge failure must be non-fatal.** If the backend `/admin` drain POST fails
  (backend restarting), the core wait path must fall through to the passive poll — the
  write is already durably enqueued; worst case is the pre-fix ~30s latency, never a
  lost write. Test this explicitly.
- **Backpressure under burst.** Many concurrent `wait=True` writes each POST a drain;
  `_drain_lock` serializes, so nudges queue on the lock. Acceptable (each pass drains
  ALL pending files, so a single pass often satisfies many waiters), but confirm no
  thundering-herd on the lock under load.
- **Two-version ship.** Core + backend bump together; the backend-bump CI gate (#83)
  must fire. A core-only deploy without the new backend endpoint would leave the nudge
  POSTing a 404 → falls through to poll (degrades gracefully to pre-fix behavior, not a
  crash) — but verify the graceful-degradation path in a mixed-version smoke.
- **Don't regress observability.** Keep `QUEUE_DRAIN_INTERVAL=30` (§7). The nudge is
  additive, not a replacement for the interval loop.

---

## Appendix — evidence index (file:line)

| Claim | Location |
| --- | --- |
| Core owns only FileQueue; drainer is backend | `yadgar/core/lifecycle/lifecycle.py:47-51` |
| Only backend sets `_st._queue_drainer` | `yadgar/backend/embed_service/embed_service_lifecycle.py:139` |
| Dead nudge guard in wait path | `yadgar/core/server/tools/wiki.py:294-297` |
| Wait timeout read + default 15 | `yadgar/core/server/tools/wiki.py:304`; `config.py:321` |
| `wait_for_job` cross-process FS poll | `yadgar/_shared/file_queue/queue.py:80-110` |
| Passive 30s interval sleep | `yadgar/backend/queue_drainer/__init__.py:179` |
| `_DRAIN_INTERVAL = 30.0` | `yadgar/backend/queue_drainer/__init__.py:46` |
| `QUEUE_DRAIN_INTERVAL: int = 30` | `yadgar/_shared/config/config.py:421` |
| `drain_now()` synchronous + `_drain_lock` durable | `yadgar/backend/queue_drainer/__init__.py:188-206` |
| Wiki apply on drain (op == wiki_add) | `yadgar/backend/queue_drainer/apply.py:124-132` |
| Similarity gate in drainer | `yadgar/backend/queue_drainer/dlq.py:259+` |
| Cold drain ~12s + task #26 5→15 rationale | `config.py:319-320` |
| Backend `/admin` write-surface (reuse target) | `yadgar/backend/embed_service/embed_service_routes.py:294` |
| `drainer.drain_cycle` metric (size the timeout) | `@observe` on `_drain_once`, `queue_drainer/__init__.py` |
| ADR-0078 parallel: `_st._retriever` None in core | `docs/CHANGELOG.md:54` |
| Two-container topology | `docs/reference/architecture.md:231-233` |
| 30s interval was deliberate (observability) | yadgar memory id=10 (v4.1.3) |
| `wait_timeout` = success-still-converging precedent | `yadgar/tests/core/test_adr.py:369-430` |
