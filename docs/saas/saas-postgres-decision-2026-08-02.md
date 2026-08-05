# SaaS SQL Engine: Postgres (decided 2026-08-02)

**Status:** DECIDED by user, 2026-08-02. Replaces the MariaDB decision in
`docs/plans/split-store-engine-decision-2026-08-02.md` §4.5 for the SaaS tier.
**Scope:** the SaaS + team deployment tiers. Solo stays SQLite (embedded,
file-based — no server process, no engine decision needed).

---

## 1. The decision

**Postgres is the SQL engine for the SaaS and team tiers.** MariaDB, which
the split-store decision (§4.5) chose for the relational set, is replaced by
Postgres for the SaaS architecture.

This does NOT amend the split-store decision's in-flight work on the current
Python yadgar — that decision (MariaDB for the current monolith's relational
set) stands for the current codebase. This decision is for the **new
clean-slate SaaS rewrite**, where the db-per-service addendum splits the
relational set into 10 service-owned databases.

---

## 2. Why Postgres over MariaDB for SaaS

| Factor | Postgres | MariaDB | Verdict |
|---|---|---|---|
| **Row-Level Security (RLS)** | native, mature — `CREATE POLICY` per tenant, enforced at the engine | no native RLS — isolation is application-code-only | **Postgres wins decisively for multi-tenant SaaS** |
| Tenant isolation enforcement | RLS policies on every table: `USING (tenant_id = current_setting('app.tenant_id')::uuid)` — enforced even if a service forgets to filter | application must remember `WHERE tenant_id = ?` on every query — one missed query leaks | **Postgres — the engine enforces it, not the code** |
| Async Rust driver | `tokio-postgres` / `sqlx` (Postgres) — mature, async-native, Rust-first | `asyncmy` (MySQL wire) — exists but Python-first; Rust MySQL drivers are less mature | **Postgres for Rust-first** |
| Alembic (Python, for any remaining Python services) | `postgresql+asyncpg://` — first-class | `mysql+asyncmy://` — first-class | tie (both work; the split-store §4.4 argument applies to both) |
| `sqlx` migrations (Rust-native, no Python needed) | full support, the standard Rust migration path | MySQL support exists but less idiomatic | **Postgres** |
| JSON columns (for the spine's scalar arrays: `blocked_by[]`, `supersedes[]`) | `JSONB` — indexable, queryable, binary | `JSON` — text-based, less indexable | **Postgres** |
| `SELECT ... FOR UPDATE` (spine D31 — semantic number allocation) | ✅ | ✅ | tie |
| `SKIP LOCKED` (scheduler job dispatch) | ✅ | ✅ (since 10.6) | tie |
| Replication | streaming replication, logical replication | binlog, Galera | both adequate; Postgres's logical replication is cleaner for per-DB |
| Per-database isolation | `CREATE DATABASE` per service — native, separate `pg_dump`, separate ownership | same | tie |
| Right-sizing per service | each DB can be on a separate Postgres instance independently | same | tie |
| Rust ecosystem fit | `sqlx` + `tokio-postgres` + `barrel` (migrations) — the mainstream Rust DB stack | `sqlx` MySQL support is secondary to Postgres | **Postgres** |
| License | PostgreSQL License (MIT-like, permissive) | GPL-2.0 (copyleft — the split-store §4.5 license analysis applies) | **Postgres — no NOTICE/source-offer obligation** |

**The decisive factors:** RLS for multi-tenant isolation (the highest-risk
work in the SaaS plan, §12.1) + the Rust ecosystem fit (`sqlx`/`tokio-postgres`
is the mainstream Rust DB stack) + the permissive license (no GPL-2.0
distribution obligation for the SaaS image).

---

## 3. What RLS buys for the SaaS tier

The SaaS rewrite plan (§12.1) flags multi-tenant isolation as the
highest-risk work: *"every SurrealQL query, every SQL query, every Valkey
key must carry tenant_id and every storage trait impl must enforce it. One
missed query leaks tenant A's memories to tenant B."*

**With Postgres RLS, the engine enforces it, not the application code.**

```sql
-- Per-table, one-time:
ALTER TABLE adr ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON adr
    USING (tenant_id = current_setting('app.tenant_id')::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);

-- Per-request, the service sets the tenant context:
SET LOCAL app.tenant_id = 'a1b2c3d4-...';  -- from the IAM-attested JWT
SELECT * FROM adr;  -- engine automatically filters to this tenant's rows
```

The `USING` clause filters reads; the `WITH CHECK` clause enforces writes.
A service that forgets `WHERE tenant_id = ?` doesn't leak — RLS filters
automatically. A service that tries to write a row with the wrong
`tenant_id` gets rejected. **The isolation is defense-in-depth at the
engine layer, not a convention that one missed query breaks.**

This is the single strongest argument for Postgres over MariaDB in the
SaaS context. The split-store decision chose MariaDB for the monolith
(where there's no multi-tenancy); the SaaS rewrite has multi-tenancy as
a core requirement, and RLS is the right tool.

---

## 4. What this changes in the prior plans

| Prior decision | Amendment for SaaS |
|---|---|
| split-store §4.5 (MariaDB decided) | **Unchanged for the current Python monolith.** MariaDB stays the decision for the in-flight split-store work. **For the SaaS rewrite, Postgres.** |
| architecture-principles §3.2 (Postgres for SaaS/team) | **This is the decision that §3.2 references.** |
| SaaS rewrite plan §3.5 (stores) | Postgres replaces MariaDB in the store list |
| SaaS rewrite plan §12.1 (tenant isolation) | RLS is the primary isolation mechanism, not application-code-only `WHERE tenant_id = ?` |
| spine D30 (portability — "allocate next value in a series, transactionally") | Unchanged — `SELECT ... FOR UPDATE` works on Postgres identically |
| spine D34 (Alembic owns the SQL chain) | For SaaS: `sqlx` migrations (Rust-native) or Alembic (`postgresql+asyncpg://`) if any Python services remain. Both are first-class. |

---

## 5. Solo tier stays SQLite

Solo mode (`--features solo`) uses embedded SQLite — one `.db` file per
service, no server process, no engine decision. SQLite's WAL mode handles
concurrent access from one process. The `sqlx` crate supports both SQLite
and Postgres with the same query interface, so the service code is
engine-agnostic — the `DATABASE_URL` env var selects the engine.

**Solo → SaaS path:** a user can start solo (SQLite files) and migrate to
SaaS (Postgres) by dumping each SQLite file and loading it into the
corresponding Postgres database. The schema is the same (defined in
`sqlx` migrations that target both). This is the db-per-service benefit:
each migration is per-service, not a god-DB migration.

---

## 6. The Rust stack

| Layer | Crate | Why |
|---|---|---|
| Async driver | `sqlx` | compile-time SQL checking, async-native, supports both SQLite + Postgres from the same codebase |
| Migrations | `sqlx migrate` | per-service migration directory, versioned, Rust-native — no Alembic needed for pure-Rust services |
| Connection pooling | `sqlx`'s built-in pool + `PgBouncer` (SaaS) | per-service pool; PgBouncer in transaction-mode for SaaS with many replicas |
| RLS support | native Postgres + `SET LOCAL` via `sqlx` | the service sets `app.tenant_id` per transaction from the JWT claim |

**No Python in the SaaS SQL path.** The split-store decision's Alembic
rationale (§4.4 — "Alembic works out of the box with MySQL wire") was for
the Python monolith. The SaaS rewrite is Rust-first; `sqlx migrate`
replaces Alembic for pure-Rust services. If any Python service remains
(e.g. the Ettin sidecar during the candle port transition), it uses
`postgresql+asyncpg://` with Alembic — both are first-class.

---

## 7. License

PostgreSQL License (similar to MIT/BSD) — permissive, no copyleft, no
distribution obligation, no NOTICE/source-offer requirement. This
eliminates the GPL-2.0 license compliance work from the split-store
decision §5.3 (the `NOTICE` file + `check_third_party_notices.py` ratchet
for MariaDB). The ratchet is still valuable for SurrealDB (license to
verify) and any other bundled binary, but the MariaDB-specific obligation
is gone for the SaaS tier.

---

## 8. Summary

| Tier | SQL engine | Why |
|---|---|---|
| Solo | SQLite (embedded, per-service `.db` files) | no server process, zero config, `sqlx` supports it |
| Team | Postgres (one cluster, 10 databases) | RLS for isolation, `sqlx`/`tokio-postgres` for Rust, permissive license |
| SaaS | Postgres (right-sized per service, RLS per tenant) | RLS is the decisive factor — engine-enforced tenant isolation, not application convention |
| Current Python monolith (in-flight) | MariaDB (unchanged — split-store §4.5 stands) | that's a different codebase; this decision is for the SaaS rewrite |
