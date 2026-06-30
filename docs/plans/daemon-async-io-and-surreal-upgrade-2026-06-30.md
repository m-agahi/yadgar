# Daemon async-I/O refactor + SurrealDB upgrade — evaluation & recommendation

Date: 2026-06-30
Status: design / decision doc (no code changes)
Related: `daemon-offload-A-2026-06-30.md` (Fix A — threadpool offload, the crashing path),
`daemon-hang-rca-and-recovery-2026-06-30.md` (RCA), `daemon-concurrency-design-2026-06-30.md`
(queue/pool/formula — written in parallel), crash issue #74.

---

## TL;DR

- **Prod runs SurrealDB in SERVER mode** (`YADGAR_DB_URL=http://yadgar-backend:8000`,
  `Dockerfile:17`, `docker-compose.yml:83`). In server mode **all** SurrealDB I/O goes
  through `httpx.Client.post("/sql")` (`storage/client.py` `_q_server`/`_q_timeout`),
  **NOT** the `surrealdb` SDK. The SDK `Surreal(...)` client is used **only** for
  embedded `surrealkv://` mode (dev/test/local).
- **Therefore the prod async fix is an `httpx.AsyncClient` swap, not a SurrealDB SDK
  migration.** `AsyncSurreal` matters only for the embedded/dev path.
- **`AsyncSurreal` is real and viable** — ships in the same `surrealdb` package as
  `Surreal` (the 1.0 rewrite onward; latest is **2.0.0**, 2026-04-23), and it **does**
  support `surrealkv://` embedded async. No SDK *upgrade* is strictly required for prod
  (the dep `surrealdb>=1.0.0` already allows it), but bumping the floor is cheap.
- **SurrealDB server upgrade v3.0.5 → v3.1.5 is recommended and low-risk**: 3.1 is the
  "operational maturity" release; **on-disk/catalog layout is unchanged from 3.0.x → rolls
  forward in place**; the only breaking change is in **GraphQL**, which yadgar does not use
  (it speaks `/sql` HTTP). It is **independent** of the async refactor — decoupled axis.
- **The big call: async-I/O is the correct *foundation*; the threadpool-offload (Fix A) is
  a backstop, not the foundation.** BUT async does **not** make Fix A wholesale
  unnecessary — **they compose**, because `git` subprocess calls and the embedded-mode SDK
  still block the loop after the httpx swap. Recommended: **phase async-I/O in, keep Fix A
  OFF as the deployed safety net for the residual blocking surface**, retire Fix A only once
  every remaining blocking call site is async.

---

## 1. SurrealDB Python client — async support (task item 1)

### Verified facts (online)

- The `surrealdb` package exposes **two factory functions**: `Surreal` (sync, blocking)
  and `AsyncSurreal` (async). "Both classes have the same methods, the only difference is
  the addition of the `async`/`await` keywords." Use `AsyncSurreal` with asyncio
  frameworks (FastAPI, aiohttp).
  Source: <https://surrealdb.com/docs/sdk/python/concepts/create-a-new-connection>,
  <https://surrealdb.com/blog/how-we-improved-the-python-sdk-for-our-1-0-stable-version>
- `AsyncSurreal` **supports embedded `surrealkv://`** (and `mem://`, `file://`,
  `rocksdb://`): "pass a `mem://`, `file://`, or `surrealkv://` URL to the `Surreal` or
  `AsyncSurreal` factory function … The SDK supports sync or async Python runtimes … and
  support for SurrealDB embedded in-memory and on-disk."
  Source: <https://surrealdb.com/docs/languages/python/concepts/embedded-databases>,
  <https://surrealdb.com/docs/surrealdb/embedding/python>
  - Caveat: embedded connections "do not support the full set of features available with
    WebSocket or HTTP connections" (no sessions/transactions/live-queries in embedded).
    yadgar's embedded path uses plain `.query()` only, so this is not a blocker.
- The 1.0 rewrite switched the wire protocol from JSON to **CBOR** and gives "100% control
  over how the async request is implemented with no hidden threads."
  Source: <https://surrealdb.com/blog/how-we-improved-the-python-sdk-for-our-1-0-stable-version>
- **Latest SDK: `2.0.0`, released 2026-04-23**; supports Python 3.10–3.13.
  Source: <https://pypi.org/project/surrealdb/>

### Code reality (verified in repo)

- Dep pin: `surrealdb>=1.0.0` (`pyproject.toml:71`).
- The SDK `Surreal` import + use appears **only** in the embedded path:
  `storage/client.py:353` `from surrealdb import Surreal`, `:362`
  `Surreal(f"surrealkv://{resolved}")`, dispatched in `_q_embedded` (`client.py:530`).
- **Server mode never touches the SDK.** `_q` (`client.py:548`) branches on `self._db_url`:
  if set → `_q_server` (`client.py:494`) → `self._http.post("/sql", …)`; else → embedded SDK.
- Prod sets `YADGAR_DB_URL` → server branch always taken in containers.

### Verdict — item 1

**`AsyncSurreal` is VERIFIED available and viable, and the current dep already permits it
(no hard upgrade required).** But it is **only relevant to the embedded/dev/test path**.
For the prod hot path, the SurrealDB SDK is not on the critical path at all — the
async lever there is `httpx.AsyncClient`. Recommend bumping the dep floor to
`surrealdb>=2.0.0` opportunistically (clean async API, current CBOR protocol) when/if the
embedded path is converted, but this is not gating.

---

## 2. SurrealDB SERVER version (task item 2)

### Code reality (verified)

- Server is pinned **`v3.0.5`** in three places: `Dockerfile.backend:20`
  (`COPY --from=surrealdb/surrealdb:v3.0.5`), `Dockerfile.ci:38`
  (`ARG SURREAL_VERSION=v3.0.5`), `scripts/install/restore.sh:153`.

### Verified facts (online)

- **Latest server: `v3.1.5` (2026-06-19).** 3.1.x line: 3.1.0 (Jun 5) → 3.1.5 (Jun 19).
  Source: <https://github.com/surrealdb/surrealdb/releases>
- **3.1 = "operational maturity" release**: production stability, DiskANN
  (larger-than-memory vector search), audit logging / slow-query observability (Enterprise),
  faster release/security process.
  Source: <https://surrealdb.com/blog/surrealdb-3-1-stability-diskann-and-a-new-release-process>
- **Concurrency/perf gains relevant to yadgar's recall fan-out:**
  - In-memory backend now uses **optimistic lock coupling** — readers acquire lock-free
    access, retry only on a late conflict; "readers proceed without blocking writers, and
    vice versa." Directly helps the N-concurrent-recall pattern async enables.
  - **Warm-lookup ANN path rewritten end to end**; HNSW inherits the new caches at no
    migration cost — relevant because recall is vector-search-heavy.
  - "Concurrent builds and KNN queries are safe at release."
  Source: <https://surrealdb.com/3.1>, blog link above.
- **Breaking changes:** confined to the **GraphQL** surface (more expressive, breaking for
  GraphQL clients). yadgar uses the `/sql` HTTP endpoint, **not** GraphQL → not affected.
  Source: <https://surrealdb.com/3.1>
- **Migration: in-place.** "The catalog and on-disk layouts are unchanged from 3.0.x, so
  existing deployments roll forward in place."
  Source: blog link above.
- Known open perf issue to watch (not a regression vs 3.0.5, a pre-existing class): heavy
  `count() GROUP ALL` scans can monopolize the runtime under TiKV (#7358). yadgar uses the
  embedded/single-node engine, not TiKV — low relevance, but note before any backend swap.
  Source: <https://github.com/surrealdb/surrealdb/issues/7358>

### Verdict — item 2

**Recommend upgrading the pinned server v3.0.5 → v3.1.5.** Gains: lock-free reader
concurrency (in-memory backend) + rewritten warm-lookup ANN path, both of which directly
benefit concurrent recall. Risk is **low**: in-place roll-forward (no migration), the only
breaking change is GraphQL (unused). **It is INDEPENDENT of the async refactor** — the
upgrade is a separate axis driven by perf + the new security-patch cadence, *not* required
to make the client async (prod speaks HTTP `/sql`, which is stable across 3.0→3.1).
Sequence them separately: server bump can ship first/standalone, soak, then async.

---

## 3. Async embed / rerank (task item 3)

### Code reality (verified)

- `remote_embeddings.py:36` — `httpx.Client(base_url=embed_url, timeout=30.0, …)` (sync).
- `backend/ml_client.py:632` — `httpx.Client(base_url=base_url, …)` (sync) for the
  core→backend `/embed` `/rerank` calls.
- An async httpx pattern **already exists** in the codebase: `server/http.py:284-285`
  `async with httpx.AsyncClient(timeout=2.0) as _aclient:` ("§9 Q5: async httpx client to
  avoid blocking the event loop"). So the team already validated the idiom.

### Verified facts (online)

- `httpx.AsyncClient` is the async equivalent of `httpx.Client`: **same surface, but every
  request method must be awaited** (`await client.post(...)`), lifecycle is
  `async with httpx.AsyncClient() as c:` / `await c.aclose()`, streaming uses
  `async with c.stream()` + `aiter_*`. It is **not a transparent drop-in** (await/async
  keywords propagate to call sites) but the per-call change is mechanical.
  Source: <https://www.python-httpx.org/async/>
- AsyncClient supports connection pooling and concurrency limits; the docs warn to use a
  single scoped/global client (no per-call construction in hot loops) — matches yadgar's
  existing singleton pattern. Limits via `httpx.Limits(max_connections=…)`.
  Source: <https://www.python-httpx.org/async/>,
  <https://www.python-httpx.org/advanced/#pool-limit-configuration>

### Verdict — item 3

**Yes — swapping the core's embed/rerank `httpx.Client` → `httpx.AsyncClient` lets recall
`await` the backend without blocking the loop.** This is the cleanest part of the refactor:
the cross-encoder rerank is **CPU on the BACKEND**, not the core (`backend/embed_service.py`
already offloads scoring with `asyncio.to_thread`/`run_in_executor` server-side). From the
**core's** perspective, embed and rerank are **pure network I/O** — an exact fit for async.
The core never does the CPU work, so there is no CPU-bound work to block its loop here.

---

## 4. The architectural question — async-I/O vs threadpool-offload (task item 4, the crux)

### What the threadpool-offload (Fix A) is, and why it crashes

Fix A (`server/_offload.py`, default OFF, `config.py:722 OFFLOAD_TOOLS=False`) keeps the
~60 sync `def` tool bodies sync and dispatches each onto a **bounded ThreadPoolExecutor**
via `loop.run_in_executor` + `asyncio.wait_for`, so a blocking body can't freeze the loop.
The crash on real recall (#74) is the predictable failure of running **non-thread-safe**
state across pool threads: the sync `httpx.Client` and the embedded `surrealdb.Surreal`
client are shared singletons not designed for concurrent multi-thread use, and the storage
engine carries mutable per-call state. Offload trades loop-freeze for a thread-safety hazard
class. That is *why* the user's instinct ("make the I/O async instead") is sound.

### Why async-I/O is the correct foundation

If storage (`_http.post`), embed, and rerank become `await`-able, the MCP tool bodies become
`async def` and **the single asyncio loop serves many concurrent recalls natively** — which
is the entire point of asyncio. No threads, **no cross-thread shared-state hazard, no
ThreadPoolExecutor, no `wait_for`-frees-coroutine-but-leaks-worker accounting** (the gnarly
O2-gate logic in `_offload.py` exists *only* to paper over the threadpool model). FastMCP
already dispatches `async def` tool bodies (the audit confirmed; the codebase already has
async HTTP route handlers using `asyncio.to_thread` around sync storage, e.g.
`http_wiki_versioning.py`, `http_bookmarks.py`). The core is **pure-I/O** from its own
vantage (all CPU — embeddings, rerank — lives on the backend), so async is a textbook fit:
asyncio best practice is "async for I/O-bound, threads/processes for CPU-bound"; the core is
the former.

### Why async does NOT eliminate Fix A wholesale — they COMPOSE

After the httpx-async swap, the following still **block the loop** and remain a
loop-freeze hazard, exactly the class Fix A was built to contain:

1. **`git` subprocess.** `_offload.py`'s own docstring names *"the proven inline `git`
   subprocess"* as a demonstrated loop-freezer. `httpx.AsyncClient` does nothing for it.
   git stays blocking until converted to `asyncio.create_subprocess_exec` (or kept behind
   `asyncio.to_thread`). Wiki commits, snapshot/restore, and several admin tools shell out.
2. **Embedded-mode SDK calls.** If/when the embedded path stays sync `Surreal`, those
   `.query()` calls block. Converting to `AsyncSurreal` is possible (item 1) but is extra
   scope; until then embedded/dev/test still needs offload-or-`to_thread`.
3. **Residual sync CPU/JSON** on the core (large response normalization, JSON encode of
   unbounded SQL bodies — see the `_q_server` TODO H-4) is small but non-zero.

So: **async kills the offload *for the recall I/O path* (DB read + embed + rerank), not for
the whole tool surface.** The honest verdict is **compose, not replace**: async-I/O is the
foundation that removes the *need to offload the hot recall path*; Fix A (or targeted
`asyncio.to_thread` wrappers) remains the backstop for the residual blocking surface (git,
embedded SDK) until those too go async.

### Verdict — item 4

**Async-I/O is the right foundation; threadpool-offload is a transitional backstop.**
Recommendation: build async-I/O, keep Fix A deployed but **OFF** as the safety net, and
**retire Fix A only after every residual blocking call site (git, embedded SDK) is async or
explicitly `to_thread`-wrapped**. Do not frame async as a wholesale replacement on day one.

---

## 5. asyncio best-practice fit (task item 5)

- The cross-encoder rerank is **CPU-bound on the BACKEND**, already offloaded server-side
  (`backend/embed_service.py:740 await asyncio.to_thread(_score)`,
  `:275 run_in_executor(..., score_cross_encoder, …)`). The **core** does **no** CPU rerank.
- Therefore the **core daemon is a pure-I/O workload** from its own loop's perspective:
  DB over HTTP, embed over HTTP, rerank over HTTP, git over subprocess. Standard asyncio
  guidance — "use async/await for I/O-bound concurrency; use threads/processes only for
  CPU-bound work" — points cleanly at **async** for the core, with the **one exception** of
  git subprocess (convert to async subprocess) and any embedded SDK calls (convert to
  `AsyncSurreal` or `to_thread`). **Confirmed: async is a clean fit for the core.**

---

## 6. Phasing (async is the bigger change — sequence it)

Refactor surface is broad (~hundreds of `.query`/`_q`/`embed`/`rerank`/`_http.post` call
sites across `yadgar/`). Phase to keep each step shippable + reversible:

- **Phase 0 (independent, ship first):** Server bump v3.0.5 → v3.1.5 (in-place, GraphQL-only
  breaking change, unused). Soak. Standalone PR. Optionally bump `surrealdb>=2.0.0` floor.
- **Phase 1 — async transport, leaf-first:** Introduce `httpx.AsyncClient` singletons in
  `storage/__init__.py` (server mode), `remote_embeddings.py`, `backend/ml_client.py`
  alongside the sync ones. Add `async` variants of `_q_server`/`_q_timeout` (`_aq_server`)
  and async `embed`/`rerank`. Keep sync paths intact (embedded + any sync caller).
- **Phase 2 — async recall path end-to-end:** Make the recall tool body + its storage/embed/
  rerank calls `async def`/`await` top-to-bottom. Prove one hot tool fully async under
  concurrent load (this is the #74 crash repro path — verify no crash, no loop-freeze).
- **Phase 3 — widen to remaining hot tools** (wiki_query, restore, memorize read paths).
- **Phase 4 — residual blockers:** convert `git` to `asyncio.create_subprocess_exec` (or
  wrap `to_thread`); convert embedded path to `AsyncSurreal` (or wrap `to_thread`).
- **Phase 5 — retire Fix A** only once Phases 1–4 leave no inline-blocking call on any
  async tool body. Until then Fix A stays deployed-but-OFF as the backstop.

Each phase: TDD (failing concurrency/no-block test first), loop until clean.

---

## 7. Composition with the parallel concurrency doc + #74

- **vs `daemon-concurrency-design-2026-06-30.md` (queue/pool/formula):** async-I/O changes
  *what* the loop does (await instead of block) but the **downstream limiter moves, not
  disappears.** With async tool bodies, N concurrent recalls fan out to the backend
  simultaneously → the real cap becomes the **backend's `RERANK_MAX_CONCURRENCY`** (and
  embed concurrency), not the core loop. That is precisely the seam the concurrency doc's
  queue/pool/formula governs: async makes the core capable of N-parallel, and the
  concurrency-design formula sizes N so the backend isn't overrun. They are complementary —
  async = mechanism, concurrency-design = the governor. Keep the core's outbound concurrency
  bounded (`httpx.Limits` + a semaphore sized to the backend formula), not unbounded.
- **vs the #74 crash fix:** #74 is the threadpool-offload crashing on real recall. Async-I/O
  **dissolves the root cause for the recall path** (no threads → no shared-client
  thread-safety hazard). The immediate #74 mitigation remains "Fix A default OFF" (already
  the case); the durable fix is Phase 2 (async recall path), after which the offloaded recall
  path that crashes simply no longer exists. Do **not** ship Fix A ON as the #74 fix —
  ship async as the fix and keep Fix A as the inert backstop.

---

## 8. Open questions / risks

- **Backend client lifecycle under async:** the core's `httpx.AsyncClient` must be created
  on the running loop and closed in lifecycle shutdown (mirror the sync singleton + the
  existing `server/http.py` async pattern). One AsyncClient per process, not per call.
- **Mixed sync/async during phasing:** sync callers of a now-async storage method need a
  bridge. Prefer pushing async upward (caller becomes async) over `asyncio.run` islands
  (which deadlock on the running loop). Where a sync caller is unavoidable mid-phase, wrap
  the *sync* path in `asyncio.to_thread`, never call the async path via `run`.
- **Embedded transactions:** AsyncSurreal embedded lacks sessions/transactions/live-queries
  (online caveat) — confirm the embedded path doesn't rely on them before converting (it
  uses plain `.query()`, so likely fine).
- **CBOR vs current JSON-over-HTTP:** prod uses raw `/sql` HTTP (text/plain body), unrelated
  to the SDK's CBOR — no change. Only the embedded SDK path touches CBOR; verify on a
  `surrealdb>=2.0.0` bump.

---

## 9. Bottom line / recommendation

1. **Async-I/O is the correct foundation** for the daemon's concurrency; the
   threadpool-offload (Fix A) is a backstop, not the answer. Build async.
2. **For prod, the async lever is `httpx.AsyncClient`, not the SurrealDB SDK** — prod speaks
   `/sql` HTTP. `AsyncSurreal` is for the embedded/dev path only (and is available now).
3. **Async and Fix A COMPOSE, they don't replace** — git subprocess + embedded SDK still
   block; keep Fix A deployed-but-OFF until those go async (Phases 4–5), then retire it.
4. **Bump server v3.0.5 → v3.1.5** as an independent, low-risk, ship-first step (in-place
   roll-forward, GraphQL-only breaking change which yadgar doesn't use; real concurrency
   gains for recall fan-out).
5. **The async refactor's downstream governor is the parallel concurrency doc's
   queue/pool/formula** sizing the core→backend fan-out against `RERANK_MAX_CONCURRENCY` —
   build async with bounded outbound concurrency, not unbounded.
