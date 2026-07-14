# Security Stack Skeleton — HTTPS + Auth + Encryption for Multi-User / Cloud / SaaS

**Status:** SKELETON — exploratory investigation. Decision-ready menu, not an implementation spec.
**Date:** 2026-07-13
**Scope:** Future eventuality — yadgar as a self-hostable *and* cloud/SaaS multi-tenant service. Nothing here is scheduled or committed; this is a layered menu of choices with recommendations, alternatives, phasing, and rough effort.

---

## BLUF

Yadgar today is a **single-shared-bearer-token, plaintext-HTTP, no-at-rest-encryption, single-identity** daemon designed for local/container use. That baseline is *appropriate for what it is now* (loopback binding, fail-secure auth, timing-safe token compare) but has **zero** of the primitives a multi-user/SaaS product needs: no per-user identity, no TLS story, no tenant data isolation, no at-rest encryption.

The recommended stack, in one line per layer:

- **Transport:** Terminate TLS at a **reverse proxy (Caddy)** / managed LB — never in-process. Daemon stays plain-HTTP-on-localhost in every deployment shape.
- **AuthN:** **FastMCP `TokenVerifier` (JWKS)** for MCP + **Authlib/joserfc** OAuth2/OIDC for humans + **roll-your-own SHA-256-hashed prefixed API keys** for agents/programmatic.
- **AuthZ:** Move the DB connection from **system-user Basic auth → record-user `signin()`** with `PERMISSIONS … WHERE tenant = $auth.tenant`; **namespace-per-tenant** as the SaaS sweet spot; RBAC for teams.
- **At-rest:** Volume encryption (**EBS+KMS CMK** / LUKS) as the floor + **application-level envelope encryption** (`aws-encryption-sdk`, per-tenant keys) for sensitive fields — but **embeddings must be decrypted in trusted memory to be searched** (no ANN over ciphertext).
- **In-transit (backend↔DB):** N/A while embedded (in-process); when split across hosts, **SurrealDB server-mode TLS** (`--web-crt`/`--web-key`), terminate at LB.
- **Rate-limit:** **`limits`** library, sliding-window, keyed per-API-key, `memory://` single-process → `redis://` when multi-worker.

**Biggest open decision:** the tenant-isolation model — shared-DB-with-record-user-permissions (cheapest, real cross-tenant CVE history) vs namespace-per-tenant (SaaS default) vs DB/instance-per-tenant (hard isolation, worst density). This decision cascades into the AuthN, at-rest key strategy, and backup/export design.

---

## Current State (verified — observed state, source-of-truth)

Confirmed by reading the repo 2026-07-13. Where the task brief's stated baseline differed from observed state, observed state is recorded here.

| Dimension | Observed state | Evidence |
|---|---|---|
| **AuthN** | Single shared bearer token `YADGAR_MCP_AUTH_TOKEN`, timing-safe `hmac.compare_digest`. No per-user identity, no sessions, no JWT, no RBAC. | `yadgar/core/auth_middleware/auth_middleware.py:112,128,145` |
| **Fail-secure** | `YADGAR_REQUIRE_AUTH` defaults true; returns 503 if required but token unset. | `auth_middleware.py:116-125` |
| **Protected routes** | `/admin/*`, `/api/*`, `/hooks/*`, `/mcp`. Exempt: `/health`, `/health/live`, `/metrics`. | `auth_middleware.py:31,34` |
| **MCP transport** | FastMCP — SSE (default) / streamable-http / stdio. **Plaintext HTTP, no TLS anywhere.** | `yadgar/core/server/_app.py:50-68,187,215,236-240` |
| **Binding** | Core `127.0.0.1:8765` (loopback default); backend embed `:8001`; SurrealDB `:8000`. | `yadgar/_shared/config/config.py:56-57`; docker-compose |
| **CORS** | Default-deny + loopback whitelist, configurable via `YADGAR_ALLOWED_ORIGINS`. | `_app.py:75-83` |
| **Core↔backend (embed)** | Same `YADGAR_MCP_AUTH_TOKEN` bearer. | `yadgar/backend/ml_client/ml_client.py:245-247` |
| **Core↔SurrealDB** | HTTP **Basic auth** with `YADGAR_DB_USER/PASS` (system user), headers `surreal-ns: yadgar`, `surreal-db: main`. | `yadgar/_shared/storage/__init__.py:244` |
| **Storage — TWO modes** | **Dev default: embedded** SurrealKV in-process (`surrealkv://`, `fcntl.flock` single-writer). **Prod: server-mode** — separate `surreal start` process on `:8000`, Basic-auth over HTTP. | `storage/__init__.py:283-310`; `entrypoint-backend.sh:174-182` |
| **At-rest encryption** | **None.** RocksDB default, no cipher config. Relies on FS perms / container isolation. | grep: no `encrypt`/`cipher` in prod paths |
| **Rate limiting** | Token-bucket limiter for **auto-capture** + inference semaphore + log throttle + recall-throttle. **No per-IP/per-token HTTP request quota.** | `yadgar/_shared/rate_limit/rate_limit.py`; `core/server/http.py:~865-876` |
| **Secret handling** | Env-var + YAML (I25 three-way sync). YAML config **not chmod-600'd**. Token read live per-request (not cached). | `config/config.py:16-52` |
| **Test escape hatch** | `YADGAR_ALLOW_ROOT=1` bypasses token validation (dev/test only). | `storage/__init__.py:234-241` |

**Corrections to the stated baseline:**
- Storage is **not purely in-process** — that's the dev default. Prod already runs **server-mode SurrealDB** on `:8000` with Basic auth. This matters for the tenancy phasing (server-mode is a *precondition* prod already satisfies).
- **ADR-0078 could not be located** in the repo; do not treat it as authority for the storage design. The observed code is the authority.

---

## Recommended Stack — per layer

### Layer 1 — Transport (HTTPS/TLS)

**PRIMARY: Reverse-proxy TLS termination (Caddy).** Daemon keeps serving plain HTTP on localhost; the proxy owns the full cert lifecycle (auto-ACME/Let's Encrypt via HTTP-01, renewal, OCSP stapling, HTTP→HTTPS redirect, HTTP/2) in a ~3-line Caddyfile. **Same architecture both targets:** self-host → Caddy in front; cloud/SaaS → drop Caddy, terminate at managed LB (AWS ACM / GCP managed certs). Daemon code never changes.

- **Alternatives:** **Traefik** — only if/when k8s (native ingress) or dynamic multi-container discovery; more config + Docker-socket mount otherwise. **nginx + certbot** — mature but no built-in ACME, more moving parts, no gain here.
- **Anti-pattern (flag):** in-process TLS (`uvicorn --ssl-keyfile`) — cert renewal needs full restart (drops in-flight conns), no OCSP stapling, TLS state unshared across workers. uvicorn's own docs steer to a proxy. Only defensible for pinned pod-to-pod mTLS, which a mesh does better.
- **Air-gapped self-host:** `step-ca` (Smallstep) runs a private ACME CA so Caddy still auto-provisions.

### Layer 2 — AuthN

Different client types get different mechanisms — this is deliberate, not redundant:

| Client | Mechanism | Library | Notes |
|---|---|---|---|
| **MCP client** | Bearer/JWT first; OAuth2 where client support confirmed | **FastMCP `TokenVerifier` (JWKS)** | Lightest — resource-server validation only; IDP issues, FastMCP validates. OAuth2 support is uneven across MCP clients, so Bearer is the reliable floor. |
| **Human interactive** | OAuth2/OIDC Auth-Code + PKCE (Device Grant for CLI) | **Authlib** (flows) + **joserfc** (JOSE) | Authlib has documented Starlette async integration. joserfc is Authlib's designated `python-jose` successor. |
| **Programmatic agent / CI** | API key (default) or OAuth2 Client Credentials | **roll-your-own** (~150 LOC) | See API-key design below. |
| **Internal service** | API key or mTLS | httpx `cert=`, `step-ca`+ACME | mTLS only for a small fixed caller set with a compliance mandate; overkill for most self-host. |

**API-key design (roll-your-own — no production-grade framework-agnostic Python lib exists):**
- **Format:** `{svc}_{type}_{env}_{random}`, e.g. `yd_sk_live_<secrets.token_urlsafe(32)>` (256-bit). Prefix drives secret-scanner detection + fast triage (Stripe pattern).
- **Hashing: SHA-256, NOT argon2/bcrypt.** Counterintuitive but correct: slow hashes protect *low-entropy human passwords*; a 256-bit CSPRNG key is infeasible to brute-force regardless of hash speed, so argon2 buys zero security and adds ~100ms/req + a DoS surface. Store prefix plaintext for O(1) lookup, verify with `hmac.compare_digest`. **This flips back to argon2 only if you ever allow user-chosen keys.**
- **Lifecycle:** single-reveal at creation; scopes as JSON checked in middleware; `revoked_at` at verify; short-TTL Redis cache to dodge per-request DB hits; dual-key grace window for rotation.

**MCP spec auth story (flag — moving target):** the MCP spec defines a full **OAuth 2.1** framework (mandates PKCE S256; HTTP transports only; server = OAuth Resource Server exposing `/.well-known/oauth-protected-resource`, RFC 9728/8707). It's OPTIONAL but expected for remote servers. DCR was deprecated in favor of Client ID Metadata Documents; downstream/service auth is underspecified; NSA CSI (May 2026) flags MCP auth as optional with no RBAC/verifiable identity. **Build behind FastMCP's `TokenVerifier` abstraction so spec churn is FastMCP's problem, not ours.**

- **Avoid:** `python-jose` (abandoned — last release a types-stub, algorithm-confusion issues → use joserfc/PyJWT). `fastapi-users` (maintenance-mode + FastAPI-only; couples user model, won't run on bare Starlette/FastMCP — wrong layer).
- **PyJWT** is the lighter pick for verify-only, but MUST pass `algorithms=["RS256"]` explicitly (never derive from token `alg` → `alg:none`/confusion attack).

### Layer 3 — AuthZ (per-tenant identity + data isolation)

**The load-bearing gate — precise framing:** SurrealDB `DEFINE TABLE … PERMISSIONS` clauses apply **only to record users**, NOT to system users. Yadgar today connects as a **system user** (Basic auth `YADGAR_DB_USER/PASS`) — which **bypasses all PERMISSIONS**. So in-engine row-level isolation currently does not exist regardless of server-vs-embedded mode.

**PRIMARY prereq for any multi-tenancy:** switch the DB connection from **system-user Basic auth → record-user `signin()`** against a `DEFINE ACCESS … TYPE RECORD` (renamed from deprecated `DEFINE SCOPE`), and put `PERMISSIONS FOR select/create/update/delete WHERE tenant = $auth.tenant` (default-deny) on every table. `$auth` then carries the authenticated tenant. Prod already runs server-mode, so no engine migration is needed — this is a connection-auth + schema change, plus a data backfill to stamp `tenant` on every row.

**Isolation tiers (security d > c > b > a):**
- **(a) Shared DB + row-level perms** — cheapest; logical-only. Real cross-tenant permission-bypass CVEs have shipped (graph traversal, field-level SELECT, live-query). Use only with heavy CVE-tracking + defense-in-depth.
- **(b) Database per tenant** — hard schema boundary, clean backup/drop; N schemas migrate in lockstep.
- **(c) Namespace per tenant** — clearest boundary, **usual SaaS sweet spot (recommended default)**; still one engine/process (shared blast radius).
- **(d) Instance per tenant** — hard physical isolation; worst density/cost; reserve for high-value tenants.

**RBAC for teams:** `DEFINE USER` with OWNER/EDITOR/VIEWER at root/ns/db governs *system* users; `PERMISSIONS` governs *record* users — two separate mechanisms, use both.

### Layer 4 — Encryption at rest

**No native at-rest encryption in self-hosted SurrealKV/RocksDB** (2 tracking issues open since 2022, zero implementation activity — do not plan around it landing). Only Surreal Cloud (managed) encrypts at rest. Encrypt *below and around* SurrealDB instead:

- **Floor (necessary, not sufficient): volume encryption.** **EBS + KMS customer-managed CMK** (cloud — use CMK not default `aws/ebs` for rotation/disable control) / **LUKS** (self-host). Transparent: protects stolen/decommissioned disk, presents plaintext to every running process. Does NOT protect against a compromised daemon or logical cross-tenant leak.
- **Sensitive-field protection: application-level envelope encryption.** **PRIMARY: `aws-encryption-sdk`** — built-in envelope encryption, native KMS keyrings, AEAD defaults (AES-GCM + HKDF + key commitment), data-key caching for cost. **Alt:** pyca `cryptography` (AES-GCM in hazmat) if avoiding AWS lock-in — but you hand-roll all KMS/envelope machinery. **Avoid `age` for Python** (alpha, author-warned insecure).
- **⚠️ HARD CONSTRAINT — encrypted semantic search is impossible.** AES-GCM/Fernet ciphertext destroys the distance geometry HNSW/IVF need; you cannot run cosine/ANN over ciphertext. Yet **embeddings ARE sensitive** — vec2text recovers ~92% of short inputs from embeddings. Resolution: **envelope-encrypt embeddings at rest, decrypt into trusted process memory to build the ANN index and search.** At SaaS scale, do the decrypt-and-index inside an **AWS Nitro Enclave** so even parent-root can't read plaintext. Do not attempt DCPE/property-preserving (leaky, emerging) or FHE (research-stage, too slow).
- **Key management + rotation:** KMS rotation is transparent (retains historical versions; decrypt auto-selects; never re-encrypts payload — you only re-wrap DEKs). **PRIMARY multi-tenant pattern: per-tenant CMK wrapping cached DEKs** — cryptographic isolation + clean GDPR crypto-shredding (delete tenant CMK → data unrecoverable). **Scale caveat:** CMK quota 100k/region; past ~10k tenants → shared-CMK + per-tenant-DEK (cheaper, no crypto isolation, must track/delete every DEK for erasure).
- **Backup/export impact:** envelope encryption means backups carry ciphertext + wrapped DEKs; export/restore must have KMS access. Crypto-shredding gives clean per-tenant deletion. The existing 24h wiki JSON-lines backup would export ciphertext once fields are encrypted — the restore path needs decryption wiring.

### Layer 5 — Encryption in-transit (backend↔DB)

- **Embedded mode = in-process = no network hop = TLS irrelevant.** State this explicitly in the threat model.
- **Server mode (prod today, Basic auth over HTTP on `:8000`) = network exists.** SurrealDB supports TLS natively: `--web-crt`/`--web-key` (env `SURREAL_WEB_CRT`/`SURREAL_WEB_KEY`); client uses `wss://`/`https://` scheme. Docs recommend terminating at LB/reverse proxy; internal mTLS between yadgar and SurrealDB if split across hosts.
- **⚠️ Flag:** SurrealDB Python SDK self-signed/custom-CA handling is undocumented — verify against the pinned SDK version before relying on it.

### Layer 6 — Rate limiting / abuse

**PRIMARY: `limits` library, called from a ~30-line Starlette middleware.** Best-maintained option (it's the engine under slowapi/Flask-Limiter; 4M+ weekly downloads; Redis/Memcached/Mongo backends). Use **sliding-window-counter** (kills fixed-window boundary-burst), keyed **per-API-key** (not per-IP) for an MCP daemon. Backing store: `memory://` for single-process self-host; swap URI to `async+redis://` for multi-worker/restart-survival.

- **Alternative (least code):** slowapi — nicest ergonomics, runs on bare Starlette, but a thin alpha wrapper over `limits` with stalled merge velocity.
- **Avoid:** `fastapi-limiter` (FastAPI-DI-coupled), `starlette-limiter` (unmaintained), `asgi-ratelimit` (dead, 2022), `pyrate-limiter` (leaky-bucket only, no ASGI middleware).
- Yadgar already has an auto-capture token-bucket limiter — the new layer is HTTP-request-level per-key quotas on public endpoints, distinct from that.

---

## Phasing

### Phase 1 — Single-user remote (yadgar exposed beyond localhost, one identity)
- **Transport:** Caddy reverse proxy + auto-ACME (or managed LB in cloud). Client over HTTPS. **[required]**
- **AuthN:** keep single bearer token, but issue it over TLS; optionally graduate to FastMCP `TokenVerifier` if an IDP already exists.
- **At-rest:** volume encryption (EBS+KMS CMK / LUKS).
- **In-transit DB:** only if the daemon/DB are split across hosts — server-mode TLS.
- **AuthZ / tenancy:** N/A.
- **Rate-limit:** `limits` middleware, `memory://`.

### Phase 2 — Small teams (multiple identities, one org)
- **AuthN:** per-user identity — Authlib/OIDC for humans + API keys for agents. FastMCP `TokenVerifier` for MCP clients.
- **AuthZ:** **switch DB connection to record-user `signin()`**; `PERMISSIONS WHERE tenant = $auth.tenant` on every table; **namespace-per-tenant**; RBAC via `DEFINE USER` for ops. Backfill `tenant` on existing rows.
- **At-rest:** begin envelope encryption (`aws-encryption-sdk`) for sensitive fields/embeddings, single CMK; decrypt-in-memory search path.
- **Rate-limit:** per-API-key quotas; move to `redis://` if multi-worker.

### Phase 3 — Full SaaS (many tenants, public)
- **AuthZ:** namespace-per-tenant to density limits; DB/instance-per-tenant for high-value; shared-DB only with heavy CVE-tracking.
- **At-rest:** **per-tenant CMK wrapping cached DEKs** (crypto-shredding for GDPR); shift to shared-CMK+per-tenant-DEK past ~10k tenants. Consider **Nitro Enclaves** for the decrypt-and-index path.
- **In-transit:** TLS everywhere at LB; internal mTLS yadgar↔SurrealDB.
- **MCP auth:** full OAuth 2.1 + PKCE S256 via FastMCP if MCP-client OAuth support is broad enough.

---

## Hard dependencies + prereqs (ordering)

1. **TLS first (Phase 1).** Everything else (OAuth redirect flows, PKCE, secure cookies, API-key transmission) assumes TLS. No auth work is safe over plaintext.
2. **Record-user DB auth before ANY tenant isolation.** System-user Basic auth bypasses PERMISSIONS — until the connection authenticates as a record user, no `WHERE tenant = $auth.tenant` clause does anything. This is the gate for all of Phase 2 AuthZ.
3. **Tenant column/schema + data backfill** must precede enabling row-level permissions (default-deny locks out un-stamped rows).
4. **KMS + envelope-encryption wiring before at-rest field encryption**; and the **decrypt-in-memory search path** must exist before encrypting embeddings, or search breaks.
5. **Per-user identity (Phase 2 AuthN) before RBAC/AuthZ** — can't authorize identities that don't exist.
6. **Redis** becomes a hard dep once multi-worker (rate-limit shared state, API-key cache).

---

## Open questions

1. **Tenant isolation model** — shared-DB-record-user (a) vs namespace-per-tenant (c) vs DB/instance-per-tenant (b/d)? Cascades into AuthN, key strategy, backup design. *(Biggest decision.)*
2. **IDP: build or buy?** Self-host an OAuth server (Authlib `OAuthProvider` — high burden) vs bring an external IDP (Auth0/WorkOS/Keycloak/Descope) and use FastMCP `TokenVerifier`. Buy is almost certainly right unless air-gapped.
3. **Embeddings-at-rest posture** — encrypt-and-decrypt-in-process (simpler, root can read plaintext) vs Nitro Enclave (strong, build cost)? Threat model determines this.
4. **Do embeddings even need at-rest encryption in Phase 1/2?** vec2text says they're recoverable → sensitive; but encrypting them forces the in-memory-decrypt search path. Decide when the threat model justifies the complexity.
5. **Existing 24h backup path** — how does envelope encryption change export/restore? Does restore need standing KMS access?
6. **Config file hardening** — chmod-600 the YAML on startup? (Small, orthogonal, worth doing regardless of phase.)
7. **SurrealDB Python SDK TLS** — does it cleanly handle custom-CA/self-signed for internal mTLS? Undocumented; needs a spike against the pinned version.
8. **MCP client OAuth maturity** — is `TokenVerifier`/Bearer enough, or is full OAuth 2.1 needed? Depends on which MCP clients must be supported.

---

## Rough effort per layer (T-shirt, SKELETON-level)

| Work item | Effort | Notes |
|---|---|---|
| Caddy/LB TLS termination | **S** | Config-only; no daemon code change. |
| Config chmod-600 hardening | **S** | Startup one-liner. |
| Rate-limit middleware (`limits`, per-key) | **S–M** | ~30 LOC + store wiring. |
| FastMCP `TokenVerifier` (JWKS) integration | **M** | Wire IDP JWKS; assumes external IDP. |
| API-key system (issue/hash/scope/rotate/revoke) | **M** | ~150 LOC + storage schema + middleware. |
| OAuth2/OIDC human login (Authlib + external IDP) | **M–L** | Buy-IDP path; L if self-hosting the OAuth server. |
| Record-user DB auth + `PERMISSIONS` schema | **L** | Connection-auth change + per-table clauses + data backfill; touches every read/write path. |
| Namespace-per-tenant provisioning + routing | **L** | Tenant lifecycle, per-ns sessions, migrations across N namespaces. |
| Envelope encryption + KMS (per-tenant keys) | **L** | Key mgmt, rotation, backup/export rewiring. |
| Encrypted-embeddings + decrypt-in-memory search | **L–XL** | Redesign of the search/index path; Nitro Enclave pushes to XL. |
| Internal mTLS yadgar↔SurrealDB | **M** | Only when hosts split; step-ca/cert-manager for cert lifecycle. |

---

## Sources

**Transport / AuthN / rate-limit:**
- https://caddyserver.com/docs/automatic-https
- https://doc.traefik.io/traefik/https/acme/
- https://www.uvicorn.org/deployment/
- https://fastapi.tiangolo.com/deployment/https/
- https://docs.authlib.org/en/stable/oauth2/client/web/starlette.html
- https://pypi.org/project/joserfc/
- https://github.com/mpdavis/python-jose
- https://pyjwt.readthedocs.io/en/stable/algorithms.html
- https://github.com/fastapi-users/fastapi-users
- https://docs.stripe.com/keys
- https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- https://gofastmcp.com/servers/auth/oauth-proxy
- https://pypi.org/project/limits/
- https://github.com/laurentS/slowapi
- https://github.com/abersheeran/asgi-ratelimit
- https://smallstep.com/docs/step-ca/
- https://www.nsa.gov/Portals/75/documents/Cybersecurity/CSI_MCP_SECURITY.pdf (verify path — may 404)

**At-rest / in-transit / multi-tenancy:**
- https://surrealdb.com/docs/surrealkv
- https://surrealdb.com/docs/surrealdb/security
- https://surrealdb.com/docs/surrealdb/security/security-best-practices
- https://github.com/surrealdb/surrealdb/issues/88
- https://github.com/surrealdb/surrealdb/issues/3415
- https://surrealdb.com/legal/security-addendum
- https://docs.aws.amazon.com/ebs/latest/userguide/ebs-encryption.html
- https://docs.aws.amazon.com/kms/latest/developerguide/concepts.html
- https://docs.aws.amazon.com/encryption-sdk/latest/developer-guide/python.html
- https://docs.aws.amazon.com/encryption-sdk/latest/developer-guide/data-key-caching.html
- https://cryptography.io/en/latest/hazmat/primitives/aead/
- https://github.com/FiloSottile/age
- https://ironcorelabs.com/docs/cloaked-ai/how-it-works/
- https://arxiv.org/abs/2310.06816
- https://github.com/vec2text/vec2text
- https://aws.amazon.com/ec2/nitro/nitro-enclaves/
- https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys.html
- https://docs.aws.amazon.com/kms/latest/developerguide/deleting-keys.html
- https://aws.amazon.com/blogs/architecture/simplify-multi-tenant-encryption-with-a-cost-conscious-aws-kms-key-strategy/
- https://surrealdb.com/docs/surrealdb/cli/start
- https://surrealdb.com/docs/surrealdb/deployment
- https://surrealdb.com/docs/languages/python/concepts/connecting-to-surrealdb
- https://surrealdb.com/docs/surrealdb/security/authentication
- https://surrealdb.com/docs/surrealdb/security/permissions
- https://surrealdb.com/docs/surrealql/statements/define/access/record
- https://surrealdb.com/docs/surrealdb/introduction/concepts/namespace
- https://surrealdb.com/docs/languages/rust/concepts/multi-tenancy
- https://advisories.gitlab.com/cargo/surrealdb/GHSA-vjjx-rfw4-rmfc/
