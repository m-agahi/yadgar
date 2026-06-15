# PLAN — v7: Team usability (SKELETON / architecture spec)

**Status:** SKELETON drafted 2026-06-05 evening as "v8 team usability"; RENUMBERED 2026-06-05 night to v7 after PD-44 retired the original v7 ("real-time synthesis" Option B — see DECISIONS.md PD-44 for retire rationale). Hypothetical / architecture-only at this stage. NOT dispatch-ready. Will revisit after v6 (LLM curator scaffolding) crystallizes — v6 batch curator + v5.x write-time gates cover the synthesis use cases the retired v7 was supposed to solve.

**Origin:** User discussion 2026-06-05 — "i need to discuss v8 which is making this beautiful product usable by teams" (original v8 framing; subsequently renumbered to v7 after PD-44 retired old v7). Personal-memory engine value translates into "team second brain" if the architectural tensions are resolved up front.

**Scope:** team collaboration on shared knowledge + memory while preserving the personal-memory contract that gives yadgar its identity. Multi-tenant + federated identity + auth/authz + sharing UX + conflict resolution + privacy/compliance.

**Cross-cutting dependency:** `docs/DECISIONS.md` PD-43 — LLM inference is pluggable, default OFF for personal mode, 4 backend paths (local LLM / remote API / Claude pass-through interactive or headless via OAuth share / team backend). v6 (curator) + v7 (team) inherit this strategy. v7 team-backend path (slot v7.2.5) is the natural home for curator features — strengthens v7 value prop (team gets curator without per-user hardware cost).

**Estimated effort:** 2-3 months engineering for v7.0 foundational layer alone. Multi-quarter cycle for full v7.0-v7.4 chain.

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

## Decision points (must resolve before v7.0 dispatch)

### DP-A — Architecture model

**Decided (skeleton):** Federated personal-first (per rationale above).

**To re-confirm at dispatch time:** has v7 real-time synthesis introduced anything that changes the sync substrate? If v7 ships a peer-to-peer sync layer, v7 reuses it; if v7 stays single-process, v7.2 needs its own.

### DP-B — Default privacy

**Lean:** Default-private + opt-in promote-to-team. Matches federated-personal-first model.

**Tradeoff:**
- Default-private: lower friction for individuals; less default value for new team members joining (no team context to discover until peers explicitly share)
- Default-team: higher discoverability; risk of leaking WIP personal context (e.g., debugging notes with credentials)

**Decision before v7.0:** stay default-private. Provide tooling for bulk-promote-by-tag if onboarding pain real.

### DP-C — Identity provider

**Options:**
- **OIDC** (Google Workspace, Okta, Microsoft Entra) — enterprise-friendly, complex auth flow
- **Forgejo PAT-style bearer tokens** — simple, dev-friendly, no SSO integration cost
- **SAML enterprise** — large org requirement, heaviest integration

**Lean:** Ship Forgejo-style bearer tokens FIRST (v7.0), add OIDC for v7.1+ once team UX validated. SAML only if enterprise customers request.

### DP-D — Conflict resolution on concurrent writes

**Options:**
- **CRDT (Conflict-free Replicated Data Type)** — automatic merge, decentralized, complex to implement correctly. SurrealDB has some primitives but full CRDT semantics on memory + wiki are nontrivial.
- **Merge-request pattern** — explicit human review of conflicting edits. Slower, predictable, low engineering cost.
- **Last-write-wins (LWW)** — simplest, lossy. v5.46.x cycle already showed how rough this is for wiki RMW.

**Lean:** Merge-request for wiki (matches v5.64 surgical edit primitives; explicit promote workflow); LWW + version history for memory (CRDT v7.3+ if peer-to-peer sync ships).

### DP-E — Sharing UX

**Options:**
- **Explicit:** user calls `share_memory(memory_id, team_id)` / `share_wiki(slug, team_id)`. Discoverable, intentional, friction-heavy.
- **Shadow-mirror with annotations:** all writes optionally mirror to team with auto-anonymization. Discoverable by team without owner action. Risk: privacy leak.
- **Auto-promote heuristics:** signal-based (high heat + multiple access + cross-session) auto-suggests "looks like team-relevant — promote?". Best UX if heuristics accurate; risk of bad suggestions.

**Lean:** Explicit (v7.0) → add auto-suggest in v7.1 once team usage patterns observable.

### DP-F — Cost model

**Options:**
- **Self-hosted only** — open source, deploy on own infra. Lowest support burden, lowest revenue path.
- **Managed SaaS only** — yadgar runs team servers, customers pay per team/user/seat. Higher support burden, revenue path.
- **Hybrid** — self-hosted free, managed paid tier with SSO + audit log + SLA. Most ambitious; affects v7 scope by orders of magnitude.

**Lean:** Self-hosted for v7.0 (validate architecture works); decide on managed offering after.

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

## Slot allocation (v7.0 → v7.4)

### v7.0 — Team server foundational

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

### v7.1 — Team wiki + curation

**Goal:** human-in-the-loop curation of team knowledge.

**Deliverables:**
- Merge-request workflow for team wiki edits (uses v5.64 surgical edit primitives + v5.41 versioning)
- "Propose to team" UI for promoting personal pages to team scope (review queue)
- Conflict resolution on concurrent edits: surface as merge-request, not auto-merge
- Team audit log: query "who edited X", "what changed today"
- Curator role (team admin) for resolving merge requests + retiring stale team knowledge

**Effort:** ~1.5-2 months.

---

### v7.2 — Federated personal-team sync

**Goal:** personal yadgar pulls team facts; team yadgar can pull opt-in personal anchors.

**Deliverables:**
- Sync protocol: HTTP poll OR webhook push (decide at dispatch; webhook needs team server to be reachable)
- Bidirectional: pull team-shared into personal recall (boost ranking); push opt-in personal anchors to team
- Conflict resolution: LWW with version history for memory; merge-request for wiki
- Sync state cache + reconciliation on disconnect/reconnect
- Opt-in granularity: user controls which personal anchors push to team

**Effort:** ~2 months.

---

### v7.2.5 — Team-backend inference (per PD-43)

**Goal:** team server hosts curator + synthesis inference (LLM backend) for the team. Unblocks v6+v7 features for users without local hardware.

**Deliverables:**
- Team server runs curator/synthesis backend (local LLM on team infra OR remote API with team's pooled budget)
- Per-team config: `inference.backend = local:<model> | api:<provider> | claude-shared-account`
- Team members opt into using team backend vs their own (per-user override)
- Audit log: per-inference attribution (which user's task triggered it)
- Cost accounting: per-user metering for budget/billing
- Curator runs nightly on team-shared knowledge with team-server inference (personal yadgar may opt to sync curator outputs into personal recall)

**Effort:** ~2 months. Could overlap with v7.2 federated-sync.

**Inference path matrix (per PD-43, applies to all v7 deployments):**

| User has | Personal mode default | Team mode default |
|---|---|---|
| Local hardware (22+ GB RAM, GPU) | OFF (opt-in `local:<model>`) | inherit team-backend OR override `local:<model>` |
| No local hardware | OFF (opt-in `api:<provider>` or `claude-passthrough`) | inherit team-backend (no per-user cost) |
| Heavy Claude usage | OFF (opt-in `claude-passthrough:headless` — needs OAuth share to daemon) | irrelevant if team-backend handles inference |

---

### v7.3 — Multi-agent coordination

**Goal:** concurrent Claude instances on the same team work coherently.

**Deliverables:**
- Shared recall cache (team server caches recent team_recall results; clients consult cache before hitting DB)
- Trace propagation: yadgar tool calls across multi-agent sessions tagged with shared trace_id
- Lock-free conflict resolution: CRDT-lite for wiki (limited to v5.64 surgical edit primitives — text-anchor + positional with version guards); LWW for memory writes (rare for the same record to be concurrently written)
- Multi-agent SubagentStop coordination: team subagents flush to team queue, not personal

**Effort:** ~2 months. Possibly subsumes v7.2 federated-sync work.

---

### v7.4 — Privacy + compliance

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
- **§ Sync protocol (v7.2)** — NEW section once sync substrate decided (HTTP poll vs webhook)
- **§ Visibility scoping** — NEW section, expands existing branch + directory context model with team + role
- **§ Audit trail** — NEW section

Architecture updates land BEFORE v7.0 dispatch per P1 invariant.

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
| Workflow rule "every doc on master" | preserves | v7 plan lands on master once architecture committed |

---

## Config Knob Lifecycle (P3)

NEW knobs for v7.0:

- `YADGAR_TEAM_ENABLED` (bool, default `false`) — opt-in flag for team-aware mode
- `YADGAR_TEAM_SERVER_URL` (str, default empty) — URL of team yadgar instance
- `YADGAR_TEAM_AUTH_TOKEN` (secret str, default empty) — bearer token for team server
- `YADGAR_TEAM_DEFAULT_VISIBILITY` (str, default `"private"`) — `private | team | role:<name>`
- `YADGAR_TEAM_SYNC_INTERVAL_SECONDS` (int, default 300) — federated sync poll interval

All knobs need yaml incremental-sync logic per X5 pattern (`yadgar config sync`).

---

## Schema Constraint Lifecycle (P4)

NEW migration (v7.0, post-v5.46.x cycle close + post-v6 + post-v7):

- `wiki_page.team_id` NOT NULL (default `"personal"` for existing rows; backfill before constraint)
- `wiki_page.visibility` NOT NULL DEFAULT 'private'
- `memory.team_id` NOT NULL (default `"personal"`)
- `memory.visibility` NOT NULL DEFAULT 'private'
- NEW table `team` (team_id PRIMARY KEY, name str, created_at, audit_log_enabled bool)
- NEW table `team_member` (team_id, user_id, role str, added_at)
- NEW table `audit_log` (team_id, user_id, action, target_id, timestamp, metadata)
- NEW table `share_request` (id, team_id, source_record_id, requested_by, status, reviewed_by) — for merge-request workflow in v7.1

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
| `docs/PLAN_V5_64_WIKI_EDIT_PRIMITIVES.md` | v7.1 merge-request workflow reuses v5.64 surgical edit primitives for collaborative wiki editing |
| `docs/PLAN_V6_*.md` (LLM curator scaffolding) | v6 ships before v7; team server can opt into curator suggestions for team knowledge |
| `docs/PLAN_V7_*.md` (real-time synthesis, TBD) | v7 sync semantics inform v7.2 federated-sync design |
| Future `docs/PLAN_V7_0_TEAM_SERVER.md` | TBD when dispatch-ready; ships AFTER v7 |

---

## Bug Class Precedent (P7)

**Precedent 1 — branch-on-wiki rationale (v5.0 → v5.50+ archaeology):** speculative-infrastructure pattern. v7 must not ship team features that have zero users. Mitigation: every v7 tool gets a usage-metric counter pre-dispatch; if zero non-test usage after 1 month live, evaluate retire.

**Precedent 2 — wiki RMW corruption class:** multi-author wiki edits 10x harder than single-author. Mitigation: v7.1 explicit merge-request workflow (no auto-merge) + v5.64 surgical edits as foundation.

**Precedent 3 — secret leak class (v5.42.x cycle):** auth tokens at MCP boundary need defense-in-depth; never log token contents.

**Verification probes (post-v7.0 ship):**
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
- OIDC client (v7.1+): `authlib` or similar — pin major
- (v7.4+) SAML enterprise: `python-saml` or similar
- Encryption at rest (v7.4): `cryptography` (already in yadgar) for KMS integration

---

## Agent Dispatch Budget (P11)

v7 plan dispatch is months out. Per-slot estimates:

- v7.0: ~3-4 months engineering, multi-cycle dispatches
- v7.1: ~1.5-2 months
- v7.2: ~2 months
- v7.3: ~2 months (overlap possible with v7.2)
- v7.4: ~2-3 months

Total: 11-14 months for full v7.0 → v7.4 chain. Concurrent dispatches possible across slots once v7.0 architecture stable.

---

## Hard things you'll inherit

1. **LLM inference hardware barrier (PD-43).** v6 curator + v7 synthesis require LLM inference. Local LLM gates out individual users (22+ GB RAM minimum for usable model). Per PD-43: pluggable backend with 4 paths (local / API / Claude pass-through interactive or headless / team backend). v7 team-backend is the value-prop pivot — feature parity for users who don't have hardware, at team-shared infra cost. Personal mode keeps features default-OFF; explicit opt-in + backend choice.

2. **OAuth share for headless Claude pass-through (per PD-43, VALIDATED 2026-06-05).** Background curation requires non-interactive Claude — `claude -p` runs in ephemeral container with bind-mounted `~/.claude/.credentials.json:ro`. Proof-of-concept probe succeeded. Yadgar daemon scope-restricts use to curator/synthesis only. Trust boundary continuation (daemon already holds secrets). Audit log per invocation. Production needs pre-built `docker.io/openfantasy/yadgar-curator:VER` image with claude-code baked in (drops cold-start from ~20-30s npm install to ~1-2s) — v6 plan deliverable, parallel to yadgar-ci image (PD-42).

3. **Wiki conflict resolution at multi-author scale.** v5.42-v5.46 cycle showed how rough single-author RMW is. Multi-author 10x harder. v5.64 surgical edit primitives are the foundation but not the whole solution.

2. **Auth/authz at MCP boundary.** Every tool call needs identity context. Bearer tokens are simple but enterprises want SSO. Audit log required for compliance.

3. **Search relevance with mixed personal+team scope.** Which to boost? Recency vs anchor signal vs team-visibility flag. Tuning will require user research.

4. **Migration story for existing single-user installs.** Some users will want to upgrade in place, others will side-by-side. Tooling for both.

5. **Pricing decision (DP-F) affects everything downstream.** Managed offering = 3-5x infrastructure cost + ops burden + SLA contracts + on-call rotation. Self-hosted only = revenue path unclear. Hybrid = product complexity.

6. **Privacy posture for the default-private model.** If a user accidentally writes a personal record marked `visibility=team`, it's leaked. Need confirmation prompts + dry-run modes + post-write audit.

7. **Real-time synthesis (v7) sync substrate may or may not extend to v7.2.** Coupling decision affects v7 architecture deeply.

---

## Defer rationale

v7 is 2 majors away. v6 LLM curator + v7 real-time synthesis must ship first. v7 in particular informs sync substrate decisions for v7.2. This skeleton parks the architectural thinking now so v7 dispatch isn't a cold start.

When v7 becomes the next major: re-read this skeleton, resolve DP-A through DP-H, decompose into per-slot plans following the v7.0 → v7.4 outline, dispatch.
