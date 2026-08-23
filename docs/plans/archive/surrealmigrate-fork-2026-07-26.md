# Surrealmigrate fork + selective upstream PRs

**Task:** TBD (proposed — see §6.1) — fork `surrealdb/surrealkit` for dogfooding LLM authoring + Python + embedded-mode + multi-namespace work, while upstreaming small human-authored Flyway-parity gap items (`info`, `--dryRun`, `validate`, `baseline`) directly to the canonical repo.
**Status:** INVESTIGATION PHASE — proposal, no commitments. Review-only.
**Builds on:** [[yadgar-investigation-migration-script-system-2026-07-26]] (the parent investigation plan), `docs/plans/archive/build-vs-buy-library-audit-2026-07-12.md` (KEEP-CUSTOM verdict, now partially outdated), `docs/plans/settings-to-db-config-migration-2026-07-24.md` (sister train).
**Related:** ADR-0163 (DB-backed runtime_config — sanctioned-write-path model), ADR-0078 (DB isolation), ADR-0124 (MCP-routed migrations precedent for *content*, not yet for *schema*).
**Date:** 2026-07-26

## 1. The proposal

### 1.1 What
Fork `surrealdb/surrealkit` (Apache-2.0, founder-co-authored, v0.7.0-pre as of 2026-07-26) into a yadgar-owned repo. Use the fork as the working copy for LLM-assisted and Python-side migration features yadgar needs. Upstream small Flyway-parity gap items (`info`, `--dryRun`, `validate`, `baseline`) as tight human-authored PRs to the canonical repo. Keep the LLM surface in the fork.

### 1.2 Why
- **surrealkit is the right architecture** for SurrealDB migrations. It directly addresses every primitive the 2026-07-12 build-vs-buy audit named as missing (declarative schema-as-code, phased rollout, rollback, resume state, embedded mode, programmatic API). Forks of an architecture-correct canonical-org project are cheaper than competing with them.
- **OSS community is not fond of LLM-generated features and PRs** (per user observation, 2026-07-26). Maintainers do not want to review 2000-line LLM-authored PRs or own the debugging/security-review of AI-generated code paths. Trying to upstream the LLM features directly will fail, or be merged-but-shorn-of-the-AI-part, wasting the work.
- **The Flyway-parity gap items are general-purpose and small.** `info` (status), `--dryRun` (preview), `validate` (body fingerprint), `baseline` (mark existing DB at v) are 50-300 LOC each, design-rationale-friendly, and every SurrealDB user benefits. These will be accepted upstream if the PR is tight and human-authored.
- **The yadgar migration runner is small (30 LOC) but the 25 SurrealQL migration bodies carry domain semantics** (HNSW migration 001, bi-temporal edges 007, branch backfill TX 004:97-112, embedded-vs-server index split `_init_schema:1286-1351`) that a generic diff tool doesn't model. Yadgar needs an LLM authoring tool that **knows the yadgar domain** — that's the fork's differentiator, not the fork's competitor to surrealkit.

### 1.3 The fork model
**Repo:** `m-agahi/surrealmigrate` (proposed) — explicit, low-ambiguity placement under the user's personal GitHub org. See §6.2 for naming alternatives.

**Tier 1 — Dogfood in fork, never upstream:**
- `surrealmigrate author` (LLM authoring CLI/MCP) — LLM proposes migration body + backfill + test skeleton + CAPABILITY_REGISTRY diff + MIGRATION_NOTES.md diff
- `surrealmigrate plan --llm` (LLM-proposed migration plan)
- `surrealmigrate validate --against-dogfood <config>` (dogfood-specific validator hooks)
- Python binding via PyO3 (if surrealkit stays Rust-only)
- `surrealdb-py` embedded-mode path (gap #8 from the parent plan's §3.2 table)
- Multi-namespace flow (gap #7 — v7-team-usability work)
- LLM-authored test scaffolds
- Yadgar-specific hooks (e.g. `validate --memory-blocks` that knows the yadgar block schema)

**Tier 2 — Upstream PR, but human-authored and small:**
- `surrealkit info` (status dashboard; query `schema_version` vs `_MIGRATIONS`, list pending with per-migration `expected_row_delta`)
- `surrealkit --dryRun` for `rollout` (preview phase without writing)
- `surrealkit validate --checksum` (body fingerprint; detect drift between catalogued migration and on-disk SQL)
- `surrealkit baseline <version>` (insert `schema_version` rows ≤ version without running `fn`s)
- Total scope: ~50-300 LOC each, design-rationale-friendly, general-purpose.

**Tier 3 — Upstream PR if there's a maintainer who wants it (proactive, but no pressure):**
- True down-migration synthesis (paired forward+reverse SQL alongside each migration)
- Rename vs drop+add inference (declarative `rename` statement that translates to a series of `DEFINE FIELD OVERWRITE` + data migration)

### 1.4 Operating rules for the fork
To avoid the **fork-rot problem** (where a fork falls 6 months behind upstream and the merge becomes impossible):

1. **Weekly rebase onto upstream master.** CI must pass after every rebase — no "we'll fix it later" merges. Owner: yadgar maintainer (user + AI agents).
2. **Tier 2 PRs go upstream FIRST, then into the fork.** Don't carry a Tier 2 patch in the fork for more than 2 weeks waiting on upstream review. If upstream rejects, the fork keeps it as a patch on top, but the PR was made.
3. **Tier 1 features land behind feature flags** in the fork (`--features llm-author`, `--features py-binding`, etc.) so the LLM code paths are opt-in. This makes rebases less painful — the upstream-mergeable surface stays clean.
4. **CI on the fork runs the full yadgar migration test suite against the fork.** This is the dogfood loop: every fork change must keep all 25 yadgar migrations green. If it doesn't, the fork change is wrong.
5. **Public position:** the fork is explicitly "yadgar's working copy of surrealkit + dogfooded LLM features." README says "if you want LLM authoring or Python bindings, use this fork; if you want the canonical tool, use `surrealdb/surrealkit`." No pretense of competing.
6. **Time-box the fork.** After 6-12 months, re-evaluate: (a) are the LLM features valuable and used? (b) does the maintainer want any of them upstream? (c) is the fork sustainable as a separate repo? The honest probability that the fork exists in 18 months as a separate repo is maybe 30-40%; the probability the LLM features exist in some form is much higher.

### 1.5 Why this works (and what the alternatives miss)

| Alternative | Why it doesn't work |
|---|---|
| **Clean-slate Python framework** | Competes with the canonical-org tool on its core surface; loses on Rust ergonomics + adoption signal; wrong fight. |
| **Pure upstream PRs for LLM features** | Maintainer reviews reject LLM-authored PRs as a class; wastes the work. |
| **Pure fork, no upstream engagement** | Fork rot; the LLM features become irrelevant because they don't track upstream improvements. |
| **Wait for surrealkit v1.0 + PyO3 binding** | Yields the v7-team-usability window entirely. Yadgar needs the dogfooded authoring tool by v5.166 to land the migration operator experience train on time. |
| **Build a new framework that handles "all sorts of migrations"** | The right architecture (sync + rollout + state tables) already exists in surrealkit. Building it again is wasted effort. The differentiation has to be on the **layers above** surrealkit: Python ergonomics, LLM authoring, embedded-mode, multi-namespace, Flyway-parity gap items. |

## 2. Scope and deliverables

### 2.1 Fork bootstrap
- Create `m-agahi/surrealmigrate` (or agreed name — see §6.2).
- Initial commit = `git clone surrealdb/surrealkit && git remote rename origin upstream && git remote add origin git@github-personal:m-agahi/surrealmigrate.git && git push -u origin master`.
- README: explain the fork's purpose, point to upstream, list the Tier 1/2/3 split.
- LICENSE: keep Apache-2.0 (upstream's license). Add `FORK_NOTICE.md` per Apache-2.0 §4(d) (state changes).
- CI: mirror upstream's CI; add yadgar-migration-test-suite as a downstream job.
- Branch protection: master requires CI green + rebase-up-to-date check.

### 2.2 Tier 1 features (in fork, dogfooded by yadgar)
- **T1.1 — `surrealmigrate author` CLI + `surrealmigrate_author` MCP:**
  - LLM proposes `_migration_NNN_<desc>.py` body + backfill UPDATE + test skeleton + CAPABILITY_REGISTRY diff + MIGRATION_NOTES.md diff
  - Context sources (via yadgar `recall`): prior migrations in same area, current table schema, CAPABILITY_REGISTRY entries, MIGRATION_NOTES.md planned changes
  - Validates by running `surrealmigrate status --dryRun` against a sandboxed copy of the DB
  - Refuses to write if any of the 4 outputs is empty
  - NEVER applies; always "draft-and-validate"
- **T1.2 — PyO3 binding for `surrealkit::MigrationRunner`:** expose `MigrationRunner.new(&db).up()` to Python. Optional — only if the Rust-only limitation blocks yadgar's dogfood.
- **T1.3 — `surrealdb-py` embedded-mode path:** the `_db_url is None` branch in `yadgar/_shared/storage/_init_schema:1286-1351` uses the Python v2 SDK's embedded mode. Surrealkit doesn't model this surface. The fork adds a `--db-mode=python-embedded` flag that talks to the Python SDK via the binding.
- **T1.4 — Multi-namespace flow:** `surrealmigrate apply --namespaces ns1,ns2,ns3 --coordinated` rolls a single migration across N namespaces in lockstep with per-ns `schema_version` tracking. Needed for v7-team-usability.
- **T1.5 — Yadgar-specific validator hooks:** `surrealmigrate validate --memory-blocks` knows the yadgar block schema, `surrealmigrate validate --bitemporal` enforces the `valid_from`/`valid_until` semantics, etc.

### 2.3 Tier 2 features (upstream PRs)
- **T2.1 — `surrealkit info`:** query `schema_version` table vs registered `__entity` migrations, list pending with per-migration `expected_row_delta`. Implementation: ~150 LOC, single new CLI subcommand. Design rationale: matches Flyway's `info` verb; every SurrealDB user with a non-trivial migration set wants this. Pre-PR: open an issue on surrealdb/surrealkit asking if the maintainer wants it, link to a draft PR.
- **T2.2 — `surrealkit --dryRun`:** for `rollout start/complete/rollback`. Implementation: ~80 LOC (the existing flow has a `txn` flag that can be repurposed; or a new `dry_run: bool` field on the `RolloutPlan` struct). Design rationale: matches Flyway's `-dryRun`; operator safety. Pre-PR: same — issue + draft PR.
- **T2.3 — `surrealkit validate --checksum`:** SHA-256 fingerprint of the migration body, stored in `__entity` row, compared on `validate` run. Implementation: ~100 LOC. Design rationale: detects drift between catalogued and on-disk SQL. Pre-PR: issue + draft PR.
- **T2.4 — `surrealkit baseline <version>`:** insert `__entity` rows for everything ≤ version without running any migration. Implementation: ~50 LOC. Design rationale: matches Flyway's `baseline`; fresh-install-on-existing-DB flow. Pre-PR: issue + draft PR.

### 2.4 Tier 3 features (upstream if interested, fork otherwise)
- **T3.1 — True down-migration synthesis:** paired forward+reverse SQL. Migration file format becomes:
  ```surql
  -- migrate:up
  DEFINE FIELD last_audit_at ON memory TYPE option<datetime>;
  UPDATE memory SET last_audit_at = created_at WHERE last_audit_at IS NONE;

  -- migrate:down
  UPDATE memory SET last_audit_at = NONE;
  REMOVE FIELD last_audit_at ON memory;
  ```
  Implementation: ~200 LOC + migration-file format change. Design rationale: true reversible migrations. Risk: format change may break existing users; surrealkit is pre-1.0 so a format change is cheap to land now.
- **T3.2 — Rename vs drop+add inference:** declarative `rename` directive that translates to a series of `DEFINE FIELD OVERWRITE` + data migration. Implementation: ~300 LOC + new statement type. Design rationale: matches the yadgar migration 016/018/023 `relax→update→re-tighten` pattern in declarative form. Risk: significant new feature.

### 2.5 What the fork is NOT
- **Not a competitor to surrealkit.** The README says so explicitly. No "surrealkit is dead, long live surrealmigrate" marketing.
- **Not a Python rewrite of the Rust core.** The Tier 1 PyO3 binding is the surface; the migration body execution stays in Rust.
- **Not an LLM wrapper around arbitrary SurrealDB operations.** Scope is migration-specific; not a general "AI for SurrealDB" tool.
- **Not a SaaS.** Self-hosted only, no telemetry, no account model.

## 3. Phased rollout

### Phase 0 — Decision (no code)
Same as the parent plan's Phase 0: confirm scope, ordering, ownership, time-box.

### Phase 1 — Fork bootstrap
- 1.1: Create `m-agahi/surrealmigrate` (or agreed name)
- 1.2: Mirror upstream; CI green on the empty fork
- 1.3: README + FORK_NOTICE.md + LICENSE
- 1.4: Yadgar migration test suite runs against the fork in CI (downstream job)

### Phase 2 — Tier 2 upstream PRs (start the engagement)
- 2.1: Open `surrealkit info` design issue on surrealdb/surrealkit (link to draft PR)
- 2.2: Open `surrealmigrate --dryRun` design issue
- 2.3: Open `surrealmigrate validate --checksum` design issue
- 2.4: Open `surrealmigrate baseline` design issue
- 2.5: Each design issue → draft PR if maintainer signals interest

### Phase 3 — Tier 1 fork-only features
- 3.1: `surrealmigrate author` (LLM authoring) — biggest single feature
- 3.2: PyO3 binding (if needed for yadgar dogfood)
- 3.3: `surrealdb-py` embedded-mode path
- 3.4: Multi-namespace flow (v7-team-usability work)
- 3.5: Yadgar-specific validator hooks

### Phase 4 — Tier 3 (upstream or fork, maintainer decision)
- 4.1: Down-migration synthesis (propose upstream; fork if rejected)
- 4.2: Rename inference (propose upstream; fork if rejected)

### Phase 5 — Time-boxed re-evaluation
- 5.1: At 6 months: are Tier 1 features used? Maintainer engagement signal?
- 5.2: At 12 months: keep / merge / re-evaluate decision

## 4. Constraints and risks

### 4.1 I32 / I25 / I29 touchpoints
- **I32 (CAPABILITY_REGISTRY):** the fork adds new CLI subcommands and MCP surface; each must be catalogued in `docs/contracts/CAPABILITY_REGISTRY.md` (yadgar-side, since the fork is a separate repo).
- **I25 (config three-way sync):** orthogonal. The fork's config knobs (e.g. `SURREALMIGRATE_LLM_MODEL`) need their own three-way sync story.
- **I29 (no-dead-capability):** every fork-only feature that gets used by yadgar must be in the yadgar CAPABILITY_REGISTRY. Features that ship in the fork but yadgar doesn't use are dead — delete them per I29.

### 4.2 OSS community norms
- **License:** keep Apache-2.0. Add FORK_NOTICE.md. Don't relicense to MIT/BSL — that's the most common fork-rot trigger.
- **Attribution:** README clearly states "this is a fork of surrealdb/surrealkit; all original work © SurrealDB Ltd. and contributors, Apache-2.0." Don't strip the upstream copyright headers.
- **No co-mingling of code provenance.** When upstream merges a Tier 2 PR, the code should be cleanly attributable to the yadgar fork's author. When yadgar re-syncs from upstream, the merge is a clean rebase.
- **Don't upstream without the maintainer's blessing on the timeline.** Tier 2 PRs are made when the maintainer signals interest (via issue response, Discord, etc.), not on the yadgar maintainer's deadline. If the maintainer is slow, the fork can carry the feature as a patch on top while waiting.

### 4.3 Dogfood risk
- **Yadgar depends on the fork, which depends on upstream.** A 6-month upstream feature gap or breaking change can break the fork. Mitigation: weekly rebase + CI on every rebase; pinned dependency in yadgar's `pyproject.toml` (not floating `main`).
- **The fork's CI runs the yadgar migration test suite.** This is the dogfood loop. If the fork breaks yadgar, the fork change is wrong. This is a feature, not a bug.
- **Fork maintenance is real work.** ~1-2 hours/week for rebase + CI triage. Owner: yadgar maintainer. If the work is unowned, the fork rots.

### 4.4 LLM-PR aversion (the user-stated constraint)
- The fork **is** the answer to the LLM-PR aversion. Tier 1 (LLM features) stays in the fork; Tier 2 (general-purpose) goes upstream as small human-authored PRs.
- If the maintainer ever signals interest in LLM features (after seeing the fork's Tier 1 mature), the path is: "take the authoring tool as a separate repo that calls into surrealkit as a library" or "hand the maintainer a clean PR with a human design rationale and a curated subset of the LLM features." Both are maintainer-driven decisions on the maintainer's timeline.

## 5. Test plan

### 5.1 Fork bootstrap tests
- `git clone m-agahi/surrealmigrate && cargo build && cargo test` — must mirror upstream's test suite green.
- `git remote -v` — must show `upstream = surrealdb/surrealkit` and `origin = m-agahi/surrealmigrate`.
- README must contain the word "fork" within the first 100 chars (linter check).

### 5.2 Tier 2 upstream PR tests
- Each Tier 2 PR has a `tests/integration/<feature>.rs` test in the fork that runs against a fresh SurrealDB instance.
- PR description includes: "Closes #<design-issue>" + "Design rationale: <50-100 words>" + "Tested on: yadgar's 25-migration suite + 1 fresh-DB scenario."
- CI must pass on the PR branch.

### 5.3 Tier 1 fork features tests
- `surrealmigrate author` golden-file tests: N hand-crafted change descriptions produce expected output, validated against `surrealmigrate status --dryRun`.
- PyO3 binding: importable from Python, basic `MigrationRunner.new(&db).up()` works against a Python v2 SDK DB.
- Multi-namespace flow: spin up 3 namespaces, apply migration, assert per-ns `schema_version` row created in lockstep.
- Yadgar-specific validators: golden-file tests for `validate --memory-blocks` + `validate --bitemporal`.

### 5.4 Yadgar dogfood loop
- Yadgar's CI runs the full migration test suite against the fork.
- Failure → fork change is wrong; fix before merge.
- The 25 existing migrations must remain green across every fork release.

## 6. Open questions for user

### 6.1 Task tracking
**This plan needs a task in the harness task list.** Propose: **#48 (harness #0051) — "Surrealmigrate fork: bootstrap + Tier 2 upstream PRs".** The task should mirror what's in the parent plan's task #45/#0048: investigation artifact complete, design proposed, no commitments. Review-only. Add to the yadgar task-list wiki page when approved.

**Subtasks (proposed, in the harness list):**
- Fork bootstrap: create `m-agahi/surrealmigrate`, mirror upstream, README + LICENSE, CI green.
- Tier 2 PR 1: `surrealkit info` design issue + draft PR.
- Tier 2 PR 2: `surrealkit --dryRun` design issue + draft PR.
- Tier 2 PR 3: `surrealmigrate validate --checksum` design issue + draft PR.
- Tier 2 PR 4: `surrealkit baseline` design issue + draft PR.
- Tier 1 feature 1: `surrealmigrate author` (LLM authoring CLI + MCP).
- Tier 1 feature 2: PyO3 binding (if needed for yadgar dogfood).
- Tier 1 feature 3: `surrealdb-py` embedded-mode path.
- Tier 1 feature 4: Multi-namespace flow.
- Time-boxed re-evaluation: 6-month and 12-month checkpoints.

### 6.2 Repo name and org placement
- **Option A: `m-agahi/surrealmigrate`** (proposed) — user's personal GitHub org. Aligns with the 2026-07-25 migration of yadgar to `m-agahi`. Pros: clear ownership, no org politics. Cons: tied to one maintainer; if maintainer disappears, fork is unmaintained.
- **Option B: `openfantasy/surrealmigrate`** — the org the yadgar core/backend repos live under. Pros: same home as yadgar. Cons: blurs the line between yadgar-the-product and surrealmigrate-the-tool.
- **Option C: `yadgar-contrib/surrealmigrate`** — a new org for yadgar-side forks. Pros: explicit "this is a yadgar-side fork." Cons: another org to maintain.

Recommendation: **Option A** (matches the yadgar migration precedent and the user's stated preference for personal-org ownership of side projects). Confirm?

### 6.3 Time-box
- 6 months or 12 months before re-evaluation? Recommendation: 6 months for the first checkpoint, 12 months for the keep/merge decision.
- 6-month checkpoint question: "are Tier 1 features used? is the maintainer engaging with Tier 2 PRs?"
- 12-month decision: keep / merge-back / re-evaluate / sunset.

### 6.4 Tier 1/2/3 split
- Is this the right line? Specifically:
  - Should `validate --checksum` be Tier 2 (general-purpose, every SurrealDB user wants it) or Tier 1 (yadgar-specific, since yadgar's `__entity` schema is its own)? Recommendation: Tier 2 (general-purpose).
  - Should `baseline` be Tier 2 or Tier 1? Recommendation: Tier 2 (general-purpose; matches Flyway).
  - Should down-migration synthesis (T3.1) be Tier 3 or Tier 1? Recommendation: Tier 3 (significant feature, needs maintainer buy-in on the format change).

### 6.5 Phase 0.5 schema-files reorg
The parent plan proposes a Phase 0.5: reorganise the 25 yadgar migration bodies + the `_init_schema` declarative section as `database/schema/*.surql` files (mirror surrealkit layout without depending on it). Question: should this happen **before** the fork bootstrap (so the fork's first dogfood target is already laid out) or **after** (so the fork bootstrap is just upstream mirror + Tier 1/2 features)?

Recommendation: **before** — the reorg is cheap (~2h) and makes the fork's first Tier 1 work (`surrealmigrate author`) have a clearer input surface.

### 6.6 LLM-PR aversion specifics
- The user observed the OSS community is not fond of LLM-generated features and PRs. Confirm: does this apply to **code authored by an LLM under human review** (acceptable) or **code generated end-to-end with no human review** (unacceptable)? My read: most maintainers are fine with LLM-assisted code as long as a human owns the design rationale and has reviewed the diff. The fork model assumes this read. Confirm?

## 7. Yadgar findings footer (handoff contract)

For any subagent that picks up this plan in a follow-up session, the Yadgar findings are:

- **The 2026-07-12 build-vs-buy audit's "no SurrealQL migration framework exists" claim is factually true but the spirit ("nothing serious exists in any ecosystem") was invalidated 12 weeks before the audit** by the launch of `surrealdb/surrealkit` (2025-09-19) and the Odonno redirect (2026-04-11). The audit's evidence base should be revised to name surrealkit explicitly and re-cost the build-vs-buy against it.
- **Fork + selective upstream PRs is the right operating model for LLM-heavy features** when the canonical maintainer is LLM-averse. Tier 1 (LLM features, Python, embedded-mode, multi-namespace) stays in the fork. Tier 2 (general-purpose Flyway-parity gap items) goes upstream as small human-authored PRs. Tier 3 (significant features) goes upstream if the maintainer signals interest, fork otherwise.
- **The fork's differentiator is not the Rust core** (surrealkit is the right architecture for that). The differentiator is the layers above: Python ergonomics, LLM authoring, embedded-mode path, multi-namespace flow, Flyway-parity gap items. Building a parallel Rust framework is the wrong fight.
- **The 25 yadgar migration bodies carry domain semantics** (HNSW migration 001, bi-temporal edges 007, branch backfill TX 004:97-112, embedded-vs-server index split `_init_schema:1286-1351`) that a generic diff tool doesn't model. The LLM authoring tool needs to know the yadgar domain — that's the fork's value-add, not the fork's competitor to surrealkit.
- **No LLM/agent migration tool targets SurrealDB.** Migration co-pilot market for SurrealDB doesn't exist yet. Yadgar is a representative consumer; if the fork's Tier 1 features prove valuable, they're valuable to the broader SurrealDB Python ecosystem (vector store users, AI-memory projects).
- **Fork-rot is the dominant risk.** Weekly rebase + CI on every rebase + pinned dependency in yadgar's `pyproject.toml` are the mitigations. Time-box: 6-month checkpoint, 12-month keep/merge decision.
- **No wiki page** is titled or tagged "surrealmigrate" or "surrealkit fork." The investigation docs (parent plan + this plan) are the first curated entries; promote to wiki page once approved.
