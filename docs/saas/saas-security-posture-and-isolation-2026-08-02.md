# SaaS Security Posture + Isolation Tiers (2026-08-02)

**Status:** DECISION — states the security model explicitly so it's not
ambiguous. Adds the dedicated-deployment tier for physical isolation.
**Branch:** `docs/saas-security-posture-and-isolation-2026-08-02` (doc-only)
**Amends:** `docs/saas/saas-architecture-principles-2026-08-02.md`
Principle 8 (security as boundary) — adds the explicit posture statement
and the isolation tier table.

---

## 1. The security posture, stated explicitly

> **Yadgar SaaS uses encryption at rest and in transit with per-tenant keys.
> The server processes plaintext for search and synthesis. This is NOT
> zero-knowledge E2EE — the operator can decrypt with KMS access.**

No false promises. The product's value (server-side semantic search,
reranking, LLM answer synthesis) requires plaintext. True E2EE where the
operator can't decrypt is not feasible without TEE infrastructure, which
is out of scope for the launch.

### 1.1 What "encryption at rest" means

- Every tenant's sensitive content (memory content, wiki page bodies, ADR
  decision text) is encrypted with a per-tenant DEK (data-encrypting key).
- DEKs are generated per-tenant, wrapped by the tenant's KEK (key-encrypting
  key), stored in `yadgar_vault` DB.
- The KEK is held in a real KMS (AWS KMS / GCP KMS / HashiCorp Vault). Solo:
  a sealed local file.
- The DB stores `EncryptedPayload { ciphertext, key_id, nonce, algorithm }`.
  A DB dump or disk theft gets ciphertext, not plaintext.
- Key rotation is per-tenant. A compromised DEK is scoped to one tenant.
  Rotate via `vault.rotate_key(tenant_id)` — old DEK kept for decrypt-only,
  new DEK for future writes.

### 1.2 What "encryption in transit" means

- TLS 1.3 everywhere: client → gateway, gateway → services, services →
  stores.
- Content payload is additionally encrypted with the tenant DEK as a second
  layer (so even a TLS termination point or a compromised load balancer
  doesn't see plaintext).
- Inter-service calls (gateway → recall, recall → ml) carry the tenant DEK
  context in the attested JWT — downstream services decrypt with the tenant
  DEK, process in plaintext, re-encrypt before storing.

### 1.3 What the operator CAN do (and can't)

| Capability | Operator (us) | Tenant admin | Tenant user |
|---|---|---|---|
| Decrypt tenant content | ✅ (via KMS + DEK) | ❌ | ❌ |
| Read tenant content at rest | ✅ (ciphertext only without KMS) | ❌ | ❌ |
| Read tenant content during processing | ✅ (plaintext in memory) | ❌ | ❌ |
| Access tenant's API keys | ✅ (stored in `yadgar_iam` DB) | ✅ (own keys) | ❌ |
| Rotate tenant's DEK | ✅ | ✅ (via vault API) | ❌ |
| Delete tenant's data | ✅ | ✅ | ❌ |

**This is the same model as every SaaS that does server-side processing**
(Notion, Linear, GitHub, Slack). The operator can access data with
sufficient privilege. The protection is:
1. Per-tenant key isolation — one tenant's compromise doesn't affect others.
2. Postgres RLS — tenant A's queries never return tenant B's rows.
3. KMS audit log — every DEK unwrap is logged; the operator can't decrypt
   without leaving a trace.
4. Least-privilege service credentials — each service connects to its own
   DB with scoped grants, not a superuser.

### 1.4 What is NOT encrypted (and why)

| Field | Encrypted? | Why |
|---|---|---|
| Memory content | ✅ (per-tenant DEK) | the sensitive payload |
| Wiki page body | ✅ | the sensitive payload |
| ADR decision/context/rationale | ✅ | sensitive prose |
| Embeddings | ❌ (plaintext in Surreal) | needed for KNN search; embeddings are not reversible to plaintext but carry semantic signal — accepted as a known leak |
| Tags | ❌ | needed for server-side filtering (recall tags filter) |
| Heat, importance, timestamps | ❌ | metadata needed for ranking + decay |
| Directory context | ❌ | needed for scoping queries |
| Tenant ID | ❌ | needed for RLS + routing |
| ADR number, status, title | ❌ | metadata for listing + filtering |

**The design decision:** metadata (tags, heat, timestamps, directory,
tenant_id, titles, status) is plaintext for server-side filtering. Content
(the actual prose) is encrypted. Embeddings are plaintext (accepted leak —
they carry semantic signal but aren't reversible).

---

## 2. Isolation tiers — multi-tenant, dedicated, self-hosted

The SaaS offers three isolation tiers. The architecture is the same code;
the deployment topology differs.

### 2.1 Tier matrix

| Tier | Isolation | DB | Keys | LB routing | Use case |
|---|---|---|---|---|---|
| **Shared (default)** | logical (tenant_id + RLS) | shared Postgres cluster, per-tenant RLS policies | shared KMS, per-tenant DEK | path/header-based tenant routing | most users, cheapest |
| **Dedicated (premium)** | physical (own deployment) | own Postgres instance | own KMS key | dedicated hostname/subdomain, LB routes to dedicated stack | regulated industries, compliance |
| **Self-hosted** | complete (customer's infrastructure) | customer's Postgres/SQLite | customer's KMS or file key | customer's LB | air-gapped, sovereign, maximum control |

### 2.2 Shared tier (the default SaaS)

```
client → api.yadgar.ai (shared gateway)
  → LB extracts tenant_id from JWT
  → routes to shared gateway → shared recall → shared Surreal → shared Postgres
  → RLS ensures tenant A never sees tenant B's rows
  → per-tenant DEK ensures tenant A's key can't decrypt tenant B's content
```

- One cluster, N tenants, `tenant_id` on every row, RLS enforced.
- Cheapest, highest density, good enough for most.
- **Noisy neighbor** mitigated by per-tenant rate limits + quotas (metering).
- **Isolation failure** is the highest-risk work (one missed `WHERE
  tenant_id` leaks) — mitigated by RLS (engine-enforced, not application
  convention) + property-based cross-read tests on every PR.

### 2.3 Dedicated tier (premium — physical isolation)

```
client → acme.yadgar.ai (dedicated hostname)
  → LB routes *.yadgar.ai wildcard → dedicated gateway (ACME's own instance)
  → ACME's gateway → ACME's recall → ACME's Surreal → ACME's Postgres
  → ACME's own KMS key (not shared with other tenants)
  → no other tenant's data in the same DB, same Surreal, same KMS
```

- A full dedicated stack (gateway + recall + write + ml + llm + stores)
  provisioned per customer. Same Helm chart, different namespace + values.
- Own Postgres instance (not shared cluster). Own SurrealDB instance. Own
  KMS key. Own Valkey instance.
- **Physical isolation:** no other tenant's data in the same processes, DBs,
  or key infrastructure. A RLS bug in the shared tier can't affect this
  tenant because they're not in the shared DB.
- **Cost:** higher (dedicated infrastructure per customer). Right-sized
  per customer (small instance for a small org, large for a big one).
- **Provisioning:** Helm chart deploys a dedicated stack. `yadgar init
  --dedicated --domain acme.yadgar.ai` creates the namespace, deploys the
  chart, provisions the KMS key, creates the first admin user.

### 2.4 Self-hosted tier (customer's infrastructure)

```
customer deploys the Helm chart on their own k8s cluster (or docker-compose)
customer's own Postgres, Surreal, Valkey, KMS (or file key for solo)
customer's own LB (Ingress, Traefik, whatever)
yadgar.ai has zero access to the data
```

- The customer runs the full stack on their infrastructure. We ship the
  Helm chart + the binaries. They have full control.
- **Air-gapped:** works with no connection to yadgar.ai. Updates are
  offline (download the chart + images, deploy locally).
- **Sovereign:** data never leaves the customer's jurisdiction.
- **Support:** best-effort (we can't see their data). Updates via the
  chart. Bug reports from their logs.

### 2.5 The edge LB routing (high-level, not product design)

This is infrastructure, not product — stated here for completeness, not
detailed design:

```
*.yadgar.ai  →  wildcard DNS  →  edge LB (Cloudflare / AWS ALB / nginx)

  → if Host: api.yadgar.ai        → shared gateway (multi-tenant)
  → if Host: acme.yadgar.ai       → dedicated gateway (ACME's namespace)
  → if Host: *.yadgar.ai          → lookup: is this a dedicated hostname?
    → yes → route to dedicated stack
    → no  → 404
```

**The LB's only job:** route by hostname to the right gateway. It does NOT
decrypt content (TLS passthrough to the gateway), does NOT read tenant data,
does NOT do auth (the gateway does that). It's a dumb router.

**At-rest isolation is at the DB layer, not the LB layer.** The LB routes
traffic; the DB enforces isolation. In the shared tier, RLS does it. In the
dedicated tier, separate DBs do it. The LB is not a security boundary — the
gateway + IAM + RLS are.

**Dedicated hostname provisioning:** when a customer upgrades to dedicated,
the provisioning flow: create namespace → deploy Helm chart → create
wildcard DNS record (`acme.yadgar.ai → LB IP`) → create KMS key → create
first admin user → customer gets their URL. The LB picks up the new
hostname via the wildcard DNS; the dedicated gateway in the namespace
responds. No LB reconfiguration needed — the wildcard catches all
`*.yadgar.ai`, the routing is by namespace/Ingress.

---

## 3. What this means for the architecture

The isolation tiers are **deployment topology, not code changes.** The
same 14 services, the same traits, the same protocol crate — different Helm
values:

| Setting | Shared | Dedicated | Self-hosted |
|---|---|---|---|
| `YADGAR_MODE` | `saas` | `saas` | `saas` or `solo` |
| Gateway replicas | 3+ (shared) | 2 (dedicated) | 1-2 |
| Postgres | shared cluster, per-tenant DBs + RLS | dedicated instance | customer's instance |
| SurrealDB | shared cluster, per-tenant scoping | dedicated instance | customer's instance |
| KMS | shared KMS, per-tenant KEK | dedicated KMS key | customer's KMS or file |
| Valkey | shared cluster | dedicated instance | customer's instance |
| LB | api.yadgar.ai (shared) + wildcard for dedicated | acme.yadgar.ai | customer's LB |
| Tenant isolation | RLS (engine-enforced) | separate DBs (physical) | customer's network |
| Model selection | per-tenant config | per-tenant config | customer's config |

**No service code changes between tiers.** The composition root reads
env vars; the topology is config-driven. A service that works in shared
mode works identically in dedicated mode — it just talks to a different
Postgres/Surreal/KMS endpoint.

---

## 4. The security section for the architecture principles doc

Add to `saas-architecture-principles-2026-08-02.md` Principle 8:

> **Security posture (stated explicitly):** Yadgar SaaS uses encryption at
> rest (per-tenant DEKs, KMS-wrapped KEKs) and encryption in transit
> (TLS 1.3 + payload-level DEK encryption). The server processes plaintext
> for search and synthesis. This is NOT zero-knowledge E2EE — the operator
> can decrypt with KMS access. This is the same model as every server-side
> SaaS (Notion, Linear, GitHub, Slack).
>
> **Isolation tiers:** shared (logical, RLS + per-tenant DEK), dedicated
> (physical, own DB + own KMS key, `*.yadgar.ai` hostname), self-hosted
> (customer's infrastructure, air-gapped capable). Same code, different
> topology. The edge LB routes by hostname; the DB enforces isolation.

---

## 5. Open questions

1. **KMS choice for the shared tier.** AWS KMS (us-east/eu-west), GCP KMS,
   or a self-managed Vault? Affects multi-region + data residency.
   **Recommendation:** AWS KMS for launch (simplest, multi-region via
   replica keys), Vault for self-hosted.
2. **Dedicated tier provisioning automation.** How automated is the
   `yadgar init --dedicated` flow? Fully self-serve (customer clicks
   "upgrade" → namespace created automatically) or manual (ops team
   provisions)? **Recommendation:** manual for launch (few dedicated
   customers), self-serve after the pattern is proven.
3. **Embedding encryption.** Embeddings are plaintext (accepted leak). If a
   tenant demands embedding encryption, the only path is client-side embed
   generation (embed locally, send encrypted vectors, store encrypted
   vectors, KNN on encrypted vectors = not possible without TEE or
   client-side KNN). **Recommendation:** document as a known limitation;
   offer dedicated tier (own Surreal, own KMS) as the mitigation for
   embedding-privacy-conscious customers.
4. **Data residency.** EU customers need EU-only data. Shared tier: deploy
   a EU cluster (eu-west Postgres + Surreal + KMS), route EU tenants to it
   via the LB (JWT claim → region routing). Dedicated tier: deploy in the
   customer's region. **Recommendation:** EU cluster as a separate Helm
   deployment, LB routes by tenant's region claim. Not day-1, but the
   architecture supports it (no cross-region data sharing).
