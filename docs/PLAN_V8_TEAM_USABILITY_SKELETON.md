# PLAN — v8: Team usability (SKELETON / architecture spec)

**Status:** SKELETON drafted 2026-06-05 evening. Hypothetical / architecture-only at this stage. NOT dispatch-ready. Will revisit after v6 (LLM curator scaffolding) + v7 (real-time synthesis) architectures crystallize — v7 sync semantics inform v8.2 federated-sync protocol design.

**Origin:** User discussion 2026-06-05 — "i need to discuss v8 which is making this beautiful product usable by teams." Personal-memory engine value translates into "team second brain" if the architectural tensions are resolved up front.

**Scope:** team collaboration on shared knowledge + memory while preserving the personal-memory contract that gives yadgar its identity. Multi-tenant + federated identity + auth/authz + sharing UX + conflict resolution + privacy/compliance.

**Estimated effort:** 2-3 months engineering for v8.0 foundational layer alone. Multi-quarter cycle for full v8.0-v8.4 chain.

---

## Recommended architectural direction

**Federated personal-first.** Each user retains their personal yadgar (single-user value intact, default-private). Team scope is opt-in: explicit promote-to-team for memories + wiki pages. Team server is coordination point, not surveillance.

Alternatives considered + rejected:
- **Tenanted single-instance** — one server, per-user namespaces. Risk: org-level groupthink, harder privacy story, harder migration from existing installs.
- **Team-only server** — separate server for team knowledge, users have both. Risk: doubled infra cost, sync complexity, unclear authority.

Why federated personal-first wins:
- Preserves yadgar's "your brain" identity (single-user value intact)
- Personal context stays private by default (no leak risk)
- Team scope = explicit opt-in (anchors, wiki pages)
- Easier privacy story for sales (each user controls their own data)
- Multi-agent collab still works (team server is coordination point)
- Migration story: existing single-user installs unchanged; team-server is opt-in deployment

---

## Decision points (must resolve before v8.0 dispatch)

### DP-A — Architecture model

**Decided (skeleton):** Federated personal-first (per rationale above).

**To re-confirm at dispatch time:** has v7 real-time synthesis introduced anything that changes the sync substrate? If v7 ships a peer-to-peer sync layer, v8 reuses it; if v7 stays single-process, v8.2 needs its own.

### DP-B — Default privacy

**Lean:** Default-private + opt-in promote-to-team. Matches federated-personal-first model.

**Tradeoff:**
- Default-private: lower friction for individuals; less default value for new team members joining (no team context to discover until peers explicitly share)
- Default-team: higher discoverability; risk of leaking WIP personal context (e.g., debugging notes with credentials)

**Decision before v8.0:** stay default-private. Provide tooling for bulk-promote-by-tag if onboarding pain real.

### DP-C — Identity provider

**Options:**
- **OIDC** (Google Workspace, Okta, Microsoft Entra) — enterprise-friendly, complex auth flow
- **Forgejo PAT-style bearer tokens** — simple, dev-friendly, no SSO integration cost
- **SAML enterprise** — large org requirement, heaviest integration

**Lean:** Ship Forgejo-style bearer tokens FIRST (v8.0), add OIDC for v8.1+ once team UX validated. SAML only if enterprise customers request.

### DP-D — Conflict resolution on concurrent writes

**Options:**
- **CRDT (Conflict-free Replicated Data Type)** — automatic merge, decentralized, complex to implement correctly. SurrealDB has some primitives but full CRDT semantics on memory + wiki are nontrivial.
- **Merge-request pattern** — explicit human review of conflicting edits. Slower, predictable, low engineering cost.
- **Last-write-wins (LWW)** — simplest, lossy. v5.46.x cycle already showed how rough this is for wiki RMW.

**Lean:** Merge-request for wiki (matches v5.64 surgical edit primitives; explicit promote workflow); LWW + version history for memory (CRDT v8.3+ if peer-to-peer sync ships).

### DP-E — Sharing UX

**Options:**
- **Explicit:** user calls `share_memory(memory_id, team_id)` / `share_wiki(slug, team_id)`. Discoverable, intentional, friction-heavy.
- **Shadow-mirror with annotations:** all writes optionally mirror to team with auto-anonymization. Discoverable by team without owner action. Risk: privacy leak.
- **Auto-promote heuristics:** signal-based (high heat + multiple access + cross-session) auto-suggests "looks like team-relevant — promote?". Best UX if heuristics accurate; risk of bad suggestions.

**Lean:** Explicit (v8.0) → add auto-suggest in v8.1 once team usage patterns observable.

### DP-F — Cost model

**Options:**
- **Self-hosted only** — open source, deploy on own infra. Lowest support burden, lowest revenue path.
- **Managed SaaS only** — yadgar runs team servers, customers pay per team/user/seat. Higher support burden, revenue path.
- **Hybrid** — self-hosted free, managed paid tier with SSO + audit log + SLA. Most ambitious; affects v8 scope by orders of magnitude.

**Lean:** Self-hosted for v8.0 (validate architecture works); decide on managed offering after.

### DP-G — Migration from v5/v6/v7 single-user

**Question:** how does a user with existing personal yadgar opt into a team?

**Options:**
- **In-place upgrade:** existing yadgar daemon becomes team-aware; user adds team_id config + auth token. No data migration; opt-in promote individual records.
- **Side-by-side:** install team-aware yadgar alongside personal; sync configured manually. Higher friction, lower risk.
- **Migration tool:** automated importer that moves selected records from personal to team. Risk of accidental over-share.

**Lean:** In-place upgrade. Existing yadgar gains optional team-membership config without data migration; promotion is per-record.

### DP-H — Pricing tier

**Defer to DP-F resolution.** If managed offering: flat per-team vs per-user vs hybrid. If self-hosted only: irrelevant.

---

## Slot allocation (v8.0 → v8.4)

### v8.0 — Team server foundational

**Goal:** multi-tenant yadgar daemon with shared knowledge layer.

**Deliverables:**
- SurrealDB multi-tenancy (namespace per team, database per scope: personal / team-shared)
- Forgejo-style bearer token auth at MCP boundary
- New MCP tools:
  - `share_memory(memory_id, team_id, visibility="team")` — promotes personal memory to team scope
  - `unshare_memory(memory_id, team_id)` — revokes team visibility
  - `share_wiki(slug, team_id)` — promotes wiki page
  - `unshare_wiki(slug, team_id)` — revokes
  - `team_recall(query, team_id)` — query team-scoped memories
  - `team_wiki_query(query, team_id, tags=[...])` — team wiki search
  - `team_anchors(team_id)` — list team-visible anchors
- Caller-context required: `team_id` per call (similar to v5.42.3 `branch_hint` contract)
- Default scope: personal (private). Team scope opt-in per record.
- Migration: existing yadgar installs gain optional `YADGAR_TEAM_CONFIG` env var; no data migration on upgrade
- Audit trail: every team-scope write logged (who, when, what)

**Effort:** ~3-4 months. Tier-1 invariants P1-P11 mandatory.

---

### v8.1 — Team wiki + curation

**Goal:** human-in-the-loop curation of team knowledge.

**Deliverables:**
- Merge-request workflow for team wiki edits (uses v5.64 surgical edit primitives + v5.41 versioning)
- "Propose to team" UI for promoting personal pages to team scope (review queue)
- Conflict resolution on concurrent edits: surface as merge-request, not auto-merge
- Team audit log: query "who edited X", "what changed today"
- Curator role (team admin) for resolving merge requests + retiring stale team knowledge

**Effort:** ~1.5-2 months.

---

### v8.2 — Federated personal-team sync

**Goal:** personal yadgar pulls team facts; team yadgar can pull opt-in personal anchors.

**Deliverables:**
- Sync protocol: HTTP poll OR webhook push (decide at dispatch; webhook needs team server to be reachable)
- Bidirectional: pull team-shared into personal recall (boost ranking); push opt-in personal anchors to team
- Conflict resolution: LWW with version history for memory; merge-request for wiki
- Sync state cache + reconciliation on disconnect/reconnect
- Opt-in granularity: user controls which personal anchors push to team

**Effort:** ~2 months.

---

### v8.3 — Multi-agent coordination

**Goal:** concurrent Claude instances on the same team work coherently.

**Deliverables:**
- Shared recall cache (team server caches recent team_recall results; clients consult cache before hitting DB)
- Trace propagation: yadgar tool calls across multi-agent sessions tagged with shared trace_id
- Lock-free conflict resolution: CRDT-lite for wiki (limited to v5.64 surgical edit primitives — text-anchor + positional with version guards); LWW for memory writes (rare for the same record to be concurrently written)
- Multi-agent SubagentStop coordination: team subagents flush to team queue, not personal

**Effort:** ~2 months. Possibly subsumes v8.2 federated-sync work.

---

### v8.4 — Privacy + compliance

**Goal:** enterprise-ready privacy + compliance posture.

**Deliverables:**
- Per-record visibility scoping: `visibility ∈ {private, team, role:<role-name>}`
- Audit log query interface: who recalled what, when
- Per-team retention policies: auto-purge after N days
- GDPR-friendly delete: `forget_user(user_id, team_id)` cascading delete with audit
- At-rest encryption for records marked `sensitive=true` (per-team encryption key in KMS)
- SAML enterprise SSO integration (if DP-C revisited)

**Effort:** ~2-3 months. May spin off enterprise tier here.

---

## Architecture Conformance (P1)

Cites future `docs/architecture.md` updates needed:

- **§ Multi-tenancy model** — NEW section documenting team server, personal-first federation, namespace mapping
- **§ Identity + auth at MCP boundary** — NEW section, expands existing YADGAR_MCP_AUTH_TOKEN model to per-team scope
- **§ Sync protocol (v8.2)** — NEW section once sync substrate decided (HTTP poll vs webhook)
- **§ Visibility scoping** — NEW section, expands existing branch + directory context model with team + role
- **§ Audit trail** — NEW section

Architecture updates land BEFORE v8.0 dispatch per P1 invariant.

---

## Touched Invariants (P2)

| Invariant | Verb | Notes |
|---|---|---|
| I9 (daemon start latency budget) | **preserves** | Team config loading is one-off at startup, not per-request |
| I23 (Prometheus metric availability) | **changes** | New metrics: team_recall_count, team_wiki_writes_count, share_count, audit_log_count |
| I25 (config three-way-sync) | **changes** | New knobs: YADGAR_TEAM_CONFIG, YADGAR_TEAM_SERVER_URL, YADGAR_TEAM_AUTH_TOKEN |
| I26 (secret-gate) | **changes** | Auth tokens at MCP boundary; per-record visibility scope enforcement |
| P3 (config knob lifecycle) | **applies** | New knobs MUST have yaml incremental-sync logic + env-var precedence |
| P4 (schema constraint lifecycle) | **applies** | NEW: team_id NOT NULL on shared records; visibility NOT NULL with default 'private'; audit_log table |
| P5 (MCP contract changes) | **applies** | NEW MCP tools (share_memory, team_recall, etc.); existing tools gain optional team_id param |
| P7 (production write-path test) | **applies** | Every new MCP tool MUST have a drainer-write production-path test |
| Workflow rule "every doc on master" | preserves | v8 plan lands on master once architecture committed |

---

## Config Knob Lifecycle (P3)

NEW knobs for v8.0:

- `YADGAR_TEAM_ENABLED` (bool, default `false`) — opt-in flag for team-aware mode
- `YADGAR_TEAM_SERVER_URL` (str, default empty) — URL of team yadgar instance
- `YADGAR_TEAM_AUTH_TOKEN` (secret str, default empty) — bearer token for team server
- `YADGAR_TEAM_DEFAULT_VISIBILITY` (str, default `"private"`) — `private | team | role:<name>`
- `YADGAR_TEAM_SYNC_INTERVAL_SECONDS` (int, default 300) — federated sync poll interval

All knobs need yaml incremental-sync logic per X5 pattern (`yadgar config sync`).

---

## Schema Constraint Lifecycle (P4)

NEW migration (v8.0, post-v5.46.x cycle close + post-v6 + post-v7):

- `wiki_page.team_id` NOT NULL (default `"personal"` for existing rows; backfill before constraint)
- `wiki_page.visibility` NOT NULL DEFAULT 'private'
- `memory.team_id` NOT NULL (default `"personal"`)
- `memory.visibility` NOT NULL DEFAULT 'private'
- NEW table `team` (team_id PRIMARY KEY, name str, created_at, audit_log_enabled bool)
- NEW table `team_member` (team_id, user_id, role str, added_at)
- NEW table `audit_log` (team_id, user_id, action, target_id, timestamp, metadata)
- NEW table `share_request` (id, team_id, source_record_id, requested_by, status, reviewed_by) — for merge-request workflow in v8.1

Per P4 invariant: backfill existing rows BEFORE applying NOT NULL constraint. Verify SurrealDB `IS NONE` semantics before designing migration (v5.42.5 Bug 1 precedent).

---

## MCP Contract Changes (P5)

NEW MCP tools per slot above. Each tool's contract:

- Caller-context required: `team_id` (mandatory for team-scoped tools)
- Auth required: bearer token in YADGAR_MCP_AUTH_TOKEN OR team-token via `YADGAR_TEAM_AUTH_TOKEN`
- Defense-in-depth: MCP boundary checks team_id format + auth scope; drainer pre-apply re-validates team_id against caller's accessible teams
- Error responses:
  - `unauthorized_team`: caller not a member of team_id
  - `record_not_team_visible`: record exists but visibility doesn't include caller's role
  - `concurrent_share_conflict`: someone else promoted this record concurrently

EXISTING tools (v5.x) gain optional `team_id` param:
- `recall(team_id?)` — recall scoped to personal + team if specified
- `wiki_query(team_id?)` — wiki search scoped to personal + team
- `memorize(team_id?)` — stores in personal by default OR team if specified + caller has permission

---

## Cross-Plan Coordination (P6)

| Plan | Relationship |
|---|---|
| `docs/PLAN_V5_64_WIKI_EDIT_PRIMITIVES.md` | v8.1 merge-request workflow reuses v5.64 surgical edit primitives for collaborative wiki editing |
| `docs/PLAN_V6_*.md` (LLM curator scaffolding) | v6 ships before v8; team server can opt into curator suggestions for team knowledge |
| `docs/PLAN_V7_*.md` (real-time synthesis, TBD) | v7 sync semantics inform v8.2 federated-sync design |
| Future `docs/PLAN_V8_0_TEAM_SERVER.md` | TBD when dispatch-ready; ships AFTER v7 |

---

## Bug Class Precedent (P7)

**Precedent 1 — branch-on-wiki rationale (v5.0 → v5.50+ archaeology):** speculative-infrastructure pattern. v8 must not ship team features that have zero users. Mitigation: every v8 tool gets a usage-metric counter pre-dispatch; if zero non-test usage after 1 month live, evaluate retire.

**Precedent 2 — wiki RMW corruption class:** multi-author wiki edits 10x harder than single-author. Mitigation: v8.1 explicit merge-request workflow (no auto-merge) + v5.64 surgical edits as foundation.

**Precedent 3 — secret leak class (v5.42.x cycle):** auth tokens at MCP boundary need defense-in-depth; never log token contents.

**Verification probes (post-v8.0 ship):**
1. Multi-tenant SurrealDB namespace isolation: write to team A, query as team B, verify no leakage
2. Bearer token enforcement: invalid token returns 401, not 200-with-empty-result
3. Visibility scoping: personal record never appears in team_recall output
4. Audit log fires on every team-scope write
5. share_memory cannot escalate visibility for records caller doesn't own

---

## Rollback Path (P9)

- Per-tool: revert MCP tool registration; existing recall/wiki_query keep working without team_id param
- Per-migration: each schema change reversible via rollback migration (drop new columns, drop new tables, restore prior constraint set)
- Auth: revoke all bearer tokens; existing YADGAR_MCP_AUTH_TOKEN model unchanged
- Sync state: per-team sync state cache is regenerable; can be wiped without data loss

---

## Dependency Pinning (P10)

NEW external deps (resolve at dispatch):

- Identity library: `python-jose` (JWT validation) — pin major
- OIDC client (v8.1+): `authlib` or similar — pin major
- (v8.4+) SAML enterprise: `python-saml` or similar
- Encryption at rest (v8.4): `cryptography` (already in yadgar) for KMS integration

---

## Agent Dispatch Budget (P11)

v8 plan dispatch is months out. Per-slot estimates:

- v8.0: ~3-4 months engineering, multi-cycle dispatches
- v8.1: ~1.5-2 months
- v8.2: ~2 months
- v8.3: ~2 months (overlap possible with v8.2)
- v8.4: ~2-3 months

Total: 11-14 months for full v8.0 → v8.4 chain. Concurrent dispatches possible across slots once v8.0 architecture stable.

---

## Hard things you'll inherit

1. **Wiki conflict resolution at multi-author scale.** v5.42-v5.46 cycle showed how rough single-author RMW is. Multi-author 10x harder. v5.64 surgical edit primitives are the foundation but not the whole solution.

2. **Auth/authz at MCP boundary.** Every tool call needs identity context. Bearer tokens are simple but enterprises want SSO. Audit log required for compliance.

3. **Search relevance with mixed personal+team scope.** Which to boost? Recency vs anchor signal vs team-visibility flag. Tuning will require user research.

4. **Migration story for existing single-user installs.** Some users will want to upgrade in place, others will side-by-side. Tooling for both.

5. **Pricing decision (DP-F) affects everything downstream.** Managed offering = 3-5x infrastructure cost + ops burden + SLA contracts + on-call rotation. Self-hosted only = revenue path unclear. Hybrid = product complexity.

6. **Privacy posture for the default-private model.** If a user accidentally writes a personal record marked `visibility=team`, it's leaked. Need confirmation prompts + dry-run modes + post-write audit.

7. **Real-time synthesis (v7) sync substrate may or may not extend to v8.2.** Coupling decision affects v8 architecture deeply.

---

## Defer rationale

v8 is 2 majors away. v6 LLM curator + v7 real-time synthesis must ship first. v7 in particular informs sync substrate decisions for v8.2. This skeleton parks the architectural thinking now so v8 dispatch isn't a cold start.

When v8 becomes the next major: re-read this skeleton, resolve DP-A through DP-H, decompose into per-slot plans following the v8.0 → v8.4 outline, dispatch.
