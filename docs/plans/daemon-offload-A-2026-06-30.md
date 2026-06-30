# Fix A — Offload Sync MCP Tool Bodies Off the Asyncio Loop (2026-06-30)

**STATUS: IN REVIEW.** Plan only — NO implementation. This document will be
audited by a separate agent before any build. It is the foundation for the real
bug fix (P1 in the RCA); P0 (healthcheck-kill auto-recovery, commit `e783510`,
nix-only) is already deployed and only *recovers* a hang. Fix A removes the
*cause*: the single asyncio loop thread blocking under wiki-repo load.

Grounded in the verified RCA: `docs/plans/daemon-hang-rca-and-recovery-2026-06-30.md`
(read it first). All file:line references below were re-verified against live
source on 2026-06-30.

> **MCP caveat.** The yadgar `recall` transport dropped mid-call repeatedly during
> this design (the very loop-stall this plan fixes is a plausible contributor).
> `wiki_query` returned cleanly (empty). Per the task contract, this plan proceeds
> from the live code; observed state wins. Reconcile against any hang/perf wiki
> pages once `recall` is stable.

---

## 0. The one-paragraph thesis

The core daemon registers ~60 sync `def` tool bodies through a single wrapper
(`_app.py:343` `_instrumented`) and FastMCP 1.28.0 runs them **inline on the event
loop** (`func_metadata.py:92-95`, sync branch = bare `fn(...)`, no offload —
verified in the RCA Linchpin). Fix A makes that wrapper `async def` and dispatches
the sync body through `asyncio.to_thread(...)`, so every tool body runs on a worker
thread and the loop stays free to serve `/health` and other requests. Because the
**core daemon's** hot-path work is socket IO to the backend (httpx → SurrealDB,
embed, rerank) **and** the proven inline `git` subprocess — both of which **release
the GIL while blocked** — the offload genuinely frees the loop (GIL verdict below).
**Scope honesty:** Fix A offloads the `@_tool()` MCP tool bodies. It does **NOT**
cover the inline git on the `@custom_route` **hook handlers** (`http.py:838/1203/1234`)
— a co-firing loop-block vector at the actual incident — see §0.1; closing the
*whole* "loop never blocks" goal needs that too.
The cap on the worker pool *is* the concurrency control; the shared httpx clients
and unprotected caches the offload newly exposes get locks; a per-request
`asyncio.wait_for` frees the loop on a wedged op (but cannot kill the worker
thread — P0 health-kill remains the backstop for that residual).

### 0.1 What Fix A does NOT cover (named, so the audit can weigh the gap)

Fix A's offload boundary is `_instrumented` (`_app.py:343`), which wraps only the
`@_tool()`-decorated MCP tool bodies (~60 of them). Two loop-block vectors fall
**outside** that boundary and remain inline on the loop after Fix A:

1. **Hook-route inline git (RCA runner-up #2 — co-fired at the actual hang).** The
   `/hooks/*` handlers are `@custom_route` async functions, **not** `@_tool()`, so
   `_instrumented`/the offload never touches them. They run blocking git inline on
   the loop:
   - `http.py:838` — `_detect_branch` inline in `async hook_subagent_stop`
     (note: `_memorize` beside it at `:849` **is** already `to_thread`-wrapped; the
     branch-detect is **not**).
   - `http.py:1203` — `subprocess.run(["git","rev-parse"], timeout=3)` inline.
   - `http.py:1234` — same pattern.
   The incident timeline shows **two `POST /hooks/auto-capture`** immediately before
   the freeze, so this vector was plausibly **co-firing** with the tool-body git at
   the hang. **Fix A does not remove it.** Therefore the §0 thesis is scoped to tool
   bodies, not "the loop can never block." **Decision for the user/auditor (O11):**
   fold hook-route git offload into Fix A (wrap each in `to_thread`, matching the
   already-correct `:849` pattern) or defer to a sibling fix. Deferring is
   acceptable; leaving it unstated is not.
2. **The backend container** (O7) — separate process, separate loop.

---

## 1. Foundational architecture pin (READ FIRST — the plan rests on this)

**Verified production wiring (nix, deployed).** The system is two containers:

| Container | Serves | Hot-path execution |
|-----------|--------|--------------------|
| **core** (`:8765`, the one that hangs) | MCP tools (`recall`/`wiki_*`/`memorize`/…) | **ALL remote** — httpx to backend |
| **backend** (`yadgar-backend`) | SurrealDB `:8000`, embed `:8001`, `/rerank` | in-process torch / ONNX / SurrealKV |

Evidence (`/home/max/git/nix/modules/home/yadgar.nix:506`, core `docker run` env):

- `YADGAR_DB_URL=http://yadgar-backend:8000` → storage is **server-mode httpx**
  (`storage/__init__.py:208`; `client.py:557-560` branches to `_q_server()` httpx
  POST when the URL is set, `:515-527`).
- `YADGAR_EMBED_URL=http://yadgar-backend:8001` → embedding is **REMOTE**
  (`lifecycle.py:264` `if os.environ.get("YADGAR_EMBED_URL")` → builds
  `RemoteEmbeddingEngine`, `remote_embeddings.py:58` httpx `POST /embed`), and the
  ML/rerank client is **REMOTE** (`lifecycle.py:269`
  `RemoteMLClient(os.environ["YADGAR_EMBED_URL"])`; `ml_client.py:664` httpx
  `POST /rerank/{mode}`).

**Why this is load-bearing:** the task brief described the core hot paths as
"sync CPU embedding/rerank on the loop (46s worst-case cross-encoder)." That is
true of the **backend** container, **not** the core daemon. In the **core**
daemon, embedding, rerank, and storage are all **httpx socket IO**. This changes:

1. **The GIL verdict** (§3) — core's blocking work releases the GIL (socket IO +
   git subprocess), so offload works cleanly. No CPU-bound-holding-GIL risk on
   the core loop.
2. **Thread-safety scope** (§5) — core must harden the **remote** client objects
   (`RemoteEmbeddingEngine`, `RemoteMLClient`, the httpx storage client) and the
   loop-affine `asyncio.Lock`. The **local** torch/lazy-loader state
   (`EmbeddingEngine` local model cache, `LocalMLClient` lazy CE/NLI loaders) is
   **backend-only** and **out of scope for Fix A** — but the backend likely needs
   the same offload separately (§9 open decision O7).

**This document is for the CORE daemon (Fix A). The backend container's own loop
(does it run rerank inline on its asyncio loop? it serves `/rerank` — likely the
same bug class) is a related, separately-scoped follow-up — see O7.**

---

## 2. The offload mechanism (exact change)

### 2.1 Current dispatch (verified)

`/home/max/git/yadgar/yadgar/server/_app.py`, the `_tool()` decorator factory
(`:310`) → `decorator(func)` (`:333`):

```python
# :341  trace-wrap the original sync tool fn (preserved)
_traced_func = _trace_span(f"tool.{func.__name__}")(func)

@functools.wraps(func)
def _instrumented(*args, **kwargs):          # :343  SYNC — runs inline on the loop
    import yadgar.server._state as _st_ref
    if _st_ref._maintenance_mode:
        return {"error": "maintenance", ...}
    _t0 = _time.monotonic(); _status = "ok"
    try:
        result = _traced_func(*args, **kwargs)   # :358  THE BLOCKING CALL (inline)
    except Exception:
        _status = "error"; raise
    finally:
        # metrics: duration + count (labels=func.__name__)
        ...
    # token-estimate metric
    return result

return mcp_server.tool()(_instrumented)       # :383  FastMCP registration
```

FastMCP dispatch (container SDK `mcp` 1.28.0,
`mcp/server/fastmcp/utilities/func_metadata.py:92-95`):

```python
if fn_is_async:
    return await fn(**arguments_parsed_dict)   # offloaded-friendly branch
else:
    return fn(**arguments_parsed_dict)         # INLINE on the loop — current path
```

### 2.2 The change

Make `_instrumented` **`async def`** and run the (still-sync) traced body through
`asyncio.to_thread`. FastMCP then takes the `fn_is_async` branch and `await`s it,
so the sync work executes on a worker thread, off the loop.

```python
import asyncio  # add module-level import to _app.py (none today)

@functools.wraps(func)
async def _instrumented(*args, **kwargs):        # NOW async
    import yadgar.server._state as _st_ref
    if _st_ref._maintenance_mode:
        return {"error": "maintenance", ...}     # stays on loop — fine, trivial

    _t0 = _time.monotonic(); _status = "ok"
    try:
        # OFFLOAD: the entire traced sync body runs on a worker thread.
        result = await _run_offloaded(_traced_func, *args, **kwargs)
    except Exception:
        _status = "error"; raise
    finally:
        # metrics emit stays on the loop thread (microsecond dict ops) — fine
        ...
    return result
```

**Design choices, justified:**

- **Offload the WHOLE `_traced_func`, not a sub-call.** `_traced_func` is the
  trace-wrapped tool body (`:341`). Running the trace span + tool body on the
  worker thread keeps the span wall-time accurate (it measures the real work) and
  is one clean boundary. OpenTelemetry spans are not loop-affine — `trace_span`
  uses context vars / a thread-local-safe tracer, but **the audit must confirm
  span context propagates correctly across the thread boundary** (OTEL context is
  `contextvars`-based; `asyncio.to_thread` copies the current context into the
  worker via `contextvars.copy_context()` — so the parent span IS inherited. Verify
  against `yadgar/tracing.py` `trace_span`).
- **Metrics stay on the loop.** The `finally` block touches Prometheus counters
  (`yadgar_mcp_request_duration_ms` etc.) — microsecond in-memory ops, safe on the
  loop; no need to offload them. (`prometheus_client` counters are themselves
  thread-safe, so even if they ran on the worker it'd be fine — but keeping them on
  the loop is simplest.)
- **`functools.wraps` preserves async-ness.** `iscoroutinefunction` reads the code
  object's `CO_COROUTINE` flag, which `functools.wraps` does **not** overwrite
  (`wraps` copies `__wrapped__`/`__name__`/`__doc__`, not the code flags). So
  FastMCP's `fn_is_async` detection sees `_instrumented` as async. **Audit
  precondition AP1: add a unit test asserting
  `inspect.iscoroutinefunction(registered_tool)` is True post-decoration.**
- **Sync-only assumption — guard it.** `_run_offloaded` dispatches the body into a
  worker thread, which is correct **only if the original `func` is sync `def`**. If
  any `@_tool()` target were `async def`, `to_thread(_traced_func)` would run the
  coroutine *function* in a thread and return an un-awaited coroutine object (a
  silent bug). Risk is low (an async tool body would already be broken under
  today's sync `_instrumented`, so none should exist), but make it explicit: at
  decoration time, branch on `inspect.iscoroutinefunction(func)` — sync → offload;
  async → `await` directly on the loop (or assert sync-only and fail loudly).
  Pairs with AP1.

### 2.3 `_run_offloaded` — the offload primitive

Do **not** use bare `asyncio.to_thread` (it dispatches to the default executor,
which is unbounded up to `min(32, cpu+4)` and shared with everything else). Use a
**dedicated bounded `ThreadPoolExecutor`** owned by the app, via
`loop.run_in_executor`:

```python
# module-level in _app.py (or a small server/_offload.py)
_TOOL_POOL: ThreadPoolExecutor | None = None  # created at startup

def _offload_enabled() -> bool:
    return os.environ.get("YADGAR_OFFLOAD_TOOLS", "1") not in ("0", "false", "no")

async def _run_offloaded(fn, *args, **kwargs):
    if not _offload_enabled():
        return fn(*args, **kwargs)               # KILL-SWITCH: inline (today's behaviour)
    loop = asyncio.get_running_loop()
    call = functools.partial(fn, *args, **kwargs)
    fut = loop.run_in_executor(_TOOL_POOL, _ctx_wrap(call))
    return await asyncio.wait_for(fut, timeout=_TOOL_TIMEOUT_SEC)   # §6
```

- `_ctx_wrap` copies the current `contextvars.Context` into the worker
  (`ctx = contextvars.copy_context(); return lambda: ctx.run(call)`) so trace
  context + any request-scoped context vars propagate (matches `to_thread`'s
  semantics; explicit because we use a custom executor).
- The pool is created at daemon startup and shut down on graceful stop (wire into
  `lifecycle.py` startup/shutdown; the existing `server.shutdown()` path).

---

## 3. GIL analysis — THE FOUNDATION (does offload actually free the loop?)

**Question:** Python threads share one GIL. `asyncio.to_thread` only frees the
loop if the offloaded work **releases the GIL while blocked/computing**. If a hot
path holds the GIL the whole time, offloading it does NOT free the loop — it just
moves a GIL-hogging op to another thread that still starves the loop.

**Verdict for the CORE daemon: YES — offload frees the loop. Per-path:**

| Core hot path | What blocks | Releases GIL? | Evidence |
|---|---|---|---|
| **Inline `git` subprocess** (proven RCA cause) | `subprocess.check_output` waiting on child | **YES** | CPython `subprocess` releases the GIL while the child runs / it blocks on the pipe. This is the **strongest** evidence — it is *exactly* the documented hang and offloading it unblocks the loop with certainty. `project.py:119-122,163-177`. |
| **Storage query** (`_q_server`) | `httpx.Client.post` socket IO | **YES** | `httpx` sync IO blocks in a C socket read; CPython releases the GIL around blocking socket syscalls. `storage/client.py:515-527`. |
| **Query embedding** | `RemoteEmbeddingEngine` httpx `POST /embed` | **YES** | Remote — socket IO, same as above. `remote_embeddings.py:58`. NOT local torch in core. |
| **Rerank** | `RemoteMLClient` httpx `POST /rerank/{mode}` | **YES** | Remote — socket IO. `ml_client.py:664`. The 46s CPU cross-encoder runs in the **backend** container, not on core's loop. |

**Conclusion:** every core hot path is GIL-releasing (socket IO or subprocess).
Offloading them to a worker thread genuinely frees the loop thread to keep
spinning coroutines (incl. `/health`). **There is no core hot path that holds the
GIL for its whole duration**, so the bad-foundation risk ("offload that doesn't
help") does **not** apply to the core daemon.

**Honest scoping caveats the audit must weigh:**

1. **Lead evidence = git, not embedding/rerank.** The RCA *proved* the inline git
   subprocess is the primary cause and *ruled out* embedding-load and
   pool-saturation as the core hang cause. The plan's GIL claim leans hardest on
   the git subprocess (the certain case). Embedding/rerank being remote httpx is a
   bonus (also GIL-releasing) but was not the proven trigger.
2. **The 46s CPU rerank is a BACKEND concern, not core.** Do not claim Fix A
   removes a 46s CPU stall from the core loop — that stall never ran on the core
   loop. If the **backend** container runs rerank inline on *its* asyncio loop
   (it serves `/rerank`), that is the same bug class and needs the same fix — but
   it is **out of scope** here (O7). The audit should confirm whether the backend
   has its own loop-block and whether torch/ONNX (`CrossEncoder.predict`,
   flashrank ONNX) release the GIL there — they do during C++ inference, but that
   is a separate plan.
3. **JSON serialization (de)serialization** of tool results happens partly on the
   worker (inside the body) and partly on the loop (FastMCP envelope). Large
   payloads serialized on the loop are pure-Python (GIL-held) but bounded; not a
   freeze risk at current payload sizes. Note for completeness.

---

## 4. Bounded worker pool (the real concurrency control)

The cap on in-flight offloaded tool bodies **is** the concurrency control that
prevents a wiki-repo wave from spawning unbounded threads. Use a dedicated
`ThreadPoolExecutor(max_workers=N)` (not the shared default executor).

**Recommended N: start at `8`, env-tunable (`YADGAR_TOOL_POOL_WORKERS`).**

Rationale (and why not bigger / smaller):

- **The real ceiling is the backend's shared httpx connection pool, not CPU.**
  Core's work is IO-bound to the backend. More worker threads than backend
  connections just serialize on the connection pool (see §5 item on httpx
  `limits`). So N is bounded by *useful* downstream concurrency, not core CPU.
- **httpx connection limits must be raised in lockstep.** If the shared
  `httpx.Client` keeps the default `Limits(max_connections=...)` or — as the RCA
  noted — a **1-connection singleton**, then N workers all queue on that one
  connection and offload buys latency isolation but **zero throughput**. **The
  plan REQUIRES raising the storage + embed + rerank httpx pool `limits` to
  ≥ N** (e.g. `httpx.Limits(max_connections=N, max_keepalive_connections=N)`) so
  N workers can actually run N concurrent backend round-trips. Verify the current
  `httpx.Client(...)` construction limits at `storage/client.py:240-251`,
  `remote_embeddings.py:34`, `ml_client.py:624` and raise them. **This is a named
  dependency of the pool sizing — the audit must check it is not forgotten.**
- **Memory:** worker threads here hold no models (models live in the backend), so
  per-thread footprint is just stack + request buffers — cheap. N=8 is safe on
  memory.
- **Why 8 not 32:** 8 gives meaningful parallelism for the ~10-concurrent-subagent
  load that caused the incident, while keeping backend pressure bounded. It is a
  starting point — tune against the soak test (§7) and live wiki-repo load.
- **Overflow behaviour:** with the executor full, `run_in_executor` queues the
  callable in the executor's unbounded work queue. **This is acceptable** because
  the loop stays free (the queue lives off-loop) and `/health` keeps answering;
  excess requests wait their turn rather than freezing the daemon. (Optionally add
  an `asyncio.Semaphore(N + queue_cap)` to *reject* past a hard cap with a busy
  error — see O4; default is to queue.)

---

## 5. Thread-safety hardening — enumerate ALL shared mutable state the offload exposes

Moving tool bodies off the single loop thread to N concurrent workers makes every
piece of shared mutable state that tool bodies touch a **race**. Below: the
**complete** enumeration from a tree-wide grep, **scoped to what the CORE daemon's
tool bodies actually reach** (remote wiring §1). Each item: location, hazard,
fix.

### 5.1 MUST-FIX for Fix A (core daemon reaches these)

| # | State | File:line | Hazard | Fix |
|---|---|---|---|---|
| T1 | `RemoteEmbeddingEngine._query_cache` (OrderedDict) | `remote_embeddings.py:30`, RMW `:73-84` (`move_to_end`/`popitem`) | concurrent get/set → corruption, lost entries | wrap RMW in a `threading.Lock` (per-engine `self._cache_lock`) |
| T2 | `RemoteEmbeddingEngine._client` (httpx.Client) | `remote_embeddings.py:34`, used `:58` | **VERIFY (load-bearing):** httpx.Client is documented thread-safe for concurrent requests (httpcore's connection pool is lock-guarded) — confirm against the pinned httpx version before relying on it. If true → no lock, perf-only bottleneck. The current `limits=` are **unconfirmed** (the "1-conn singleton" claim came from the task brief, not measured); the audit must read the actual construction call to establish the baseline | if confirmed thread-safe: no lock; **raise `limits` to ≥ N** (§4). If the version is NOT concurrent-safe: add a lock |
| T3 | `RemoteMLClient._client` (httpx.Client) | `ml_client.py:624`, used `:664-688` | same as T2 — VERIFY thread-safety + read actual baseline `limits=` | same as T2 |
| T4 | `RemoteMLClient._breakers` (dict of CircuitBreaker) | `ml_client.py:646-656`, mutated `:689-698` (`record_success`/`record_failure`/`is_open`/`_state`/`_open_at`) | concurrent breaker state RMW → wrong open/close, lost failure counts | add a `threading.Lock` around breaker state transitions (per-breaker or per-client) |
| T5 | `StorageEngine._http` (httpx.Client) | `storage/__init__.py:240-251`, used `client.py:515-527` | same VERIFY as T2/T3 (thread-safe? actual `limits=`?) | raise `limits` to ≥ N; confirm the engine is a process singleton shared across tool bodies |
| T6 | **`_action_batch` guarded by `asyncio.Lock`** | `_state.py:93` (lock `:69-71` region) | **LOOP-AFFINE primitive.** An `asyncio.Lock` is **not thread-safe** and is bound to the loop. If any offloaded tool body `await`s/acquires it from a worker thread → breakage / undefined behaviour | **AUDIT PRECONDITION AP2** (below). Tool bodies are sync — they cannot `await` an asyncio.Lock anyway; the risk is a tool body calling a helper that touches the loop. Must grep + confirm no sync tool body reaches `_action_batch` or any asyncio primitive on the worker thread. If one does, that path stays on the loop or converts to `threading.Lock`. |
| T7 | `_last_session_context`, `_last_prompt_recall`, `_last_recalled_ids`, `_team_inbox_positions`, `_plan_file_hashes`, `_event_queue`, `_event_seq`, `_system_metrics_cache` | `_state.py:105-139` | OrderedDict/dict/deque/int written by tool bodies, **unprotected** | for each that a tool body mutates: guard with the existing `threading.Lock`s (`_queue_lock`/`_event_lock`/`_metrics_lock`, `:69-71`) or add one. **Audit which of these tool bodies actually write vs. which are loop-only (hook routes).** |
| T8 | `get_settings` / config `@lru_cache` | `config_registry.py:103-105`, `config.py` | `functools.lru_cache` **is** thread-safe for the cache dict itself (CPython locks internally), but `clear_config_caches()` racing a read is undefined | low risk (config rarely cleared at runtime); note + add lock only if runtime cache-clear is reachable from a tool body |
| T9 | `_yaml_layer` `@lru_cache(maxsize=1)` | `config_registry.py:72` | same as T8 — lru_cache thread-safe; first-population race is benign (idempotent load) | accept; document |
| T10 | branch caches `_detect_branch_cached` / `_get_default_branch_cached` (`@lru_cache`) | `project.py:110-160` | `lru_cache` is thread-safe; the **git subprocess inside** is the thing being offloaded. Concurrent misses now run on N workers (fine — that's the goal) | accept; lru_cache handles the cache races |
| T11 | Prometheus metric counters (`yadgar_mcp_*`, `yadgar_tool_token_estimate_total`) | `metrics.py`, emitted `_app.py:370-378` | `prometheus_client` metrics are thread-safe | accept; kept on loop anyway (§2.2) |

### 5.2 BACKEND-ONLY (out of scope for Fix A — core does NOT reach these)

Listed so the audit can confirm they are correctly excluded, and flagged for O7
(backend offload follow-up):

- `EmbeddingEngine._model_cache` (class dict, `embeddings.py:58`), `_query_cache`
  (`:64`), `_ensure_model` TOCTOU (`:161-184`) — **local torch engine**; core uses
  `RemoteEmbeddingEngine`, so core never touches these. Backend does.
- `LocalMLClient` lazy loaders `_gte_reranker`/`_nli_model`/`_flashrank_ranker`/
  `_cross_encoder` (`ml_client.py:340-456`), `_last_used` (`:345,472,493`),
  TOCTOU in `score_cross_encoder`/`score_nli` — **backend** in-process ML.
- `embed_service.py:322-326` `_engine`/`_reranker` (already double-checked
  `threading.Lock`) — **backend**.
- `_enrichment_pipeline` module global (`storage/__init__.py:87`) — confirm whether
  core's tool bodies touch it; if write-once-read-many and populated at startup,
  benign. **Audit item.**

### 5.3 Already-safe (no action)

- `rate_limit.py:30-31` `_buckets` — guarded by `threading.Lock`. ✓
- `security/allowlist.py:71-72` `_allowlist` — guarded by `threading.Lock`. ✓
- `tracing.py:54-58` `_SPAN_LOG_*` — guarded by `threading.Lock`. ✓
- `embed_service.py:322` `_engine`/`_reranker` — double-checked `threading.Lock`. ✓
  (backend anyway)

**Audit precondition AP2 (loop-affinity grep) — MANDATORY before build:** grep
every sync tool body (the functions decorated with `@_tool()`) for loop-bound
primitives: `asyncio.Lock`, `asyncio.get_event_loop`, `get_running_loop`,
`run_coroutine_threadsafe`, `loop.`, `await ` inside a sync helper they call,
`asyncio.Event`/`Queue`. Any hit = a path that breaks when run on a worker thread.
This is the RCA's own flagged precondition ("audit tool bodies for loop-thread
affinity"). The result of this grep gates the build: zero hits → proceed; any hit
→ that tool either stays inline (per-tool opt-out) or the loop-affine call is
converted to a thread-safe equivalent.

---

## 6. Per-request timeout (now effective post-offload)

Today a per-request timeout is useless: the blocking git/IO runs on the loop, so
`asyncio.wait_for` can't fire (the loop is the thing that's blocked). **After
offload**, the body runs on a worker, the loop is free, and `asyncio.wait_for`
around the awaited future **works**.

- **Wrap the offloaded future** (§2.3 `_run_offloaded`):
  `await asyncio.wait_for(fut, timeout=_TOOL_TIMEOUT_SEC)`.
- **Recommended `_TOOL_TIMEOUT_SEC = 30s`, env-tunable
  (`YADGAR_TOOL_TIMEOUT_SEC`).** Rationale: normal tool calls are sub-second to a
  few seconds; the worst legitimate case is a cold backend rerank round-trip.
  30s is generous enough to avoid false timeouts yet bounds a wedged op. Tune
  against soak-test p99.
- **On timeout — the critical honest caveat:** `asyncio.wait_for` cancels the
  *awaiting coroutine* and frees the **loop**, but **a thread running in a
  `ThreadPoolExecutor` cannot be killed.** The worker thread keeps running the
  wedged op (e.g. a git stuck in D-state, the RCA's 2h hypothesis) and **holds its
  pool slot** until it finally returns or errors. Design consequences:
  - The caller gets a `TimeoutError` → return a structured `{"error": "timeout"}`
    to the client; the loop and `/health` stay responsive (the whole point).
  - But the **pool slot leaks** until the op self-releases. Enough simultaneously-
    wedged ops (≥ N) → **pool exhaustion**: new tool calls queue indefinitely,
    throughput stalls. **Crucially, the loop stays alive → `/health` still answers
    → the daemon is not "hung" in the HTTP-000 sense → P0 healthcheck-kill does
    NOT trip.** This is a real residual: a pool-exhausted-but-loop-alive daemon.
  - **Mitigations (state explicitly):** (a) the offloaded git already has
    `timeout=2` at the subprocess level, so the common case self-releases fast;
    (b) httpx calls carry their own client timeouts, bounding IO waits; (c) the
    truly-unkillable case (git D-state) is rare and is exactly what P0's
    healthcheck-kill was designed for **only if** it manifests as HTTP-000 — which
    pool exhaustion does NOT. **Open decision O5:** add a liveness signal that
    trips on *pool exhaustion* (e.g. `/health` returns degraded when the pool has
    been saturated for > T seconds), so P0 can kill a pool-starved daemon. Without
    this, pool exhaustion is a new (rarer, slower) failure mode the auditor must
    weigh against the hang it replaces.

---

## 7. Test strategy (the user's hard requirement — REAL, not mocks)

### 7.1 The harness problem (must be solved, not papered over)

**Existing e2e tests call `server.memorize()` IN-PROCESS** (`tests/e2e/conftest.py:146-211`
`e2e_engines` fixture → `server.init_engines()` → direct Python calls). They
**bypass `_instrumented` and the event loop entirely** — there is no FastMCP
dispatch, no loop to block. **This harness CANNOT exercise the offload or
demonstrate loop-blocking.** Using it would be exactly the "async-mock theater"
the user forbade.

**Decision: the concurrency e2e test MUST use a real daemon over HTTP.** Spawn the
actual core daemon as a subprocess (the real `_app.py` dispatch, real
`_instrumented`), pointed at a **real SurrealDB** via the existing
`reap-test-surreal` / `surreal_server` session fixture (`tests/conftest.py:269-300`),
and drive it with an HTTP client (`POST /mcp` + `GET /health`). This is the only
path that exercises the real dispatch boundary the fix changes.

### 7.2 Test 1 — concurrency/load e2e (the red→green guard, MUST fail today)

**Goal:** prove the loop stays responsive while a tool body blocks.

**Mechanism — a deterministic loop-blocker (no git-cache-timing flake):** register
a **test-only sync tool** whose body does `time.sleep(_SLOW_SEC)` (e.g. 2s).
`time.sleep` releases the GIL, so inline it blocks the loop **only by occupying
the loop thread** — which is *precisely* what offload fixes. This is more
deterministic than racing a git cache-bucket boundary and exercises the same
mechanism (a sync body holding the loop thread).

- Gate the test tool behind an env flag (`YADGAR_TEST_TOOLS=1`) so it is never
  registered in production.

**Test body:**
1. Start the real daemon subprocess (real surreal, isolated data dir, free port).
2. Fire **N≥8 concurrent** calls to the slow test tool (or N concurrent real
   `recall`/`wiki_read` calls with a deliberately-blocking backend stub — but the
   sleep-tool is cleaner and real).
3. **Concurrently** poll `GET /health` repeatedly while the slow calls are
   in-flight.
4. **Assertions:**
   - **Today (offload OFF / `YADGAR_OFFLOAD_TOOLS=0`):** `/health` is starved —
     times out / returns HTTP 000 — for ~`N×_SLOW_SEC / 1` (loop blocked serially).
     **This is the FAIL-ON-TODAY proof.** Run the test in both modes; assert this
     mode is unresponsive.
   - **After A (offload ON):** `/health` returns 200 within a tight budget (e.g.
     200ms p99) **the whole time** the slow calls run, AND the N slow calls all
     complete in ~`_SLOW_SEC` wall-clock (parallel), not `N×_SLOW_SEC` (serial).
5. The test is parametrized over `YADGAR_OFFLOAD_TOOLS ∈ {0,1}` — `0` asserts the
   pathology (xfail-strict-style: must be unresponsive), `1` asserts the fix.

### 7.3 Test 2 — offload assertion (thread-id check)

A test-only tool returns `threading.get_ident()` / `threading.current_thread().name`.
- Offload ON → the returned ident is **not** the event-loop thread's ident
  (capture the loop thread ident via a pure-async route or a known marker).
- Offload OFF → the returned ident **is** the loop thread.
Proves the body genuinely runs off-loop.

### 7.4 Test 3 — thread-safety stress (unit/integration, real objects)

Hammer the newly-exposed shared state from many threads, no mocks of the unit
under test:
- **`RemoteEmbeddingEngine._query_cache`:** spin K threads each doing M
  `encode()` calls with overlapping + distinct keys (stub only the httpx
  transport to return deterministic vectors fast — the **cache RMW** is the unit
  under test, not the network). Assert: no exceptions, no `KeyError`/`RuntimeError:
  OrderedDict mutated during iteration`, cache size respects its bound, no lost/
  duplicated entries. **Fails without the T1 lock, passes with it.**
- **`RemoteMLClient._breakers`:** K threads racing `record_success`/`record_failure`
  on the same breaker; assert final counts are consistent (no lost updates).
  Fails without the T4 lock.
- **Lazy-init race** (if any core-reachable lazy init exists after the §5 audit):
  K threads hitting first-use simultaneously; assert single initialization, no
  half-built object observed.

### 7.5 e2e constraints (the harness rules)

- **Real surreal** via `reap-test-surreal.sh` + `surreal_server` session fixture;
  skip (not fake) if `surreal` binary absent (matches existing e2e policy).
- **Deterministic:** `-p no:randomly`, `-n0` (serial e2e), `--reruns 2` for the
  network-timing-sensitive `/health` budget assertion (matches the `make e2e`
  target). The sleep-tool removes most timing flake.
- **CI-runnable:** wires into `make e2e` (`-m e2e`); the daemon-subprocess fixture
  must use `_find_free_port()` and an isolated `YADGAR_DATA_DIR` (tmp_path), never
  the production data dir (`_assert_not_real_data_dir`), and must respect the
  production-DB guard (`pytest_configure`, exit 78).
- **No module-scope OTEL poison:** the `make e2e` target already sets
  `OTEL_SDK_DISABLED=true`; the new test must not init a module-scope TracerProvider.
  The daemon subprocess inherits `OTEL_SDK_DISABLED`. Any in-test OTEL use goes
  through the function-scoped `reset_otel_state` autouse fixture pattern
  (`test_tracing.py`).
- **Port/daemon lifecycle:** new fixture `daemon_subprocess` (function or
  module scoped) that boots the core daemon, waits for `/health` 200, yields base
  URL, and tears down (SIGTERM + reap). Model it on `surreal_server`'s spawn/wait/
  teardown shape.

### 7.6 What is explicitly NOT acceptable

- No mocking `_instrumented`, FastMCP, or `asyncio.to_thread` and asserting it was
  "called." The test must exercise the **real** dispatch + a **real** blocking
  body + a **real** `/health` route.
- No in-process `e2e_engines` shortcut for Test 1/2 (it bypasses the loop).

---

## 8. Rollout / safety

- **Flag-gated with a kill-switch.** `YADGAR_OFFLOAD_TOOLS` env (§2.3).
  **Recommendation: default-ON (`"1"`)** — the inline path is the *proven bug*; the
  whole point is to stop shipping it. But ship with the kill-switch so a live
  regression can be reverted to inline behaviour by flipping one env var + restart
  (no redeploy of code). This mirrors yadgar's I25/I32 default-flip discipline:
  land the knob, default it to the safe-new behaviour, keep the escape hatch.
- **Phasing:**
  1. Land the offload + pool + locks + tests behind the flag, **default-ON**, with
     the §5 audit (AP1/AP2) complete and green.
  2. Raise httpx `limits` to ≥ N in the same change (hard dependency — §4).
  3. Validate in CI (`make e2e` concurrency test green; thread-safety stress
     green; existing suite green — Loop-Until-Clean).
  4. **Live validation: the wiki-repo load is the real test.** Reproduce the
     incident's ~10-concurrent-subagent wiki/recall load against the patched
     daemon; watch loop-lag + `/health` p99 + the pool saturation metric. Keep P0
     healthcheck-kill deployed throughout as the backstop.
  5. Only after live soak, consider removing the kill-switch (a later cleanup).
- **I25/I32 implications of the new knobs:** three new env knobs
  (`YADGAR_OFFLOAD_TOOLS`, `YADGAR_TOOL_POOL_WORKERS`, `YADGAR_TOOL_TIMEOUT_SEC`)
  plus raised httpx `limits`. Register them in the config registry / settings
  surface consistent with existing knob conventions; document defaults; ensure
  `YADGAR_PROFILE=minimal` and the test profile inherit sane values. The auditor
  should confirm these knobs are surfaced the same way other I-series knobs are,
  not bolted on ad hoc.
- **Version bump + changelog:** Fix A is daemon code → version bump (per repo
  convention, e.g. v5.82). Note the SDK-pin dependency: if `mcp` is bumped past
  1.28.0 to a version that offloads sync tools itself, re-verify `func_metadata`
  (RCA Open Decision 4) — Fix A would become redundant/double-offload and need
  reframing.

---

## 9. Risks + open decisions (numbered — for the user/auditor)

1. **O1 — httpx pool limits are a hard dependency, easily forgotten.** Raising the
   storage/embed/rerank `httpx.Limits` to ≥ N is REQUIRED for the pool to deliver
   throughput, not just latency isolation. If shipped without it, offload helps
   `/health` responsiveness but tool throughput stays bottlenecked at the old
   connection cap. **Audit must verify the limits change ships with the pool —
   and first establish the baseline `limits=` (currently unconfirmed; the
   "1-conn singleton" figure came from the task brief, not measured) plus confirm
   httpx.Client concurrent-request thread-safety for the pinned version (T2/T3/T5).**
2. **O2 — pool exhaustion is a NEW failure mode** (§6). A wedged op past timeout
   leaks its thread/slot; ≥ N wedged → throughput stalls **with the loop still
   alive**, so P0 healthcheck-kill (which keys on HTTP-000) does **not** trip.
   Decide: add a pool-saturation liveness signal (O5) or accept the residual.
   This is the most important thing the audit should scrutinize — Fix A trades a
   loud HTTP-000 hang for a quieter pool-exhaustion stall.
3. **O3 — N (pool size).** Recommended 8; depends on backend connection capacity
   and the real wiki-repo concurrency. Tune against soak. Confirm the backend can
   serve N concurrent `/embed` + `/rerank` + SurrealDB calls without itself
   stalling (backend has its own loop — O7).
4. **O4 — queue vs reject on overflow.** Default: queue (loop stays free). Option:
   `asyncio.Semaphore` hard cap → reject with busy error past N+queue_cap.
   Rejecting protects the backend from overload; queuing is simpler. Decide.
5. **O5 — pool-saturation liveness.** Should `/health` go degraded when the worker
   pool is saturated for > T seconds, so P0 can kill a pool-starved-but-loop-alive
   daemon? Closes the O2 residual. Adds a metric + a `/health` branch.
6. **O6 — loop-affinity audit outcome (AP2).** The plan assumes no sync tool body
   reaches an `asyncio.Lock`/loop primitive on the worker thread. **This must be
   proven by grep before build** (§5 T6). If a tool *does* (e.g. via a shared
   helper that touches `_action_batch`'s asyncio.Lock), that path needs a per-tool
   inline opt-out or conversion to `threading.Lock`. Unverified assumption →
   foundation crack.
7. **O7 — the BACKEND container is unaddressed.** The backend serves `/embed` +
   `/rerank` (in-process torch, incl. the 46s cross-encoder) and embedded
   SurrealDB. If it runs that work inline on *its* asyncio loop, it has the same
   bug class — and under N-concurrent core offload, core will now hit the backend
   with N parallel requests, potentially **moving the stall to the backend**.
   Fix A makes core resilient; it may **expose** a backend bottleneck. **Strongly
   recommend a sibling plan (Fix A-backend) audited in parallel.** Do not ship
   Fix A's raised N without confirming the backend can absorb the parallelism.
8. **O8 — OTEL span context across the thread boundary.** `to_thread` /
   `copy_context()` propagates contextvars, so the parent span is inherited — but
   the audit must confirm `yadgar/tracing.py` `trace_span` is contextvars-based
   (not a thread-local or loop-bound tracer) and that span timing/nesting stays
   correct off-loop. If `trace_span` is loop-affine, tracing breaks under offload.
9. **O9 — metrics on which thread.** Plan keeps metric emission on the loop
   (§2.2). If a future change moves it onto the worker, `prometheus_client`
   counters are thread-safe so it's fine — note for the auditor that this is a
   deliberate, reversible choice.
10. **O10 — graceful-shutdown ordering.** The new `ThreadPoolExecutor` must be
    shut down cleanly on `server.shutdown()` (drain or cancel in-flight, join with
    a bound). In-flight offloaded ops during a graceful stop need a defined
    behaviour (the existing `STOPPING=1`/graceful-stop orchestrator path). Define
    pool teardown in the lifecycle.
11. **O11 — hook-route inline git is NOT covered by Fix A** (§0.1). `http.py:838`
    (`async hook_subagent_stop`), `:1203`, `:1234` run blocking git inline on the
    loop and are `@custom_route`, not `@_tool()` — outside the offload boundary.
    Two `POST /hooks/auto-capture` co-fired at the actual hang, so this is a *real*
    residual loop-block vector after Fix A. **Decide: fold into Fix A** (wrap each
    in `to_thread`, matching the already-correct `_memorize` at `http.py:849`) **or
    defer to a sibling fix.** Either is acceptable; the §0 "loop never blocks" claim
    is only true once this is also addressed. This is the second item (with O2 and
    O7) the audit should scrutinize hardest.

---

## 10. Key files (for the build + audit)

- `yadgar/server/_app.py` — dispatch wrapper to convert (`_tool`/`decorator`/
  `_instrumented`/`_traced_func`, `310-383`; offload at the `_traced_func` call
  `:358`). Add module-level `asyncio` import + the bounded pool.
- `yadgar/server/lifecycle.py` — engine selection (`:264` remote-embed,
  `:269` `RemoteMLClient`); add pool create/teardown at startup/shutdown.
- `yadgar/remote_embeddings.py` — `_query_cache` lock (T1, `:30,73-84`); httpx
  `limits` (T2, `:34`).
- `yadgar/backend/ml_client.py` — `RemoteMLClient` httpx `limits` (T3, `:624`);
  `_breakers` lock (T4, `:646-698`).
- `yadgar/storage/__init__.py` / `storage/client.py` — httpx `limits` (T5,
  `:240-251`); confirm engine singleton.
- `yadgar/server/_state.py` — `_action_batch` asyncio.Lock loop-affinity (T6,
  `:93`); the unprotected `_state` dicts (T7, `:105-139`); existing locks
  (`:69-71`).
- `yadgar/server/http.py` — hook-route inline git (O11, `:838/1203/1234`); the
  already-correct `to_thread` pattern to mirror (`_memorize` at `:849`); `/health`
  route (`:267-340`) for the concurrency test.
- `yadgar/tracing.py` — confirm `trace_span` contextvars-safe across threads (O8).
- `yadgar/tests/e2e/` + `tests/conftest.py` (`:269-300` surreal fixture) +
  `Makefile` (`e2e` target) — new daemon-subprocess HTTP concurrency test.
- `mcp/.../func_metadata.py:92-95` — the async/sync dispatch branch (SDK 1.28.0).
- `/home/max/git/nix/modules/home/yadgar.nix:506` — core container env (the
  remote-wiring proof); P0 healthcheck-kill (already deployed).

---

## 11. Sequencing summary

1. **Pre-build audit (gates everything):** AP1 (iscoroutinefunction preserved) +
   AP2 (loop-affinity grep of all tool bodies, O6) + O8 (trace_span contextvars).
2. **Write failing tests first** (test-driven): Test 1 (concurrency e2e, must fail
   on inline), Test 3 (thread-safety stress, must fail without locks).
3. **Implement:** async `_instrumented` + bounded pool + `_run_offloaded` +
   `wait_for` timeout + the §5 locks + raised httpx limits + lifecycle pool
   teardown. All behind `YADGAR_OFFLOAD_TOOLS` (default-ON).
4. **Loop until clean:** `make e2e` + full suite + lint/types green.
5. **Live soak** under wiki-repo load with P0 backstop deployed; watch
   loop-lag/`/health` p99/pool saturation.
6. **Backend follow-up (O7)** audited in parallel — do not raise N to production
   without it.
```
