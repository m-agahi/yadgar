# Daemon Concurrency Design — Evaluation of the Redesign Suspicions (2026-06-30)

**Status:** DESIGN / EVALUATION. Read-only against live source; every verdict
re-verified at file:line on 2026-06-30. No implementation here.

> **Framing (read first).** The naive starting point — "the daemon serializes ALL
> MCP work on one asyncio loop (RCA #72)" — is **already half-fixed in v5.90.0**
> (shipped 2026-06-30, commit on `master`). Fix A (`server/_offload.py` +
> `server/_app.py`) added a bounded `ThreadPoolExecutor` offload, a per-request
> `wait_for` timeout, the O2 pool-saturation `/health` 503 signal, thread-safety
> locks, and the `RERANK_MAX_CONCURRENCY` lockstep — **but ships DEFAULT-OFF**
> (`YADGAR_OFFLOAD_TOOLS=0`). So today, with offload OFF, the daemon STILL
> serializes on the loop; with offload ON it runs a single bounded pool. This doc
> evaluates the user's redesign suspicions **against that already-shipped baseline**
> — most are "v5.90 built half of this; here is the remaining gap," not greenfield.
>
> The offload-A AUDIT (`docs/plans/daemon-offload-A-2026-06-30.md`, lines 672-744)
> is file:line-verified and already settled O1/O2/O7. This doc cites it rather than
> re-deriving; where it confirms a verdict that is itself evidence, where it
> contradicts the user that is itself a finding.

---

## TL;DR — the four verdicts

| # | User suspicion | Verdict | One-line reason |
|---|----------------|---------|------------------|
| 1 | "Write path already has a queue — verify it." | **VERIFIED (it exists) / PARTIAL (it's not what the user thinks)** | Real, durable, disk-backed file-queue + DLQ + single serial drainer. But it is **fire-and-forget, bound only by disk, no backpressure/admission** — and the *mechanism* is **not reusable for reads**. |
| 2 | "recall needs a queue too." | **WRONG (over-thinking)** | recall is already `@_tool()` → `run_offloaded` → bounded pool, whose executor **already has an unbounded work queue**. A separate FIFO adds only latency and *serializes a 13s rerank in front of a 2s prompt-recall*. The real need is bounded-concurrency (have) + timeout (have, v5.90) + admission-reject + priority (missing) — none of which is a plain FIFO. |
| 3 | "Pool needs a resource-based FORMULA, not fixed 8." | **RIGHT (fixed-8 is wrong) / but the inputs are mis-ranked** | Correct that 8 is arbitrary. But for the **core** daemon `cpu_count`/memory are near-irrelevant (IO-bound, workers hold no models). The binding constraint is the **backend's `RERANK_MAX_CONCURRENCY`**, not cores. Formula must be **two-class** (light vs heavy). |
| 4 | "or some other mechanism is also needed?" | **YES — three real gaps** | (a) **heavy/light separation** (biggest gap — one pool of 8 lets 8×13s recalls starve the 2s prompt-recall + light tools); (b) **admission control / 429** (executor queues unboundedly instead of rejecting); (c) **priority** (interactive prompt-recall vs background consolidation share one FIFO). Timeout slot-leak residual is real but already covered by O2 — referenced, not re-litigated. |

**Top recommendation:** keep the v5.90 offload as the foundation (it is correct —
audit-verified GIL-release on every core hot path). Layer on top of it, in order:
(1) a **semaphore gating ONLY the heavy rerank path** at `RERANK_MAX_CONCURRENCY`,
(2) **admission control** that rejects (`429`/busy) past a hard cap instead of
queuing unboundedly, (3) a **two-tier pool** (or two semaphores over one pool) so
interactive recall is never starved by background work. The user's "queue + formula"
does **not** imply a different architecture — it is admission + heavy/light + sizing
**layered on** the offload, not a replacement for it.

---

## Suspicion 1 — "The WRITE path already has a queue. Verify it's actually implemented."

**VERDICT: VERIFIED it exists. PARTIAL on the user's mental model — it is a durable
fire-and-forget queue, NOT a bounded-with-backpressure queue, and its mechanism is
NOT reusable for reads.**

### What is actually there (file:line)

- **The queue is a real, durable, disk-backed file queue.** `FileQueue.enqueue()`
  (`yadgar/file_queue/queue.py:62-81`) writes each op as a JSON file via the
  **atomic temp+rename** pattern (`tmp.write_text(...)` then `tmp.rename(target)`,
  `:79-80` — atomic on POSIX). `memorize`/`wiki_add`/`anchor`/`checkpoint` enqueue
  here; the call returns a `job_id` immediately.
- **A single background drainer thread.** `QueueDrainer(threading.Thread)`
  (`file_queue/__init__.py:126`) runs a poll loop: `run()` (`:165-177`) calls
  `_drain_once()` every `_drain_interval` (default 30s), waking early on a write
  notify. `_drain_once` takes `self._drain_lock` (`:218`) → `_drain_once_locked`
  (`:221`). **One thread, serial passes** — the `_drain_lock` exists precisely to
  stop the background loop and a synchronous `drain_now()` from running a pass
  concurrently ("file theft", CI flake #53, documented `:152-163`).
- **A real DLQ with a failure taxonomy.** `_DLQMixin._move_to_dlq`
  (`file_queue/dlq.py:45-110`): atomic rename into `dlq/`, a `.error.json` sidecar
  with `classification` + `failure_reason`, and a never-pruned `.events.log` audit
  trail. Taxonomy: `permanent_error | duplicate_detected | policy_rejected |
  missing_branch | missing_directory` (`:55-56`, `:118`, validators `:120-238`).
  Retry/backoff is bounded (`max_permanent_attempts`/`max_transient_attempts`,
  `:143-146`); `cleanup_dlq` expires after 90 days **with a loud warning before each
  deletion** (`queue.py:200-226`).
- **The heavy similarity-gate runs drainer-side, not request-side.** `_sim_gate_for_drainer`
  (`dlq.py:275-373`) runs the v5.39 embed+KNN duplicate check on the **drainer
  thread**, so the embed cost never lands on the MCP request path (the I1
  thin-request-path discipline). Rejections flow back to `wait=True` callers via
  `get_job_result` (`queue.py:128-143`).

### Is it bounded with backpressure, or fire-and-forget? — **fire-and-forget.**

`enqueue()` **never blocks and never rejects.** It writes a file and returns. The
only bound is **disk space**. There is:
- **No admission control** — a write flood enqueues unboundedly until the disk fills.
- **No backpressure** — the producer (MCP request) is never told to slow down; the
  drainer simply falls behind and the queue depth grows.
- **No in-memory bounded `Queue`/semaphore** — the "queue" is a directory of files,
  not a `queue.Queue(maxsize=N)`.

This is **correct for writes** and is a deliberate design, not a defect: writes are
**deferrable** (the caller doesn't need the row to exist synchronously), **retry-
idempotent** (replayed through the same tool body, `is_draining=True`), and **DLQ-
meaningful** (a permanently-failing write is real lost data worth preserving). Disk
durability across a daemon restart is the whole point.

### Is the pattern reusable for reads? — **NO (the mechanism); YES (two ideas).**

**The mechanism is the wrong shape for `recall`.** Reads are **synchronous** (the
caller blocks on the result), **latency-bound** (the ~2s prompt-recall hook budget),
**non-deferrable** (a read you serve in 30s is useless), and have **no DLQ meaning**
(a failed read is retried by the client, not preserved for replay). A disk-backed
file queue drained by a single 30s-interval serial thread is exactly the opposite of
what a 2s-budget read needs. Forcing recall through it would be a latency
catastrophe.

**Two things ARE reusable as concepts (not code):**
1. **The observability + failure-taxonomy discipline.** Per-op classification,
   structured rejection reasons, a metrics counter per reason — the read path should
   borrow this *discipline* (e.g. a `recall_rejected_total{reason=admission|timeout}`),
   not the file-queue.
2. **The admission-gate idea.** The drainer's validators (`_validate_*`) are a form
   of admission control — they reject malformed work into the DLQ rather than letting
   it consume the pipeline. The read path's missing piece (Suspicion 4b) is the same
   *idea* applied at the front door: reject excess load with a busy error rather than
   queue it unboundedly.

---

## Suspicion 2 — "recall needs a queue too." Critical evaluation.

**VERDICT: WRONG — the user is over-thinking. A separate explicit recall FIFO is
redundant at best and a latency regression at worst.**

### Does the offload executor ALREADY queue? — **Yes.**

recall is a `@_tool()` (`yadgar/server/tools/recall.py:394-395`). Post-v5.90 it
dispatches through `_instrumented_async` (`server/_app.py:420`) → `run_offloaded`
(`_app.py:430`) → `loop.run_in_executor(pool, ...)` (`_offload.py:201`) on the
bounded `ThreadPoolExecutor`. **A `ThreadPoolExecutor` already has an unbounded
internal work queue**: when all `max_workers` are busy, `submit()`/`run_in_executor`
enqueues the callable in `_work_queue` and it runs FIFO as workers free up
(`_offload.py:316-319`-equivalent semantics; the offload-A plan §4 "Overflow
behaviour" notes this explicitly). **So a separate explicit recall FIFO is
redundant** — the queueing already exists, for free, off-loop.

### So what would a separate queue ADD? — **Nothing good, here.**

The discriminating question is: *what does a queue solve that a bounded pool /
semaphore doesn't?* For recall, the honest answer is **nothing, and a plain FIFO is
strictly worse:**

- A bare FIFO **serializes** — and recall has two cost classes sharing the pool. A
  single recall is **~13s** (heavy cross-encoder rerank); a prompt-recall must
  answer inside a **~2s** hook budget. A FIFO that puts a 13s background recall in
  front of a 2s interactive prompt-recall **blows the budget** for the interactive
  call. That is the regression a naive "recall queue" causes.
- What a queue is *usually* reached for — backpressure, admission control, priority,
  fairness — a **bare** FIFO does not provide. You only get those if you build them
  *on top of* the queue. At which point the queue is an implementation detail of
  admission control, not the thing the user actually needs.

### The real need (not a plain FIFO)

recall needs four properties; v5.90 already supplies two:

| Property | Have it? | Where |
|----------|----------|-------|
| **Bounded concurrency** | **YES** | the `max_workers` cap on the pool *is* the bound (`_offload.py:135`, audit §4 "the cap IS the concurrency control"). |
| **Per-request timeout that works** | **YES** (post-offload) | `asyncio.wait_for(fut, timeout=...)` (`_offload.py:207`), default 30s. Pre-offload this was useless (the loop itself was blocked); post-offload the loop is free so it fires. |
| **Admission control (reject, not queue-forever)** | **NO** | the executor's work queue is *unbounded* — excess load queues indefinitely instead of returning a busy/429. **Gap — Suspicion 4b.** |
| **Priority / fairness** | **NO** | one FIFO pool; a background consolidation-recall burst starves interactive prompt-recall. **Gap — Suspicion 4c.** |

**Conclusion:** the right primitive is a **bounded pool + per-request timeout
(both shipped) + admission-control reject + heavy/light split + priority** — NOT a
plain FIFO queue. The user's instinct ("recall needs *something*") is right; the
specific mechanism ("a queue") is wrong. The missing pieces are admission and
priority, which are gates over the existing pool, not a new queue.

---

## Suspicion 3 — "The pool needs a resource-based FORMULA (not fixed 8)."

**VERDICT: RIGHT that fixed-8 is arbitrary. But the user's input list
(cpu_count + memory + backend limit) is mis-ranked for the CORE daemon.**

### Why fixed-8 is indeed wrong

`_pool_workers()` reads `YADGAR_TOOL_POOL_WORKERS` default `"8"`
(`_offload.py:56-61`). 8 is a hand-picked starting point (offload-A §4 "start at 8…
a starting point"), not derived from anything. The user is correct that this should
adapt.

### The brutal part — rank the inputs honestly (core daemon)

The task says fold in `os.cpu_count`, available memory, and the backend downstream
limit. For the **core** daemon those three are **not** equally important:

- **`cpu_count` — near-irrelevant.** Core's hot paths are **httpx socket IO to the
  backend + a git subprocess** — both GIL-releasing, both IO-bound (RCA §3 / audit
  GIL verdict, both VERIFIED). The worker threads do **no** CPU-bound work; they
  block on sockets. A formula keyed on cores would size the pool to the wrong
  resource.
- **Available memory — near-irrelevant.** The worker threads **hold no models** —
  embedding/rerank/SurrealDB all live in the **backend** container (`lifecycle.py`
  remote-engine selection; audit §4 "worker threads here hold no models"). Per-thread
  footprint is just stack + request buffers. Memory does not bind the core pool.
- **Backend downstream limit — THE binding constraint.** This is the input the
  audit promotes (O7, line 703/725): the core pool must **NOT** exceed what the
  backend can serve, or you get **503-storms** *and* you **accelerate pool
  exhaustion** (workers block on serialized/slow rerank holding their slots). For the
  **heavy rerank path** the ceiling is the backend's per-mode rerank semaphore
  `RERANK_MAX_CONCURRENCY` (`config.py:713`, default raised 1→8 in v5.90;
  `embed_service.py:107`, 2s acquire → 503). For the **light path** the ceiling is
  the backend httpx connection pool — which the audit measured at **httpx default
  100** (O1 downgraded, line 706: the feared "1-conn singleton" was false), so it is
  effectively not binding at any sane N.

The honest correction: **cpu/memory only re-enter the formula IF a local engine is
selected** — and v5.90's startup assert forbids exactly that under offload (CHANGELOG
"Startup fails loud if offload is ON without a remote embed URL"). So for the
offload path, **cpu/memory are moot**; the backend serving capacity is the formula.

### Recommended formula — TWO-CLASS, because recall has two cost profiles

Do not give a single `cpu × N` number. Size two classes:

**Light tools** (wiki_read, anchor, most reads — IO-bound, sub-second):
```
LIGHT = clamp(env("YADGAR_TOOL_POOL_WORKERS", default=16),
              lo=1,
              hi=min(32, backend_httpx_max_connections))   # httpx default 100 ⇒ ~32
```
Generous; the real cap is the backend connection pool (≈100), not CPU. Default 16,
env-tunable, hard-clamped to 32 so a fat-fingered env can't explode thread count.

**Heavy rerank** (recall's cross-encoder, ~13s):
```
HEAVY = min(env("YADGAR_RECALL_HEAVY_CONCURRENCY", default=backend_RERANK_MAX_CONCURRENCY),
            backend_RERANK_MAX_CONCURRENCY)                # NEVER exceed the backend
```
This is an **admission gate**, not a thread count. It must track the backend's
`RERANK_MAX_CONCURRENCY` in lockstep — exceeding it converts a core hang into a
rerank 503-storm (audit O7). It can be a **semaphore over the same pool** (simpler —
see below), not a second `ThreadPoolExecutor`.

**Adaptation + clamps:**
- **Env override** for both (`YADGAR_TOOL_POOL_WORKERS`, `YADGAR_RECALL_HEAVY_CONCURRENCY`),
  consistent with the I25 three-way knob convention.
- **Hard clamps** (`max(1, …)`, `min(32, …)`) so no env value can starve or explode
  the daemon.
- **Backend-derived ceiling**: the heavy gate reads `RERANK_MAX_CONCURRENCY` (or is
  set in lockstep at deploy). The light cap reads the backend httpx limit; absent a
  signal, default 32 (well under httpx's 100).
- **cpu_count enters ONLY as a fallback** if a local engine is ever selected (which
  the startup assert should keep impossible under offload) — `min(cap, cpu_count*2)`
  guards the degenerate local-engine case, nothing more.

**Simpler topology recommendation:** rather than two `ThreadPoolExecutor`s, run
**one pool sized to LIGHT** and gate **only the heavy rerank call** behind an
`asyncio.Semaphore(HEAVY)` (or a `threading.Semaphore` on the worker side). One pool,
one semaphore on the heavy path. Fewer moving parts than two pools, same isolation
property (a heavy-recall flood can occupy at most HEAVY workers, leaving LIGHT-HEAVY
for interactive/light tools).

---

## Suspicion 4 — "or some other mechanism is also needed?" What's MISSING.

**VERDICT: YES. v5.90 shipped several; three genuine gaps remain.**

### Credit what v5.90 already shipped (do NOT re-litigate)

- **Per-request timeout that works post-offload** — `wait_for` (`_offload.py:207`).
- **O2 saturation → `/health` 503** — `pool_saturated()` (`_offload.py:232-257`)
  uses completion-staleness so a draining legit peak is never false-flagged, and a
  wedged-worker stall trips `/health` 503 so the deployed P0 `--health-on-failure=kill`
  still kills it (the audit's hard gate, line 711 — shipped).
- **Thread-safety locks** — `_query_cache`, circuit breakers, `_stale_count_cache`,
  `_enrichment_pipeline` double-init (CHANGELOG v5.90; audit Claim-6 corrections
  applied).
- **`RERANK_MAX_CONCURRENCY` lockstep** — default 1→8 (CHANGELOG; note: read by the
  **backend** container, needs a backend env/rebump to take effect before flipping
  offload ON).

### The three real gaps

**(a) Heavy/light separation — the BIGGEST gap (ties #2 and #3 together).**
Today (and post-v5.90) there is **one** bounded pool. Eight concurrent ~13s recalls
fill all 8 slots; a 2s interactive prompt-recall and every light tool then **queue
behind 13s of rerank** — blowing the hook budget and starving cheap reads. This is
the single highest-impact missing mechanism: a semaphore gating only the heavy
rerank path (Suspicion 3) so heavy work can occupy at most HEAVY slots, leaving the
rest for light/interactive. **Recommendation: build this first.**

**(b) Admission control / backpressure (429/503 vs unbounded queue growth).**
The executor's work queue is **unbounded** (offload-A §4): past `max_workers`,
callables pile up in `_work_queue` with no cap. Under a sustained flood the loop
stays free (good) but in-flight latency grows without bound and memory creeps — and
there is no signal to the client to back off. **Missing:** a hard cap (e.g.
`asyncio.Semaphore(N + queue_cap)`, offload-A open-decision O4) that **rejects** past
the cap with a structured busy error (`{"error":"overloaded"}` / HTTP 429-equivalent
at the MCP layer), so a flood sheds load instead of growing an unbounded backlog.
This is the read-path analogue of the write-path's admission *idea* (Suspicion 1).

**(c) Priority / fairness.** One FIFO pool means **background** work (consolidation
recall, `consolidate_now`, prospective sweeps) and **interactive** work
(prompt-recall, an agent's `recall`) compete in the same queue. A consolidation burst
starves the interactive 2s budget. **Missing:** a priority signal — interactive
prompt-recall > ad-hoc recall > background consolidation — realized as either a
priority queue feeding the pool or a reserved slice of the light pool for interactive
calls. Cheapest first cut: a small **reserved interactive lane** (e.g. 2 slots the
heavy/background classes cannot take) so prompt-recall always has a worker.

### Not a gap — already covered, reference only

- **Timeout slot-leak residual** (a wedged worker holds its slot past timeout): real,
  but it is exactly O2 (`_offload.py:18-22`, audit line 700). The saturation→503
  signal + P0 kill is the designed backstop. **Reference, do not re-build.**
- **Circuit breaker** — exists for the rerank/ML client (`_breakers`,
  `ml_client.py`, now lock-guarded in v5.90). Not missing.

---

## Foundation check: is the offload (Fix A) the right base — and how does #74 compose?

### Offload IS the right foundation.

The audit verified (lines 696-707) that **every core hot path releases the GIL**
(remote httpx socket IO + git subprocess), so offloading sync tool bodies onto a
bounded pool genuinely frees the loop. The user's "queue + formula" does **NOT** imply
a different architecture — it **layers on top of** the offload:
- the "formula" (Suspicion 3) sizes the offload pool;
- the heavy/light split + admission + priority (Suspicion 4) are **gates over** the
  offload pool;
- the "write queue" (Suspicion 1) is an orthogonal, already-built, durability
  concern for a different (write) workload.

There is no competing architecture here. The offload is the substrate; the user's
ideas are the missing control layer on top of it. Build them as additions, not a
rewrite.

### #74 (offload-on-recall crash-loop) — orthogonal in principle; one discriminator.

The task states #74 (offload crash-loops on real recall) is **a separate bug being
root-caused elsewhere**. It is **not documented in any committed plan/changelog** as
of this writing, so this doc does **not** assert its root cause.

**Stance:** the crash is a **bug to fix regardless** of this design. The design is
about handling concurrency *well*; the crash is about it being *correct at all*. They
are orthogonal **in principle**. One **discriminator** for the root-causer (offered as
a hypothesis, not a verdict):

- A crash that appears specifically on **real recall under offload** — but not on
  light tools under offload, and not on recall inline — smells like an
  **offload-exposed concurrency defect**: a thread-safety race, a loop-affinity
  violation, or a contextvars-propagation issue on the path recall newly parallelizes
  (recall touches the rerank client + the caches the offload first exposed to
  concurrent access). If that is the class, the fix lives **inside this design's
  concern** (it is the thread-safety/heavy-path hardening Suspicion 4 + the audit
  Claim-6 lock list already point at) — and the heavy/light split (4a) plus the
  audit's lock corrections may well *avoid the crash class* by serializing the heavy
  rerank path that the crash rides on.
- If instead the crash is **recall-logic-specific** (a bug in the recall body that
  only manifests under the async dispatch envelope, independent of concurrency), it is
  **fully orthogonal** — fix it in the recall path and the concurrency design is
  unaffected.

The root-causer should use "does it reproduce with **one** offloaded recall, or only
with **concurrent** offloaded recalls?" as the splitter: one → recall-logic /
dispatch-envelope (orthogonal); only-concurrent → offload-exposed concurrency defect
(this design's heavy/light + locking is the right home).

---

## Recommended design (composition)

Layered on the v5.90 offload, in build order:

1. **Heavy/light split** (gap 4a, fixes the #2 latency regression, applies #3's
   two-class sizing): one pool sized to **LIGHT** (default 16, clamp ≤ min(32,
   backend-httpx-limit)); an `asyncio.Semaphore(HEAVY)` gating **only** the rerank
   call, `HEAVY = min(env, backend RERANK_MAX_CONCURRENCY)`. A reserved interactive
   lane (≈2 slots) the heavy/background classes cannot take.
2. **Admission control** (gap 4b): a hard in-flight cap; past it, **reject** with a
   structured busy error (MCP-layer 429-equivalent) instead of unbounded queue
   growth. Wire a `recall_rejected_total{reason}` metric (borrowing the write-path's
   taxonomy *discipline*, Suspicion 1).
3. **Priority** (gap 4c): interactive prompt-recall > ad-hoc recall > background
   consolidation, realized as the reserved interactive lane (cheap) or a priority
   queue (richer).
4. **Keep**: the v5.90 `wait_for` timeout, the O2 saturation→503 signal + P0 kill,
   the thread-safety locks, the RERANK lockstep. Do not rebuild these.
5. **Backend follow-up (audit O7)**: do not flip offload ON to prod-N without the
   backend's `RERANK_MAX_CONCURRENCY` actually raised (it's a backend-container env;
   v5.90 set the core-side default but flagged the backend rebump dependency).

**What is reused from the write-queue:** the *observability + failure-taxonomy
discipline* and the *admission-gate concept* — **not** the file-queue mechanism (wrong
shape for latency-bound reads).

**Composition with #74:** orthogonal bug; fix regardless. If it is the
concurrent-only / offload-exposed class, the heavy/light split + lock hardening above
is its likely home and may avoid the crash class outright; if it is recall-logic /
single-call, it is independent of this design.

---

## Key files (evidence index)

- `yadgar/server/_offload.py` — the bounded pool, `run_offloaded` (`:184`),
  `wait_for` timeout (`:207`), O2 `pool_saturated` (`:232-257`), `_pool_workers`
  default-8 (`:56`).
- `yadgar/server/_app.py` — `_instrumented_async` → `run_offloaded` dispatch
  (`:420-430`).
- `yadgar/server/tools/recall.py:394-395` — recall is a `@_tool()` (goes through the
  pool; no separate queue).
- `yadgar/file_queue/queue.py` — write queue: `enqueue` atomic temp+rename (`:62-81`),
  archive/DLQ dirs, `cleanup_dlq` (`:200-226`).
- `yadgar/file_queue/__init__.py` — `QueueDrainer` single thread (`:126`), serial
  `run()` loop (`:165-177`), `_drain_lock` (`:163`, `:218`), drainer-side sim-gate
  call site.
- `yadgar/file_queue/dlq.py` — DLQ taxonomy + `_move_to_dlq` (`:45-110`), validators
  (`:120-238`), drainer-side similarity gate (`:275-373`).
- `yadgar/config.py:713` / `config_registry.py:468` — `RERANK_MAX_CONCURRENCY`
  (default 8); `yadgar/backend/embed_service.py:107` — backend reads it (the heavy
  ceiling).
- `docs/plans/daemon-offload-A-2026-06-30.md` — Fix A plan + the file:line audit
  (O1/O2/O7 settled).
- `docs/plans/daemon-hang-rca-and-recovery-2026-06-30.md` — RCA #72 (the loop-serial
  root cause), P0 health-kill recovery.
- `CHANGELOG.md` v5.90.0 — what shipped (offload default-OFF, O2 signal, locks,
  RERANK lockstep).
