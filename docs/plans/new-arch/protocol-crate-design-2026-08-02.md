# The Protocol Crate — Full Design Plan (2026-08-02)

**Status:** DRAFT — full design plan for the very first deliverable of the
SaaS rewrite. The protocol crate is the keystone; every service depends on
it, and if it can't express the contracts cleanly, the service boundaries
are wrong.
**Branch:** `docs/move-new-arch-plans-to-subdir` (doc-only, no-release)
**Governs:** `docs/plans/new-arch/saas-architecture-principles-2026-08-02.md`
Principle 1 (the spine: differences left-shifted to the composition root).
**Amends:** `saas-architecture-principles-2026-08-02.md` Principle 7 —
the `Observable` trait is split into `Logger` + `Tracer` + `Metrics` (three
separate traits, three swap points). The principles doc's `Observable`
monolithic trait is superseded by this split.

---

## TL;DR

The `yadgar-protocol` crate is the single source of truth for every wire
type, every service trait, and every error code in the SaaS rewrite. It
contains **15 traits** (Queue, Cache, RelationalStore, GraphStore, Embedder,
Reranker, Authn, Authz, Encryptor, Meter, ObjectStore, Scheduler, Notifier,
Logger, Tracer, Metrics) + **the request/response types for every inter-service call** + **the
domain models** (Memory, Wiki, ADR, Task, AgentPrompt, Checkpoint, etc.) +
**the error envelope** + **the health/ready contracts** + **the versioning
scheme**.

**No service crate may import a backing-store SDK** — enforced by a
pre-commit lint. Services depend on `yadgar-protocol` (the traits) and
receive impls at the composition root. Swapping a backing store changes one
line at the root, zero lines in the service.

The current Python codebase has three structural defects this crate fixes:
(1) `StorageProtocol` has zero consumers and is read-only — a speculative
protocol that went unused; (2) the "API" is 79 MCP tool functions with
inconsistent signatures, no versioning, and `dict` return types — no
contract at all; (3) `_shared` accreted 31k LOC because Python has no
enforced boundary, so "shared" became a dumping ground. The protocol crate
replaces all three with one compiler-enforced contract.

**Effort: ~2 weeks.** The output: a crate that compiles, with every
inter-service message type defined and versioned. The cheapest validation
of the whole architecture — if the protocol crate can't express the
contracts cleanly, the boundaries are wrong and you find out before writing
any service code.

---

## 1. What the current system has (and what's wrong with it)

### 1.1 `StorageProtocol` — a speculative protocol with zero consumers

`yadgar/_shared/contracts/protocols.py:167-218` declares a 15-method
read-side Protocol. Its docstring claims the retrieval pipeline depends on
it. **Nothing imports it.** It is also read-only (no write methods), so it
could not carry engine #2 even if wired. Its siblings `MLClientProtocol`
and `CacheProtocol` *are* genuinely wired at `lifecycle.init_engines` —
direct evidence that a speculative protocol in this position goes unused.

**Defect:** the protocol was designed top-down ("what should the seam
look like?") instead of extracted bottom-up ("what do the consumers
actually call?"). The 15 methods are a wish list, not the real surface.
The real surface is `StorageEngine`'s ~80 public methods, and the retrieval
pipeline calls maybe 20 of them — but not the 20 in this Protocol.

### 1.2 The "API" is 79 MCP tool functions with `dict` returns

The current MCP surface (`yadgar/core/server/tools/`) is ~79 tool functions
across 20 files. The largest: `wiki.py` has 23 tools, `admin_other.py` has
14, `blocks.py` has 8. Every tool returns `dict` — untyped, unschema'd,
versioned only by "hope the client doesn't break." There is no API version
field. There is no error envelope. There is no way to add a field without
potentially breaking every client.

**Defect:** the tools are the API, and the API has no contract. The
`recall()` tool has 10 parameters (`query, max_results, min_heat, profile,
directory, branch_hint, type, mode, tags, max_chars`) — three of which
are `str | None` with magic values (`"all"`, `"fast"`, `"landscape"`).
The `memorize()` tool has 10 parameters including `is_protected`,
`tier`, `valid_until`, `ttl_days`, `reason`, `branch_hint`, `wait` —
a union of persistence options, provenance, and transport concerns in one
signature. These are not protocols; they are accumulations.

### 1.3 `_shared` accreted 31k LOC because Python has no enforced boundary

`yadgar/_shared/` is 31,000 lines across `contracts/`, `config/`,
`storage/`, `observability/`, `security/`, `file_queue/`, `rules_engine/`,
`knowledge_graph/`, `embeddings/`, `enrichment/`, `metacognition/`,
`rate_limit/`, `restoration/`, `retrieval/`, `sensory_buffer/`,
`server_helpers/`, `thermodynamics/`, `wiki/`, `write_exec/`. It's the
"both layers need it" dumping ground, and because Python's imports are
untyped at the boundary, nothing stops it from growing.

**Defect:** the `_shared` package is the god-module anti-pattern. It holds
contracts (good), config (debatable), storage clients (should be backend),
observability (should be per-layer), security (should be per-layer), and
half the retrieval pipeline (should be backend). The 31k LOC is the
evidence: a shared layer should be ~3-5k LOC of types and contracts, not
31k.

### 1.4 The HTTP backend "API" is 7 unversioned POST endpoints

`embed_service` exposes `/embed`, `/rerank`, `/recall`, `/restore`,
`/consolidate`, `/admin`, `/viz`, `/read_query` — all POST, all
unversioned (no `/v1/` prefix), all with pydantic request models that
are `model_config = {"extra": "forbid"}` (so adding a field is a breaking
change). The `RecallRequest` has 11 fields; `AdminRequest` has `op: str`
+ `payload: dict` (a god endpoint).

**Defect:** the backend "API" is an internal implementation detail
masquerading as a protocol. It has no versioning, no error envelope, no
pagination, no idempotency. Adding a field to `RecallRequest` breaks the
core↔backend contract silently if someone forgets `extra: "forbid"`.

### 1.5 Auth is a single bearer token in env, not a protocol

`BearerAuthMiddleware` reads `YADGAR_MCP_AUTH_TOKEN` from env on every
request. One token, one user, no tenants, no roles, no API keys, no
sessions. `hmac.compare_digest` is the entire authz surface.

**Defect:** there is no `Identity` type, no `Tenant` type, no `Role` type.
The concept of "who is calling" doesn't exist in the protocol layer — it
exists only in the middleware's env-var check. Every downstream service
trusts the middleware blindly, with no attested identity propagated.

### 1.6 `Memory` has 30+ fields, half of which are "v3 frontier" experiments

`yadgar/_shared/contracts/models.py:50-93` — the `Memory` model has:
`content, embedding, tags, source_episode_id, directory_context, created_at,
last_accessed, heat, last_decay_at, is_stale, file_hash, surprise_score,
importance, emotional_valence, confidence, access_count, useful_count,
embedding_model, contextual_prefix, cluster_id, is_prospective,
trigger_condition, narrative_weight, compressed, plasticity, stability,
excitability, last_excitability_update, store_type, compression_level,
original_content, sr_x, sr_y, reconsolidation_count, last_reconsolidated,
provenance_agent, vector_clock, is_protected`. 37 fields.

**Defect:** the domain model accreted every experimental field from every
research train. A protocol's domain model should be the *stable* surface,
not the research frontier. The v3 fields (`sr_x`, `sr_y`, `plasticity`,
`excitability`) are frontier experiments that may be removed — they should
not be in the wire contract. The protocol crate must separate **stable
domain fields** (what every service needs) from **experimental fields**
(what the consolidation service might set but no other service reads).

---

## 2. What the protocol crate IS

```
crates/yadgar-protocol/
  src/
    lib.rs              — re-exports everything, feature-gated
    version.rs          — SchemaVersion, ApiVersion, version negotiation
    error.rs            — ErrorEnvelope, ErrorKind, Result<T>
    health.rs           — HealthStatus, ReadyStatus, DependencyStatus
    identity.rs         — Identity, Tenant, User, Role, ApiKey, Session
    auth.rs             — Authn trait, Authz trait, AuthzDecision
    crypto.rs           — Encryptor trait, EncryptedPayload, KeyId
    queue.rs            — Queue trait, WorkItem, QueueFull, QueueTimeout
    cache.rs            — Cache trait, ScopeVersion, CacheKey
    storage.rs          — GraphStore trait, RelationalStore trait
    embed.rs            — Embedder trait, Reranker trait, EmbedRequest, RerankRequest
    meter.rs            — Meter trait, UsageEvent, Quota, QuotaCheck
    scheduler.rs        — Scheduler trait, JobDef, JobRun, JobStatus
    object_store.rs     — ObjectStore trait, SnapshotRef, ObjectMeta
    notifier.rs         — Notifier trait, Alert, AlertLevel
    logger.rs           — Logger trait, LogLevel, LogFields
    tracer.rs           — Tracer trait, SpanGuard, SpanStatus
    metrics.rs          — Metrics trait, MetricTag
    domain/
      mod.rs
      memory.rs         — Memory, MemoryId, MemoryContent, MemoryMeta
      wiki.rs           — WikiPage, WikiPageId, WikiPageVersion, WikiCrossRef
      adr.rs            — Adr, AdrId, AdrStatus, AdrTier
      task.rs           — Task, TaskId, TaskStatus, TaskState
      agent_prompt.rs   — AgentPrompt, PromptKind, PromptVersion
      checkpoint.rs     — Checkpoint, CheckpointId, Epoch
      block.rs          — MemoryBlock, BlockName, BlockScope
      bookmark.rs       — Bookmark, BookmarkPosition
      config.rs         — ConfigKey, ConfigValue, ConfigScope
  Cargo.toml            — serde, serde_json, uuid, chrono, thiserror
                          NO backing-store SDKs (sqlx, valkey, candle, etc.)
```

### 2.1 The 13 service traits

Each trait is `#[async_trait]` (or Rust's native async trait in 2026+),
`Send + Sync`, with methods that return `Result<T, ErrorEnvelope>`.

```rust
// auth.rs
#[async_trait]
pub trait Authn: Send + Sync {
    /// Verify a token (API key or JWT) and return the attested identity.
    async fn authenticate(&self, token: &str) -> Result<Identity, AuthError>;
}

#[async_trait]
pub trait Authz: Send + Sync {
    /// Check whether an identity may perform an action on a resource.
    async fn authorize(&self, identity: &Identity, action: &str, resource: &str) -> Result<AuthzDecision, AuthError>;
}

// crypto.rs
#[async_trait]
pub trait Encryptor: Send + Sync {
    async fn encrypt(&self, plaintext: &[u8], tenant_id: &TenantId) -> Result<EncryptedPayload, CryptoError>;
    async fn decrypt(&self, payload: &EncryptedPayload) -> Result<Vec<u8>, CryptoError>;
    async fn rotate_key(&self, tenant_id: &TenantId) -> Result<KeyId, CryptoError>;
}

// queue.rs
#[async_trait]
pub trait Queue: Send + Sync {
    async fn push(&self, queue: &str, item: &WorkItem) -> Result<(), QueueFull>;
    async fn pop(&self, queue: &str, timeout: Duration) -> Result<WorkItem, QueueTimeout>;
    async fn depth(&self, queue: &str) -> Result<usize, ErrorEnvelope>;
    async fn dead_letter(&self, queue: &str, item: &WorkItem, reason: &str) -> Result<(), ErrorEnvelope>;
}

// cache.rs
#[async_trait]
pub trait Cache: Send + Sync {
    async fn get(&self, key: &CacheKey) -> Result<Option<Vec<u8>>, ErrorEnvelope>;
    async fn set(&self, key: &CacheKey, value: &[u8], ttl: Option<Duration>) -> Result<(), ErrorEnvelope>;
    async fn invalidate(&self, scope: &str, scope_id: &str) -> Result<(), ErrorEnvelope>;
    async fn bump_scope_version(&self, scope_kind: &str, scope_id: &str) -> Result<ScopeVersion, ErrorEnvelope>;
    async fn scope_version(&self, scope_kind: &str, scope_id: &str) -> Result<ScopeVersion, ErrorEnvelope>;
}

// storage.rs — the graph/memory/wiki store (Surreal today, pgvector tomorrow)
#[async_trait]
pub trait GraphStore: Send + Sync {
    // ── memory CRUD ──
    async fn insert_memory(&self, tenant: &TenantId, memory: &MemoryWrite) -> Result<MemoryId, StoreError>;
    async fn get_memory(&self, tenant: &TenantId, id: &MemoryId) -> Result<Option<Memory>, StoreError>;
    async fn get_memories_by_ids(&self, tenant: &TenantId, ids: &[MemoryId]) -> Result<Vec<Memory>, StoreError>;
    async fn update_memory(&self, tenant: &TenantId, id: &MemoryId, fields: &MemoryUpdate) -> Result<(), StoreError>;
    async fn delete_memory(&self, tenant: &TenantId, id: &MemoryId) -> Result<(), StoreError>;

    // ── search ──
    async fn search_fts(&self, tenant: &TenantId, query: &str, limit: usize) -> Result<Vec<MemoryHit>, StoreError>;
    async fn search_knn(&self, tenant: &TenantId, embedding: &[f32], limit: usize) -> Result<Vec<MemoryHit>, StoreError>;
    async fn search_combined(&self, tenant: &TenantId, query: &str, embedding: &[f32], limit: usize) -> Result<Vec<MemoryHit>, StoreError>;

    // ── wiki ──
    async fn insert_wiki_page(&self, tenant: &TenantId, page: &WikiPageWrite) -> Result<WikiPageId, StoreError>;
    async fn get_wiki_page(&self, tenant: &TenantId, slug: &str) -> Result<Option<WikiPage>, StoreError>;
    async fn update_wiki_page(&self, tenant: &TenantId, id: &WikiPageId, fields: &WikiPageUpdate) -> Result<(), StoreError>;
    async fn list_wiki_pages(&self, tenant: &TenantId, filter: &WikiListFilter) -> Result<Vec<WikiPageMeta>, StoreError>;

    // ── graph ──
    async fn get_entities(&self, tenant: &TenantId, filter: &EntityFilter) -> Result<Vec<Entity>, StoreError>;
    async fn get_relationships(&self, tenant: &TenantId, entity_id: &EntityId) -> Result<Vec<Relationship>, StoreError>;
}

// storage.rs — the relational store (SQLite solo, Postgres SaaS)
#[async_trait]
pub trait RelationalStore: Send + Sync {
    // Each service that owns a SQL DB implements this for its own tables.
    // The trait is generic enough for ADR/task/prompt/config/IAM/etc.
    // But: the specific table schemas are defined in the service's own crate,
    // not here. This trait is the *access pattern*, not the schema.
    async fn execute(&self, query: &str, params: &[JsonValue]) -> Result<ExecResult, StoreError>;
    async fn query(&self, sql: &str, params: &[JsonValue]) -> Result<Vec<JsonValue>, StoreError>;
    async fn transaction(&self, f: &mut dyn FnMut(&mut dyn RelationalStore) -> Result<(), StoreError>) -> Result<(), StoreError>;
}
// NOTE: RelationalStore is intentionally low-level. Each service defines
// its own typed API on top (AdrStore, TaskStore, etc.) that calls
// execute/query with typed params. The trait exists so the backing engine
// (SQLite vs Postgres) is swappable, not so the schema is shared.

// embed.rs
#[async_trait]
pub trait Embedder: Send + Sync {
    async fn embed(&self, texts: &[&str], mode: EmbedMode) -> Result<Vec<Embedding>, MLError>;
    async fn embed_query(&self, query: &str) -> Result<Embedding, MLError>;
}

#[async_trait]
pub trait Reranker: Send + Sync {
    async fn rerank(&self, query: &str, candidates: &[RerankCandidate]) -> Result<Vec<RerankScore>, MLError>;
}

// meter.rs
#[async_trait]
pub trait Meter: Send + Sync {
    async fn record(&self, event: &UsageEvent) -> Result<(), ErrorEnvelope>;
    async fn check_quota(&self, tenant: &TenantId, action: &str) -> Result<QuotaCheck, ErrorEnvelope>;
    async fn check_rate(&self, tenant: &TenantId, key: &str) -> Result<RateCheck, ErrorEnvelope>;
}

// scheduler.rs
#[async_trait]
pub trait Scheduler: Send + Sync {
    async fn register_job(&self, job: &JobDef) -> Result<JobId, ErrorEnvelope>;
    async fn trigger_job(&self, job_id: &JobId) -> Result<JobRunId, ErrorEnvelope>;
    async fn job_status(&self, run_id: &JobRunId) -> Result<JobStatus, ErrorEnvelope>;
    async fn acquire_lock(&self, lock_name: &str, ttl: Duration) -> Result<LockToken, ErrorEnvelope>;
    async fn release_lock(&self, token: &LockToken) -> Result<(), ErrorEnvelope>;
}

// object_store.rs
#[async_trait]
pub trait ObjectStore: Send + Sync {
    async fn put(&self, key: &str, data: &[u8]) -> Result<ObjectMeta, ErrorEnvelope>;
    async fn get(&self, key: &str) -> Result<Vec<u8>, ErrorEnvelope>;
    async fn list(&self, prefix: &str) -> Result<Vec<ObjectMeta>, ErrorEnvelope>;
    async fn delete(&self, key: &str) -> Result<(), ErrorEnvelope>;
}

// notifier.rs
#[async_trait]
pub trait Notifier: Send + Sync {
    async fn emit(&self, alert: &Alert) -> Result<(), ErrorEnvelope>;
}

// logger.rs — structured event logging (NOT tracing, NOT metrics)
#[async_trait]
pub trait Logger: Send + Sync {
    fn log(&self, level: LogLevel, msg: &str, fields: &LogFields);
    fn flush(&self);  // for async sinks (Loki, Elasticsearch)
}

pub enum LogLevel { Trace, Debug, Info, Warn, Error, Fatal }

pub struct LogFields {
    pub service: String,
    pub tenant_id: Option<TenantId>,
    pub request_id: Option<Uuid>,
    pub fields: Vec<(String, JsonValue)>,
}

// tracer.rs — distributed tracing (span trees, NOT flat logs)
pub trait Tracer: Send + Sync {
    fn start_span(&self, name: &str, parent: Option<&SpanId>) -> SpanGuard;
    fn finish_span(&self, guard: SpanGuard, status: SpanStatus);
}

pub struct SpanGuard {
    pub id: SpanId,
    pub parent: Option<SpanId>,
    pub started_at: DateTime<Utc>,
}

pub enum SpanStatus { Ok, Error(String) }

// metrics.rs — counters, histograms, gauges (NOT logs, NOT spans)
pub trait Metrics: Send + Sync {
    fn counter(&self, name: &str, value: f64, tags: &[(&str, &str)]);
    fn gauge(&self, name: &str, value: f64, tags: &[(&str, &str)]);
    fn histogram(&self, name: &str, value: f64, tags: &[(&str, &str)]);
}
```

### 2.2 The domain models (stable surface only)

The current `Memory` model has 37 fields. The protocol's `Memory` has the
**stable** fields every service needs. Frontier/experimental fields stay in
the consolidation service's own types, not in the wire contract.

```rust
// domain/memory.rs
pub type MemoryId = u64;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Memory {
    pub id: MemoryId,
    pub content: String,
    pub tags: Vec<String>,
    pub tenant_id: TenantId,
    pub directory_context: String,
    pub created_at: DateTime<Utc>,
    pub last_accessed: DateTime<Utc>,
    pub heat: f32,
    pub importance: f32,
    pub is_protected: bool,
    pub is_stale: bool,
    pub store_type: StoreType,          // episodic | semantic
    pub provenance_agent: String,
    pub embedding_model: Option<String>,
    pub branch: Option<String>,
    // schema_version: u16 — on every struct, for forward-compat
    #[serde(default)]
    pub schema_version: u16,
}

// What the write service receives (not all fields are settable)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryWrite {
    pub content: String,
    pub tags: Vec<String>,
    pub directory_context: String,
    pub is_protected: bool,
    pub provenance_agent: Option<String>,
    pub tier: Option<AnchorTier>,
    pub valid_until: Option<DateTime<Utc>>,
    pub ttl_days: Option<u32>,
    pub reason: Option<String>,
    pub branch_hint: Option<String>,
}

// What the recall service returns (search hit)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryHit {
    pub id: MemoryId,
    pub content: String,         // possibly truncated per max_chars
    pub tags: Vec<String>,
    pub heat: f32,
    pub score: f32,              // retrieval score
    pub source: SourceType,      // memory | wiki
    #[serde(default)]
    pub schema_version: u16,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub truncated: Option<TruncationInfo>,
}
```

**Experimental fields excluded from the wire contract** (live in the
consolidation service's internal types, not in `yadgar-protocol`):
`surprise_score, emotional_valence, plasticity, stability, excitability,
sr_x, sr_y, reconsolidation_count, compression_level, original_content,
cluster_id, contextual_prefix, narrative_weight, vector_clock`. These are
consolidation-internal state, not inter-service messages.

### 2.3 The error envelope

```rust
// error.rs
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ErrorEnvelope {
    pub error: ErrorKind,
    pub message: String,
    pub code: String,             // machine-readable: "STORE_NOT_FOUND", "QUEUE_FULL", etc.
    pub retryable: bool,          // can the client retry?
    pub retry_after_sec: Option<u32>,  // for 429s
    pub details: Option<JsonValue>,    // structured per-error context
    pub request_id: Option<Uuid>,      // for tracing
    pub schema_version: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ErrorKind {
    Auth { status: u16, reason: String },
    Authz { action: String, resource: String },
    NotFound { resource: String, id: String },
    Store { engine: String, reason: String },
    QueueFull { queue: String, depth: usize, limit: usize },
    QueueTimeout { queue: String },
    ML { model: String, reason: String },
    Crypto { reason: String },
    RateLimit { retry_after_sec: u32 },
    QuotaExceeded { quota: String, limit: u64, used: u64 },
    Conflict { reason: String },
    Validation { field: String, reason: String },
    Internal { reason: String },
    Unavailable { service: String, reason: String },
}

pub type Result<T> = std::result::Result<T, ErrorEnvelope>;
```

**Every service returns this.** The gateway maps it to HTTP status codes:
`Auth → 401, Authz → 403, NotFound → 404, QueueFull → 429, RateLimit → 429,
QuotaExceeded → 429, Conflict → 409, Validation → 400, Unavailable → 503,
Internal → 500`. No more `dict` returns with undocumented shapes.

### 2.4 The identity + tenant types

```rust
// identity.rs
pub type TenantId = Uuid;
pub type UserId = Uuid;
pub type ApiKeyId = Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Identity {
    pub tenant_id: TenantId,
    pub user_id: UserId,
    pub roles: Vec<Role>,
    pub scopes: Vec<String>,
    pub api_key_id: Option<ApiKeyId>,
    pub authenticated_at: DateTime<Utc>,
    pub expires_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Role {
    pub name: String,         // "owner", "admin", "writer", "reader", "billing"
    pub tenant_id: TenantId,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Tenant {
    pub id: TenantId,
    pub name: String,
    pub plan: Plan,            // free, pro, enterprise
    pub created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Plan { Free, Pro, Enterprise }
```

**Every inter-service call carries `Identity` in a header** (serialized as
JWT or a compact JSON). The gateway attests it via IAM; downstream services
trust it. This is the `tenant_id` that Postgres RLS uses.

### 2.5 The health/ready contracts

```rust
// health.rs
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HealthStatus {
    pub service: String,
    pub version: String,
    pub uptime_seconds: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReadyStatus {
    pub service: String,
    pub ready: bool,
    pub dependencies: Vec<DependencyStatus>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DependencyStatus {
    pub name: String,        // "surreal", "postgres", "valkey", "iam"
    pub reachable: bool,
    pub latency_ms: Option<u32>,
    pub last_checked: DateTime<Utc>,
}
```

`/healthz` returns `HealthStatus` (always 200 if alive).
`/readyz` returns `ReadyStatus` (200 if all deps reachable, 503 otherwise).

### 2.6 The versioning scheme

```rust
// version.rs
pub const PROTOCOL_VERSION: u16 = 1;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiVersion {
    pub major: u16,
    pub minor: u16,
}

// Every request can carry X-API-Version header; every response includes it.
// Breaking changes bump major; additive changes bump minor.
// The protocol crate's PROTOCOL_VERSION is the compile-time version.
```

Every serde struct has `#[serde(default)] pub schema_version: u16` — so
adding a field is a minor version bump (old clients ignore the new field,
new clients handle its absence). Removing or renaming a field is a major
version bump (requires a new `/v2/` endpoint or a migration).

---

## 3. What the protocol crate is NOT

1. **Not a service.** It's a library. No `main()`, no HTTP server, no
   process. It compiles into every service.
2. **Not an impl.** No `sqlx`, no `valkey`, no `candle`, no `aws-sdk`.
   Only `serde`, `uuid`, `chrono`, `thiserror`. The impls live in
   `yadgar-cache`, `yadgar-storage-postgres`, `yadgar-ml-candle`, etc.
3. **Not a schema definition.** The SQL table schemas are in each
   service's own crate (`migrations/adr/`, `migrations/tasks/`). The
   protocol defines the *wire types*, not the *storage schema*. A service
   maps between them.
4. **Not a config system.** Config knobs are in `yadgar_config` (a
   service). The protocol defines the `ConfigKey`/`ConfigValue` types, not
   the config storage.
5. **Not a research frontier.** Experimental fields (v3 cognitive-map
   coordinates, engram excitability) stay in the consolidation service's
   internal types. The protocol is the stable surface.

---

## 4. The request/response types for inter-service calls

Every inter-service HTTP call has a typed request + response in the protocol
crate. No more `dict` or `JsonValue` returns (except for genuinely dynamic
content like `db_inspect`).

```rust
// recall.rs (the inter-service call, not the MCP tool)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecallRequest {
    pub query: String,
    pub tenant_id: TenantId,
    pub directory: String,
    pub max_results: u16,         // default 5, max 50
    pub min_heat: f32,            // default 0.0
    pub source_type: SourceType,  // All, Memory, Wiki
    pub profile: RecallProfile,   // Fast, Balanced, Full
    pub tags: Option<Vec<String>>,
    pub max_chars: Option<u32>,
    pub deadline_ms: Option<u32>,
    pub branch: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecallResponse {
    pub results: Vec<MemoryHit>,
    pub total_found: usize,
    pub truncated: bool,
    pub profile_used: RecallProfile,
    pub duration_ms: u32,
}

// embed.rs
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EmbedRequest {
    pub texts: Vec<String>,
    pub mode: EmbedMode,  // Document, Query, Raw
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EmbedResponse {
    pub embeddings: Vec<Option<Embedding>>,
    pub model: String,
    pub dim: u16,
}

// write.rs
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WriteMemoryRequest {
    pub memory: MemoryWrite,
    pub tenant_id: TenantId,
    pub wait: bool,         // false = async (202), true = sync (200)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WriteMemoryResponse {
    pub memory_id: Option<MemoryId>,
    pub status: WriteStatus,   // Accepted, Committed, Queued, Rejected
    pub queue_id: Option<Uuid>,
}

// ADR, task, prompt, etc. — each has its own request/response types
```

**The MCP tool layer** (in `yadgar-gateway`) translates between the
client-facing MCP tool parameters and these typed requests. The 79 current
tools with `dict` returns become typed gateway functions that call the
typed inter-service APIs. The gateway is the only place that speaks MCP;
everything behind it speaks the protocol.

---

## 5. The enforcement mechanism

### 5.1 The no-SDK-import lint

`scripts/check_service_crate_deps.py` — a pre-commit hook that parses each
crate's `Cargo.toml` and fails if a denied dependency appears in a service
crate:

```python
# Denylist (service crates must NOT import these):
DENIED = {
    "sqlx", "valkey", "redis", "candle-core", "candle-nn",
    "candle-transformers", "candle-flash-attn",
    "aws-sdk-kms", "aws-sdk-s3", "surrealdb",
    "lapin", "rdkafka", "kafka",
}

# Allowlist (impl crates that MAY import these):
ALLOWED = {
    "yadgar-cache", "yadgar-storage-sqlite", "yadgar-storage-postgres",
    "yadgar-storage-surreal", "yadgar-ml-candle", "yadgar-vault-kms",
    "yadgar-vault-file", "yadgar-iam",  # IAM is both service + impl
}

# Service crates (must NOT import denied):
SERVICES = {
    "yadgar-gateway", "yadgar-recall", "yadgar-write", "yadgar-consolidation",
    "yadgar-control", "yadgar-viz", "yadgar-metering", "yadgar-scheduler",
    "yadgar-backup", "yadgar-cli", "yadgar-hooks",
}
```

A service crate that imports `sqlx` fails CI. The impl crates are
allowlisted. This is the compiler+CI enforcement of Principle 1.

### 5.2 The protocol version check

Every serde struct has `schema_version: u16`. The protocol crate exports
`PROTOCOL_VERSION`. A service that receives a request with a higher
`schema_version` than it supports returns `ErrorEnvelope { error:
ErrorKind::Validation { field: "schema_version", reason: "unsupported
version" } }`. This makes version mismatches detectable, not silent.

### 5.3 The contract test

Each impl crate ships a `contract_tests/` directory that tests the impl
against the trait's contract — e.g., `yadgar-storage-postgres` has tests
that verify `GraphStore::insert_memory → get_memory` round-trips. These
tests run against a real Postgres (in CI) and validate that the impl
satisfies the protocol. A new impl (e.g., `yadgar-storage-pgvector`) runs
the same contract tests — if they pass, the impl is protocol-compliant.

---

## 6. What this fixes vs the current system

| Current defect | Protocol crate fix |
|---|---|
| `StorageProtocol` has 0 consumers, is read-only, is a wishlist | `GraphStore` + `RelationalStore` are extracted from the real call surface; every service that needs storage uses them |
| 79 MCP tools with `dict` returns, no versioning, no error envelope | Typed `RecallRequest`/`RecallResponse` etc.; `ErrorEnvelope` on every call; `schema_version` on every struct |
| `_shared` is 31k LOC, no enforced boundary | `yadgar-protocol` is ~3-5k LOC of pure types + traits; the no-SDK lint prevents accretion |
| HTTP backend "API" is 7 unversioned POST endpoints | Every inter-service call has a typed request/response in the protocol; URI-versioned (`/v1/recall`) |
| Auth is one env-var bearer token, no Identity type | `Identity`, `Tenant`, `Role`, `Authn` trait, `Authz` trait — attested identity propagated to every service |
| `Memory` has 37 fields including v3 frontier experiments | Protocol `Memory` has 15 stable fields; experimental fields stay in consolidation's internal types |
| No health/ready distinction | `HealthStatus` (alive) + `ReadyStatus` (can serve) — the loose-coupling protocol |
| No queue/backpressure contract | `Queue` trait + `WorkItem` + `QueueFull` — the backpressure protocol |
| No cache contract (two in-process LRUs) | `Cache` trait + `ScopeVersion` — the version-in-key invalidation contract |
| No observability contract | `Logger` + `Tracer` + `Metrics` traits — three separate concerns, each independently swappable (solo: stderr; SaaS: Loki + Tempo + Prometheus) |

---

## 7. The module dependency graph

```
yadgar-protocol (the contract layer — no SDK imports)
    ↑
    ├── yadgar-gateway       (imports protocol only)
    ├── yadgar-recall        (imports protocol only)
    ├── yadgar-write         (imports protocol only)
    ├── yadgar-consolidation (imports protocol only)
    ├── yadgar-control       (imports protocol only)
    ├── yadgar-viz           (imports protocol only)
    ├── yadgar-metering      (imports protocol only)
    ├── yadgar-scheduler     (imports protocol only)
    ├── yadgar-backup        (imports protocol only)
    ├── yadgar-cli           (imports protocol only)
    ├── yadgar-hooks         (imports protocol only)
    │
    ├── yadgar-cache         (imports protocol + valkey)  ← impl crate
    ├── yadgar-storage-sqlite   (imports protocol + sqlx) ← impl crate
    ├── yadgar-storage-postgres (imports protocol + sqlx) ← impl crate
    ├── yadgar-storage-surreal  (imports protocol + surrealdb) ← impl crate
    ├── yadgar-ml-candle     (imports protocol + candle) ← impl crate
    ├── yadgar-vault-kms     (imports protocol + aws-sdk-kms) ← impl crate
    ├── yadgar-vault-file    (imports protocol + ring) ← impl crate
    └── yadgar-iam           (imports protocol + sqlx + argon2) ← both service + impl
```

**The protocol crate is the only crate that every other crate depends on.**
No crate depends on `yadgar-gateway` or `yadgar-recall` or any other
service. No service crate depends on any impl crate directly — the
composition root wires impls into services at boot.

---

## 8. Build order (2 weeks)

### Week 1: the trait definitions + domain models

| Day | Deliverable |
|---|---|
| 1-2 | `error.rs`, `version.rs`, `health.rs`, `identity.rs` — the foundation types every other module needs |
| 3-4 | `domain/` — Memory, Wiki, ADR, Task, AgentPrompt, Checkpoint, Block, Bookmark, Config. Stable fields only. `schema_version` on every struct. |
| 5 | `auth.rs` (Authn + Authz traits), `crypto.rs` (Encryptor trait) — the security contracts |

### Week 2: the service traits + the enforcement

| Day | Deliverable |
|---|---|
| 6-7 | `queue.rs`, `cache.rs`, `storage.rs` (GraphStore + RelationalStore) — the data-plane contracts |
| 8 | `embed.rs` (Embedder + Reranker), `meter.rs`, `scheduler.rs` — the remaining service traits |
| 9 | `object_store.rs`, `notifier.rs`, `logger.rs`, `tracer.rs`, `metrics.rs` — the operational contracts (logging, tracing, metrics as three separate traits). The inter-service request/response types (RecallRequest, EmbedRequest, WriteMemoryRequest, etc.) |
| 10 | `scripts/check_service_crate_deps.py` — the no-SDK lint. A `contract_tests/` skeleton. The crate compiles, all types serialize/deserialize correctly. |

**Deliverable:** `crates/yadgar-protocol/` compiles. Every trait is defined.
Every domain model has `schema_version`. The error envelope works. The
no-SDK lint passes on an empty workspace. **This is the gate for starting
any service implementation** — IAM (the first service) cannot start until
the protocol crate's `Authn`/`Authz`/`Identity` types are defined.

---

## 9. What comes after the protocol crate

```
1. yadgar-protocol    (this plan — 2 weeks)
2. yadgar-iam         (the first service — 3 weeks)
   → depends on: Authn, Authz, Identity, Tenant, Role from the protocol
   → implements: IamClient (the Authn+Authz impl for other services)
3. yadgar-vault       (2 weeks, parallel with IAM after Identity lands)
   → depends on: Encryptor, EncryptedPayload, KeyId from the protocol
4. yadgar-cache       (1 week, parallel)
   → depends on: Cache, Queue, ScopeVersion from the protocol
   → implements: ValkeyCache, ValkeyQueue, InProcessCache, InProcessQueue
5. yadgar-gateway     (3 weeks)
   → depends on: ALL request/response types, ErrorEnvelope, Identity
   → the first consumer of IAM
```

**The protocol crate is the keystone.** Everything builds on it. If it's
wrong, everything is wrong. If it's right, every service is a leaf node
that depends only on the protocol + its own impls.

---

## 10. Open questions

1. **Should `RelationalStore` be a trait or just per-service typed APIs?**
   The current design has a low-level `execute`/`query`/`transaction`
   trait that each service wraps with typed methods. Alternative: no
   shared trait — each service defines its own `AdrStore`, `TaskStore`,
   etc. as concrete structs with `sqlx` calls, and the "swappable" part is
   that `sqlx` supports both SQLite and Postgres. **Recommendation: keep
   the trait** — it's the mechanism that lets a service swap `sqlx` for a
   different driver (e.g., `diesel`) without changing the service code.
   The trait is thin; the typed APIs are thick.
2. **Should the protocol crate use `async_trait` or native async traits?**
   As of Rust 2024, async traits are stable but without `dyn` dispatch.
   `async_trait` provides `dyn` dispatch but with a heap allocation per
   call. **Recommendation: `async_trait` for now** (we need `dyn`
   dispatch for the composition-root injection), revisit when `dyn async
   trait` stabilizes.
3. **Should `GraphStore` and `RelationalStore` be one trait or two?**
   They're separate concerns (graph/embeddings vs relational tables) but
   some services (write) use both. **Recommendation: two traits** — a
   service that needs both takes `Arc<dyn GraphStore> + Arc<dyn
   RelationalStore>`. Two traits, two impls, two swap points.
4. **How big is the protocol crate?** The current `_shared` is 31k LOC.
   The protocol crate should be ~3-5k LOC (types + traits, no impls). If
   it exceeds 8k, it's accreting impl logic — split it.
5. **Should the MCP tool definitions live in the protocol crate?** No —
   MCP is a transport concern, not a protocol concern. The protocol crate
   defines the *types*; the gateway crate defines the *MCP tool wrappers*
   that translate between MCP parameters and protocol types. MCP could be
   replaced by another transport (gRPC, GraphQL) without touching the
   protocol.
6. **Should `Logger`, `Tracer`, and `Metrics` be one trait or three?**
   They were initially one `Observable` trait. Split into three because
   they have different backends (Loki vs Tempo vs Prometheus), different
   consumers (ops vs debug vs alerting), different formats (JSON line vs
   span tree vs Prometheus format), and different volumes (high vs medium
   vs low). A solo user who only wants logs shouldn't get the tracing/metrics
   impl, and a SaaS user who sends traces to Tempo but logs to Loki
   shouldn't have to configure one trait that handles both awkwardly.
   **Decision: three traits, three impls, three swap points.** Each service
   takes `Arc<dyn Logger> + Arc<dyn Tracer> + Arc<dyn Metrics>` at the
   composition root — mix and match per deployment tier. The `tracing` crate
   (Rust's standard) is used inside the `OtelTracer` impl, not as the
   public interface — services call `self.tracer.start_span("recall")`
   without knowing which impl.

---

## 11. The one-sentence summary

**The protocol crate is the compiler-enforced contract: 15 traits, ~20
domain models, one error envelope, one identity type, one versioning scheme
— no SDKs, no impls, no frontier fields — and every service depends on it
and nothing else, so swapping a backing store changes one line at the
composition root and zero lines in the service.**
