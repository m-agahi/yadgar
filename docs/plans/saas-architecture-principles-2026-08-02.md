# SaaS Architecture Principles (2026-08-02)

**Status:** DECISION — the load-bearing rules every service, crate, and plan
in the SaaS rewrite must follow. All other plan docs (PR #25 rewrite plan,
PR #27 loose coupling + db-per-service, PR #28 queue + backpressure, the
Postgres decision) are consequences of these principles, not independent
decisions.
**Branch:** `docs/saas-architecture-principles-2026-08-02` (doc-only)
**Supersedes:** nothing. **Governs:** all SaaS rewrite plan docs.

---

## Principle 1: Differences are left-shifted to the composition root

> Every swappable component is a trait in `yadgar-protocol`. Services depend
> on the trait, never on the impl. The impl is selected at the composition
> root (the `main()` where the service boots and wires its dependencies).
> Swapping a backing store changes one line at the root, zero lines in the
> service.

### 1.1 The full swappable surface

Every cross-cutting concern is a trait. Every trait has at least two impls
(solo + SaaS). Future impls are additive — a new crate that implements the
trait, no existing code changes.

| Trait | What it abstracts | Solo impl | SaaS impl | Future impls |
|---|---|---|---|---|
| `Queue` | enqueue / dequeue / depth / DLQ | `InProcessQueue` (tokio mpsc) | `ValkeyQueue` (LPUSH/BRPOP) | `KafkaQueue`, `SqsQueue`, `NatsJetStreamQueue` |
| `Cache` | get / set / invalidate / scope-version | `InProcessCache` (HashMap + LRU) | `ValkeyCache` | `RedisClusterCache`, `MemcachedCache` |
| `RelationalStore` | SQL CRUD + transactions + migrations | `SqliteStore` | `PostgresStore` | `MysqlStore`, `CockroachStore` |
| `GraphStore` | memory/wiki bodies + embeddings + KNN + graph edges | `SurrealEmbedded` | `SurrealServer` | `PostgresPgvector`, `Neo4j`, `Weaviate` |
| `Embedder` | text → vector | `CandleEmbedder` (in-process, CPU) | `CandleEmbedder` (GPU) | `RemoteEmbedder` (HTTP), `OpenAIEmbedder` |
| `Reranker` | (query, candidates) → scored candidates | `CandleReranker` (Ettin, in-process) | `CandleReranker` (GPU) | `RemoteReranker`, `CohereReranker` |
| `Authn` | verify token → identity | `BootstrapAuthn` (one static user) | `IamClient` (HTTP to yadgar-iam) | `OidcAuthn` (Keycloak/Auth0/Google) |
| `Authz` | (identity, action, resource) → allow/deny | `AllowAllAuthz` (solo) | `IamClient` (HTTP) | `OpaAuthz` (Open Policy Agent) |
| `Encryptor` | encrypt / decrypt / key rotation | `FileKeyEncryptor` (local sealed key) | `KmsEncryptor` (AWS KMS / GCP KMS / Vault) | `HsmEncryptor` |
| `Meter` | record event / check quota / rate-limit | `SqliteMeter` (logs to local file) | `PostgresMeter` + `ValkeyRateLimiter` | `ClickHouseMeter`, `StripeMeter` |
| `ObjectStore` | put / get / list snapshots + model weights | `LocalFileStore` (~/.yadgar/data/) | `S3Store` | `MinioStore`, `GcsStore`, `AzureBlobStore` |
| `Scheduler` | register job / trigger / lock / status | `InProcessScheduler` (tokio task + cron) | `PostgresScheduler` (FOR UPDATE SKIP LOCKED + leader election) | `TemporalScheduler`, `K8sCronJobScheduler` |
| `Notifier` | emit webhooks / alerts | `StderrNotifier` (logs) | `WebhookNotifier` (HTTP POST) | `SnsNotifier`, `SlackNotifier`, `PagerDutyNotifier` |

### 1.2 The rule, stated once

> **No service crate may import a backing-store SDK.** `sqlx`, `valkey`,
> `candle`, `aws-sdk-kms`, `redis`, `kafka`, `s3` — these are imported only
> by impl crates (`yadgar-cache`, `yadgar-storage-postgres`, `yadgar-ml-candle`,
> `yadgar-vault-kms`). Service crates import only `yadgar-protocol` (the
> traits). This is **lint-enforced** — a service that imports `sqlx` fails
> CI.

### 1.3 What the service code looks like (invariant across swaps)

```rust
// crates/yadgar-write/src/service.rs — this file NEVER changes when you swap stores

pub struct WriteService {
    graph: Arc<dyn GraphStore>,      // Surreal or Postgres+pgvector
    queue: Arc<dyn Queue>,           // Valkey or Kafka or in-process
    vault: Arc<dyn Encryptor>,       // KMS or file key
    meter: Arc<dyn Meter>,           // Postgres or ClickHouse
    authz: Arc<dyn Authz>,           // IAM or OPA or allow-all
}

impl WriteService {
    pub async fn write_memory(&self, req: WriteRequest, identity: &Identity) -> Result<WriteResponse> {
        self.authz.authorize(identity, "write", &req.tenant_id)?;
        let encrypted = self.vault.encrypt(&req.content, &req.tenant_id).await?;
        self.graph.insert_memory(&req.tenant_id, &encrypted).await?;
        self.meter.record(&req.tenant_id, "write", 1).await?;
        Ok(WriteResponse::accepted())
    }
}
```

**Swap Surreal for Postgres+pgvector?** Change one line at the composition
root. The `WriteService` file doesn't get touched. Its tests don't get
touched. Its API doesn't change.

### 1.4 What the composition root looks like (the ONLY place that changes)

```rust
// crates/yadgar-write/src/main.rs — distributed mode
let graph: Arc<dyn GraphStore> = Arc::new(SurrealServer::connect(&env("SURREAL_URL")).await?);
let queue: Arc<dyn Queue> = Arc::new(ValkeyQueue::connect(&env("VALKEY_URL")).await?);
let vault: Arc<dyn Encryptor> = Arc::new(KmsEncryptor::new(&env("KMS_KEY_ARN")).await?);

// crates/yadgar-write/src/main.rs — solo mode (same crate, --features solo)
let graph: Arc<dyn GraphStore> = Arc::new(SurrealEmbedded::open("~/.yadgar/data/surreal").await?);
let queue: Arc<dyn Queue> = Arc::new(InProcessQueue::new());
let vault: Arc<dyn Encryptor> = Arc::new(FileKeyEncryptor::open("~/.yadgar/data/vault.key").await?);
```

**The `WriteService` is byte-for-byte identical in both modes.** The
composition root is the only file that differs — ~15 lines of
`Arc::new(Impl::connect(...))` either way.

### 1.5 The crate structure that enforces this

```
crates/
  yadgar-protocol/        — ALL traits live here (Queue, Cache, Storage, Embedder, ...)
                            No SDK imports. Pure trait definitions + serde types.
  yadgar-cache/           — ValkeyQueue + ValkeyCache + InProcessQueue + InProcessCache
                            (imports valkey crate, implements yadgar-protocol traits)
  yadgar-storage-sqlite/  — SqliteStore impl
                            (imports sqlx with sqlite feature)
  yadgar-storage-postgres/— PostgresStore impl
                            (imports sqlx with postgres feature)
  yadgar-storage-surreal/ — SurrealEmbedded + SurrealServer impls
                            (imports surrealdb crate)
  yadgar-ml-candle/       — CandleEmbedder + CandleReranker impls
                            (imports candle-core, candle-transformers)
  yadgar-vault-kms/       — KmsEncryptor impl
                            (imports aws-sdk-kms)
  yadgar-vault-file/      — FileKeyEncryptor impl
                            (no SDK, pure Rust + ring)
  yadgar-iam/             — IamClient (Authn + Authz impl) + the IAM service itself
  yadgar-write/           — WriteService (the business logic)
                            imports ONLY yadgar-protocol. NEVER imports sqlx/valkey/candle.
  yadgar-recall/          — RecallService (the retrieval pipeline)
                            imports ONLY yadgar-protocol.
  ...etc
```

**The lint rule:** `yadgar-write`, `yadgar-recall`, `yadgar-gateway`, etc.
(service crates) must NOT import any crate in this denylist:
`sqlx`, `valkey`, `redis`, `candle-*`, `aws-sdk-*`, `surrealdb`, `kafka`,
`rdkafka`, `lapin` (amqp), `s3`, `aws-sdk-s3`. Enforced by a
`scripts/check_service_crate_deps.py` pre-commit hook that parses each
service crate's `Cargo.toml` and fails if a denied dependency appears. The
impl crates (`yadgar-cache`, `yadgar-storage-*`, `yadgar-ml-candle`,
`yadgar-vault-kms`) are allowlisted.

---

## Principle 2: Every service boots alone (loose coupling, no chicken-and-egg)

> No service's startup blocks on another service being up. A service may
> fail to serve a request if a dependency is down, but it must boot, accept
> connections, and return a clean 503/504 — not hang, crash, or retry-loop
> forever.

### 2.1 Two health endpoints

```
/healthz       — liveness: "the process is alive, not deadlocked"
                 Always 200 if the process is running. Does NOT check dependencies.
/readyz        — readiness: "I can serve requests right now"
                 200 only if all required dependencies are reachable.
                 503 if a dependency is down (but the process is still alive).
```

### 2.2 Parallel boot, fail-fast requests

```
Cluster cold start (everything boots in parallel):
  t=0:   all services start, all stores start
  t=1s:  all services /healthz=200, gateway /readyz=200 (no deps)
  t=3s:  SQL DBs up → services with SQL DBs run migrations → /readyz=200
  t=4s:  surreal up → recall + write /readyz=200
  t=5s:  ml weights loaded → ml /readyz=200
```

No service waits at startup for another. The gateway is ready at t=1s. A
request at t=2s for recall gets 503 (recall's surreal isn't up yet). At
t=4s the retry succeeds.

### 2.3 Circuit breakers on every outbound call

Every inter-service call goes through a circuit breaker:
- **Closed** (normal): calls go through.
- **Open** (downstream failing): calls fail fast (503 or degraded response),
  not a 30s timeout.
- **Half-open** (probing): one call every N seconds to test recovery.

### 2.4 Graceful degradation per tool (policy decision, documented per service)

| Caller | Dependency down | Behavior |
|---|---|---|
| recall | ml (reranker) | return unreranked results (lower quality, still works) |
| recall | surreal | 503 (can't recall without the store) |
| write | surreal | 503 (can't write) |
| write | vault (for sensitive content) | 503 — fail closed (refuse unencrypted sensitive write) |
| write | vault (for non-sensitive) | write unencrypted + flag for later encryption (fail open) |
| gateway | iam | 503 all auth'd requests (can't auth) |
| gateway | metering | outbox (fire-and-forget, never block) |
| adr_add | config service | use derived project key (D13/D14 fallback), write the ADR |

**Every degradation is an explicit policy choice, documented in the service's
crate-level README, not an accident.**

---

## Principle 3: Database-per-service (never multiple schemas in one DB)

> Each service that needs SQL owns its own database. No shared database
> with multiple schemas. Swapping, migrating, backing up, or changing the
> engine of any one service's DB must not touch the others.

### 3.1 The 10 service-owned databases

```
yadgar_adr            — ADR metadata
yadgar_tasks          — task list
yadgar_prompts        — agent prompts
yadgar_config         — runtime config knobs
yadgar_iam            — AAA (users, api_keys, roles, tenants, sessions)
yadgar_scheduler      — job registry + runs + locks
yadgar_metering       — usage events + quotas
yadgar_vault          — DEK catalog + key rotation log
yadgar_backup         — snapshot catalog
yadgar_control        — invariants log + vacuum runs
```

### 3.2 Engine per tier

| Tier | SQL engine | Why |
|---|---|---|
| Solo | SQLite (embedded, per-service `.db` files) | no server process, zero config, `sqlx` supports it |
| Team | Postgres (one cluster, 10 databases) | RLS for isolation, mature ops |
| SaaS | Postgres (right-sized per service, RLS per tenant) | RLS = engine-enforced tenant isolation, not application convention |

### 3.3 Each DB has its own migration chain

10 separate `alembic_versions` (or `sqlx migrate` version) tables, 10
separate migration directories (`migrations/adr/`, `migrations/tasks/`,
etc.). A bad revision in one chain doesn't block the others. A schema swap
on one DB doesn't touch the other nine.

### 3.4 Non-SQL stores stay shared (they're single-dataset, not multi-schema)

- **SurrealDB** — memories, embeddings, wiki bodies. Shared between
  `yadgar-recall` (read) and `yadgar-write` (write). This is the CQRS split,
  not the multi-schema anti-pattern.
- **Valkey** — cache + rate-limit + sessions + work queues + DLQ. Shared,
  ephemeral (rebuildable from the stores).

---

## Principle 4: Backpressure is explicit (429, not silent drops or hangs)

> When a service's arrival rate exceeds its service rate, the producer
> returns 429 Too Many Requests with Retry-After. The client (or gateway)
> backs off. No silent drops, no unbounded buffering, no 30s timeouts.

### 4.1 Valkey lists as work queues (no separate broker)

Valkey (already running for cache + rate-limit + sessions) is also the work
queue. One Valkey, four jobs. No Kafka/RabbitMQ/NATS on day one.

### 4.2 The four queues + their backpressure

| Queue | Pattern | Backpressure |
|---|---|---|
| `queue:write` | async (202 to client) | 429 + Retry-After if depth > limit |
| `queue:embed` | async, write-side | implicit (slows write-to-recallable, no 429) |
| `queue:rerank` | sync with degradation | degrade to unreranked if full or timeout |
| `queue:metering` | fire-and-forget | drop + log (never on user path) |

### 4.3 The Queue trait (swappable — see Principle 1)

```rust
pub trait Queue: Send + Sync {
    async fn push(&self, queue: &str, item: &WorkItem) -> Result<(), QueueFull>;
    async fn pop(&self, queue: &str, timeout: Duration) -> Result<WorkItem, QueueTimeout>;
    async fn depth(&self, queue: &str) -> Result<usize>;
    async fn dead_letter(&self, queue: &str, item: &WorkItem, reason: &str) -> Result<()>;
}
```

Solo: `InProcessQueue` (tokio mpsc). SaaS: `ValkeyQueue` (LPUSH/BRPOP).
Future: `KafkaQueue` — a new impl crate, no service code changes.

---

## Principle 5: The client is thin (one binary + two config files)

> The AI client (Claude Code, opencode, cursor, etc.) needs nothing but a
> 2MB static binary (`yadgar-hook`) + two config files. No Python, no
> Docker, no daemon, no model weights on the client.

### 5.1 Client-side footprint

```
~/.yadgar/
  bin/yadgar-hook          — 2MB static Rust binary (hook proxy)
  config.toml              — { remote_url, api_key, tenant_id }
~/.claude.json             — MCP server registration (remote URL + bearer)
~/.claude/settings.json    — hook entries (6 lines pointing to yadgar-hook)
CLAUDE.md / AGENTS.md      — rules file
```

### 5.2 The hook proxy is dumb (~100 lines)

`yadgar-hook` does one thing: read stdin, POST to the remote URL, write
stdout, exit. No local logic except the PreToolUse guard policy cache
(downloaded at session start, evaluated in-process for latency). If the
remote is down, write a JSON spool to `~/.yadgar/pending/`, flush on next
successful session-start.

### 5.3 Install is one command

```bash
curl -sSf https://yadgar.ai/install.sh | sh
```

Downloads the binary, asks for API key, detects installed AI clients,
writes MCP registration + hooks + rules file. No daemon, no Docker, no
Python. Zero resident memory between hook fires.

---

## Principle 6: API versioning is in the protocol, not bolted on

> Every inter-service call and every client-facing API is versioned from
> day one. The version is in the protocol crate's types, not in ad-hoc
> headers.

### 6.1 Versioning rules

- **External MCP API** (client-facing): URI versioning (`/v1/recall`,
  `/v2/recall`). Stable across releases, deprecation windows for breaking
  changes.
- **Inter-service API**: `X-API-Version` header + `schema_version: u16`
  field on every protocol struct. Atomic upgrades in the monorepo (bump
  protocol, update all services in one PR). Versioning matters for rolling
  upgrades (SaaS) and for external clients.
- **Solo binary**: no HTTP between services (in-process channels), so
  versioning is compile-time. The same protocol crate ensures all services
  are version-aligned at compile time.

### 6.2 The protocol crate is the single source of truth

```
crates/yadgar-protocol/
  src/
    queue.rs          — Queue trait + WorkItem + QueueFull + QueueTimeout
    cache.rs          — Cache trait + ScopeVersion
    storage.rs        — GraphStore + RelationalStore traits + Memory + Wiki types
    auth.rs           — Authn + Authz traits + Identity + Tenant + Role
    crypto.rs         — Encryptor trait + EncryptedPayload
    meter.rs          — Meter trait + UsageEvent + Quota
    embed.rs          — Embedder + Reranker traits + EmbedRequest + RerankRequest
    scheduler.rs      — Scheduler trait + JobDef + JobRun
    object_store.rs   — ObjectStore trait + SnapshotRef
    notifier.rs       — Notifier trait + Alert
    health.rs         — HealthStatus + ReadyStatus (the /healthz + /readyz contracts)
    error.rs          — ErrorEnvelope + ErrorKind (every service returns this)
    version.rs        — SchemaVersion + version negotiation
```

Every service depends on this crate. No service defines its own wire types.
Adding a field to a response is a protocol-crate change → all services
recompile → the compiler finds every place that needs updating. **The
protocol crate is the compiler-enforced contract.**

---

## Principle 7: Observability is built-in, not bolted on

> Every service emits OpenTelemetry traces, Prometheus metrics, and
> structured logs from day one. Not a phase-2 addition.

### 7.1 The observability trait

```rust
pub trait Observable: Send + Sync {
    fn record_request(&self, service: &str, endpoint: &str, duration: Duration, status: u16);
    fn record_queue_depth(&self, queue: &str, depth: usize, limit: usize);
    fn record_circuit_state(&self, service: &str, state: CircuitState);
    fn emit_log(&self, level: LogLevel, msg: &str, fields: HashMap<String, Value>);
    fn start_span(&self, name: &str) -> SpanGuard;
}
```

Solo: `StderrObservable` (logs to stderr + a local JSONL file). SaaS:
`OtelObservable` (traces to Tempo/Jaeger, metrics to Prometheus, logs to
Loki). Same trait, different impl, selected at the composition root.

### 7.2 Required metrics per service

Every service exposes `/metrics` (Prometheus format):

```
# request metrics
http_requests_total{service,endpoint,status} N
http_request_duration_seconds{service,endpoint} histogram

# dependency metrics
dependency_up{service,dependency} 1|0
dependency_request_duration_seconds{service,dependency} histogram
circuit_breaker_state{service,dependency} closed|open|half_open

# queue metrics (if the service uses queues)
queue_depth{queue,tenant} N
queue_depth_limit{queue} N
queue_push_total{queue} N
queue_pop_total{queue} N
queue_429_total{queue} N
queue_dlq_depth{queue} N

# db metrics (if the service uses SQL)
db_connection_pool_size{service} N
db_connection_pool_in_use{service} N
db_query_duration_seconds{service,query} histogram
db_migration_version{service} N

# health
yadgar_ready{service} 1|0
yadgar_uptime_seconds{service} N
```

---

## Principle 8: Security is a boundary, not a layer

> Authn/Authz happen at the gateway. Encryption happens at the storage
> boundary. Tenant isolation happens at the engine (Postgres RLS). Security
> is enforced at the edges, not sprinkled through the services.

### 8.1 The security perimeter

```
client → gateway
  → Authn.verify(token) → Identity { tenant_id, user_id, roles }
  → Authz.authorize(identity, action, resource) → allow/deny
  → [if allow] forward to downstream service
      with X-Tenant-Id + X-User-Id + X-Roles headers (from the attested JWT)
  → downstream service trusts the headers (they came from the gateway, which
    attested them via IAM — not from the client)

write → vault.encrypt(content, tenant_id) → encrypted_payload
      → graph.insert(tenant_id, encrypted_payload)
        → Postgres RLS: USING (tenant_id = current_setting('app.tenant_id'))
          enforces that the row belongs to the calling tenant, even if the
          service code forgets to filter
```

### 8.2 Defense in depth

1. **Gateway** — authn (verify token) + authz (check permission). First line.
2. **Service** — authz (check permission for the specific action, e.g. "can
   this user delete an ADR?"). Second line.
3. **Engine** — Postgres RLS (tenant_id filter on every query, enforced by
   the engine). Third line — catches a service that forgets to filter.

A tenant data leak requires ALL THREE to fail. No single point of failure.

### 8.3 Encryption is separate from auth

`yadgar-vault` does encryption, not auth. `yadgar-iam` does auth, not
encryption. Different change cadence (auth changes often, crypto rarely) +
different blast radius (a vault bug exposes all data; an IAM bug exposes
access). Keep them separate.

---

## How the principles relate

```
Principle 1 (left-shift to composition root)
  └─ enables → Principle 2 (loose coupling: services don't import each other's SDKs,
      so they don't fail to compile when a backing store changes)
  └─ enables → Principle 3 (db-per-service: each service owns its DB through a trait,
      swapping one DB doesn't touch the others)
  └─ enables → Principle 4 (backpressure: the Queue trait makes the queue swappable)
  └─ enables → Principle 7 (observability: the Observable trait makes the
      telemetry backend swappable)

Principle 2 (loose coupling)
  └─ requires → Principle 1 (traits, so services don't depend on specific impls)
  └─ requires → Principle 8 (security at the boundary, so services don't each
      implement auth)

Principle 5 (thin client)
  └─ depends on → Principle 6 (versioned API, so the client binary doesn't
      break on server upgrades)

Principle 8 (security as boundary)
  └─ depends on → Principle 1 (Authn/Authz traits, so the auth backend is swappable)
  └─ depends on → Principle 3 (RLS per-tenant, at the engine layer)
```

**Principle 1 is the spine.** Everything else is a consequence or a
requirement it enables. If you remember one rule, remember: **the service
logic is invariant, the differences are left-shifted to the composition
root, and no service crate imports a backing-store SDK.**
