# Yadgar SaaS Rewrite — Skeleton Plan

**Status:** DRAFT — needs review. Not a decision. Not a commitment. A skeleton for
architectural discussion, iteration, and review against best practices before any
code moves.
**Date:** 2026-08-02
**Author:** Max (m-agahi) + AI architectural discussion
**Branch:** `docs/saas-rewrite-plan-2026-08-02` (doc-only PR — no code changes)
**Supersedes:** Nothing. **Does NOT supersede** ADR-0195/0196 (split-store) — those
remain live engineering work on master. This plan is a *parallel* exploration of
where the architecture could go, not a replacement for in-flight decisions.

---

## 1. Purpose and scope

This document captures a free-form architectural discussion (2026-08-02) about
re-architecting Yadgar from its current Python monolith-with-two-processes into
a loosely coupled microservice structure, replacing the bulk of the
implementation with Rust, and reshaping it as a true SaaS product. The
discussion intentionally did **not** rely on the current ADRs — it asked, "if we
started over today with what we've learned, what would the architecture look
like?"

**In scope:**
- Target service decomposition (the "what runs where" map)
- Language choices (where Rust, where Python stays, where Ettin lives)
- The SaaS spine: AAA, encryption, metering, cache, scheduling, backup
- Deployment tiers (solo / team / SaaS) and the solo-vs-distributed compile switch
- Client-side footprint (the "zero local install" goal)
- Migration order (the strangler path, not big-bang)
- Best-practice references and gaps flagged for review

**Out of scope (for this plan):**
- Implementation detail of any single service
- A binding commitment to ship any of this
- Cancellation of in-flight work (split-store, retrieval tuning, etc.)
- Billing logic, pricing model, go-to-market

**Reviewers needed before this becomes anything more than a sketch:**
- A second architect for the service decomposition (granularity, boundaries)
- A Rust/ML engineer for the Ettin-in-Rust path (candle vs Python sidecar)
- A security engineer for the IAM + Vault design (envelope encryption, KMS choice)
- A SaaS operator for the deployment tiers (k3d automation, Helm chart)
- A retrieval scientist for the retrieval pipeline port risk (the highest-risk piece)

---

## 2. What we have today (the starting line)

Current shape, measured from the code graph:

| Layer | LOC | Notes |
|---|---|---|
| `yadgar/core` | 46k | MCP server 17k, CLI 4.4k, install 4.3k, daemon 3.9k, vacuum 3.2k, hooks 2.9k |
| `yadgar/backend` | 27k | retrieval 6.2k, admin 2.9k, embed_service 2.6k, consolidation 2.4k, graph 1.5k, write 1.3k, cache 1.2k, drainer 1.2k, curation 1.1k, cls 1.0k, restoration 1.0k |
| `yadgar/_shared` | 31k | contracts + config + storage + observability — the coupling glue |
| **Total** | **~104k** Python |

A process boundary already exists: `core` ↔ `backend` over HTTP (`/recall`,
`/rerank`, `/admin/*`). The split is partially done.

**Scheduling today is smeared across four places:**
- `yadgar/core/scripts/nightly_cycle.py` (475 LOC, 7 hard-coded sequential steps)
- `deploy/systemd/*.timer` (3 timers)
- `ConsolidationScheduler` singleton in backend (6-hour sleep-cycle gate)
- `_maybe_auto_vacuum` threshold check in the core orchestrator

No single source of truth for "what runs when."

**Auth today:** single bearer token in env (`YADGAR_MCP_AUTH_TOKEN`), one
`hmac.compare_digest` check. "One operator" auth, not SaaS auth.

**Cache today:** two in-process Python LRUs (`core/cache.py`,
`backend/cache.py` with `ScopeVersions` version-in-key invalidation), named
instances: `ce`, `memory_doc`, `graph`, `engram_slot`, `db_size`. All lost on
restart, no cross-process sharing.

**Backup + vacuum today:** share the same DB-swap dance (export → strip → stop
daemons → mv DB → import) and the same snapshot dir + prune logic. Split
across `core/backup/` and `core/vacuum/` but one operational concern.

**Embed/rerank today:** Ettin-32m (`cross-encoder/ettin-reranker-32m-v1`),
32.8M params, loaded via `STCrossEncoder` (sentence-transformers). 6.3× faster
than the prior GTE-ModernBERT. T4 A/B benchmark: recall@5 0.944 vs 0.921.
Ettin is the tuned, fast reranker — must be preserved, not regressed to ONNX.
A Rust port path is `candle` (ModernBERT native) or keep a Python sidecar
initially. ONNX export would likely regress.

**Client-side today:** Python 3.14+ + pipx + venv + Docker + ~2GB model weights
+ SurrealDB + a running daemon + systemd units. ~2-4GB RAM resident. Heavy.

---

## 3. Target architecture — 13 services

The decomposition criterion is NOT "one service per current Python module"
(that gives 20 services with no independent reason to exist). The criterion
is: **does this have a different deploy cadence, scaling axis, failure mode, or
security boundary than its neighbor?** Applied to the current shape, that yields
13 services.

### 3.1 Data plane (hot path)

| # | Service | Language | Responsibility |
|---|---|---|---|
| 1 | `yadgar-gateway` | Rust/axum | MCP HTTP server, tool router (79 tools), auth enforcement point, tenant context injection |
| 2 | `yadgar-recall` | Rust | Retrieval pipeline: FTS+KNN+PPR+fusion+MMR (the IP) |
| 3 | `yadgar-ml` | Rust + Ettin | Embed + CE rerank + NLI. Isolated ML footprint; Ettin via candle (Rust-native) or Python sidecar initially |
| 4 | `yadgar-write` | Rust | Queue drainer + write-apply + similarity gate + curation |
| 5 | `yadgar-cache` | Valkey | ONE cache for everything (replaces both Python LRUs) + rate-limit counters + session tokens |

### 3.2 SaaS spine

| # | Service | Language | Responsibility |
|---|---|---|---|
| 6 | `yadgar-iam` | Rust | AAA: authn, authz, accounting. API keys, RBAC, per-tenant policy, session minting |
| 7 | `yadgar-vault` | Rust | Encryption-at-rest (envelope encryption), KMS integration, key rotation, secret-gate migration |
| 8 | `yadgar-metering` | Rust | Usage accounting, quotas, rate limits, billing events (records, does NOT bill) |

### 3.3 Operations plane

| # | Service | Language | Responsibility |
|---|---|---|---|
| 9 | `yadgar-scheduler` | Rust | THE clock: job registry, triggers, dispatch, distributed locks. Replaces the 4 scattered scheduling mechanisms |
| 10 | `yadgar-backup` | Rust | Snapshots, restore, retention, verification gate, cross-engine quiesce point |
| 11 | `yadgar-control` | Rust | Admin ops: vacuum, invariants, lifecycle, crash recovery |

### 3.4 Background (cold, dispatched BY the scheduler)

| # | Service | Language | Responsibility |
|---|---|---|---|
| 12 | `yadgar-consolidation` | Rust or Python initially | Nightly batch: decay/CLS/merge/causal/dream. Invoked by scheduler, not always-on |
| 13 | `yadgar-viz` | Rust | Galaxy layout + UI server (optional, off by default) |

### 3.5 Stores (third-party)

- `surrealdb` — graph/memory/wiki bodies + embeddings
- `mariadb` — relational set (tasks, ADR metadata, runtime_config, **+ users/api_keys/roles/quotas/snapshot_catalog/job_registry**)
- `valkey` — cache + rate-limit + sessions (shared state spine for multi-instance)
- object storage (S3/minio) — backups, export snapshots, model weights

### 3.6 Why each split earns its existence (not just "more services is better")

- **gateway ≠ recall**: gateway is stateless, restarts in 50ms, changes with
  MCP protocol. recall holds warm caches + KNN index, changes with retrieval
  heuristics. Different change velocity, different restart cost.
- **recall ≠ ml**: recall is pure compute (sub-100ms). ml holds 500MB+ model
  weights with 3-7s cold load. Folding them means every recall restart
  reloads models. **Highest-value split in the system.**
- **write ≠ recall**: writes contend (queue, similarity gate, transactions).
  Reads must never block on writes. CQRS.
- **consolidation ≠ everything**: batch job, nightly. Should not be
  always-on. Scheduler invokes it as a `CronJob`-style binary that runs + exits.
- **control ≠ gateway**: admin ops are privileged and rare. Must not share the
  hot request path's auth boundary.
- **viz ≠ everything**: UI most users never start. Don't pay its memory cost.
- **scheduler ≠ everything**: one clock, not four. No service holds its own
  timer/singleton. Scheduler decides *when*, workers do *what*.
- **backup ≠ vacuum**: they share the DB-swap dance today but backup owns the
  cross-engine quiesce point + restore verification gate — bigger than vacuum.
  Vacuum folds into `yadgar-control` as one operation among several.

---

## 4. The SaaS spine — design notes

### 4.1 `yadgar-cache` (Valkey)

Replaces the two Python LRUs. Solves three SaaS problems with one component:

1. **Cache** — one `Cache` trait impl backed by Valkey, shared across all
   services. The `ScopeVersions` invalidation mechanism (version-in-key) becomes
   a Valkey HASH `scope_versions:{kind}:{id}` → `version` that any service bumps
   and all services read. Cross-process invalidation for free.
2. **Rate limiting** — `yadgar-metering` uses Valkey `INCR` + `EXPIRE` for
   sliding-window and token-bucket counters. Atomic, shared across gateway
   replicas, survives restarts. Replaces the in-process
   `TokenBucketRateLimiter`.
3. **Session tokens / revocation** — short-lived session JWTs in Valkey with
   TTL; revocation = `DEL`. The env bearer token becomes a launch-time
   bootstrap token that calls `yadgar-iam` to mint a real session.

**Solo mode:** embedded Valkey (child process or `redb`/`sled` fallback) so
solo users don't run a separate Valkey container.

### 4.2 `yadgar-iam` — the real SaaS enabler

```
/v1/authenticate   — verify API key or JWT → {tenant_id, user_id, roles, scopes}
/v1/authorize      — (tenant_id, user_id, action, resource) → allow/deny
/v1/keys            — CRUD API keys (per-tenant, scoped, revocable, rotated)
/v1/roles           — RBAC role definitions + bindings
/v1/tenants         — tenant CRUD, isolation config
/v1/sessions        — mint/revoke session tokens (Valkey-backed)
```

**Gateway is a pure policy-enforcement point.** Every MCP tool call:

```
client → gateway → iam.authenticate(token) → {tenant, user, roles}
       → iam.authorize(tenant, user, "recall", scope) → allow/deny
       → [if allow] forward to yadgar-recall with tenant context injected
       → metering.record(tenant, "recall", latency, tokens) → async
```

- **Per-tenant data isolation is enforced at the storage layer**, not the
  gateway. Every Surreal/Maria query carries `tenant_id` from the IAM-attested
  context, injected by `yadgar-storage`'s trait impl. Not at the gateway (too
  easy to leak) — at the storage boundary.
- **RBAC roles**: `owner`, `admin`, `writer`, `reader`, `billing` — per-tenant
  bindings in MariaDB.
- **The current bearer token stays as a bootstrap/admin break-glass** —
  `YADGAR_BOOTSTRAP_TOKEN` for first-run, disabled after the first IAM user is
  created.

### 4.3 `yadgar-vault` — encryption is a separate concern from auth

```
/v1/encrypt         — encrypt plaintext with the tenant's DEK → ciphertext + DEK_id
/v1/decrypt         — decrypt ciphertext (authorized caller only)
/v1/keys/rotate      — rotate a tenant's DEK; old DEK kept for decrypt-only
/v1/keys/create      — provision a new DEK (wrapped by the tenant's KEK)
/v1/secret-scan      — the regex secret-gate, migrated here as a pre-write check
```

**Envelope encryption:** each tenant has a KEK held in a real KMS (AWS KMS /
GCP KMS / HashiCorp Vault / or for solo, a master key in a sealed file). DEKs
generated per-tenant, wrapped by the KEK, stored in MariaDB. `yadgar-write`
calls `vault.encrypt()` before storing; `yadgar-recall` calls `vault.decrypt()`
after reading. Services never see raw keys.

**What gets encrypted:** memory content fields flagged sensitive by the
secret-gate, wiki page bodies flagged confidential, ADR decision text,
runtime_config secret values. NOT embeddings (vectors, not meaningful to
decrypt) and NOT metadata (tenant_id, tags, heat — queryable).

**The current secret-gate regex patterns migrate into vault as
`/v1/secret-scan`** — a pre-write check that flags AWS keys, GitHub PATs, JWTs,
private keys BEFORE encryption.

**Pushback noted in discussion:** do NOT build `yadgar-vault` as a generic KMS.
Build it as a thin wrapper over a real KMS with a local-file fallback for solo.
Rolling your own key management is how people end up on Hacker News.

### 4.4 `yadgar-metering` — the thing solo yadgar doesn't have at all

```
/v1/record   — async: record a usage event (tenant, action, qty, ts)
/v1/quota    — check/increment quota (tenant, action) → allowed/over
/v1/rate     — rate limit check (tenant, key) → allow/deny (Valkey-backed)
/v1/report   — aggregate usage for billing (tenant, period) → JSON
/v1/alert    — emit quota-threshold events to a webhook
```

`TokenBucketRateLimiter` grown up: per-tenant per-action rate limits, quotas
per billing period, usage events exported to a billing system (Stripe / Lago /
whatever). Solo: metering runs but just logs, no billing integration. SaaS:
it's the meter you bill on.

**Pushback noted:** don't put billing logic in metering. It records events and
checks quotas. Billing is a downstream system that consumes the events. Keep
the metering service dumb — `record(event)` and `check(quota)`.

### 4.5 `yadgar-scheduler` — the one clock

```
/v1/jobs             — CRUD job definitions (id, cron, window, timeout, retry, command)
/v1/jobs/:id/run     — manual trigger (the consolidate_now MCP tool calls this)
/v1/jobs/:id/status  — last run, next run, state, history
/v1/locks            — distributed lock registry (Valkey-backed)
/v1/windows          — maintenance window definitions (per-tenant "night")
/v1/health           — leader? (Raft or Valkey SETNX leader election)
```

**Job graph** (replaces the scattered scheduling) — declared in a YAML loaded
at startup:

```yaml
jobs:
  - id: nightly-pre-backup
    trigger: cron("0 3 *")          # 03:00 in tenant's maintenance window
    window: maintenance
    timeout: 600s
    retry: { max: 1, backoff: 60s }
    run: { service: backup, action: snapshot, args: { type: pre } }

  - id: nightly-consolidation-light
    trigger: after(nightly-pre-backup, on: success)
    timeout: 1800s
    retry: { max: 1, backoff: 300s }
    run: { service: consolidation, action: run, args: { mode: light } }

  - id: nightly-vacuum-conditional
    trigger: after(nightly-consolidation-light, on: success)
    condition: "metrics.db_size_bytes > thresholds.vacuum_trigger"
    timeout: 3600s
    retry: { max: 0 }               # vacuum is non-retryable
    run: { service: control, action: vacuum }

  - id: nightly-post-backup
    trigger: after(nightly-consolidation-light, on: success)
    timeout: 600s
    run: { service: backup, action: snapshot, args: { type: post } }

  - id: nightly-prune
    trigger: after(nightly-post-backup, on: success)
    timeout: 120s
    run: { service: backup, action: prune, args: { keep_daily: 7, keep_weekly: 4 } }

  - id: weekly-consolidation-full
    trigger: cron("0 3 * * 0")
    window: maintenance
    timeout: 7200s
    run: { service: consolidation, action: run, args: { mode: full } }

  - id: reembed-stale
    trigger: cron("0 5 * * 0")
    timeout: 3600s
    run: { service: recall, action: reembed-stale }

  - id: reap-stale-tests
    trigger: every("10m")
    timeout: 60s
    run: { service: control, action: reap-tests }

  - id: graph-layout-precompute
    trigger: event("write-batch-complete")
    debounce: 300s
    run: { service: viz, action: precompute }
```

**Design rules:**
1. Scheduler is a service, not a library. No service holds its own
   timer/singleton. Delete `ConsolidationScheduler`. Everything calls
   `/v1/jobs/:id/run`.
2. Distributed locks via Valkey. Solo: no lock needed (single instance), same
   code path.
3. Maintenance windows are per-tenant. SaaS = different tenants have different
   "night."
4. Leader election for the scheduler itself (3 replicas → 1 leader). Solo:
   always leader.
5. Job dependencies, not shell scripts. The nightly cycle's 7 steps become a
   DAG. No more `_run_step` wrappers with `try/finally`.
6. **Scheduler does NOT run the job itself.** It dispatches via HTTP. This is
   the separation: scheduler decides *when*, workers do *what*.
7. **Don't grow it into a workflow engine.** Linear chain + one conditional is
   the ceiling. If you need more, use Temporal/Inngest as a separate component.

### 4.6 `yadgar-backup` — snapshots, restore, verification

```
/v1/snapshot         — create (full or incremental)
/v1/snapshot/:id     — metadata + integrity hash
/v1/restore          — restore from snapshot (with verification gate)
/v1/verify/:id       — verify integrity (row counts, checksums, referential)
/v1/prune            — retention (keep N, age-based, or GFS)
/v1/catalog          — list, filter by date/type/tenant
/v1/export           — ship to object storage (S3/minio)
/v1/import           — pull from object storage for restore
```

**Cross-engine quiesce point** (the open question from the current
split-store work) becomes the backup service's responsibility:

```
backup.snapshot() sequence:
  1. acquire write-quiesce lock (via scheduler /v1/locks) → yadgar-write pauses
  2. surreal snapshot (SurrealDB backup API or LSN-tagged export)
  3. mariadb snapshot (mariadb-backup --backup or mysqldump --single-transaction)
  4. tag both with the same quiesce_id (a UUID)
  5. release write-quiesce lock
  6. verify both snapshots (row counts, checksums)
  7. register in catalog (MariaDB)
  8. async: ship to object storage
```

**Restore verification gate** — the thing that prevents a repeat of the
2026-06-16 incident (partial restore of 1484/3622 rows passed a `>=` check
and destroyed 3,622 memories):

```
restore sequence:
  1. pull snapshot pair from object storage
  2. verify surreal snapshot: row counts per table, embedding count == memory count
  3. verify mariadb snapshot: row counts, FK integrity
  4. verify cross-engine: every mariadb row referencing a memory_id has a
     matching surreal memory (within this quiesce_id)
  5. IF ANY CHECK FAILS → refuse to restore, alert, keep current DB intact
  6. IF ALL PASS → stop services, swap both DBs, start, run check-invariants
  7. record restore event in metering + backup catalog
```

**Backup strategies:** full (current), incremental (SurrealKV LSN + Maria
incremental, GFS rotation), continuous (Surreal CDC + Maria binlog → object
storage, for PITR — SaaS premium tier).

**Solo:** full snapshots to local dir, GFS (7 daily + 4 weekly), no object
storage. **SaaS:** per-tenant snapshot isolation, S3 + SSE-KMS, PITR for
premium.

---

## 5. Repo structure — one Cargo workspace, monorepo

```
yadgar/
  crates/
    yadgar-protocol/      — wire contracts (serde, versioned). Replaces 31k LOC of _shared
    yadgar-gateway/       — MCP server, tool handlers, auth enforcement
    yadgar-recall/        — retrieval pipeline (tantivy+petgraph+ort or candle)
    yadgar-ml/            — embed + rerank (Rust shell + Ettin sidecar → candle later)
    yadgar-write/         — queue drainer, write-apply, curation
    yadgar-consolidation/ — nightly batch
    yadgar-control/       — admin, backup, vacuum, lifecycle
    yadgar-iam/           — AAA, RBAC, API keys
    yadgar-vault/         — encryption, KMS, secret-gate
    yadgar-metering/      — usage, quotas, rate limits
    yadgar-scheduler/     — job registry, triggers, dispatch, locks
    yadgar-backup/        — snapshots, restore, verification
    yadgar-viz/           — galaxy + UI
    yadgar-storage/       — Storage trait + Surreal impl + Maria impl + tenant isolation
    yadgar-cli/           — single binary (install, daemon, config, seed, code-graph)
    yadgar-hooks/         — hook entry points (called by Claude Code / opencode / etc)
  deploy/
    compose/              — docker-compose for the 13-service split
    helm/                 — chart for k3s/k8s
    install.sh            — solo quickstart
  models/                 — Ettin + embed weights (downloaded by install.sh)
  scheduler-jobs.yaml     — the single source of truth for scheduled jobs
```

**`yadgar-protocol` is the keystone.** It's what makes the microservice split
safe. Today `_shared` is 31k LOC because Python has no enforced boundary, so
"shared" accretes config + storage + observability + contracts all in one. In
Rust, the protocol crate is *only* wire types — serde structs, request/response
envelopes, error codes. Config and observability become per-service concerns.

---

## 6. Deployment tiers — same code, three topologies

The 13 crates have trait-based interfaces (`Authn`, `Authz`, `Cache`, `Encrypt`,
`Meter`, `Storage`, `Retriever`, `Embedder`). Each trait has two impls:
in-process (solo) and HTTP-client (distributed). Compile-time feature flags +
runtime mode switch select the topology.

### 6.1 Solo (default, `--features solo`)

```
curl -sSf https://yadgar.ai/install.sh | sh
yadgar daemon start
  → 1 process, all 13 services in-process via tokio::sync::mpsc channels
  → embedded SurrealKV + SQLite (relational set) + embedded Valkey
  → local file KEK (sealed), no KMS
  → metering logs to local SQLite, no billing
  → ~130MB RSS total
```

### 6.2 Team / self-hosted (`--features distributed`)

```
docker-compose up
  → 13 containers on one host, ~1-2GB total
  → one Valkey, one Maria, one Surreal, 10 Rust services
  → per-user API keys via IAM
  → local FS backups, no object storage
```

### 6.3 SaaS (`--features distributed` + `YADGAR_MODE=saas`)

```
helm install yadgar ./deploy/helm   (or k3d for dev)
  → 13 services on k8s
  → gateway scales horizontally (stateless + Valkey sessions)
  → recall scales horizontally (stateless + Surreal + Valkey cache)
  → ml scales horizontally (one per GPU, sticky routing by model)
  → write is singleton per-tenant or sharded by tenant_id (write contention)
  → consolidation = CronJob
  → IAM/Vault/Metering/Scheduler = 3 replicas each, stateless, Maria+Valkey
  → object storage = S3, KMS = AWS KMS, secrets = Vault or AWS Secrets Manager
```

**k3d automation:** a `yadgar init` CLI subcommand that runs `k3d cluster create`
+ `helm install` + `yadgar-iam bootstrap --create-admin` in one shot. ~100
lines of Rust. This is the path for *distributing* yadgar to operators who want
to run their own SaaS. It is NOT the solo path — solo never touches k8s.

---

## 7. Client-side footprint — the zero-install goal

All 9 supported clients (Claude Code, opencode, cursor, cline, windsurf, kiro,
amp, gemini, codex) support HTTP MCP transport. The full tool surface (79
tools) is remote-capable with one config entry. No Python, no binary needed for
MCP.

**Claude Code hooks are the constraint:** they must be local executables
(Claude Code runs them as subprocesses, cannot be HTTP callbacks). Solution:
one 2MB static Rust binary (`yadgar-hook`) that proxies stdin → remote POST →
stdout. All 6 hook entries point to it with different subcommands.

**Client-side footprint after rewrite:**

```
~/.yadgar/
  bin/yadgar-hook          — 2MB static binary (the hook proxy)
  config.toml              — { remote_url, api_key, tenant_id }
~/.claude.json             — MCP server registration (1 line: remote URL + bearer)
~/.claude/settings.json    — hook entries (6 lines pointing to yadgar-hook)
CLAUDE.md / AGENTS.md      — the rules file
```

**Install:**

```bash
curl -sSf https://yadgar.ai/install.sh | sh
```

Downloads the 2MB binary, asks for API key, detects installed AI clients,
writes MCP registration + hooks + rules file. No daemon, no Docker, no Python,
no model weights. Zero resident memory (the binary only runs on hook events,
exits between).

**What dies client-side:** Python 3.14+, pipx, venv, Docker, ~2GB model
weights, SurrealDB, running daemon, systemd units, ~2-4GB RAM. All replaced by
one 2MB binary + two config files.

**Latency analysis for remote hooks:**

| Hook | Frequency | Budget | Remote OK? |
|---|---|---|---|
| `UserPromptSubmit` (auto-recall) | every turn | <500ms | yes — recall is remote's job, ~50-100ms |
| `PostToolUse` (auto-capture) | every tool call | async | yes — fire-and-forget POST |
| `PreToolUse` (router-guard) | every tool call | <100ms | **borderline** — cache guard policy locally at session start, evaluate in-process |
| `SessionStart` (context) | once per session | <2s | yes — user waits anyway |
| `PreCompact` (drain) | rare | <5s | yes |
| `Stop` (checkpoint) | end of turn | async | yes |

`PreToolUse` is the only latency-sensitive hook; mitigated by downloading the
guard policy at session start and evaluating in-process (read-only, zero
network per call).

**Design rule:** `yadgar-hook` must stay ~100 lines: parse stdin, HTTP POST,
write stdout, exit. Don't make it smart. The one exception is the PreToolUse
guard policy cache. If offline resilience is needed, write a local JSON spool
to `~/.yadgar/pending/` flushed on next successful session-start. That's the
maximum local complexity — a spool directory, not a local daemon.

---

## 8. API versioning

Every inter-service call: `/v1/recall`, `/v1/embed`, `/v1/rerank`, `/v1/write`,
etc. Version in the path + `X-API-Version` header. The protocol crate carries
`#[derive(Serialize, Deserialize)]` structs with a `schema_version: u16` field
for forward-compat.

Because it's a monorepo, you do *atomic* upgrades — bump protocol, update all
13 services in one PR. Versioning matters for:
- **External MCP API** (client-facing) — stable across releases, deprecation
  windows
- **Rolling upgrades** if you run mixed versions (team/SaaS)
- **The solo binary** — doesn't use HTTP at all (in-process channels), so
  versioning is compile-time

**External API best practices to follow (from Azure API design guide):**
- URI versioning (`/v1/`, `/v2/`) for client-facing — cache-friendly, simple
- Media type versioning (`Accept: application/vnd.yadgar.v1+json`) for
  HATEOAS-friendly internal APIs
- 202 Accepted + Location header for long-running operations (consolidation,
  vacuum, backup)
- Pagination: `limit` + `offset` with a `max-limit` ceiling (DoS guard)
- OpenAPI spec generated from the protocol crate — contract-first

---

## 9. The Ettin question — don't break what's fast

Ettin-32m is the tuned, fast reranker (6.3× GTE, recall@5 0.944 on T4 A/B).
ONNX export would likely regress. Three options, ranked:

1. **Keep Ettin as a Python sidecar** (pragmatic, ship fast): `yadgar-ml` is a
   thin Rust HTTP wrapper calling a small Python process running
   sentence-transformers + Ettin. You get the Rust service boundary + the tuned
   Ettin speed. Cost: one Python process, ~300MB RSS. **Right first move.**
2. **Port Ettin to `candle`** (Rust-native, end state): candle is HuggingFace's
   Rust ML framework, loads ModernBERT weights natively. Ettin-32m is a
   ModernBERT cross-encoder. Gives you a static binary with no Python. Must
   verify recall@5 parity on the golden set before flipping. ~2-4 weeks + a
   gated A/B.
3. **ONNX export + `ort`** — rejected. Ettin native is already 6.3× GTE; ONNX
   export of a ModernBERT can regress on the specific fused attention kernels.

**Recommendation:** start with option 1, end with option 2. The service
boundary is the valuable thing; the language inside `yadgar-ml` can migrate
later.

---

## 10. What stays in Python (honest)

- **Consolidation heuristics** — until frozen and you've stopped tuning. Batch,
  not latency-critical. Port last.
- **The Ettin sidecar** (option 1) — until candle port is verified.
- **CI invariant scripts** (`scripts/check_*.py`) — run in CI, not in the
  product.
- **The nix module** — distribution, not product.
- **Model fine-tuning** (if any) — ML ecosystem.

Everything else is a Rust crate.

---

## 11. Migration order — the strangler path, not big-bang

| Step | Crate | Effort | Risk | Gate |
|---|---|---|---|---|
| 1 | `yadgar-protocol` | 2 weeks | low | wire contracts compile |
| 2 | `yadgar-cli` + `yadgar-iam` (in-process, single-user bootstrap) | 3 weeks | low | auth foundation works |
| 3 | `yadgar-cache` (Valkey trait + embedded fallback) | 2 weeks | low | replaces both Python LRUs |
| 4 | `yadgar-gateway` calling `yadgar-iam` | 3 weeks | medium | Rust gateway proxying to Python backend (hybrid) |
| 5 | `yadgar-vault` (in-process AES-GCM + file KEK, KMS trait) | 2 weeks | medium | encryption-at-rest on new writes |
| 6 | `yadgar-metering` (in-process, Valkey-backed) | 2 weeks | low | rate limits survive restarts |
| 7 | `yadgar-scheduler` (in-process tokio task, job YAML) | 2 weeks | low | replaces 4 scheduling mechanisms |
| 8 | `yadgar-ml` with Ettin Python sidecar | 2-3 weeks | medium | recall@5 parity on golden set |
| 9 | `yadgar-recall` — the IP | **2-3 months** | **high** | `make eval` + `make longmemeval` recall@k ≥ baseline on every PR |
| 10 | `yadgar-write` calling `yadgar-vault.encrypt` | 3-4 weeks | medium | write queue + similarity gate + encryption |
| 11 | `yadgar-storage` — split-store + tenant isolation | 3-4 weeks | **high** | property-based cross-read tests on every PR |
| 12 | `yadgar-backup` | 3 weeks | medium | cross-engine quiesce + verification gate |
| 13 | `yadgar-consolidation` — port last | 1-2 months | low urgency | batch, invoked by scheduler |
| 14 | `yadgar-control` (admin + vacuum + crash recovery) | 2 weeks | low | — |
| 15 | `yadgar-viz` | 1 week | low | — |

**~12-16 months total.** At step 4 you have a Rust gateway with real auth
running against the Python backend. At step 7 the SaaS spine (IAM + cache +
vault + metering + scheduler) is in place in solo mode. At step 8 the retrieval
port starts, behind the eval harness. The SaaS-ready architecture exists before
the retrieval port is complete — you can offer SaaS while the retrieval port
matures, running Rust gateway + spine against the Python recall service.

---

## 12. Highest-risk work — flag for deep review

### 12.1 Multi-tenant isolation at the storage layer

Every SurrealQL query, every Maria query, every Valkey key must carry
`tenant_id` and every storage trait impl must enforce it. One missed query
leaks tenant A's memories to tenant B. The current codebase has **zero tenant
awareness** — adding it is a storage-layer rewrite, not a gateway concern.

**`yadgar-storage`'s tenant isolation is the highest-risk, highest-scrutiny
part of this whole plan.** Mandatory: property-based tests that try to
cross-read and assert they get nothing, on every PR.

### 12.2 Retrieval pipeline port

6.2k LOC of tuned heuristics with a LongMemEval baseline (recall@5 0.87, MRR
0.93). Porting to Rust means re-tuning against that baseline. Not a
translation — a re-verification project. If numbers drop, weeks spent finding
which heuristic didn't translate. **The eval harness is the safety net — don't
start the retrieval port until `make eval` + `make longmemeval` are green and
trusted.**

### 12.3 Ettin-in-Rust (candle port)

Must prove recall@5 parity before flipping. Ettin is the tuned asset; a candle
port that regresses is worse than the Python sidecar.

### 12.4 Cross-engine backup consistency

The quiesce point (Surreal snapshot at 03:00 + Maria snapshot at 03:05) can
restore rows referencing memories that don't exist. The verification gate is
the defense — but it's new code, untested at scale, and a partial restore that
passes a weak check destroyed 3,622 memories in the 2026-06-16 incident. This
needs a chaos-test: deliberately corrupt a snapshot pair and verify the gate
refuses it.

---

## 13. Best-practice references (research conducted 2026-08-02)

### 13.1 Twelve-Factor App

The 13 services should be twelve-factor compliant:
- **I Codebase:** one repo, many deploys (solo/team/SaaS from same source) ✓
- **II Dependencies:** explicit (Cargo.lock per crate) ✓
- **III Config:** in env, not in code — each service reads its config from env
  + a config service (not hardcoded)
- **IV Backing services:** Surreal/Maria/Valkey/S3 as attached resources,
  swappable (embedded for solo, external for SaaS) ✓
- **V Build/release/run:** CI builds, Helm chart releases, runtime deploys ✓
- **VI Processes:** stateless (state in stores, not in process memory) ✓
- **VII Port binding:** each service self-contains its HTTP server ✓
- **VIII Concurrency:** scale out via processes (k8s replicas), not threads ✓
- **IX Disposability:** fast startup + graceful shutdown (Rust helps here) ✓
- **X Dev/prod parity:** solo/team/SaaS run the same crates, different
  topology flags ✓
- **XI Logs:** event streams to stdout, aggregated by the platform (not files) ✓
- **XII Admin processes:** one-off tasks via `yadgar-cli` (vacuum, seed,
  bootstrap) ✓

### 13.2 Microservices.io patterns (Chris Richardson)

Patterns this plan follows:
- **API Gateway** — `yadgar-gateway` is the single entry point, handles auth,
  fans out to services. [microservices.io/patterns/apigateway.html]
- **Database per Service** — partially: stores are shared (Surreal/Maria) but
  each service owns its schema/tables. `yadgar-storage` enforces the boundary.
  Full database-per-service is overkill for a 13-service system on one host.
- **CQRS** — `yadgar-write` (command) ≠ `yadgar-recall` (query). Different
  services, different stores-connection-pools.
- **Saga** — the nightly cycle's multi-step flow (pre-backup → consolidation →
  vacuum → post-backup → prune) is a saga. If a step fails, the scheduler
  records the failure and either retries or skips downstream — no distributed
  transaction needed because each step is idempotent (snapshots are tagged,
  consolidation is re-runnable, vacuum is idempotent given the snapshot).
- **Circuit Breaker** — gateway → service calls need circuit breakers
  (tower::limit + tower::timeout + a circuit-breaker middleware). If
  `yadgar-ml` is down, gateway degrades to no-rerank recall, not a 500.
- **Access Token** — gateway receives the client's API key, calls
  `yadgar-iam.authenticate()`, gets a short-lived JWT, forwards it to
  downstream services with `X-Tenant-Id` + `X-User-Id` headers.
- **Observability patterns:** distributed tracing (OpenTelemetry),
  log aggregation (stdout → platform), health check API (`/health` per
  service), audit logging (in `yadgar-metering`), exception tracking (Sentry
  or equivalent).

Patterns this plan deliberately does NOT follow:
- **Client-side discovery** — overkill; use server-side discovery (k8s
  Services) or localhost (solo).
- **Shared database anti-pattern** — mitigated: stores are shared at the
  process level but each service owns its tables/schemas. `yadgar-storage`
  trait is the boundary, not raw SQL.

### 13.3 Azure multitenancy guidance

Tenancy model: **fully multitenant deployments** for the SaaS tier (shared
infra, `tenant_id` in every row), with **vertically partitioned** option for
premium tenants (dedicated DB or dedicated deployment). Solo/team = effectively
single-tenant.

Key takeaways from the Azure guide applied here:
- Isolation is a spectrum, not binary — data isolation via `tenant_id` is the
  default; dedicated deployments are the premium tier.
- **Test the isolation model** — property-based cross-read tests on every PR
  (echoed in §12.1).
- **Noisy neighbor** — mitigated by per-tenant rate limits (`yadgar-metering`)
  + per-tenant quotas. A tenant hammering recall can't starve others.
- **Tenant mapping** — the gateway extracts `tenant_id` from the JWT (not from
  the URL or a header the client controls). The attestation chain is:
  client API key → `yadgar-iam.authenticate()` → JWT with `tenant_id` claim →
  downstream services trust the JWT, not the client.

### 13.4 Azure API design best practices

Applied to the external MCP API + inter-service APIs:
- Resource-based URIs (`/v1/memories`, `/v1/memories/:id`, `/v1/wiki/pages`)
  — NOT verb-based (`/create-memory`)
- HTTP methods semantically (GET=read, POST=create, PUT=replace, PATCH=partial,
  DELETE=remove)
- 202 Accepted + Location header for async operations (consolidation, backup,
  vacuum, reembed)
- Pagination with `limit`/`offset` + a `max-limit` ceiling
- Versioning: URI versioning for client-facing, media type for HATEOAS internal
- OpenAPI spec generated from the protocol crate
- Distributed tracing: `X-Request-ID` / `X-Trace-ID` propagated through the
  gateway → services → stores

---

## 14. What this plan is missing (flagged for review)

These came up in discussion but are NOT resolved. They need their own design
pass before any implementation:

### 14.1 Observability stack

The plan mentions OpenTelemetry but doesn't specify:
- Where traces go (Jaeger? Tempo? Honeycomb?)
- Where metrics go (Prometheus + Grafana? VictoriaMetrics?)
- Where logs go (Loki? Elasticsearch? stdout → platform logs?)
- Alerting rules (who gets paged when recall latency spikes?)
- Per-tenant observability (can a tenant see their own metrics?)

**Recommendation:** a separate `yadgar-observability` design doc. The 13
services emit OTel; the platform aggregates. Solo: logs to stderr + a local
JSONL file. SaaS: full OTel pipeline.

### 14.2 Service mesh

13 services on k8s — do you need a service mesh (Istio/Linkerd/Cilium)?
- **Pro:** mTLS between services, automatic retries, traffic splitting for
  canaries, observability without code changes.
- **Con:** complexity, resource overhead, another thing to operate.
- **Solo/team:** no mesh (localhost, no mTLS needed).
- **SaaS:** probably yes (mTLS between services is a SaaS baseline), but this
  needs a separate review. Linkerd (lighter) over Istio (heavier) if so.

**Recommendation:** defer the mesh decision until the SaaS tier is real.
Start with mTLS at the gateway → services boundary (where the client API key
is), plaintext between services inside the cluster. Add a mesh when you have
>1 replica of a service and need canary deploys.

### 14.3 Disaster recovery / multi-region

The plan covers backup (single-region) but NOT:
- Cross-region replication (SurrealDB + Maria async replication to a second
  region)
- RPO/RTO targets
- Failover procedure (DNS? global load balancer? Route53 health checks?)
- DR testing (GameDays, chaos engineering)

**Recommendation:** defer to a SaaS-tier design doc. Solo/team don't need
multi-region. SaaS-tier DR is its own project.

### 14.4 CI/CD for 13 services

The current CI is a monolith workflow. 13 services need:
- Per-service build + test pipelines (fast, parallel)
- Per-service deploy pipelines (canary, not big-bang)
- Protocol crate bump → coordinated multi-service deploy
- Rollback per service

**Recommendation:** a `yadgar-ci` design doc. GitHub Actions matrix per crate,
Helm-based deploys with canary stages. The monorepo helps — atomic protocol
bumps in one PR, CI builds all affected services.

### 14.5 Data migration from current Python yadgar

How do existing users (and the current dogfooding instance) migrate from the
Python monolith to the Rust microservices? This plan doesn't address:
- Schema migration (Surreal + Maria)
- Memory/wiki data migration
- Cutover procedure (run both in parallel? instant switch?)
- Rollback if the Rust version has a regression

**Recommendation:** a `yadgar-migration` design doc. The strangler path
(steps 1-15) gives a hybrid period (Rust gateway + Python backend), but the
final cutover needs a plan.

### 14.6 Billing integration

`yadgar-metering` records events but doesn't bill. The billing system
(Stripe / Lago / custom) is unspecified:
- Which billing engine?
- What's the pricing model (per-seat? per-memory? per-API-call? per-GB?)
- Free tier limits
- Trial → paid conversion

**Recommendation:** a `yadgar-billing` design doc, AFTER metering is built.
Don't design billing before you know what people use.

### 14.7 Tenant onboarding

How does a new tenant sign up?
- Self-serve signup (email + Stripe)?
- Provisioning flow (create tenant → KEK in KMS → DEK → default roles →
  bootstrap user)?
- Data residency choice (EU/US)?

**Recommendation:** a `yadgar-onboarding` design doc. Tied to billing.

### 14.8 GDPR / data sovereignty

If SaaS serves EU customers:
- Right to erasure (delete all tenant data across Surreal + Maria + Valkey +
  object storage + backups)
- Data residency (EU-only deployment?)
- DPA (data processing agreement)
- Audit log requirements

**Recommendation:** a `yadgar-compliance` design doc, BEFORE launching in EU.
This is a legal review, not just a technical one.

### 14.9 Secret management for the services themselves

The 13 services need secrets (DB passwords, KMS access, inter-service TLS
certs, S3 credentials). Where do they live?
- Solo: `~/.yadgar/config.toml` (file perms)
- Team: docker-compose secrets or a small Vault
- SaaS: Kubernetes Secrets (encrypted at rest with KMS) or external Secrets
  Operator + Vault

**Recommendation:** specified in the `yadgar-observability` or a dedicated
`yadgar-secrets` doc. Don't roll your own; use the platform's secret store.

### 14.10 The nix flake

The current nix module pins versions. The rewrite needs a new nix module for
the Rust binary / containers. Out of scope for this plan but flagged.

---

## 15. Open questions (for reviewers)

1. **Is 13 services the right granularity?** Could `yadgar-control` + `yadgar-backup`
   merge? Could `yadgar-iam` + `yadgar-vault` merge (they're both security)?
   Counter-argument: different change cadence (auth changes often, crypto
   rarely) + different blast radius (a vault bug exposes all data; an IAM bug
   exposes access). Keep separate.
2. **Valkey vs Redis vs KeyDB?** Valkey is the Redis fork (License, not BSD+SSPL).
   Need a review of the cache store choice — especially for SaaS where it's
   load-bearing.
3. **MariaDB vs Postgres for the relational set?** The current split-store work
   chose Maria for `asyncmy` + Alembic. Postgres + `asyncpg` is the other
   option. Revisit in the storage design doc.
4. **SurrealDB vs Postgres+pgvector for the memory store?** Surreal has the graph
   + memory + wiki today. Postgres+pgvector is the mainstream vector store.
   This is a bigger question than this plan — it's the store behind the IP.
5. **candle vs Python sidecar for Ettin — when to switch?** The plan says "start
   sidecar, end candle." When is "end"? After the retrieval port is stable?
   After a candle A/B proves parity? Needs a gate definition.
6. **Does the scheduler need to be Raft-based or is Valkey SETNX leader
   election enough?** Raft (openraft crate) gives strong consistency; SETNX
   is simpler but has a small window of dual-leaders. For a nightly-cycle
   scheduler, SETNX is probably fine. Needs a review.
7. **Should `yadgar-recall` hold the KNN index in-process or query Surreal's
   KNN?** In-process (hnsw_rs) is faster but uses memory + needs reindex on
   restart. Surreal KNN is simpler but slower. Current code uses Surreal KNN.
8. **Is the `yadgar-hook` binary the right boundary, or should hooks be
   MCP tools too?** Claude Code hooks can't be HTTP, but they could call a
   local MCP server instead of a raw HTTP POST. Trade-off: another layer vs.
   directness. Current plan: direct HTTP POST, simplest path.

---

## 16. What I would NOT do (pushback on the plan itself)

- **Don't rewrite the retrieval pipeline until the eval harness is trusted.**
  It's the IP. A port that drops recall@5 is worse than the Python original.
- **Don't build `yadgar-vault` as a generic KMS.** Wrap a real KMS. Rolling
  your own crypto is how people end up on Hacker News.
- **Don't put billing logic in `yadgar-metering`.** It records events. Billing
  is downstream.
- **Don't grow `yadgar-scheduler` into a workflow engine.** Linear chain + one
  conditional is the ceiling. If you need more, use Temporal/Inngest.
- **Don't make `yadgar-hook` smart.** ~100 lines. Parse stdin, POST, exit.
- **Don't do a big-bang rewrite.** The strangler path (steps 1-15) exists
  specifically to avoid it. At step 4 you're running Rust + Python hybrid. If
  the Rust path stalls, the Python backend still works.
- **Don't decompose into 20 services.** 13 is already at the edge of
  manageability for a solo/small team. More services = more operational
  overhead with no benefit unless there's a real boundary.
- **Don't add a service mesh on day one.** Defer until you have >1 replica and
  need canary deploys.
- **Don't design the billing model before the product is in use.** Build
  metering (which records), defer billing (which charges) until you know what
  people consume.

---

## 17. Next steps for this plan

1. **Review** — circulate to the reviewers listed in §1. Each should red-team
   their area.
2. **ADR** — if the plan survives review, convert it into a set of ADRs (one
   per major decision: decomposition, language choice, tenancy model, deploy
   tiers, Ettin strategy, scheduler design, backup verification gate).
3. **Prototype the protocol crate** — the cheapest validation. If
   `yadgar-protocol` can express the wire contracts cleanly in serde, the
   decomposition is viable. If it can't, the boundaries are wrong.
4. **Prototype `yadgar-hook`** — ~100 lines of Rust. If the hook proxy works
   against the current Python daemon (remote mode), the client-side story is
   validated without touching the backend.
5. **Do NOT start the retrieval port** — until `make eval` and `make
   longmemeval` are green and trusted. That's the gate.

---

## Appendix A: The 13 services at a glance

| # | Service | Lang | Hot/cold | State | Scales by | Replicas (SaaS) |
|---|---|---|---|---|---|---|
| 1 | gateway | Rust | hot | stateless | concurrent connections | 3+ |
| 2 | recall | Rust | hot | warm caches | CPU (parallel pipelines) | 3+ |
| 3 | ml | Rust+Ettin | hot | model weights | GPU | 1 per GPU |
| 4 | write | Rust | warm | queue | tenant (sharded) | 1 per tenant shard |
| 5 | cache | Valkey | hot | all | memory | 1 (or cluster) |
| 6 | iam | Rust | hot | stateless | concurrent | 3 |
| 7 | vault | Rust | warm | key cache | concurrent | 3 |
| 8 | metering | Rust | warm | stateless | event volume | 3 |
| 9 | scheduler | Rust | warm | job registry | 1 (leader) | 3 (1 leader) |
| 10 | backup | Rust | cold | snapshot catalog | 1 active + 1 standby | 2 |
| 11 | control | Rust | cold | stateless | rare | 2 |
| 12 | consolidation | Rust/Py | cold | none | tenant (CronJob) | 1 per run |
| 13 | viz | Rust | on-demand | layout cache | rare | 1 |

## Appendix B: What dies in the rewrite

| Current | Replaced by | Why |
|---|---|---|
| `nightly_cycle.py` (475 LOC) | scheduler job graph | The 7-step script is a poor man's DAG |
| `ConsolidationScheduler` singleton | scheduler `/v1/jobs/:id/run` | No service holds its own timer |
| `_maybe_auto_vacuum` | scheduler conditional job | Condition logic belongs in job definition |
| `deploy/systemd/*.timer` (3 timers) | scheduler triggers | One scheduling mechanism |
| `backup.py` (export/strip/swap) | `yadgar-backup` service | Cross-engine quiesce + verification gate |
| `vacuum/phases.py` + `launcher.py` | `yadgar-control vacuum` | Vacuum is an admin op |
| `safe_start.py` (Surreal crash recovery) | `yadgar-control recover` | Crash recovery is an admin op |
| `_shared` (31k LOC) | `yadgar-protocol` crate | Wire-only contracts, enforced by the compiler |
| Two Python LRUs | Valkey | Cross-process, survives restarts |
| `BearerAuthMiddleware` (single token) | `yadgar-iam` | RBAC, per-tenant, API key lifecycle |
| `TokenBucketRateLimiter` (in-process) | `yadgar-metering` + Valkey | Shared, atomic, survives restarts |
| Secret-gate regex (write-time) | `yadgar-vault /v1/secret-scan` | Pre-encryption check + real crypto |
| Python 3.14+ + pipx + venv + Docker (client) | `yadgar-hook` 2MB binary | Zero local install |
