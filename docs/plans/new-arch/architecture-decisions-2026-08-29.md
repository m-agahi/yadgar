# Yadgar next-arch — end goals and locked decisions

**Status:** DECISIONS RECORD — not a build plan. Captures what was decided, and why,
so the reasoning is not re-litigated later.
**Date:** 2026-08-29
**Supersedes reasoning in:** `protocol-crate-design-2026-08-02.md`,
`llm-service-design-2026-08-02.md` (both assume a single Cargo workspace monorepo —
that assumption is retired here, see D2).

---

## TL;DR — goals

- **Rust**, total microservice architecture, deployed to k8s (kind/k3d/k3s or real).
- **One repo per module.** Every module — including sub-modules like the surprise gate
  and the rules engine — gets its own repo, so it can be tuned and refactored without
  touching anything else.
- **Every feature is a plugin that can be turned on and off**: ADR, tasks, config,
  agent-prompts, and the rest.
- **Logic is separated from data access.** A module never hosts its own DB handling;
  it calls its `-db` twin over an API.
- **No engine lock-in.** Swapping databases must cost a config change and a driver
  update — never a rewrite of module logic. Managed engines (e.g. Aurora) must be
  viable targets. No SQLite-only designs.
- **Idempotent services** so they sit behind a load balancer and a k8s Service endpoint.
- **GitOps from day one** — Argo CD or Flux, not bolted on later.
- **First-class org / team / personal modes**, in the data model from the start.

---

## Why the rewrite exists

Three weeks of work on the monolith showed the failure mode directly: changing one
module required touching many others to keep them working. The rewrite exists to make
module boundaries real — enforced by topology and process isolation, not by discipline.

---

## Locked decisions

Each decision states the rule. Rationale is recorded so it is not re-argued.

### D1 — Plugins are separate processes communicating over the protocol

A plugin is its own binary, its own repo, its own deployable. Turning a plugin off
means not running it and not registering its tools.

**Why:** true isolation; independent deploy and version; a crashed plugin does not take
the system down; on/off is an operational act, not a rebuild.
**Rejected:** static linking with cargo features (on/off becomes build-time,
third parties cannot extend); dynamic `cdylib`/WASM loading (Rust has no stable ABI;
the host/guest boundary constrains the data model hard).

### D2 — One repo per module, not one workspace

The prior new-arch docs assume a single `crates/` monorepo. That is retired.

**Why:** independent versioning, CI, and refactor blast-radius per module — the
specific thing the monolith could not give.
**Rejected: monorepo.** Re-examined explicitly and rejected on operational grounds,
not architectural ones. A monorepo would keep every runtime decision here intact
(repo layout is orthogonal to service topology) and would dissolve the protocol
distribution problem, but it requires selective-build tooling and dependency-boundary
enforcement to stay usable, and that tooling cost has proven too high in prior
experience. The decision stands on that evidence.
**Consequence:** the shared contract must be a published, versioned artifact rather
than a path dependency — resolved by D16.

### D3 — There is no shared storage substrate

Each module owns its own database. Nothing is co-located for the sake of sharing.

**Why:** the monolith-era rule "bodies and embeddings must live together" was a
consequence of one process and one already-present engine, not a physics constraint.
Recall does not join across types — it runs a **separate pipeline per type** (wiki,
memory) that each produce a candidate pool, and a ranker fuses them. The fusion point
is the **ranker**, not the store. So the stores can be anywhere.
**Consequence:** cross-plugin joins are application-side. They already are today, at
measured zero cost, so this is not a regression.

### D4 — Every module is split into logic and a `-db` twin

`yadgar-adr` calls `yadgar-adr-db` over an API. A logic service never opens a database
connection.

**Why:** module logic stays free of data-access code; an engine swap redeploys one
service and leaves logic untouched; the boundary is enforced by the *absence of the
dependency*, not by discipline. Additionally, in k8s, N replicas of a logic service
with embedded pools multiplies connections against a managed engine with hard limits —
the `-db` twin is the connection concentrator.
**Cost accepted:** roughly doubles the service count, and the `-db` API becomes a
versioned wire contract requiring coordinated deploys on schema change.

### D5 — Every `-db` API call is one business operation and one transaction

`create_adr(...)`, `list_by_status(...)`, `get_with_crossrefs(...)` — yes.
`select(table, where)` or exposed `begin()`/`commit()` — never.

**Why:** a generic CRUD/query proxy produces chatty N+1 traffic over the network,
an unversionable contract, and no transaction boundary — the worst of both designs.
**Consequence:** distributed transactions are never needed. Multi-step operations
become idempotent-retryable instead, which the load balancer requires anyway.

### D6 — Uniform twinning: every stateful module gets a `-db` twin

No per-module judgment calls about which stores are "big enough" to deserve one.

**Why:** consistency; no recurring argument about why one module differs; every
module keeps the engine-swap property.
**Cost accepted:** maximum k8s object count and maximum version-skew surface.
**Rejected:** twins only for memory/wiki/graph (cheapest, but adr/tasks/prompts then
lose independent engine choice — a stated goal).

### D7 — Abstraction is at the capability level, never the SQL level

Ports are domain-shaped operations. `yadgar-store` provides engine primitives —
pool, transaction, migration runner, capability probe, backup harness, tracing — and
**knows zero entity schemas**. Adapters encoding a module's schema ship inside that
module's repo.

**Why:** abstracting at the SQL level forces lowest-common-denominator SQL and forfeits
pgvector, JSONB, and full-text — precisely what recall needs. And a driver repo holding
every module's adapters becomes the new coupling point: every schema change touches it,
every module waits on its release. That is the monolith one layer down.
**Consequence:** "any database" means "any database with the required capabilities."
A module needing vector search requires a vector-capable engine. The driver probes
capabilities and the module **fails at boot** when they are missing. Never degrade silently.

### D8 — Concurrency control is optimistic: version column and compare-and-set

Every mutable entity carries `version: u64`. Writers read, compute, and write with an
expected version; a mismatch returns `Conflict` and the writer re-reads and retries.

**Why:** the `memory` table has at least seven distinct writers today, and
thermodynamics mutates existing rows (heat, plasticity, stability). Under D5 there is
no cross-network transaction, so a read-modify-write without CAS is a lost-update race —
and behind a retrying load balancer, a guaranteed one. CAS is general: it protects any
future writer, not just the ones known today.
**Consequence:** every mutable model carries `version`; every writer handles retry;
recompute-on-retry must be idempotent.
**Rejected:** folding mutators into the `-db` service as domain operations — it removes
the race by construction but ties tuning of a module like thermodynamics to a redeploy
of `memory-db`, defeating D2.

### D9 — Idempotency is a write-contract property, not a deploy property

Every write carries a client-supplied `idempotency_key`, deduplicated in the owning
module's store under a uniqueness constraint.

**Why:** behind a load balancer with retries, a write fires more than once. Retrofitting
this later means touching every write path — exactly the debt the rewrite exists to escape.

### D10 — Recall visibility is topology, not policy

A module that should not appear in search simply does not register a retrieval provider.

**Why:** today `_shared/wiki/policy.py` carries a `POLICY_BY_TYPE` registry where ADR,
`task_list`, and the agent-prompt library are marked `recall_disposition="exclude"`,
enforced in the stage-1 `WHERE` clause. That the user observed "prompts and tasks never
rank" is that deliberate policy working, not an embedding defect. Splitting these into
their own modules formalizes an existing decision and deletes the registry.

### D11 — The queue is a broker, and its client contract is explicit

`memorize` is already asynchronous: it enqueues and returns, and the heavy pipeline runs
in a background drainer. That stays. The file-based queue does not.

**Why:** the current `wait=True` polls for a terminal *file* under `~/.yadgar/queue/`.
With replicas behind a Service, a different pod handles the poll and the contract breaks.
**Consequence:** a real broker (NATS/Redis/SQS) plus a poll-by-id or callback API. This
is a **client-visible contract change**, not an infrastructure swap.
**Also:** because the write path is already async, extra network hops on it are cheap.
**Only `recall` is latency-critical.** Optimization effort belongs there.

### D12 — Identity and scope fields exist in the model from day one

Every user-facing entity carries, from the first migration and whether or not team mode
ships early:

- `owner_user_id` — **who `PRIVATE` means.** Required; `visibility` is meaningless
  without it.
- `visibility` — an ordered ladder, defaulting to the most restrictive:
  `PRIVATE` (owner only) → `TEAM` (members of `team_id`) → `ORG` (everyone in the
  installation).
- `team_id` — meaningful only at `TEAM` visibility, naming which team, since a user may
  belong to several. `ORG` requires no identifier: under D27 one deployment is one
  organisation, so no `org_id` field exists. Hosting several organisations in one
  deployment is multi-tenant SaaS, which D27 rules out; adding the column speculatively
  would burden every schema and every query for a direction already rejected.
- `created_by` / `updated_by` — audit identity, distinct from ownership.

**Why:** the v7 plan requires team scoping, and adding a NOT NULL scope column to every
entity after the stores hold data is the touch-every-module change this rewrite exists to
prevent. Ownership and authorship are deliberately separate fields: a record promoted to
team scope, or edited by another member, keeps its owner while `updated_by` changes.
Under D8, where a single entity has many writers coordinating by compare-and-set,
`updated_by` is what makes a version conflict diagnosable.
**Open:** whether agent provenance (today's `provenance_agent` — which subagent produced
a record, an axis distinct from which user) belongs in the universal envelope or only on
entities where it applies.

### D13 — Repo and service are different units

A module gets its own repo unconditionally. Whether it gets its own k8s Deployment is a
separate decision, made on scaling and deploy-cadence grounds.

**Why:** independent tuning and refactoring — the stated goal — is delivered by the repo
boundary. A separate Deployment adds a network hop and a k8s object, and buys only
independent scaling.

### D14 — "Dream" is the name for the consolidation / sleep-compute family

One module family, named `dream` in the new architecture.

### D15 — APIs are versioned, and a version retires only when it is provably unused

The version is part of the service name (`adr.v1.AdrDb`, `adr.v2.AdrDb`), not a header.
One binary serves every live major concurrently. Within a major, changes are
additive-only: a new field is optional, and a field is never repurposed or renumbered.

A version stays live until no caller references it. **That condition is measured, not
tracked.** Every call carries caller identity and API version, exported as
`rpc_calls{service, version, caller}`. A version is retired when that series reads zero
for a defined window, with the remaining callers named in the dashboard.

**Why:** with modules in separate repos there is no atomic cross-module refactor, so
N-1 compatibility is the only way to deploy. A manually maintained list of "who still
uses v1" is fiction within weeks; a metric is not.

### D16 — Transport is gRPC over protobuf; the `.proto` files are the shared contract

Services communicate over gRPC (`tonic` + `prost`). The `.proto` definitions, not a
Rust crate, are the artifact shared across repos. `buf breaking` runs in CI against the
previous tag and rejects any breaking change.

**Why:** protobuf's field-number model makes D15's additive-only rule structural rather
than aspirational, and the CI gate enforces it mechanically — discipline alone is what
the monolith already failed at. Codegen means the wire contract is generated rather
than hand-written, which in a multi-repo layout is the difference between skew and
safety. A proto-based contract also keeps non-Rust modules possible.
**Resolves O3:** the shared artifact is the proto set, not a published Rust crate.
**Rejected:** HTTP/JSON (L4 load balancing works and it is curl-debuggable, but version
discipline stays manual and serialization is slower on the recall fanout);
Cap'n Proto / FlatBuffers (faster zero-copy, but the Rust RPC ecosystem is too thin).
**Consequence:** `yadgar-gateway` is the only bilingual service — JSON/MCP outward to
clients, gRPC inward. **Open (O8):** gRPC holds one long-lived HTTP/2 connection, so a
standard k8s Service pins a client to a single pod and leaves replicas idle. Requires
either a headless Service with client-side balancing or a service mesh.

### D17 — A cache lives with the component that can invalidate it

Only **content-addressed** caches may live outside the owning `-db` service. Everything
else lives inside it, because under D4 the `-db` service is the single writer for its
store and therefore the only component that can invalidate correctly.

Three tiers:

| Tier | Where | Key | Invalidation |
|---|---|---|---|
| Compute (embeddings, CE/NLI scores) | shared store, reachable by `embed` / `rank` | `sha256(input) + model_checkpoint_hash` | none needed — the key determines the value |
| Row (hot entities by id) | inside the owning `-db` | `id + version` | free — D8's CAS version is the cache key |
| Query / result (candidate pools, searches) | inside the owning `-db` | `query + scope + epoch` | epoch bump on write |

**Why:** a logic service caching entity rows produces two-level staleness with no
invalidation path and no owner. Content-addressed caches are exempt because a stale
entry is impossible by construction.

### D18 — No invalidation signal may be delivered in-process

Every cache-coherence mechanism — epochs, scope versions, per-id invalidation — must
work across replicas or not exist.

**Why:** this is already a latent bug. `ScopeVersions`
(`backend/cache/scope_versions.py:101`) is a plain in-process singleton backing the
`engram_slot` and `graph` caches plus `memory_doc`'s per-id invalidation. Under
replication, a write on one replica bumps only its own counter and every other replica
serves stale entries indefinitely — silently, with a 45-minute TTL backstop as the only
bound. Horizontal scaling is a stated goal, so this cannot be carried forward.
**Consequence:** compute caches move out of process to a shared store — with N replicas
an in-process cache means N cold caches and an N-fold miss rate on the hot cross-encoder
path. The file-based epoch bus also dies: it works today only because both processes
mount the same volume, which in k8s means RWX storage and `flock` over a network
filesystem. Epochs become counters in the shared cache store.
**Nice property:** D8 was not chosen for caching, but per-row `version` removes the need
for a scope counter on row caches entirely.

### D19 — Cache behaviour is one library, not per-service code

A single `yadgar-cache` crate provides the cache implementation, budgeting, metrics, and
key discipline; every `-db` service uses it.

**Why:** today `core/cache/cache.py` and `backend/cache/cache.py` are two separately
maintained implementations of the same design. With ~19 `-db` services, per-service
cache code is that duplication multiplied.
**Consequence:** budgets become per-Deployment resource settings rather than a single
percentage-of-container-RAM constant.

### D20 — Greenfield. No data migration is in scope

The new system starts empty. Existing memories and wiki pages are not migrated, and no
schema, API, or storage decision may be shaped by compatibility with the current corpus.
If the data is wanted later, it will be moved by hand as a separate exercise, possibly
never.

**Why:** stated directly as a goal. Designing for a migration that may not happen
imports the old schema's compromises into a system built to escape them.
**Consequence:** ADR, tasks, and agent-prompts having no tables today stops being a
migration cost and becomes a free hand — they get the schema they should have had.
The inventory of ~34 existing tables below is **reference material for understanding
behaviour, not a porting specification.**

### D21 — One shared cache deployment, one logical cache namespace per service

The cache is **infrastructure**, not a service: a single Valkey deployment, with each
`-db` service using its own key namespace. No component fronts another component's
database.

**Why:** one thing to operate, one memory budget, and no cold-cache-per-replica problem —
while each `-db` keeps sole ownership of its own invalidation, which under D4 it is the
only component able to do correctly.
**Rejected: a cache service that fronts the databases.** A read-through proxy over every
store would have to know all ~19 entity query shapes, making every schema change touch
it — the "driver as a service" already rejected in D7. It also is not the writer, so it
cannot invalidate correctly, and routing writes through it would create a single
chokepoint for every store.
**Note:** Valkey has no read-through capability of its own; a cache-through design would
require a bespoke proxy service, which is what the rejection above refers to.

### D22 — Transport is split by call shape: gRPC for synchronous, a broker for asynchronous

| Shape | Transport |
|---|---|
| Synchronous reads — recall fanout, `-db` reads, config lookups | gRPC (D16) |
| Asynchronous work and events — ingestion, dream triggers, DLQ | message broker |

**Why:** running synchronous reads over a broker means publishing a request, consuming
from a reply queue, and correlating by id — RPC reimplemented on a message bus, with two
broker hops instead of one direct call, no deadline propagation, no generated typed
contract, and degraded tracing. Conversely, the write path is already a queue (D11), so
using a broker there names an existing shape rather than adding one.
**Broker product: NATS JetStream.** Chosen on operational grounds — lighter to run than
RabbitMQ, no Erlang/mnesia clustering behaviour, and a strong Rust client
(`async-nats`). Licence verified Apache-2.0 (see the licence section below).
RabbitMQ remains a viable fallback (MPL-2.0, fine as an unmodified dependency).
**Latency note:** broker throughput differences do not affect perceived performance here,
because everything on the broker is already asynchronous. The latency-critical path is
`recall`, which is gRPC. Choose the broker on operational and licensing grounds.

### D23 — Load balancing is client-side; a service mesh is deferred

Services are reached through headless Services returning every pod IP, with per-request
balancing in the client (`tonic` supports this natively). No mesh initially.

**Why:** a standard k8s Service balances per *connection*, and gRPC multiplexes every
request over one long-lived HTTP/2 connection — so a normal Service pins each client to
a single pod and leaves the other replicas idle. Client-side balancing fixes this with
no additional infrastructure.
**Resolves O8.**
**Revisit when:** telemetry becomes the pain rather than balancing. A mesh's real payoff
at this service count is uniform golden metrics, traces, retries, and mTLS without
instrumenting each service. Migrating later is largely removing the client-side
balancing configuration, so deferring is cheap. Linkerd is the light option.
**Dependency:** the mesh option is gated on the licensing check for Linkerd's stable
distribution.

---

## What the current system teaches

Evidence gathered 2026-08-29 against the live Python codebase. Per D20 none of this
is a porting specification — it is recorded because it documents behaviour worth
keeping, and failure modes worth not repeating.

- `memorize` is **fully async on the client path** — it enqueues and returns
  `{stored, queued, queue_id}`. The whole heavy pipeline (write gate, curator,
  thermodynamics, astrocyte, engram, prospective, CLS, contradiction, conflict
  resolver) runs in the drainer. Only `recall` is synchronous.
- ADR, tasks, agent-prompts, and disciplines have **no tables**. They are kind-tagged
  `wiki_page` rows — which is why their recall behaviour needed a policy registry to
  express. Under D20 they simply get proper schemas.
- The `memory` table is written by **at least seven** subsystems. It is the primary
  contention point of any split, and the reason D8 exists.
- ~34 table kinds exist today, plus two non-DB stores: the file queue/DLQ directories
  and the code-graph index.
- The storage layer is ~15,270 LOC of mixins over one engine. It is the monolith core.
- Backup discipline is not optional: an incident on 2026-06-16 destroyed 3,622
  memories via a faulty restore-verification check. Under D6 there are N databases and
  therefore N backup paths and **no global point-in-time snapshot**. The backup and
  restore-verify harness must be contract-enforced in `yadgar-store` so no module can
  ship without one. Loss of cross-module snapshot consistency is accepted, since
  nothing is transactionally coupled across modules.
- Code-graph maintains a derived index. Derived data is rebuildable and need not be
  backed up.
- **20 caches exist today.** Only two (`ce`, `embed`) are content-addressed. They share
  a ~400MB pool; `memory_doc`, `engram_slot`, and `graph` each hold an independent
  ~400MB cap, so the backend ceiling exceeds 1GB.
- Core caches (`wiki_read`, `wiki_query`, `project_brief`, `agent_prompt_prelude`) use a
  **file-backed epoch bus** on a volume both processes mount — `flock`, read, increment,
  atomic replace. Backend caches use the in-process `ScopeVersions` instead, which is
  the D18 defect.
- `config_ptc` and `ledger_ptc` (`backend/cache/cache_budgets.py:451,490`) are fully
  built and registered but have **zero production callers**. Do not port dead
  infrastructure into the new architecture.

---

## Repo inventory (DRAFT — names not ratified)

Roughly **55 services / 60 repos** under D6. Recorded so the number is not a surprise.

**Contracts (libraries, not services)**
`yadgar-protocol` · `yadgar-store`

**Edge and control**
`yadgar-gateway` · `yadgar-iam`(+db) · `yadgar-config`(+db) · `yadgar-queue`(+db) ·
`yadgar-audit`(+db)

**Ranked knowledge** — register retrieval providers
`yadgar-memory`(+db) · `yadgar-wiki`(+db) · `yadgar-graph`(+db)

**Addressed knowledge** — no retrieval provider
`yadgar-adr`(+db) · `yadgar-task`(+db) · `yadgar-prompt`(+db) · `yadgar-block`(+db) ·
`yadgar-bookmark`(+db)

**Write pipeline** — async behind the queue
`yadgar-write` · `yadgar-writegate` · `yadgar-curator` · `yadgar-thermo` ·
`yadgar-cls`(+db) · `yadgar-astrocyte`(+db) · `yadgar-engram`(+db) ·
`yadgar-prospective`(+db) · `yadgar-rules`(+db) · `yadgar-conflict`

**Read pipeline** — latency-critical
`yadgar-recall` · `yadgar-embed` · `yadgar-rank`

**Background**
`yadgar-dream`(+db) · `yadgar-vacuum`(+db) · `yadgar-causal`(+db) ·
`yadgar-narrative`(+db) · `yadgar-checkpoint`(+db) · `yadgar-codegraph`(+db)

**LLM**
`yadgar-llm` — providers as crates inside, or six sibling repos (see O2)

**Surface and ops**
`yadgar-viz` · `yadgar-cli` · `yadgar-hooks` · `yadgar-deploy`

### D24 — At least one fully permissive engine stays a first-class target

Whatever engines are supported, a stack composed only of permissively-licensed
components must remain viable end to end — no BSL or source-available component may
become a hard requirement.

**Why:** the project is open source and intended for organisations to self-host. Many
organisations refuse BSL and source-available dependencies by policy, regardless of
whether the specific restriction would apply to them. If the only supported engine were
SurrealDB, those organisations could not adopt yadgar at all. D7's engine-agnostic ports
already make this nearly free; this decision states that it must be kept true rather
than allowed to rot.
**Consequence:** SurrealDB is *an* engine, never *the* engine. Deployment artefacts
reference upstream images rather than vendoring or redistributing them.

### D25 — Audit is its own module, and audit events are written transactionally

`yadgar-audit` + `yadgar-audit-db` record who did what to which record and when. The
identity fields of D12 capture current state only and cannot answer "who deleted this".

An audit event is written to an **outbox table in the same transaction as the entity
change it describes**, and a relay publishes it to the broker afterwards.

**Why:** committing the entity change and then publishing an audit event is a dual write.
A crash between the two loses the event, so the record changed but the audit says it did
not — and an audit log with silent holes is worse than no audit log, because it will be
trusted. D5 already guarantees each `-db` call is exactly one transaction, so the outbox
costs nothing structurally, and D22's broker is already the delivery path. Delivery is
at-least-once; consumers must treat repeated events as idempotent.

**Audit is not observability.** Traces and metrics are sampled and operational; audit
must be complete and retained under a policy. They are separate systems with separate
storage and separate guarantees, and neither may stand in for the other. Today's
`action_log` table is precedent, not an audit log.

**Append-only, therefore exempt from D8.** Nothing updates an audit row, so audit
entities carry no `version` and no compare-and-set.

**Its toggle is unusual and deliberate.** Every other module is enabled or disabled as an
operational act. Audit is enabled per team by that team's own configuration, making it
the first module whose on/off state is driven by another module's data.

**Reads are audited, on a separate stream with separate guarantees.** A complete audit
needs read access recorded, but read volume is orders of magnitude higher than write
volume, so the two classes are stored separately and treated differently:

| Class | Delivery | Blocking | Retention |
|---|---|---|---|
| create / update / delete | outbox, atomic with the change | n/a — already async | permanent by default, retention configurable |
| read | published directly to the broker, fire-and-forget | never | rotated on a window, sampling permitted |

**Why the asymmetry:** a read has no write transaction to attach an outbox row to, so the
exactly-with-the-change guarantee is structurally unavailable for reads. Read auditing is
therefore best-effort by nature. This must be stated rather than discovered, or a gap in
the read log will later be read as evidence that a read did not happen. Read audit also
sits on `recall`, the only latency-critical path, so it must never block the response.
The two classes are stored separately because mixing a high-volume rotating dataset with
a permanent one in one store makes partitioning and reclamation unpleasant.

**Open:** how retention and any right-to-erasure obligation reconcile with append-only
immutability — the usual resolution is that audit rows reference identifiers rather than
content, with deletion recorded as a tombstone event rather than by rewriting history.

### D26 — One delete, owner-only, soft

There is one delete operation. The owner of a record may delete it; nobody else can.
Deletion is soft: the row is tombstoned with `deleted_at`, never physically removed on
the request path.

Sharing is a field, not a copy: promoting a record widens `visibility` (and sets
`team_id` when the target is a team) on the same row. Deleting a shared record therefore
removes it for everyone who could see it —
the same rule as deleting a message in a shared channel. A member who wants to keep it
authors their own copy.

**Why:** an earlier draft split deletion into unshare, remove-from-view, and erase, with
team promotion creating a separate copy. That is more correct in a multi-party sense and
too complicated to be used correctly — an agent given three delete verbs will choose the
wrong one, and copy-on-promotion doubles every shared record. One verb with an obvious
rule is worth more than a precise one nobody applies right.

**Erasure is not an agent operation.** Physical removal of tombstoned rows is an
administrative action via CLI, on a schedule or on request. The tool surface has no
erase.

**Synthesized entities record `derived_from`.** Anything the curator, consolidation, or
an LLM produces records the identifiers it was built from. Agents never set this field —
the synthesis path does. It is kept because lineage cannot be reconstructed after the
fact and it is what makes synthesis debuggable, independent of any deletion policy.

**Acknowledged limit:** deletion removes access, not knowledge. A member who has read
shared content still knows it.

### D27 — One deployment. No local databases, no federation

Every user of an installation talks to the same deployed services. There are no
per-user instances, no local databases, and no synchronisation between installations.
Personal scope is a `visibility` value on shared infrastructure, not a separate machine.

**Why:** stated directly. It removes an entire category of design: no sync protocol, no
conflict resolution between instances, no CRDTs, no merge-request flow for propagating
records between personal and team stores.
**Consequence:** the v7 plan's "federated personal-first" model — a private instance per
user with per-record promotion to a team server — **does not apply.** Where that plan is
consulted for team-mode design, its scoping and sharing mechanics are superseded here;
its data-model requirements (`team_id`, `visibility`, audit) still stand.
**Consequence:** the earlier concern about N databases having no global point-in-time
snapshot remains, but it is now a per-installation operational matter rather than a
distributed one.


### D28 — `ask` is the default retrieval tool, and it returns identifiers, not passages

`ask` takes a question, lets an internal LLM drive retrieval iteratively under a
hop cap, and returns an answer plus citation identifiers. `recall` remains as the
primitive `ask` calls, the fallback when the LLM is unavailable, and the escape hatch
for callers that want raw hits.

`ask` and `recall` answer different needs. `recall` takes keywords and returns ranked
passages to read. `ask` takes a question and returns a **fully detailed answer whose
claims are individually sourced**, which the caller can act on — or use to go correct
the sources. The answer is not abbreviated; brevity is not the goal.

**The response carries no memory or wiki bodies** — the answer, and for each citation a
URN, a short label saying what the record is, and a relevance score.

**Claims are cited individually, with inline markers.** The answer text carries markers
that resolve to citations, so a specific statement maps to the specific record it came
from. This is what makes the two things identifiers are for actually possible: updating
a record that turns out to be wrong, and checking a particular claim against its source.
A bare list of contributing identifiers serves neither. It also yields a free signal —
**a claim with no marker is ungrounded**, produced by the model rather than found in the
corpus, and visibly so.

**Why:** a `recall` call makes the caller pay context for every passage returned,
whether or not it bears on the question, and most do not. Reduced context cost is a
consequence of answering the question instead of shipping the pile, not the purpose of
the tool. Iteration with a cap is what makes chained questions answerable — a second
query informed by what the first returned — while bounding a question the corpus cannot
answer, which would otherwise loop against the one latency-critical path in the system.

**Never fails on a shortfall.** LLM unavailable or out of budget returns the top
citations with a template answer and `synthesized: false`. A caller must always be able
to distinguish a synthesised answer from a fallback.

**Accepted cost:** returning identifiers means a caller cannot verify the answer without
a second call, and synthesis introduces a hallucination surface raw retrieval does not
have. Mandatory per-claim citations, a label that makes an irrelevant citation visible
without fetching it, and the continued availability of `recall` bound the risk without
removing it. This is a deliberate trade of verifiability for context cost, to be
revisited if answers prove unreliable in practice.

See `ask-tool-design.md` for the message shapes, the hop loop, and the latency budget.

### D29 — The LLM is stateless between calls; only a request holds context

No LLM state lives in a service process. Conversational continuity is carried by an
**opaque conversation token**: the service returns a short handle, the prior context
lives in the shared cache under that key with a TTL, and the caller passes the handle
back on the next `ask`.

**Why**, three independent reasons:

1. This system *is* the memory. An LLM keeping its own conversation history creates a
   second, competing store that is not persisted, not audited, and not subject to the
   visibility rules of D12.
2. Session state destroys the content-addressed cache of D17. Caching a generation on
   the hash of its prompt only works when the prompt is fully determined by the question
   and the retrieved context.
3. Session affinity would pin a user to one pod, which is exactly what D23's client-side
   per-request balancing exists to avoid.

Reasons 2 and 3 are why the state lives in the shared cache rather than in a process:
any replica can serve the next turn, so no affinity is needed, and the generation cache
still keys deterministically on the token plus the question.

Reason 1 is the one this concedes. There *is* conversation state — but it is ephemeral
scratch under a TTL, never entering the corpus, never recalled, and never competing with
stored memories.

**Rejected: the caller resends prior turns on each request.** Stateless, but every
follow-up re-pays the full context cost, which is the specific thing `ask` exists to
avoid. Correct for a one-shot tool, wrong for a dialogue-shaped one.

**Consequence:** an expired or evicted token must degrade to being treated as a new
conversation **and say so in the response**. Silently answering without context the
caller believes is present is the one failure mode this must not have. Conversation
state is owned by a user and visibility-scoped like everything else.


### D30 — `ask` behaviour is configuration, and changes to it are gated on measurement

Prompts, hop cap, deadline budget, model selection, and retrieval mix are values read
from configuration, not constants compiled into the service. Changing any of them is a
config change, not a redeploy.

A change is only accepted on evidence. A fixed evaluation set — questions with known
good answers and known correct citations — is run against a candidate configuration and
scored, and the score is what justifies keeping the change.

**Why:** a synthesis layer's quality is not self-evident, and a prompt that reads better
frequently answers worse. Without a measurement harness, tuning is guesswork that
accumulates; with one, a regression is caught by the same mechanism that proved the
improvement. Making the knobs configuration rather than code is what makes iterating
cheap enough to actually do.

**Useful property:** because the generation cache is keyed on a hash that includes the
prompt (D17), changing a prompt changes the key. Old generations cannot leak into a new
configuration's results, and no invalidation step is needed.

**Consequence:** `ask` must log question, hops taken, citations returned, whether the
answer was synthesised, and timing. That log is both the operational signal and the
corpus the evaluation set grows from.

### D31 — Curation learns from the questions asked, never from the answers generated

Questions and citation behaviour feed knowledge curation. Generated answers do not enter
the corpus.

Signals that are used:

- a question that could not be answered, or that exhausted its hops, is a **documented
  gap** — the highest-value input curation can receive, because it is evidence of
  something actually needed rather than something merely stored;
- records cited often are load-bearing and should be treated as such;
- records that match retrieval repeatedly but are never cited are noise candidates.

**Why generated answers are excluded:** feeding synthesis output back in as retrievable
content makes the system learn from itself, and errors compound with nothing to correct
them. This is not hypothetical — the current system already carries a guard excluding
`llm_synthesized` content from synthesis input for exactly this reason. The lesson is
carried forward rather than relearned.

**Consequence:** an answer worth keeping must be written deliberately by a person or an
agent as an ordinary memory or wiki page, passing the same gates as any other write,
and never persisted as a side effect of having been generated.


### D32 — When cited sources disagree, `ask` reports the disagreement rather than resolving it

Where the records supporting an answer conflict, the answer says so, cites both sides,
and does not silently choose between them.

**Why:** picking a winner is the dangerous behaviour. A confident answer synthesised
from contradictory sources hides exactly the thing the caller most needs to know, and
hides it behind fluent prose. Reporting the conflict costs a sentence and converts a
silent wrong answer into an actionable one.

It is also the highest-value moment for the identifiers of D28. "These two records
disagree, here is each one" is directly actionable: the caller resolves the conflict at
the source, and the corpus improves. A resolved-silently answer leaves both records
wrong.

**Consequence:** contradiction detection moves onto the synchronous `ask` path, where it
has a latency cost that the existing background detector does not. It must fit the
deadline of D28 or be skipped for that request — degraded, never blocking.

**Feeds curation (D31):** a detected disagreement is a gap signal in its own right, and
one of the cleanest, because it identifies both records involved.


### D33 — Prompts are files in git, delivered by GitOps, gated on the evaluation set

The prompts an LLM service uses are not compiled into it and are not stored in the
database. They are files in a repository, delivered to the running service as
configuration by the GitOps controller, and reloaded without rebuilding or redeploying
the binary. Changing one is a commit, a review, and a sync.

**Why:** the two requirements — modifiable without a build, yet properly versioned —
conflict unless something bridges them, and GitOps is that bridge and is already a
committed part of this architecture. It gives real versioning: a diff, a review, a
revert.

**The decisive property is that a prompt change gets CI.** D30's evaluation set runs on
the pull request, so a prompt that regresses the score cannot merge. Stored in a
database instead, there is no pull request to gate, and D30 degrades into a convention
people are expected to remember.

**Rejected: storing them in the agent-prompt library.** It versions in the database
rather than in git, so there is no diff, no review and no revert; any caller could change
every user's answers instantly with no gate; and it would make the answering service
depend on the prompt store being reachable, with nothing to bootstrap from on a fresh
install. The library remains correct for what it holds today — dispatch prompts an
instance uses to brief a subagent, which are per-project, change often, and affect only
their caller. The distinguishing property is blast radius, not mechanism.

**The prompt version hash is logged with every answer**, so a change in behaviour is
attributable to a specific revision and evaluation results stay tied to what produced
them. It also makes reloading safe rather than merely convenient: during a rollout two
replicas may briefly run different prompts, but since the generation cache keys on the
prompt hash (D17), those are two distinct entries — a brief mixed period, never a
corrupted one.

### D34 — Prompts resolve most-specific-first: user, then team, then org, then system

A prompt may exist at four levels. The most specific one that exists wins, and the same
name may exist at several levels. System-level prompts are seeded at install and are
immutable; the other three are editable by whoever owns that level.

**This is D12's scope axis read in the opposite direction.** Visibility widens
`PRIVATE → TEAM → ORG`; resolution narrows `USER → TEAM → ORG → SYSTEM`. Stating it
explicitly because an implementation that applies one ordering where the other belongs
looks plausible and is wrong.

**A prompt has two sections, and only one of them is overridable:**

| Section | Overridable |
|---|---|
| Contract — cite every claim, refuse to invent, report disagreement, honour the deadline | never |
| Behaviour — tone, verbosity, framing, formatting | at any of the three editable levels |

The ladder resolves the behaviour section. The service appends the contract section
regardless of what any level overrode. **Why:** if an override could replace the whole
prompt, a single user-level edit silently removes the citation and no-invention
guarantees of D28 and the disagreement reporting of D32. Properties the system
guarantees cannot be one override away from optional.

**Org-level changes are gated; user and team are not.** A user or team prompt affects
only its own scope, so the blast radius is contained and the cost of a bad edit is borne
by whoever made it. An org-level prompt changes every answer in the installation, so it
requires an evaluation run and an audit entry before taking effect, even though it is
not immutable.

### D35 — System-level seeded content is git-controlled, immutable, and exempt from decay

An installation ships with system-level content: prompts, help pages, how-tos,
explanations of the system itself, and anchored memories about them. It is defined in
git and seeded at setup.

**Prompts are immutable by construction.** They live in git and reach the service as
configuration, never entering the database, so there is no write path to abuse — which
is stronger than a flag that something will eventually bypass.

**Seeded memories and wiki pages cannot use that trick**, because they must be in the
store to be retrievable. They therefore require:

- **Deterministic identity derived from the source file**, so re-seeding an updated
  version updates the existing record in place. Without this, every deployment duplicates
  the help corpus.
- **An immutability flag with a seeder-only write path** — one component may set it, and
  no ordinary write may modify a record carrying it.
- **Exemption from decay, vacuum and heat-based eviction.** A how-to that is rarely read
  must not age out. Otherwise the onboarding material of a system nobody has yet
  onboarded to is exactly what gets deleted first.

Seeded content **does** participate in retrieval — surfacing "how do I do this" on demand
is its purpose. It is simply neither deletable nor decayable.


---

## Dependency licences (verified 2026-08-29 against each project's own LICENSE file)

| Dependency | Licence | Notes |
|---|---|---|
| NATS server + JetStream | Apache-2.0 | CNCF. Synadia attempted a BUSL relicence in early 2025; the May 2025 settlement moved the trademarks to the Linux Foundation and the code stayed Apache-2.0. Residual vendor risk: the core maintainers' employer already tried once. |
| `async-nats` | Apache-2.0 | Tracks the server. |
| RabbitMQ | MPL-2.0 | File-level weak copyleft — an unmodified dependency is unencumbered. Broadcom-owned, no foundation. |
| `lapin` / `amqprs` | MIT | Rust AMQP clients, if RabbitMQ is ever used. |
| Valkey | BSD-3-Clause | Linux Foundation. Forked from Redis 7.2.4 after the March 2024 SSPL/RSALv2 change. The cleanest governance of anything here. |
| **SurrealDB core** | **BSL 1.1** | **Not open source today.** Converts to Apache-2.0 on 2030-01-01, or four years after each version's release, whichever is earlier. Offering a "Database Service" requires a commercial licence until then. Company-owned, no foundation. |
| `tonic` | MIT | |
| `prost` | Apache-2.0 | |
| `buf` CLI | Apache-2.0 | The hosted Buf Schema Registry is a separate commercial product; its terms were not verified. Only the CLI is relied on. |
| Linkerd | Apache-2.0 (code) | Since February 2024 free pre-built *stable* binaries are no longer published — edge builds and source only. Buoyant Enterprise is free for organisations of 50 or fewer. Relevant only if D23's mesh option is taken up. |

**Consequence for engine selection:** SurrealDB is the only BSL-encumbered item in this
stack. The project is open source and not commercial, and will be self-hosted by the
organisations that use it — none of which is a "Database Service" offering, so the BSL
restriction does not bite. SurrealDB stays available as an engine, carrying a licence
note. See D24 for the constraint that keeps it from becoming a hard requirement.

---

## Open — not yet decided

- **O1 — Repo names not ratified.** The table above is a draft.
- **O2 — Two levers on the service count**, both preserving repo-per-module and the
  twin rule: (a) crate-fold the tiny write-path modules (astrocyte ~414 LOC, engram,
  prospective ~229 LOC) into `yadgar-write`, keeping their own repos and `-db` twins,
  saving ~5 services; (b) LLM providers as crates rather than six repos.
- ~~O3 — protocol distribution across repos~~ — **RESOLVED by D16**: the shared
  artifact is the `.proto` set, versioned and gated by `buf breaking`.
- ~~O8 — gRPC load balancing under k8s~~ — **RESOLVED by D23**: client-side balancing
  over headless Services; mesh deferred.
- ~~O9 — Broker product~~ — **RESOLVED by D22**: NATS JetStream, licence verified
  Apache-2.0.
- **O11 — Engine selection per module.** Open by design under D7 and bounded by D24.
  Which engine each module defaults to is still unchosen; SurrealDB is permitted but
  must not be the only option.
- ~~O10 — Dependency licence audit~~ — **DONE**, see the licence section above.
  Re-run it before adding any new infrastructure dependency: licences in this space
  have changed repeatedly and prior knowledge is not reliable.
- **O4 — Reverse crossref resolution.** A module owns its outbound edges. "What links
  to this ADR" becomes a fanout, or needs a small graph module owning the edge table.
- **O5 — GitOps layout**: one umbrella chart with per-module subcharts and an
  `enabled` value (easy day one, painful at 20+ modules), versus a chart per repo
  driven by an Argo ApplicationSet (idiomatic, scales past 30 repos).
- **O6 — Domain model.** Not yet laid out. Must carry `version` (D8),
  `idempotency_key` (D9), and `team_id`/`visibility` (D12) from the first migration.
- **O7 — Dangling references when a plugin is off.** Straw proposal: references become
  URNs (`yadgar:adr:0466`); the owner resolves its own; a disabled module resolves to
  `unresolved(plugin_off)` so a renderer shows a dead link rather than a lie. No
  cascade, no orphan cleanup. Not ratified.
