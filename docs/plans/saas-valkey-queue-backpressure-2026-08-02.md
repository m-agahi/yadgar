# SaaS Queue + Backpressure Design (2026-08-02)

**Status:** DRAFT — addendum to the SaaS rewrite plan (PR #25) and the
loose-coupling addendum (PR #27). Fills the gap where neither explicitly
covered work queues or backpressure.
**Branch:** `docs/saas-valkey-queue-backpressure-2026-08-02` (doc-only)
**Amends:**
- `docs/plans/saas-rewrite-plan-2026-08-02.md` §3.5 (stores — Valkey role expands)
- `docs/plans/saas-loose-coupling-and-db-per-service-2026-08-02.md` §5.5 (metering outbox generalizes to a queue trait)

---

## 1. The problem

Three paths in the 13-service architecture have arrival rates that can
exceed service rates:

| Path | Why it backs up | Consequence without backpressure |
|---|---|---|
| gateway → write | write does similarity gate + Surreal write (~10-50ms/op); an agent session can burst 50-200 writes in minutes | gateway hangs on write calls, or OOMs buffering them in-process |
| gateway → ml (embed) | every write needs an embedding; embed is CPU/GPU-bound | gateway blocks on embed, write queue stalls |
| recall → ml (rerank) | every recall needs rerank; rerank is CPU/GPU-bound | recall latency spikes to seconds, user experience degrades |

Without a queue + backpressure, the gateway either blocks (bad — the user
waits), buffers in-process (bad — OOM under load), or drops silently (worst
— data loss). The right answer: **queue the async work, reject with 429
when the queue is full, let the client retry.**

---

## 2. The decision: Valkey lists as work queues (no separate broker)

**Valkey (already running for cache + rate-limit + sessions) is also the
work queue.** No Kafka, no RabbitMQ, no NATS. One Valkey, four jobs:

```
Valkey (one process/cluster):
  1. cache            — scope_versions, query result caches, model caches
  2. rate-limit + sessions — INCR/EXPIRE counters, session JWTs with TTL
  3. work queues      — LPUSH/BRPOP for: write, embed, rerank, metering-outbox
  4. DLQ              — dead-letter lists for failed work items
```

**Why not a separate message broker:**
- The workload is bursty agent sessions, not streaming data pipelines.
- Valkey is already running — one less moving part, one less thing to
  operate, one less failure mode.
- Solo mode: embedded Valkey handles all four jobs in ~10MB.
- SaaS: Valkey cluster (which you need for cache scaling anyway) handles
  the queue load.
- The swap point (if you ever outgrow Valkey queues) is a `Queue` trait
  with a Valkey impl and a future Kafka/Redpanda impl — a swap, not a
  rewrite.

**When you WOULD need a real broker** (not day-one):
- Durability requirements that Valkey AOF can't meet (rare — write
  durability is backed by Surreal, not the queue)
- Event replay / event sourcing (a billing-system problem, not yadgar-core)
- Cross-cluster streaming (a scale you don't have at launch)

---

## 3. The Queue trait

Every queue operation goes through a trait, so the backing store is
swappable:

```rust
// crates/yadgar-protocol/src/queue.rs (sketch)

pub trait Queue: Send + Sync {
    /// Enqueue a work item. Returns Ok if accepted, Err if the queue
    /// is at capacity (backpressure signal — caller should return 429).
    async fn push(&self, queue: &str, item: &WorkItem) -> Result<(), QueueFull>;

    /// Dequeue a work item, blocking up to timeout. Returns Ok(item)
    /// or Err(Timeout) if no work arrived.
    async fn pop(&self, queue: &str, timeout: Duration) -> Result<WorkItem, QueueTimeout>;

    /// Current queue depth. Used by the gateway for backpressure checks
    /// and by monitoring for alerting.
    async fn depth(&self, queue: &str) -> Result<usize>;

    /// Move an item to the dead-letter queue for the given queue.
    /// Called by a consumer after max retries exhausted.
    async fn dead_letter(&self, queue: &str, item: &WorkItem, reason: &str) -> Result<()>;
}

pub struct WorkItem {
    pub id: Uuid,
    pub tenant_id: Uuid,
    pub payload: serde_json::Value,
    pub enqueued_at: DateTime<Utc>,
    pub attempt: u32,
    pub max_attempts: u32,
}

pub enum QueueFull { AtCapacity { depth: usize, limit: usize } }
pub enum QueueTimeout { Elapsed }
```

Two implementations:
- `ValkeyQueue` — the default, LPUSH/BRPOP/LLEN, used by solo + SaaS.
- `InProcessQueue` — `tokio::sync::mpsc` channels, used in solo mode when
  even embedded Valkey is undesirable (zero external dependencies). Fallback.

---

## 4. The queues

| Queue name | Producer | Consumer | Pattern | Backpressure |
|---|---|---|---|---|
| `queue:write` | gateway | yadgar-write | async (agent doesn't wait) | 429 if depth > WRITE_QUEUE_LIMIT |
| `queue:embed` | yadgar-write | yadgar-ml | async (write waits for embed to complete the memory) | write stalls (not 429 — embed is on the write critical path, not the user's) |
| `queue:rerank` | yadgar-recall | yadgar-ml | **synchronous** (recall waits for rerank, but degrades to unreranked if timeout/queue-full) | degrade, not 429 |
| `queue:metering` | gateway (all services) | yadgar-metering | fire-and-forget (outbox) | drop + log (metering is never on the user path) |
| `queue:*:dlq` | any consumer | dead-letter inspector | after max retries | monitored, not backpressured |

### 4.1 Write queue — async, 429 backpressure

```
client → gateway /v1/memorize
  → IAM auth (~5ms)
  → check LLEN queue:write
    → if > WRITE_QUEUE_LIMIT (e.g. 1000):
        return 429 "write queue full, retry after N seconds"
        (headers: Retry-After: N, X-Queue-Depth: D)
  → LPUSH queue:write {memory_json}
  → return 202 Accepted + Location: /v1/status/{item_id}
    (the write will complete async — the agent doesn't wait)

yadgar-write: BRPOP queue:write (blocking consumer loop)
  → parse WorkItem
  → similarity gate (check for duplicates)
  → vault.encrypt (if sensitive)
  → surreal INSERT
  → if success: mark done, maybe enqueue embed if new embedding needed
  → if failure: retry up to max_attempts, then dead_letter()
```

**Why 202 not 200:** the write is async. The client gets a status URL
(can poll or ignore — most agents don't care, they fire-and-forget the
write and trust it'll land). This is the same pattern as the current
Python yadgar's file queue (`wait=False` returns `{stored: True, queued:
True}`), just over Valkey instead of files.

**Why 429 not 500:** a full queue is not an error — it's a load signal.
429 + `Retry-After` tells the client (or the gateway's retry layer) to
back off and try again. This is standard HTTP backpressure.

### 4.2 Embed queue — async, write-side, no 429

Embed is on the **write** critical path (a memory without an embedding is
not recallable), but NOT on the **user's** critical path (the user fired
the write and got 202). So embed queue backpressure doesn't 429 the user —
it slows down write completion:

```
yadgar-write: after surreal INSERT, LPUSH queue:embed {memory_id, text}
yadgar-ml: BRPOP queue:embed → embed → update surreal with embedding
  → if queue:embed is deep, writes take longer to become recallable
    (acceptable — a 30s delay between write and recallability is fine)
  → if embed fails after max_attempts: dead_letter + log (the memory
    exists without an embedding — reembed-stale can fix it later)
```

**Backpressure here is implicit:** if embed is slow, the embed queue
grows, write completion takes longer. The gateway doesn't 429 — it already
returned 202. The only visible effect is that freshly-written memories
take longer to appear in recall results. This is the current Python
yadgar's behavior too (embed is async post-write).

### 4.3 Rerank queue — synchronous with graceful degradation

Rerank is on the **user's** critical path (recall waits for it). But
recall can degrade:

```
yadgar-recall: after retrieval + fusion, before returning results
  → check LLEN queue:rerank
    → if > RERANK_QUEUE_LIMIT (e.g. 100) OR circuit breaker open:
        return unreranked results (recall@k drops, but the tool works)
        + header: X-Rerank-Status: degraded
    → else: LPUSH queue:rerank {candidates}
      → BRPOP queue:rerank:results {item_id} with 200ms timeout
        → if timeout: return unreranked (don't make the user wait)
        → if result: return reranked
```

**This is the graceful-degradation pattern from the loose-coupling
addendum (§5.4):** recall without ml degrades to unreranked. The queue
adds a backpressure layer — even if ml is up but overwhelmed, recall
degrades instead of hanging. The user gets results faster, just slightly
lower quality.

### 4.4 Metering queue — fire-and-forget outbox

```
any service: after completing a request
  → LPUSH queue:metering {tenant_id, action, qty, ts}
  → if LLEN > METERING_QUEUE_LIMIT: DROP + log (metering is never on the
    user path — a dropped usage event is recoverable from logs, a failed
    user request is not)

yadgar-metering: BRPOP queue:metering → write to yadgar_metering DB
  → if DB write fails: retry up to max_attempts, then dead_letter
```

**Already covered in the loose-coupling addendum §5.5** — this just
formalizes it as a Valkey queue instead of "a local outbox."

### 4.5 Dead-letter queues

Every queue has a corresponding `queue:*:dlq`. After `max_attempts`
retries, the consumer moves the item to the DLQ with a reason. A
`yadgar-control` admin tool inspects DLQs and can requeue or dismiss
items (mirrors the current Python yadgar's `dlq_inspect` / `dlq_requeue`
/ `dlq_dismiss` MCP tools).

---

## 5. Backpressure protocol — the 429 contract

When a queue is at capacity, the producer returns **429 Too Many Requests**
with:

```
HTTP/1.1 429 Too Many Requests
Content-Type: application/json
Retry-After: 5                          ← seconds to wait before retrying
X-Queue-Depth: 1047                     ← current depth (observability)
X-Queue-Limit: 1000                     ← the limit that was hit

{
  "error": "write_queue_full",
  "message": "The write queue is at capacity. Retry after 5 seconds.",
  "retry_after_sec": 5,
  "queue_depth": 1047,
  "queue_limit": 1000
}
```

**The gateway handles 429 from downstream services by propagating it to
the client** (with the same Retry-After header). The MCP client (Claude
Code, opencode, etc.) sees the 429 and can retry the tool call after the
backoff. This is standard HTTP backpressure — no custom protocol.

**Circuit breaker integration:** if a downstream service returns 429
repeatedly (e.g. >50% of requests in a 10s window), the gateway's circuit
breaker opens for that service — subsequent requests fail fast (503)
instead of each one hitting the 429. The circuit closes after a cooldown
period. This prevents a slow downstream from getting hammered by retries.

---

## 6. Queue depth limits (configurable per tenant)

| Queue | Default limit | Solo | SaaS |
|---|---|---|---|
| `queue:write` | 1000 | 100 (less memory) | 1000 per tenant (per-tenant isolation) |
| `queue:embed` | 500 | 50 | 500 per tenant |
| `queue:rerank` | 100 | 20 | 100 per tenant |
| `queue:metering` | 5000 | 1000 | 5000 (shared, not per-tenant — metering is fire-and-forget) |

**Per-tenant queue isolation (SaaS):** queue names include the tenant_id:
`queue:write:{tenant_id}`. A noisy tenant filling their write queue doesn't
block other tenants' writes. The depth check is per-tenant, not global.

**Configurable:** limits are runtime config knobs in `yadgar_config` (the
config service's DB), live-tunable via `config_set` — no restart needed.

---

## 7. What this replaces from the current Python yadgar

| Current Python | SaaS rewrite |
|---|---|
| `yadgar/_shared/file_queue/` (file-based async write queue + DLQ) | `queue:write` Valkey list + `queue:write:dlq` |
| `wait=False` returns `{stored: True, queued: True}` | `202 Accepted` + `Location: /v1/status/{id}` |
| `wait=True` blocks until drainer commits | synchronous call to `yadgar-write` (no queue — for read-your-writes consistency) |
| `dlq_inspect` / `dlq_requeue` / `dlq_dismiss` MCP tools | `yadgar-control` admin tools, same three operations on Valkey DLQ lists |
| `QUEUE_DRAIN_INTERVAL` / `QUEUE_MAX_PERMANENT_ATTEMPTS` / `QUEUE_BACKOFF_*` config knobs | same knobs, in `yadgar_config`, live-tunable |
| embed is async post-write (sleep_compute/embed_compress.py) | `queue:embed` Valkey list, same async pattern |
| reembed_stale (nightly, scheduler-triggered) | scheduler job → enqueues to `queue:embed` in bulk |

**The file queue's semantics are preserved** — async writes, retry with
backoff, dead-letter after max attempts, `wait=True` for read-your-writes.
The backing store changes from files to Valkey (faster, cross-process,
survives restart with AOF persistence, already running).

---

## 8. The `wait=True` case (read-your-writes consistency)

Some MCP tools need read-your-writes: `wiki_add(wait=True)` blocks until
the drainer commits so a subsequent `wiki_read` sees the page. Under the
queue architecture, `wait=True` is a **synchronous call to
`yadgar-write`** (bypassing the queue), not a queued write + poll:

```
gateway /v1/wiki_add (wait=True)
  → call yadgar-write directly (HTTP, not via queue)
    → yadgar-write: similarity gate → vault.encrypt → surreal INSERT → commit
  → return 200 (the write is committed, a subsequent read sees it)

gateway /v1/wiki_add (wait=False)
  → LPUSH queue:write {page_json}
  → return 202 Accepted (async — read may not see it for a few seconds)
```

**The queue is for the async path. The sync path is a direct HTTP call.**
This matches the current Python yadgar's `wait` parameter semantics
exactly — the rewrite preserves the API, changes the backing store.

---

## 9. Solo vs SaaS topology

### Solo (embedded Valkey or in-process channels)

```
--features solo:
  queue = InProcessQueue (tokio::sync::mpsc channels)
  → zero external dependencies, no Valkey needed for the queue
  → but Valkey is still there for cache + rate-limit + sessions
  → or: embedded Valkey handles all four jobs (simpler, one store)
```

The solo binary can use either:
1. **Embedded Valkey for everything** (cache + rate-limit + sessions + queues) — simplest, one store.
2. **In-process channels for queues + embedded Valkey for cache/sessions** — zero-Valkey mode for the most minimal solo binary.

Recommendation: option 1 (embedded Valkey for everything) — one store, one
set of semantics, the queue trait's Valkey impl is the default everywhere.
Option 2 is the fallback for environments where even embedded Valkey is
undesirable.

### SaaS (Valkey cluster)

```
--features distributed:
  queue = ValkeyQueue (LPUSH/BRPOP against the Valkey cluster)
  → per-tenant queue names: queue:write:{tenant_id}
  → queue depth monitoring via LLEN, alerting via yadgar-metering
  → DLQ inspection via yadgar-control admin tools
```

The Valkey cluster that handles cache for SaaS also handles queues. Queue
load is additional but not a separate scaling axis — if the cluster handles
the cache load, it handles the queue load (queues are lighter than cache:
small JSON items, no large payloads).

---

## 10. Monitoring + alerting

Queue depth is a primary operational metric. Each service exposes:

```
/metrics:
  queue_depth{queue="write",tenant="..."} 1047
  queue_depth{queue="embed",tenant="..."} 23
  queue_depth{queue="rerank",tenant="..."} 5
  queue_depth_limit{queue="write"} 1000
  queue_push_total{queue="write"} 4523
  queue_pop_total{queue="write"} 4476
  queue_429_total{queue="write"} 12          ← backpressure events
  queue_dlq_depth{queue="write"} 3           ← dead-letter count
```

**Alerting rules (in yadgar-metering or the platform's alertmanager):**
- `queue_depth / queue_depth_limit > 0.8` for 5 min → warn (approaching backpressure)
- `queue_429_total` rate > 10/min → page (backpressure is actively rejecting requests)
- `queue_dlq_depth > 0` → warn (items are failing — investigate)

---

## 11. What this amends in the prior plans

| Prior plan | Amendment |
|---|---|
| SaaS rewrite §3.5 (stores — "Valkey: cache + rate-limit + sessions") | Valkey role expands to: **cache + rate-limit + sessions + work queues + DLQ** |
| SaaS rewrite §4.1 (cache — three problems solved) | Add: 4. work queues + backpressure (this doc) |
| Loose-coupling §5.5 (metering outbox) | Generalizes to a `Queue` trait (§3 of this doc) — metering is one of four queues, not a special case |
| SaaS rewrite §11 (migration order) | Insert: the `Queue` trait goes into `yadgar-protocol` (step 1); the `ValkeyQueue` impl goes with `yadgar-cache` (step 3); write/embed/rerank queue wiring goes with their respective services (steps 8-10) |

---

## 12. The one-sentence summary

**Valkey lists are the work queue; writes are async (202 + 429 backpressure),
rerank is synchronous-with-degradation, metering is fire-and-forget, and
the file-based queue from the current Python yadgar becomes a Valkey list
with the same semantics — one less moving part, not one more.**
