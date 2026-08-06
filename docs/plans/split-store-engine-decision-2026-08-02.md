# Split-store engine decision — 2026-08-02

**Status:** decision doc. Supersedes the "does the relational set leave SurrealDB"
half of task 0119.

**Update 2026-08-06:** every open question in §8 is now decided — see ADR-0199 through
ADR-0213, cited inline against each item below. The gate this doc originally stated
("§8.A blocks any code") is **LIFTED**. §8's tables are kept as the historical record of what
was asked; they are not reopened by keeping them.

**Decided by the user, not open for re-litigation in this doc:** the backend runs **two
engines**. SurrealDB keeps graph, memory, wiki bodies and embeddings. A second SQL engine
takes the relational set. PostgreSQL is ruled out. Embedded-in-process SQLite is ruled out.

What *is* open, and what this doc exists to resolve: **which** SQL engine, and **what the
split costs**. §6 records the counter-case for deferring, because a decision doc that cannot
state its own downside is not evidence.

---

## 0. TL;DR

1. **The cost of the split is not in the tables. It is in the operational paths.** The
   relational set is ~845 LOC of ordinary CRUD with nothing to migrate. Backup consistency,
   the restore verification gate, migrations and `check_invariants` are where the bill lands.
2. **One question picks the engine:** does Alembic-grade migration tooling matter enough to
   constrain the choice? Yes → MariaDB. No → rqlite. §4.4.
3. **Identity is not a blocker** (per the user's rule: the engine driver owns it).
   **Superseded 2026-08-04 by ADR-0197:** the nuance below no longer survives — the
   `AUTO_INCREMENT` surrogate key *is* the semantic `ADR-NNNN`/task number, with no separate
   `number` column and no `MAX+1 FOR UPDATE` allocation. §5.1.
4. **Backup expands rather than ports.** Engine #2 leaves the vacuum pipeline entirely —
   InnoDB does not need it. What survives is the cross-engine *quiesce point*. §5.2.
5. **Two findings weaken specific motivations** without touching the decision: SurrealDB
   already has FULLTEXT/BM25 in server mode (§6.3), and `runtime_config` is empty (§1.4).

---

## 1. What the relational set actually is

Measured 2026-08-02 against the live daemon. Every prior estimate in the spine and knob
plans is stale; the numbers below replace them.

### 1.1 It is mostly not tables yet

| Kind | Stored as today | Cardinality |
|---|---|---|
| Tasks | one markdown wiki page, slug `{project}-task-list`, `page_type="task_list"` | 6 pages DB-wide; `yadgar-task-list` = **16,060 chars** |
| ADRs | one wiki page per ADR + one markdown-table index page | **193** index rows vs **194** per-ADR pages — see §1.3 |
| Agent-prompts | one wiki page per pattern + a TOC page | 57 `agent-prompt-*` + 6 `agent-discipline-*` |
| Knobs | a real SurrealDB table (`migrations.py:340`) | **0 rows** |

There is **no `task` table** in `migrations.py`. There is **no `_LedgerMixin`**, despite
ADR-0183 D30 naming it "the engine seam." The relational tables that exist today are
`runtime_config`, `memory_block` and `wiki_bookmark` — **845 LOC** of mixin, no vectors, no
graph edges, no FTS.

**Consequence:** there is no legacy relational corpus to migrate. The split is applied at
birth, where ADR-0183 already said portability discipline costs nothing.

### 1.2 The plans' numbers are wrong

- Spine plan says the task page is **70,341 chars**. It is **16,060** — it shrank ~77% when
  the completed tasks were culled. The plan's token-cost impact is roughly **4× overstated**.
- Spine plan and prior ADR text say ~181–183 ADRs, 11 superseded. Actual: **193–195** pages,
  **12** superseded.

Both must be restated before either plan is built against.

### 1.3 ADR index drift is not hypothetical — it already happened

The spine plan frames "the index write can lag the page write, so `_next_adr_id` cannot
trust it" as a risk. It is a **live defect**: `ADR-0124` has a page and **no index row**
(193 index rows vs 194 per-ADR pages, confirmed by query).

A query on `page_type='adr'` returns **195**, which is *not* a third discrepancy: it is the
194 per-ADR pages plus `yadgar-adr-index` itself, which also carries `page_type='adr'`. Any
count assertion on `page_type='adr'` must exclude the index by slug predicate — otherwise the
migration's verification gate is off by one against a corpus where being off by one is
exactly the bug it is checking for.

`adr_add` (`adr.py:143-291`) does two sequential non-atomic writes — page, then index —
under a *process-local* lock, and computes the next ID by scanning
`wiki_list(slug_prefix=..., limit=10000)` on **every call**. The index page (32,163 chars)
is regex-parsed on every `adr_list` **and** re-parsed in `project.py:1866` `_build_adr_log`
on every `project_brief`.

This is the single strongest present-cost argument for moving ADR *metadata* to a table.

### 1.4 The knob table is empty

`config_list()` → `[]`. `db_inspect` → 0 rows. Even `code_graph.enabled` has no row.

The kind carrying the most infrastructure — a table, an index, four MCP tools, the PTC
read-through cache, a host client and CLI surface — holds **no data**. Migrating it is a
schema move with nothing to move.

**This is time-sensitive.** Task 0095 (project identity key scheme) notes the re-key is free
*only while `runtime_config` is empty*. That window is open right now and closes the moment
task 0035 seeds its first row. Decide 0095 before seeding, or pay a migration to change it
later.

---

## 2. The cost is in four operational paths, not the tables

Two investigations produced apparently opposite verdicts. They were pricing different things,
and reconciling them is the core of this doc.

- Pricing **migration**: near zero. Nothing to move, tables born portable, 845 LOC of CRUD.
- Pricing **operations**: non-zero and data-loss-adjacent on every line.

Both are correct. The bill is here:

| Path | Entry point | What engine #2 changes |
|---|---|---|
| **Backup** | `core/backup/backup.py:65,147,187` | Expands — see §5.2. Native SQL backup replaces the export/swap shape. **Cross-engine quiesce point is new.** |
| **Restore verification** | `core/vacuum/__init__.py:99-104` | The gate requires **exact per-table equality**. Tables not enumerated are silently outside the data-loss guard. |
| **Migrations** | `_shared/storage/migrations.py:1175` | Own version table + own ordered list, or the two drift. Called from `StorageEngine.__init__`. |
| **`check_invariants`** | `backend/admin_exec/invariants.py` | Cross-engine referential checks are **new code**, not a port. Degrades slowly — the worst failure mode. |

**Why the verification gate is non-negotiable:** on 2026-06-16 a vacuum bug destroyed **3,622
memories** because a partial restore (1,484 of 3,622) passed a `>=` check. The gate now
demands exact equality. A table outside its enumeration is outside the guard, silently. That
incident is the reason these four arms land in the **same commit** as the first engine-#2
row, not after it.

---

## 3. What the split does *not* cost

Measured, not assumed. Each of these was a plausible objection that the evidence kills:

- **Cross-store joins.** Already application-side — recall fetches candidate ids then
  hydrates rows (ADR-0183). Nothing to preserve.
- **`source_memory_ids`.** Effectively dead: **2 populated rows out of 2,213** DB-wide, and
  **none on ADRs** (hardcoded `None`). Zero cost.
- **Foreign keys.** None enforced anywhere. Crossrefs are cleaned on delete; no constraints
  exist. Zero cost.
- **`[[slug]]` crossrefs and embedding reachability.** 52 crossref rows touch `-adr-`,
  including task-list→ADR edges, and ADR/task pages are embedded and recall-visible by
  default policy. **Cost is zero if bodies stay in Surreal** and only index/metadata moves.
  It becomes severe if bodies move too.

**That last conditional is the whole design.** Metadata to SQL, prose bodies stay in Surreal
with their embeddings. It is also what ADR-0182 already decided.

- **Footprint.** Not a discriminator. Against a 3.2 GB backend image, the candidates add
  0.7%–3.4%. Live Surreal RSS is 455 MB (the only verified RSS figure; every candidate's is
  unverified and should be measured before it decides anything).

---

## 4. Engine options

### 4.1 The two axes the SQLite objection spans

The stated objection — *"sqlite is not a proper choice for loose coupling and future
microservice structure"* — is a **deployment-topology** constraint, not a dialect one:

| Axis | Rules out |
|---|---|
| **Topology** — must be a network-listening server process | embedded SQLite, DuckDB, pglite, H2/HSQLDB embedded |
| **Dialect** — SQLite vs MySQL vs Postgres | nothing on its own |

So SQLite-dialect-over-a-network is not excluded by the objection as stated. It is excluded
anyway, by health — see §4.2.

### 4.2 The SQLite-as-real-server branch is effectively dead

Verified live 2026-08-02:

- `libsql-server` (sqld) — last release **2025-02-14**; last code commit under
  `libsql-server/` **2025-12-19**; only a doc typo since. Not archived, MIT. The README says
  *"new features are being developed in Turso"* and points new projects there.
- The `tursodatabase` org has **no self-hostable server repo**. `turso-cli` is a CLI for
  Turso **Cloud**; `pyturso`'s remote mode is embedded-replica sync against that cloud, not a
  client for a self-hosted daemon.
- Turso Database (the Rust rewrite) is pre-1.0, its Postgres wire frontend is self-described
  **experimental**, and its primary shape is in-process.

The vendor's product is the cloud; the self-hostable server is the part they stopped
developing. Revisit in 6–12 months.

### 4.3 Shortlist

| Axis | **MariaDB** | **rqlite** | **Dolt** |
|---|---|---|---|
| License (verified) | GPL-2.0 | MIT (since 2014) | Apache-2.0 |
| Latest | rolling, pushed 2026-08-02 | v10.2.7, 2026-07-06 | v2.2.3, 2026-07-30 |
| Bus factor | very high (Foundation + plc) | ⚠️ **effectively one** (P. O'Toole, incl. both Python libs) | company-backed, single-company |
| Image, compressed amd64 | 107.7 MB | **24.5 MB** | 44.0 MB (tarball) |
| Async Python driver | `asyncmy` 0.2.11 (2026-01-15), CI-tests MariaDB | ⚠️ either-or, see below | inherits MySQL wire |
| Alembic | ✅ out of the box (`mysql+asyncmy://`) | ⚠️ either-or | ✅ |
| FTS | native `FULLTEXT` | FTS5 compiled in | ⚠️ "basic functionality only" |
| Backup | `mariadb-dump` logical, `mariadb-backup` physical hot + **incremental via LSN** | `GET /db/backup` → plain SQLite file; S3 auto-backup skipping unchanged | `dolt dump`/`backup` + git-like push |
| Replication | binlog, semi-sync, Galera | **Raft, day-1** | clone/merge + binlog |

Reference point: `surrealdb/surrealdb:v3.2.3` = 59.5 MB compressed, 455 MB live RSS.

**Dismissed with reason:** MySQL (Oracle dual-license; MariaDB dominates it on every axis
here) · Firebird (GitHub reports `license: null`; `firebird-driver` 2.0.3 is **sync-only**;
1,886 open issues on 1.4k stars) · DoltgreSQL (strictly less mature than Dolt for the same
idea) · DuckDB, pglite, H2/HSQLDB (**topology** — the exact objection that killed embedded
SQLite) · CockroachDB, TiDB, YugabyteDB (cluster-first; disqualifying for a solo install) ·
ClickHouse (OLAP columnar, wrong shape for ~250 rows).

### 4.4 The discriminating question

**Does Alembic-grade migration tooling matter enough to constrain the engine choice?**

- **Yes → MariaDB (or Dolt).** `mysql+asyncmy://` is a first-class SQLAlchemy 2.0 async
  dialect, so Alembic works out of the box. This **moots task 0051 (surrealmigrate fork)
  outright and collapses most of 0048**. The 2026-07-12 build-vs-buy audit's `KEEP-CUSTOM`
  verdict was reasoned on *"Alembic = SQLAlchemy-only … no SurrealQL migration framework
  exists in Python"* — a real SQL engine flips that premise. **This is the only argument for
  the split that is about present cost rather than future shape.**
- **No → rqlite.** Smallest image, single MIT binary, FTS5 built in, best backup story on the
  list, Raft replication day-1. But its Python stack (`pyrqlite`, `sqlalchemy-rqlite`) is
  **sync-only** and effectively single-maintainer, forcing an either-or with no third option:
  drive its HTTP/JSON API with the `httpx` the backend already has (async, zero new driver,
  **no Alembic** — 0051 survives), or take the sync driver for Alembic and eat a threadpool
  hop per query in an async backend.

**Recommendation: MariaDB**, on the strength of the migration-tooling saving and the bus
factor. rqlite is the better *fit* to the stated constraints and would be the pick if
migrations were already solved.

**Biggest risk of the recommendation:** MariaDB is a full RDBMS for ~55 task rows, ~195 ADRs
and an empty knob table — the most over-provisioned choice on the list. **Disqualifying
evidence would be** a measured idle RSS that materially degrades the 4 GB-capped backend
container (the embed service already sits at ~1.3 GB RSS). Measure before committing.

### 4.5 DECIDED — MariaDB (user, 2026-08-02)

Rationale given: maturity, and expected resource behaviour. §4.4's discriminator resolves in
its favour, so **task 0051 (surrealmigrate fork) is mooted and most of 0048 collapses into
"adopt Alembic"**.

One correction to the stated rationale, recorded so it is not carried forward as fact:
MariaDB is the **heaviest** of the three shortlisted (107.7 MB compressed vs rqlite's
24.5 MB; default `innodb_buffer_pool_size` = 128 MiB). In context that is immaterial — 3.4%
of a 3.2 GB image — and the maturity and Alembic arguments carry the decision on their own.
But it is not the light option, and **idle RSS remains unverified**. Measure before the first
row lands (§4.4's disqualifying condition still stands).

#### License — Apache-2.0 (yadgar) vs GPL-2.0 (MariaDB server): no conflict

Yadgar is Apache-2.0 (`pyproject.toml:10`, `LICENSE`). MariaDB server is GPL-2.0. Apache-2.0
and GPLv2 are genuinely incompatible **for combining into a single work** — but that is not
what happens here. Yadgar speaks the MySQL wire protocol to a **separate process**. No GPL
code is linked in; yadgar's Apache-2.0 licensing is unaffected.

Two conditions this conclusion depends on:

1. **The client must stay pure-Python.** `asyncmy` (PyMySQL-derived) reimplements the wire
   protocol and does not link `libmariadb`. Substituting **MariaDB Connector/C** (LGPL-2.1)
   or **MySQL Connector/Python** (GPL + FOSS exception) changes the analysis. ⚠️ `asyncmy`'s
   own license is **unverified** — confirm from its repo before the first commit.
2. **Image distribution carries a source-offer obligation.** `openfantasy/yadgar-backend` is
   published to Docker Hub; bundling the MariaDB binary means distributing GPL-2.0 software
   (GPL-2.0 §3). Yadgar's own code is protected by the mere-aggregation clause (§2, final
   paragraph), but the MariaDB layer needs a `NOTICE`/third-party entry pointing at upstream
   source.

Note this obligation class is **not new** — the image already does
`COPY --from=surrealdb/surrealdb:v3.1.5`. ⚠️ **Unverified and worth checking independently:**
SurrealDB's current license may be BSL 1.1 rather than a standard OSI license, which would be
a larger pre-existing exposure than the GPL-2.0 one being added here. Confirm from upstream.

---

## 5. Day-one design points

### 5.1 Identity — the engine owns it

**User's rule, binding:** the driver's job. Application code uses only `INSERT` / `SELECT` /
`UPDATE` / `DELETE`, and does not manage indexes or sequences.

This **dissolves** the cross-engine round-trip concern. `_next_id`
(`_shared/storage/client.py:437`, `UPSERT counter:{table} SET val = (val ?? 0) + 1`) stays a
Surreal-only mechanism. Engine-#2 tables use the engine's native identity column.

**RETIRED 2026-08-04 by ADR-0197 — read this before the paragraph below, not after.** The
mechanism this section originally proposed — `SELECT MAX(number)+1 … FOR UPDATE` allocating a
separate semantic `number` column — **must not be built**. It does not exist anywhere in the
implementation and citing this section as if it does is exactly the drift ADR-0197 exists to
correct. **What is actually decided:** the `AUTO_INCREMENT` primary key itself **is** the
semantic ADR/task number. There is no separate `number` column and no allocation step. The
existing corpus renumbers at seed time (one known gap, `ADR-0124`, so everything from `0125`
shifts down by one) — accepted, since nothing downstream resolves an ADR by the number printed
in old prose: `adr_get` and every crossref resolve by row id / `body_slug`. This still **fixes
§1.3's live drift**, because the number and the row are the same write by construction, not
because of a `MAX+1` transaction.

~~The one nuance the rule does not cover: `ADR-0194` is a *semantic, user-visible* number, not
a surrogate key. `AUTO_INCREMENT` is per-table, not per-project, and burns values on rollback —
so it cannot be the ADR number. With a real engine this is easy and stays inside the four
verbs: `SELECT MAX(number)+1 … FOR UPDATE` inside a transaction, per project. That also fixes
§1.3's live drift, because the number and the row become one atomic write instead of two.~~
**Kept struck through, not deleted, because the bootstrap plan cites this section as binding —
a reader following that citation must land on the retirement notice above, not silently rebuild
a mechanism ADR-0197 already killed.**

### 5.2 Backup — expand, do not port

Engine #2 **leaves the vacuum pipeline entirely.** The export → side-build → atomic-swap
machinery exists because SurrealKV never GCs dead row-versions. InnoDB does not have that
problem and needs no equivalent. Do not build a second vacuum arm.

Use the engine's native procedures instead: `mariadb-backup` gives full / incremental (via
LSN) / differential hot backups — a capability the Surreal path does not have at all.

**What survives, and it is the real work:** the **cross-engine quiesce point**. Restoring a
Surreal snapshot from 03:00 alongside a SQL snapshot from 03:05 yields rows referencing
memories that do not exist. Someone must decide the ordering and the reconciliation, and
`check_invariants` must police it. This is ADR-0183's predicted "orphaned rows as a permanent
background class," transposed from vectors to task/ADR rows.

### 5.3 License compliance — required artifacts, with a ratchet

**This must ship with the feature, not after it.** Publishing
`openfantasy/yadgar-backend` with a bundled GPL-2.0 binary creates a distribution obligation
the moment the image is pushed. A prose reminder in a plan doc rots; this repo's working
pattern is a ratchet script, so use one.

**Deliverable 1 — `NOTICE` / `THIRD-PARTY-LICENSES.md` at the repo root.** One entry per
third-party binary or bundled artifact shipped inside a published image. Each entry carries:
component name · version · license · upstream source URL · how the source offer is
discharged. Apache-2.0 §4(d) also makes a `NOTICE` file the conventional home for this, so
one artifact satisfies both yadgar's own license and the GPL-2.0 obligation.

Minimum entries at the time of writing:

| Component | Where bundled | License | Obligation |
|---|---|---|---|
| MariaDB server | `Dockerfile.backend` (new) | GPL-2.0 | source offer → upstream URL |
| SurrealDB | `COPY --from=surrealdb/surrealdb:v3.1.5` (**already shipping**) | ⚠️ **verify** — may be BSL 1.1 | TBD once verified |
| `asyncmy` | Python dep | ⚠️ **unverified** | must be permissive; see §4.5 |

**⚠️ The SurrealDB row is a pre-existing exposure, not a new one.** The image has bundled it
for many releases with no NOTICE entry. If its license is BSL 1.1 rather than an OSI license,
that is a larger question than the GPL-2.0 one being added here, and it is *already live in
shipped images*. Audit it in the same pass.

**Deliverable 2 — `scripts/check_third_party_notices.py`, wired into `.pre-commit-config.yaml`
and both CI mirrors.** It fails when a bundled component has no NOTICE entry. Concretely:
parse every `COPY --from=<image>` and package-install line in `Dockerfile*`, parse the
component table in `NOTICE`, and hard-fail on any bundled component missing an entry, or any
entry whose named version no longer matches what the Dockerfile pins.

Model it on the existing liveness ratchets — `scripts/check_registry_prose_liveness.py` and
`scripts/check_model_id_liveness.py` — which already follow exactly this
"artifact-vs-declaration drift" shape and are wired at `.pre-commit-config.yaml:195` and
`:206`.

**Gate:** the ratchet must be green *before* the first image containing MariaDB is pushed.
Not before merge of the code — before **publish**, since publishing is what triggers the
obligation.

### 5.4 Deployment constraints that must hold

- **ADR-0078 / anchor #33:** only backend functions touch the DB; core is HTTP-forward only.
  Engine #2 therefore lives **inside the `yadgar-backend` container**, reachable only from
  backend code, never exposed to core or the host beyond a `127.0.0.1` debug bind. All three
  shortlist candidates satisfy this — they are single-process daemons.
- **`docker-compose.yml` needs a real change:** the backend mounts `yadgar-db-data:/data:ro`
  with `read_only: true` and only `/tmp` as tmpfs. Engine #2 needs a **writable** data path.
  That is a security-posture change, not a cosmetic one.
- **`entrypoint-backend.sh` `cleanup()`** is a strictly ordered writers-first stop
  (`_stop_writers` → `_stop_surreal_and_wait`, `SURREAL_STOP_DEADLINE=25` deliberately under
  podman's `--stop-timeout 30`). Engine #2 needs its own position in that ordering and its
  own exit-status check. Note this is *sequencing*, not a second corruption class — SurrealKV
  fragility (ADR-0090: *"every stop is a corruption dice roll"*) is a SurrealKV property;
  InnoDB and Raft-log-plus-fsync both survive an ungraceful stop far better.
- **New secrets** in the `:?`-guarded compose env block, `/etc/yadgar/secrets.env`, the nix
  module, and every unit generator. **Task 0122 already records 4–5 divergent unit
  renderers**, one of them out-of-repo (`modules/home/yadgar.nix`) that nothing in the repo
  can test. Every one needs the new env and volume, consistently.
- **Install is this repo's worst surface** — 65 of 203 audited defects were
  install/packaging. Every bullet above lands on exactly that surface. This is the honest
  operational price of the split.

---

## 6. The case for deferring

Recorded because the decision should be taken with its downside visible, not because it
reopens the decision.

**6.1 ADR-0183 already chose the cheap version.** Accepted 2026-07-30: split the seam into
`LedgerStore` and `VectorStore` **as interfaces, with one implementation backing both**, and
explicitly *"an INTERFACE, not a query compiler."* That makes the split deferrable at
near-zero cost — build `LedgerStore` on Surreal now, write the second implementation when the
need is concrete, keep single-store transactions meanwhile.

**6.2 There is no performance case.** Zero knob rows, ~55 tasks, ~195 ADRs. Nothing is slow;
no table is large; a single `SELECT` scans the lot without an index. The case for the split
is architectural and topological, and is stronger stated as such than dressed as an
engineering need.

**6.3 The FTS motivation is testable today with zero new engines.** SurrealDB already has
FULLTEXT/BM25 **in server mode** — the v5.58 fix guarded `DEFINE INDEX … FULLTEXT` to server
mode precisely because embedded v2 lacked it, and prod runs server mode.

Note this weakens the *ADR-retrieval* motivation specifically, and the two must not travel
together: ADR **metadata** to SQL kills the 32 KB regex parse and the two-write drift — keep
it. ADR **retrieval** leaving embeddings is separately unmotivated (embedding cost is one
`all-MiniLM-L6-v2` call per page over 195 rows, backfilled automatically) and **unmeasurable**
— `golden_set.jsonl` has 43 rows with zero ADR coverage, LongMemEval is general chat memory,
and a cross-encoder **reranks, it never retrieves**, so a concept query sharing no lexical
terms with an abbreviation-heavy ADR title never enters the candidate set for the CE to
rescue.

**6.4 The seam this would justify has no consumers.** `StorageProtocol`
(`_shared/contracts/protocols.py:167-218`) declares a 15-method read-side Protocol whose
docstring claims the retrieval pipeline depends on it. **Nothing imports it.** It is also
read-only — no write methods — so it could not carry engine #2 even if wired. Its siblings
`MLClientProtocol` and `CacheProtocol` *are* genuinely wired at
`_shared/runtime/lifecycle.init_engines`. Direct evidence that a speculative protocol in this
position goes unused.

---

## 7. What this decides for the two plans

| Plan | Change |
|---|---|
| **Spine** (`task-table-refactor-2026-07-29.md`) | Restate stale numbers (§1.2). Promote ADR index drift from risk to live defect (§1.3). Identity per §5.1 — engine-owned PK, `MAX+1 FOR UPDATE` for the semantic ADR number. D7 archive-never-delete survives, because a table makes closed rows free by `SELECT`ing open ones. Bodies stay in Surreal — non-negotiable per §3. |
| **Knob** (`settings-to-db-config-migration-2026-07-24.md`) | Phase ordering rests on a false premise — the store is **empty** (§1.4). The REFACTOR classification exists only because the backend has no PTC; chained caches replace that workaround. Correct the "N HTTP round-trips per decay batch" framing: it is a hazard of the *proposed* migration, not a present defect — `heat_decay.py` hoists config outside its loops today. |
| **0098** (dialect seam) | Rescope. As written ("make SurrealDB swappable") it is 9k–12k LOC, ~60% judgment, against a corpus this decision does not need touched. Scoped to "let the relational set address engine #2" it is 1.8k–2.2k LOC. Two concrete storage classes with matching method names, selected at the existing composition root — no ABC, per §6.4. |
| **0051 / 0048** | **0051 mooted, most of 0048 collapses into "adopt Alembic"** — MariaDB is MySQL-wire, so `mysql+asyncmy://` gives Alembic out of the box (§4.5). Close 0051 once the first migration runs against the real engine, not before. |
| **0095** (project identity key) | **Blocking and time-boxed.** Free to re-key only while `runtime_config` is empty. Decide before task 0035 seeds a row. |
| **NEW — third-party license notices** | `NOTICE` + `scripts/check_third_party_notices.py` ratchet (§5.3, §8.1). Gate is **publish**, not merge. Covers the pre-existing unaudited SurrealDB bundle as well as MariaDB. File as its own task. |

---

## 8. Decision register — every open question across all three plans

Consolidated 2026-08-02 so the whole decision surface can be reviewed in one place. Each
entry names its source doc; the plans keep their own detail. **ALL ANSWERED 2026-08-04–06 —
see the ADR column added to each table below.** The tables are kept verbatim as the historical
record of what was asked; only the resolution is new.

### 8.A ANSWERED — was BLOCKING, gate now LIFTED

| # | Question | Source | Why it blocks | Decided |
|---|---|---|---|---|
| A1 | **Project identity key scheme (task 0095).** Three schemes coexist: `project_id` = `owner/repo`, a basename-derived `body_slug`, and the config store's absolute filesystem path. | decision §1.4 · spine D32 · knob "Blocking before any seed" | **Time-boxed.** Re-key is free only while `runtime_config` is empty (0 rows, verified). Task 0035 seeding one row closes the window. Spine's refinement: the gate is the first `config_set`, **not** the schema — so a zero-row schema-only pilot may proceed with 0095 open. **Confirm that reading.** | **ADR-0199** — `owner/repo`, host excluded, `local/<basename>` fallback; resolved once per session, never re-derived per write. Slug mechanics in **ADR-0202**. |
| A2 | **Widen the config write path to accept `float`.** `_JSON_VALUE_TYPES` (`tools/runtime_config.py:52`) excludes it; 88 of 344 Settings fields are float — the entire stated Batch 1. | knob §0.3 | One-line fix, but Phase 1 cannot start without it. The alternative — string-encoding at ~145 call sites — reintroduces the phantom-knob class and should be **rejected explicitly**, not left open. | **ADR-0207** — widen `_JSON_VALUE_TYPES` to include `float`, no coercion layer. String-encoding rejected explicitly. |
| A3 | **Does the knob plan own the engine-#2 operational bootstrap?** Its Phase 0.9 currently carries backup, restore-verification, Alembic and cross-engine `check_invariants`. | knob §G | That is far larger than "knob migration" implies. Three alternatives are listed in the knob doc. Scope must be settled before sequencing. | **ADR-0203** — the bootstrap becomes its own plan/train, config table schema-only + zero rows. **Amended by ADR-0210:** the "own train" packaging is overridden by later user direction — it ships as cars of the combined strict-typing train instead; everything else in ADR-0203 (four-arms-together, schema-only) still stands. |

### 8.B ANSWERED — needed before the first engine-#2 row lands

| # | Question | Source | Note | Decided |
|---|---|---|---|---|
| B1 | **Cross-engine quiesce point for backup.** Both engines stopped, or a Surreal snapshot taken while SQL holds a read view? | decision §5.2 · ADR-0196 | A Surreal snapshot at 03:00 + a SQL snapshot at 03:05 restores rows referencing memories that do not exist. | **ADR-0204** — hold the existing maintenance write-gate across both snapshots (assert, drain, snapshot MariaDB, snapshot Surreal, release), release-on-abort mandatory. **Amended by ADR-0210:** its "reads stay available" claim is FALSE and withdrawn — the gate short-circuits every MCP tool, so the window is a full outage, accepted; backup runs as a nightly step, not an independent schedule, and hard-fails without the gate. **Further amended by ADR-0211:** withdraws ADR-0210's own gate-primitive-rewrite clause (nested TTL takes the min) as a misreading of working code — nesting semantics are unchanged; the enter response gains a `deadline_seconds` field instead. |
| B2 | **The ADR two-write drift is RELOCATED, not eliminated.** `adr_add` goes from page-then-index (both Surreal) to page-then-row (Surreal, then MariaDB) — same two writes, now across an engine boundary with no transaction. | spine §12 | The spine specifies row-last ordering plus a `check_invariants` cross-engine check, but this is ADR-0183's predicted orphan class **arriving early**. Deserves an explicit call rather than an author's mitigation. | **ADR-0201** — writes go row-first (amends ADR-0198); ADR page bodies are prose-only, all metadata lives on the row. |
| B3 | **`body_slug` stays basename-derived**, with two live DB-wide collision surfaces: `get_wiki_page_by_slug` (`_shared/storage/wiki.py:380`, slug-only) and UNIQUE `wiki_bookmark_slug_idx` (`migrations.py:190,1404`). | spine D32 · §12 | Deferring to 0095 is the author's call. Confirm, or accept a wiki-corpus re-slug as separate scope. | **ADR-0202** — slug is `owner_repo_kind-id` (opaque, immutable, `/`→`_` over the whole path, lowercase); `project_id` resolves once per session and travels as a caller parameter thereafter. |
| B4 | **Does the knob store move in this train (task 0119)?** | spine §12 | Determines whether Car A needs a `runtime_config` revision ahead of the ledger tables. | **ADR-0203**, same as A3 — `config` table ships schema-only, zero rows, as part of the bootstrap. **Amended by ADR-0210:** ships inside the strict-typing train, not a separate one. |

### 8.C VERIFIED — cheap, and each one could have invalidated a decision

| # | Item | Source | If it comes back wrong | Verified |
|---|---|---|---|---|
| C1 | `asyncmy`'s own license | decision §4.5 | Breaks the Apache-2.0/GPL-2.0 no-conflict analysis; a GPL/LGPL connector changes everything. | **ADR-0205** — `asyncmy` 0.2.11 is Apache-2.0, no conflict. **Re-verify per ADR-0212:** the dependency later resolved to 0.2.13 — confirm the license carried before relying on it again. |
| C2 | MariaDB idle RSS in the 4 GB backend container (embed already ~1.3 GB) | decision §4.5 | §4.4's stated disqualifying condition for MariaDB. | **ADR-0205** — measured 86.6 MB (podman stats) / 119 MB summed VmRSS, ~2% of the cap. §4.4's disqualifying condition is NOT met. |
| C3 | **SurrealDB's current license** — possibly BSL 1.1 | decision §5.3 | Not blocked on the split; **live in already-published images today**. Larger pre-existing exposure than the GPL-2.0 one being added. | **ADR-0205** — confirmed BSL 1.1 (Change Date 2030-01-01, Additional Use Grant bars use "as a Database Service"). NOTICE gap is real and already live in shipped images (task 0132). The SaaS-tier question is explicitly DEFERRED, not resolved. |
| C4 | Read frequency of the ~145 CHEAP-classified knobs | knob §F | The classification is reasoned, not measured. The knob plan argues the mitigation is cheaper than the measurement — confirm or reject. | **ADR-0205** — accepted UNMEASURED: the mitigation (hoist / cache) is correct regardless of the count, so measuring 145 knobs to produce a number nobody would act on was rejected as effort better spent elsewhere. |

### 8.D DECIDED — was scope, not urgent

| # | Question | Source | Decided |
|---|---|---|---|
| D1 | Test the FTS-prefilter hypothesis **inside SurrealDB** first (it already has FULLTEXT/BM25 in server mode), or drop the ADR-retrieval change and keep only the metadata move? Dropping ADR embeddings is currently **unmeasurable** — `golden_set.jsonl` has 43 rows with zero ADR coverage. | decision §6.3 | **ADR-0206** — the FTS-prefilter hypothesis is dropped; ADR embeddings are kept as-is. Reframed as two access modes: intentional lookup (tasks/prompts/knobs/an ADR by slug) never enters recall at all; ambient recall carries ADRs alone, with superseded ADRs down-weighted, never excluded. |
| D2 | Per-knob **global-vs-directory-scoped** decision. `WIKI_SIM_CONTENT_THRESHOLD`'s consumers are split between dir-aware (`tools/wiki.py:986`) and dir-blind (`queue_drainer/dlq.py:316`), so a per-dir override behaves inconsistently. | knob §D | **ADR-0207** — ALL KNOBS GLOBAL, no per-project override (follows ADR-0198's removal of the `directory` column; closes a real NULL-uniqueness hole in the unique index). |
| D3 | The agent-prompt `uses` column ships **with a reader**, or the write-only counter is deleted rather than migrated. | spine D40 | **ADR-0207** — ships with a reader; it is the evidence base for task 0015's prompt-pruning question. |

### 8.E Defects surfaced in passing — file, don't decide

Not part of the split; found while investigating it.

- `agent-prompt-toc` has `page_type = null` → `DEFAULT_POLICY` include, so the TOC is
  recall-visible while every page it indexes is excluded.
- `wiki_append_section` (`tools/wiki.py:1206`) enforces **no** content-size cap, while
  `wiki_write_task_list` and `wiki_add` both reject at 65,536 — and it is the path the
  stop-hook template recommends for task-list edits.
- `wiki_read` / `wiki_get` are completely uncapped (task 0085's cap covers `recall()` only).
- The agent-prompt usage counter is write-only — `get_prompt_usage_counts` has no caller
  outside its own incrementer.
- `HOPFIELD_MAX_PATTERNS` is on the knob plan's DEAD list but has a live reader at
  `_shared/contracts/engram.py:26` — the whole dead list needs re-verification before any
  delete PR.

### 8.1 Definition of done for the license work

- [ ] `NOTICE` / `THIRD-PARTY-LICENSES.md` exists at the repo root with an entry per bundled
      component (MariaDB, SurrealDB, and any other `COPY --from` artifact).
- [ ] `asyncmy`'s license verified from its repo and recorded.
- [ ] SurrealDB's current license verified from upstream and recorded — including whether the
      **already-published** images need a retroactive NOTICE.
- [ ] `scripts/check_third_party_notices.py` written, wired into `.pre-commit-config.yaml`
      and both CI mirrors, and **red before green** (prove it fails on a missing entry).
- [ ] Ratchet green before the first image containing MariaDB is **published**.

---

## 9. Evidence

All findings dated 2026-08-02, from four read-only investigations against
`feat/v5.172-bug-train` and the live daemon. Licenses, release dates and image sizes verified
from GitHub API / raw LICENSE / Docker Hub API on that date rather than asserted from
training data. Every unverified figure is marked as such in §4.3 and §3.
