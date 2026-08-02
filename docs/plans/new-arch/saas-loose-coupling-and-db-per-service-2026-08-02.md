# SaaS Architecture Addendum: Loose Coupling + Database-per-Service

**Status:** DRAFT — addendum to the SaaS rewrite plan (PR #25, merged) AND to
the in-flight spine/knob plans that are about to land on master.
**Date:** 2026-08-02
**Branch:** `docs/saas-loose-coupling-and-db-per-service-2026-08-02` (doc-only)
**Amends:**
- `docs/plans/new-arch/saas-rewrite-plan-2026-08-02.md` §3.5 (stores), §4 (SaaS spine), §6 (deploy tiers)
- `docs/plans/split-store-engine-decision-2026-08-02.md` §4.5 (one MariaDB)
- `docs/plans/task-table-refactor-2026-07-29.md` §3 (schema — three tables in one MariaDB), §2.7 D34 (Alembic owns MariaDB)
- `docs/plans/settings-to-db-config-migration-2026-07-24.md` §G (runtime_config as first mover onto MariaDB)

---

## 1. The two constraints

### 1.1 Loose coupling at startup (no chicken-and-egg)

> "services do not rely on each other to come up. but they need each other to
> work. this will prevent chicken and eggs during cold starts or reboot of the
> cluster."

**Principle:** every service boots independently and becomes ready without
any other service being up. A service may *fail to serve a request* if a
dependency is down (e.g. `yadgar-recall` can't recall without `surrealdb`),
but it must *boot, accept connections, and return a clean 503/504* — not hang,
crash, or retry-loop forever waiting for a dependency.

This is the **fail-fast + circuit-breaker** pattern, not the **wait-for-
dependencies** pattern. No service's startup blocks on another service's
readiness. A cluster cold start is: everything boots in parallel, everything
becomes ready, requests that need a down dependency get 503 until it recovers.

### 1.2 Database-per-service (never multiple schemas in one DB)

> "each of the adr, tasklist, agent prompts, config knobs that need to have a
> sql db to work must have their own db. this is what i have learned the hard
> way to never implement multiple schemas in the same db. otherwise swapping
> and changing will be a nightmare."

**Principle:** each service that needs SQL owns its own database. No shared
database with multiple schemas. This is a direct amendment to the current
spine + knob + split-store plans, which all assume **one MariaDB** holding
`task` + `adr` + `agent_prompt` + `runtime_config` — exactly the anti-pattern.

---

## 2. What the current plans assume (and why it's wrong)

The three in-flight plans all converge on **one MariaDB** for the relational set:

| Plan | What it puts in MariaDB | Citation |
|---|---|---|
| split-store-engine-decision | "MariaDB takes the relational set" — all of it | §4.5, §0 TL;DR |
| task-table-refactor (spine) | `task` + `adr` + `agent_prompt` — three tables in one MariaDB | §3 Schema, §2.1 D2 ("Three tables") |
| settings-to-db-config-migration (knob) | `runtime_config` — "the first mover onto MariaDB" | §G, Phase 0.9 |

The spine plan's §2.7 D34 says: *"Alembic owns MariaDB, with its own version
table and its own ordered chain."* — one Alembic chain, one MariaDB, four
schemas. The knob plan's §G explicitly positions `runtime_config` as the
pilot for that same MariaDB.

**The lesson learned the hard way:** this is the god-DB anti-pattern. If
`runtime_config` needs to swap to Postgres (or a config service, or etcd),
you can't touch it without risking the `task`/`adr`/`agent_prompt` tables
in the same DB. If the ADR table needs a schema migration, it shares an
Alembic chain with the knob store, and a bad revision blocks all four.
If the task table corrupts, it's in the same InnoDB file as the ADR index.
Swapping, changing, backing up, or right-sizing any one is a nightmare
because they're all in one engine, one backup, one migration chain.

**This addendum splits them.** Each gets its own database.

---

## 3. Revised store inventory — database-per-service

### 3.1 The four relational surfaces from the current plans, separated

| Surface | Current plan | Owns these tables | Revised DB |
|---|---|---|---|
| **ADR metadata** (spine D2) | one MariaDB, shared | `adr` (§3: origin, number, status, date, subsystem, tier, supersedes[], superseded_by[], body_slug, created_at, modified_at) | **`yadgar_adr`** — its own DB |
| **Task list** (spine D2) | one MariaDB, shared | `task` (§3: origin, number, title, status, active_form, state, plan_path, blocked_by[], blocks[], body_slug, created_at, modified_at) | **`yadgar_tasks`** — its own DB |
| **Agent prompts** (spine D2) | one MariaDB, shared | `agent_prompt` (§3: origin, number, kind, purpose, composes[], uses, status, body_slug) | **`yadgar_prompts`** — its own DB |
| **Config knobs** (knob plan) | one MariaDB, shared ("first mover") | `runtime_config` (key, value, scope, directory_context, typed JSON) | **`yadgar_config`** — its own DB |

### 3.2 The SaaS spine services from PR #25, each with their own DB

| Service | Owns | DB |
|---|---|---|
| `yadgar-iam` | users, api_keys, roles, role_bindings, tenants, sessions | `yadgar_iam` |
| `yadgar-scheduler` | jobs, job_runs, job_locks, maintenance_windows | `yadgar_scheduler` |
| `yadgar-metering` | usage_events, quotas, quota_state | `yadgar_metering` |
| `yadgar-vault` | dek_catalog, kek_metadata, key_rotation_log, secret_scan_log | `yadgar_vault` |
| `yadgar-backup` | snapshot_catalog, restore_history, verification_results | `yadgar_backup` |
| `yadgar-control` | invariants_log, vacuum_runs, lifecycle_events | `yadgar_control` |

### 3.3 Full inventory — 10 service-owned SQL DBs + shared non-SQL stores

```
SQL databases (each service owns its own, never shared):
  yadgar_adr            — ADR metadata (spine)
  yadgar_tasks          — task list (spine)
  yadgar_prompts        — agent prompts (spine)
  yadgar_config         — runtime config knobs (knob plan)
  yadgar_iam            — AAA (SaaS spine)
  yadgar_scheduler      — job registry (SaaS spine)
  yadgar_metering       — usage/quotas (SaaS spine)
  yadgar_vault          — encryption keys (SaaS spine)
  yadgar_backup         — snapshot catalog (SaaS spine)
  yadgar_control        — admin ops log (SaaS spine)

Non-SQL stores (shared, but each is one engine, one dataset — not the multi-schema anti-pattern):
  surrealdb             — graph, memory, wiki BODIES + embeddings (read: recall, write: write)
  valkey                — cache + rate-limit counters + session tokens (shared, ephemeral)
  object storage        — backups, model weights (S3/minio)
```

**The SurrealDB store stays shared between `yadgar-recall` and `yadgar-write`**
— that's the CQRS split (read vs command), not the multi-schema anti-pattern.
Surreal holds one dataset (memories + embeddings + wiki bodies) accessed
through two surfaces. The wiki *bodies* stay in Surreal (spine D4,
non-negotiable); only the *metadata* moves to per-service SQL DBs.

**Valkey stays shared** — it's a cache, not a system of record. If it's lost,
nothing is permanently gone; it's rebuilt from the stores.

---

## 4. What this changes in the spine plan (task-table-refactor)

### 4.1 D2 revised — three tables, three databases

D2 currently says: *"Three tables — `task`, `adr`, `agent_prompt`. No
generic `record` table."* The rationale is correct (different status enums,
required fields, indexes). **But they go in three separate databases, not
one.**

| Table | Database | Alembic chain |
|---|---|---|
| `adr` | `yadgar_adr` | its own `alembic_versions` table |
| `task` | `yadgar_tasks` | its own `alembic_versions` table |
| `agent_prompt` | `yadgar_prompts` | its own `alembic_versions` table |

### 4.2 D34 revised — Alembic per database, not one chain

D34 currently says: *"Alembic owns MariaDB, with its own version table and
its own ordered chain."* That's one chain for all four schemas. Revised:
**each database has its own Alembic chain.** Three separate `alembic_versions`
tables, three separate revision directories (`migrations/adr/`,
`migrations/tasks/`, `migrations/prompts/`), three independent upgrade paths.

**Why:** a bad revision in the ADR chain must not block the task chain. A
schema swap on the prompts DB must not touch the ADR DB. The spine plan's
D34 argument for "two migration systems must not be merged" (Surreal vs
MariaDB) applies **equally within the SQL layer**: four schemas in one
MariaDB with one Alembic chain is the same coupling problem at a smaller
scale.

### 4.3 D33 revised — the spine's dependency on the config store

D33 currently says the spine reads `project.key_override` from the config
store at write-time (lazy, non-fatal). Under db-per-service, the config
store is `yadgar_config` (a separate DB). The dependency is the same — the
spine's `adr`/`task`/`prompt` services call the config service's HTTP API to
resolve `project_id`, with a fallback to the derived key (D13/D14) if config
is down. **The cross-DB dependency becomes a cross-service HTTP call, which
is already what the loose-coupling protocol (§5 below) handles.**

### 4.4 D31 — the `MAX(number)+1 FOR UPDATE` stays, per-database

D31's allocation mechanism (`SELECT MAX(number)+1 ... FOR UPDATE` inside the
INSERT transaction) works identically per-database. Each DB's `adr`/`task`/
`prompt` table has its own `(project_id, origin, number)` uniqueness
constraint and its own `FOR UPDATE` lock. No cross-database transaction
needed — the semantic number is allocated within the same DB as the row.

### 4.5 The cross-engine quiesce point (split-store §5.2) expands

The split-store decision's quiesce point was Surreal + one MariaDB. Under
db-per-service, it's Surreal + N SQL databases. For solo (SQLite files):
stop writes (quiesce lock), `cp` the files (SQLite WAL-mode files are
consistent after checkpoint), resume. For SaaS (Postgres/MariaDB
instances): snapshot each DB within the quiesce window. The verification
gate (split-store §5.2, the 2026-06-16 incident defense) must verify N
DBs, not 2. See §6 below.

---

## 5. Loose coupling at startup — the protocol

### 5.1 The rule

**Every service boots and becomes ready independently.** No service's
`/health` readiness depends on another service being up. No service's
startup code waits for a dependency's `/health` to return 200.

### 5.2 Two health endpoints (Kubernetes pattern)

```
/healthz       — liveness: "the process is alive, not deadlocked"
/readyz        — readiness: "I can serve requests right now"
```

- **Liveness** is always 200 if the process is running. It does NOT check
  dependencies. A deadlocked process fails liveness; a process with a down
  DB passes liveness.
- **Readiness** is 200 only if the service can serve its core function.
  `yadgar-recall`'s `/readyz` is 200 only if SurrealDB is reachable + its
  own SQL DB (if any) is reachable. If either is down, `/readyz` returns
  503 — but the process is still alive (`/healthz` 200) and retries in the
  background.

### 5.3 Startup sequence (the anti-chicken-and-egg)

```
Cluster cold start (everything boots in parallel):

  t=0:   all services start. all stores start (surreal, N SQL DBs, valkey).
  t=1s:  all services have booted, /healthz = 200 for all.
         /readyz = 503 for services whose DB isn't up yet.
  t=2s:  SQL DBs are up (or SQLite files opened). services with SQL DBs
         connect, run their own Alembic migrations, /readyz → 200 for
         adr, tasks, prompts, config, iam, scheduler, metering, vault,
         backup, control.
  t=4s:  surreal is up. recall + write /readyz → 200.
  t=4s:  valkey is up. cache + metering rate-limit /readyz → 200.
  t=5s:  ml loads model weights, /readyz → 200.
  t=1s:  gateway has no DB — it was /readyz=200 at t=1s.
         It calls iam/recall/etc per-request, returns 503 for any
         dependency that isn't ready yet.
```

**No service waits at startup for another service.** The gateway is ready
at t=1s. If a client calls `/v1/recall` at t=2s (before surreal is up), the
gateway calls `yadgar-recall`, gets a 503 (recall's `/readyz` is 503), and
returns 503 to the client. The client retries. At t=4s, surreal is up,
recall's `/readyz` flips to 200, the retry succeeds.

### 5.4 What this means for service code

Every service implements:

1. **Boot without dependencies.** Process starts, binds HTTP port, serves
   `/healthz` = 200. Does NOT block on connecting to its DB or other
   services.
2. **Lazy dependency connection.** A background task connects to the DB,
   runs migrations, flips a `ready` atomic flag. `/readyz` returns 200 only
   after the flag flips.
3. **Circuit breakers on outbound calls.** Every call to another service
   goes through a circuit breaker. Downstream down → fail fast (503 or
   degraded response), not a 30s timeout.
4. **Graceful degradation per tool:**
   - `recall` without `ml` → unreranked results (recall@k drops but works)
   - `recall` without `surreal` → 503 (can't recall without the store)
   - `write` without `surreal` → 503 (can't write)
   - `write` without `vault` → fail closed (refuse to write sensitive
     content unencrypted) — **policy decision, documented per service**
   - `adr_add` without `yadgar_config` → use derived key (D13/D14 fallback),
     write the ADR, flag for later key resolution — **D33's non-fatal read**
   - `gateway` without `iam` → 503 all auth'd requests
   - `gateway` without `metering` → outbox (fire-and-forget, never block)
5. **No startup-time dependency injection that blocks.** If `yadgar-write`
   needs `yadgar-vault`, it gets a `VaultClient` (HTTP client + circuit
   breaker) at boot. The client is constructed immediately (no network
   call). The first actual call happens on the first write request.

### 5.5 Metering is fire-and-forget — never block a user request

The gateway writes usage events to a local outbox (Valkey list or a local
SQLite spool). A background process ships events to `yadgar-metering`. If
metering is down, events queue locally; when it recovers, they ship. A
missed billing event is recoverable; a failed user recall is not.

---

## 6. The cross-engine backup expands (split-store §5.2 amendment)

The split-store decision's quiesce point was Surreal + one MariaDB. Under
db-per-service it's Surreal + 10 SQL databases. The backup service
(`yadgar-backup`) owns the expanded quiesce:

```
backup.snapshot() sequence:
  1. acquire write-quiesce lock (via scheduler /v1/locks) → all write services pause
  2. surreal snapshot
  3. for each SQL DB (adr, tasks, prompts, config, iam, scheduler, metering, vault, backup, control):
     snapshot (pg_dump / mariadb-dump / sqlite cp, depending on engine)
  4. tag all snapshots with the same quiesce_id (UUID)
  5. release quiesce lock
  6. verify all snapshots (row counts, checksums)
  7. register in backup catalog (yadgar_backup DB)
  8. async: ship to object storage
```

The **restore verification gate** (the 2026-06-16 incident defense) must
verify all N DBs + Surreal + cross-engine referential integrity (every
SQL row referencing a `body_slug` has a matching Surreal wiki page).

**Solo mode (SQLite files):** stop writes, `cp` all `.db` files + the
SurrealKV dir + the Valkey RDB, resume. ~seconds. Each SQLite file is
independently restorable.

---

## 7. Solo vs SaaS topology with db-per-service

### 7.1 Solo (default, `--features solo`)

```
~/.yadgar/data/
  adr.db              — ADR metadata (SQLite, WAL mode)
  tasks.db            — task list (SQLite)
  prompts.db          — agent prompts (SQLite)
  config.db           — runtime config knobs (SQLite)
  iam.db              — users/keys/roles (SQLite, solo: one bootstrap user)
  scheduler.db        — jobs/runs/locks (SQLite)
  metering.db         — usage events (SQLite, solo: logs only)
  vault.db            — DEK catalog (SQLite, chmod 600)
  backup.db           — snapshot catalog (SQLite)
  control.db          — invariants/vacuum log (SQLite)
  surreal.surrealkv/  — memory store (embedded)
  valkey.rdb          — cache (embedded or child process)
```

10 SQLite files + embedded Surreal + embedded Valkey. Each SQLite file is
independently swappable, backupable, inspectable with `sqlite3`. If the
vault DB corrupts, restore just that file. If you want to swap the ADR
store for Postgres later, migrate just that file. **The god-DB nightmare
is structurally impossible.**

SQLite handles concurrent access from one process fine (WAL mode). The
solo binary is one process with all services in-process — each service
opens its own SQLite file. Total DB overhead: ~zero (each file is a few
KB to a few MB).

### 7.2 Team / self-hosted (`--features distributed`)

```
docker-compose up
  → 10 Postgres/MariaDB databases (one per service, possibly on one cluster
    with separate DBs — NOT separate schemas in one DB)
  → surrealdb container
  → valkey container
  → 10+ Rust service containers
```

One Postgres cluster with 10 databases (`CREATE DATABASE yadgar_adr;
CREATE DATABASE yadgar_tasks; ...`) is the cheapest team option. Each DB
has its own `pg_dump`, its own ownership, its own migration chain. This is
NOT the same as 10 schemas in one DB — `pg_dump yadgar_adr` doesn't touch
`yadgar_tasks`.

For users who need isolation (metering's write load impacts the scheduler):
split to separate Postgres instances per service. The service code doesn't
care — it connects to a `DATABASE_URL` env var.

### 7.3 SaaS (`--features distributed` + `YADGAR_MODE=saas`)

```
helm install yadgar ./deploy/helm
  → 10 Postgres databases (right-sized per service: ADR store tiny,
    metering high-write, vault security-isolated)
  → surrealdb (cluster mode)
  → valkey (cluster mode)
  → object storage (S3 + SSE-KMS)
  → 13 services on k8s, horizontal scaling per service
```

Right-sizing per service:
- `yadgar_adr` — tiny (hundreds of rows), smallest Postgres instance
- `yadgar_tasks` — small (thousands), small instance
- `yadgar_prompts` — tiny (dozens), smallest
- `yadgar_config` — small, read-heavy, small instance
- `yadgar_metering` — high-write append-mostly, larger instance or
  ClickHouse if volume demands (swap independently)
- `yadgar_vault` — tiny but security-critical, isolated instance, encrypted
  volumes
- `yadgar_scheduler` — small + transactional, small instance
- others — small

**Each can be swapped, migrated, sharded, or moved to a different engine
independently.** That's the whole point.

---

## 8. What this costs (honest)

- **More moving parts at the DB layer.** 10 SQLite files (solo) or 10
  Postgres databases (SaaS) vs 1 MariaDB. 10 things to back up, 10 things
  to monitor.
- **Cross-service queries are impossible.** "Show all ADRs for projects
  with >1000 memories" requires API composition (call `yadgar-adr` for ADRs,
  `yadgar-recall` for memory counts, join in the gateway) — not a SQL JOIN.
  This is the microservices.io "Database per Service" cost.
- **Distributed transactions are hard.** `adr_add` writes to Surreal (body
  page) + `yadgar_adr` (metadata row) — two engines, no atomic transaction.
  The spine plan's D33/§4.1 already handles this with row-last ordering +
  `check_invariants` cross-engine checks. Under db-per-service the
  cross-engine check is the same shape, just across more DBs.
- **More connection pools.** 10 services × 3 replicas = 30 connection
  pools. Use PgBouncer (transaction-mode) or bump `max_connections`.
- **10 Alembic chains.** 10 `alembic_versions` tables, 10 migration
  directories. CI runs each service's migrations independently. This is
  more files but less coupling — a bad revision in one chain doesn't
  block the others.

**The cost is real but bounded.** The benefit — "I can swap, migrate, back
up, or change the engine of any one service's DB without touching the
other nine" — is the lesson learned the hard way. You pay a little every
day (more to operate) to avoid paying catastrophically once (a god-DB
migration that blocks everything for weeks).

---

## 9. What dies (revised from the current plans)

| Pattern in current plans | Replaced by | Why |
|---|---|---|
| One MariaDB for `task` + `adr` + `agent_prompt` + `runtime_config` (spine §3, knob §G, split-store §4.5) | 4 separate databases: `yadgar_adr`, `yadgar_tasks`, `yadgar_prompts`, `yadgar_config` | Never multiple schemas in one DB |
| One Alembic chain for all SQL schemas (spine D34) | One Alembic chain per database (10 chains total) | A bad revision in one chain doesn't block the others |
| Implicit startup ordering (services wait for their DB) | Parallel boot + `/healthz` vs `/readyz` + circuit breakers | No chicken-and-egg on cold start |
| `runtime_config` as "first mover onto MariaDB" (knob §G) | `yadgar_config` — its own DB from the start, not a pilot in a shared MariaDB | The pilot pattern implies the shared DB is the destination; this addendum says the destination is per-service DBs |

---

## 10. Open questions added by this addendum

1. **Are ADR/tasks/prompts/config separate services or libraries inside
   gateway/control?** The DB-per-service rule is about data isolation, not
   process isolation. Recommendation: **libraries (crates) that gateway/
   control link against, each opening their own SQLite file.** They don't
   need separate processes because they don't have a different deploy
   cadence or scaling axis. Revisit if any becomes high-throughput.
2. **Postgres vs MariaDB vs SQLite per service?** The split-store decision
   chose MariaDB for the relational set (§4.5). Under db-per-service, each
   service can choose independently. Solo: SQLite (embedded, file-based).
   Team/SaaS: Postgres (mature, RLS for tenant isolation, JSON columns for
   arrays in the spine schema). The spine plan's D30 portability capability
   ("allocate next value in a series, transactionally") works on all three.
   **Recommendation: SQLite for solo, Postgres for team/SaaS. Drop MariaDB
   from the SaaS plan — Postgres's RLS + `asyncpg` + Alembic is a stronger
   fit for per-tenant isolation than MariaDB.** The split-store decision's
   MariaDB rationale (Alembic out of the box) applies equally to Postgres.
3. **Does the scheduler's DB need to survive a full cluster rebuild?** Job
   definitions are in the YAML (checked into the repo), re-loaded on boot.
   Job *run history* is telemetry, not state. Don't back it up as critical.
4. **Metering event delivery — outbox in which store?** Valkey list
   (ephemeral, acceptable to lose a few events) or a local SQLite spool.
   Not a SQL DB per service — it's a queue, not a system of record.
5. **How does `check_invariants` work across 10 SQL DBs + Surreal?** The
   spine plan's cross-engine check (D35d, §4.1) was designed for 2 engines.
   Under db-per-service it's N+1 engines. The invariants service
   (`yadgar-control`) calls each service's `/v1/verify` endpoint, collects
   results, reports cross-engine referential integrity. This is API
   composition, not cross-DB SQL — consistent with the loose-coupling
   protocol.

---

## 11. The one-sentence summary

**Every service boots alone, owns its own database, and degrades gracefully
when a dependency is down — so a cluster cold start is parallel, not
sequential, and swapping any one service's database doesn't touch the
other nine.**
