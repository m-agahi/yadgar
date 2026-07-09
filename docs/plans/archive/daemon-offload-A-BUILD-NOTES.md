# Fix A — BUILD NOTES (feat/daemon-offload-A)

Companion to `daemon-offload-A-2026-06-30.md` + its `## AUDIT`. Records the
concrete build decisions (advisor-vetted) so the diff is reviewable.

## Default: OFF for first release (advisor override of plan's default-ON)

`YADGAR_OFFLOAD_TOOLS` default = **OFF**. Rationale: this changes the threading
model across ~60 tools; default-ON bets all of it on first deploy. OFF keeps prod
behaviour byte-identical to today's code + the already-deployed P0 health-kill
backstop. Flip ON deliberately after live soak (one-line default change). The
proven trigger stays live until the flip — mitigated by P0 in the interim. **This
flip is the immediate follow-up.**

Three-way knob semantics (I25): unset → OFF. `("1","true","yes","on")` → ON;
anything else → OFF.

## Offload mechanism (`server/_offload.py` + `_app.py`)

- **Dual-wrapper (correction during build).** `_app._tool` builds BOTH a sync and
  an async wrapper around the same traced body (`_build_tool_wrappers`). The ASYNC
  wrapper is registered with FastMCP (SDK takes its `await fn` branch → offload).
  The SYNC wrapper is RETURNED as the module-level name. WHY: FastMCP `.tool()`
  returns the original fn unchanged, so the module name binds to whatever the
  decorator returns; ~20 internal/test callers import a `@_tool()` name and call
  it SYNCHRONOUSLY — a single async wrapper handed them a coroutine (20 test
  failures). The sync wrapper preserves the pre-Fix-A direct-call contract (run
  inline); ONLY the FastMCP dispatch path offloads. Sync-only guard at decoration.
  Metrics/trace-span shared via `_emit_metrics`; the trace-wrapped sync body runs
  on the worker.
- `_run_offloaded(fn, *args, **kwargs)`: if offload disabled → call inline
  (today's behaviour). Else `loop.run_in_executor(_pool, _ctx_wrap(call))` wrapped
  in `asyncio.wait_for(timeout)`. `_ctx_wrap` = `copy_context().run` so the OTel
  parent span (set on the loop by FastMCP) propagates to the worker.
  `trace_span` confirmed contextvars-based (`get_tracer`+`start_as_current_span`).
- Bounded `ThreadPoolExecutor(max_workers=N)`, lazy-created bound to the running
  loop, torn down in `lifecycle.shutdown()` (`shutdown(wait=False, cancel_futures=True)`
  + bounded join so wedged threads don't hang graceful stop).
- Knobs: `YADGAR_TOOL_POOL_WORKERS` (default 8), `YADGAR_TOOL_TIMEOUT_SEC`
  (default 30). On timeout → structured `{"error":"timeout"}`, metrics status set.

## O2 — THE GATE: pool-saturation health signal (advisor-critical)

**Counter decrement on the WORKER THREAD at true completion** (inside `_ctx_wrap`'s
`finally`), NOT coroutine-side. If decremented coroutine-side, a `wait_for` timeout
frees the counter while the wedged worker still holds its slot → `/health` reads
200 → P0 can't kill → the exact O2 regression the audit gated. The counter must
track *true pool occupancy*.

Signal = **completion-staleness**, not "full-since":
`degraded(503) when inflight >= pool_max AND (now - last_completion_ts) > T`.
"Full-since" would 503 a healthy daemon under legit peak (queue keeps pool full
while draining) — P0 kills a healthy daemon at the worst moment. Staleness only
trips when nothing drains. `T` (`YADGAR_TOOL_SATURATION_GRACE_SEC`, default 45) set
> wait_for timeout (30) so leaked threads trip it but legit ops keep resetting
`last_completion_ts`. Own `submitted`/`completed`/`last_completion_ts` counters
(threading.Lock guarded); never read executor internals. Queue stays unbounded; no
rejecting semaphore.

`/health` (`_build_health_payload`) reads `offload_pool_saturated()` → sets
`status="degraded"` → handler returns 503 (existing C1 path). P0's `curl -f` trips.

## Claim-1 startup assert

When offload ON, assert `YADGAR_EMBED_URL` set (remote engines). Else the
`lifecycle._init_embedding_client` else-branch builds local torch EmbeddingEngine +
LocalMLClient → CPU on the worker (still GIL-held during pure-python, breaks the
GIL premise). Fail loud at startup.

## O11 — 3 hook-route git wraps

`http.py` hook handlers (`@custom_route` async, NOT `@_tool` — outside offload):
`_detect_branch` inline (subagent_stop ~838) + 2 `subprocess.run(["git",...])`
(~1203, ~1234). Wrap each in `asyncio.to_thread`, mirroring the existing
`_memorize` `to_thread` (~849).

## O7 — backend rerank concurrency

`config.py:708` `RERANK_MAX_CONCURRENCY: 1 → 8` + registry default. **Backend
container (5.8.0) reads this — needs rebump/env to pick up.** Flagged for deploy;
NOT a silent change.

## Claim-6 locks (threading.Lock, never asyncio.Lock — acquired on workers)

- `RemoteEmbeddingEngine._query_cache` (remote_embeddings.py) — RMW lock.
- `_CircuitBreaker` mutators/queries (ml_client.py) — per-breaker lock.
- `_stale_count_cache` (project.py:2230) — RMW lock (the plan-MISSED one).
- `_enrichment_pipeline` (storage/__init__.py:87) — double-checked init lock
  (the plan-MISSED double-init).
- DROP the 4 over-scoped http.py-only `_state` items (loop-thread-only, async
  routes). O1 dead (httpx default 100).

## O6/AP2 re-verification (post-build, binding gate)

Re-ran the loop-affinity grep AFTER the build (the audit's grep predated the
builder's lock edits). **CLEAN — zero hits:**

```
grep -rnE 'asyncio\.(Lock|Event|Queue|Semaphore|create_task|ensure_future)|get_running_loop|get_event_loop|run_coroutine_threadsafe|run_until_complete|\bawait ' yadgar/server/tools/
→ (no matches)
```

No sync tool body reaches a loop-affine asyncio primitive on a worker thread. All
4 new locks are `threading.Lock` (worker-safe), zero `asyncio.Lock` added. The
remote-client hot paths (remote_embeddings / ml_client / storage.client) reached
by tool bodies contain no loop-affine primitives either. Foundation verified —
the offload boundary is safe to flip ON.

NOTE: the test suite cannot substitute for this grep — default-OFF runs bodies
inline (sync wrapper), and the e2e only exercises the test tools, so no real tool
body ever ran on a worker thread in the green suite. The grep is the only check
that catches a loop-affine real-tool body before the prod flip.

## Tests (real, not mocks)

- e2e concurrency (`tests/e2e/`): real daemon subprocess + real surreal + HTTP.
  Test-only `time.sleep` tool behind `YADGAR_TEST_TOOLS=1`. Parametrize
  OFF→/health starved (FAIL on today) vs ON→/health 200 in budget.
- O2 exhaustion: op sleeps >> wait_for timeout; fire ≥N; assert /health 503 at
  ~timeout+T. Also the regression guard for worker-side decrement (RED with
  coroutine-side).
- offload thread-id assertion; thread-safety stress on cache/breakers/stale_count.
- No module-scope OTEL poison; OTEL only on the pytest env.
