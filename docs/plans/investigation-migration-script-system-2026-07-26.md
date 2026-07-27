# Migration-script system — investigation & design plan

**Task:** #45 (harness #0048) — investigate the DB migration-script system now that yadgar has multiple users.
**Status:** INVESTIGATION PHASE — survey complete, design proposed, no commitments. Review-only.
**Builds on:** [[yadgar-adr-0078]] (DB isolation), [[yadgar-adr-0124]] (MCP-routed migrations precedent), [[yadgar-adr-0163]] (sanctioned-write-path model), `BC-ST2` (forward-deterministic, no data loss — `⏳[r] P1`).
**Related plans:** `docs/plans/settings-to-db-config-migration-2026-07-24.md` (sister train; same operator-experience vocabulary), `docs/plans/db-audit-fix.md` (live data-remediation pattern), `docs/plans/v7-team-usability.md` (long-horizon multi-tenant angle), `docs/plans/security-stack-skeleton-2026-07-13.md` (multi-namespace migrations = L-scale downstream work).

## 1. The problem

### 1.1 User statement (2026-07-26)
"Investigate the DB migration-script system now that yadgar has multiple users."

The implicit ask: when yadgar was a single-user dogfood, operator-runnable Python scripts in `scripts/migrate_*.py` + a `MIGRATION_NOTES.md` handoff worked. With multiple users on different v5.x versions running against shared or independent DBs, that handoff has friction:

- A user upgrading from pre-v5.8 to v5.8 has to read `MIGRATION_NOTES.md` to find `scripts/migrate_v5_7_to_v5_8.py`, understand it, and run it. If they don't, the framework migration runs on first daemon start — no preview, no rollback, no opt-out.
- The v2-format export/reimport scripts (`migrate_v235_to_v204.py`, `migrate_to_server.py`) carry a TABLES list frozen pre-v5.41. Any new table (wiki_page_version, wiki_bookmark, memory_block, runtime_config, bitemporal columns, shadow-gate columns) is silently dropped.
- The two precedent one-offs in `scripts/migrate_*.py` are **operator-runnable StorageEngine scripts**, which ADR-0124 already established is the wrong shape: "Migrate only via the daemon MCP tools; never StorageEngine." But the directive is about *content* migrations (wiki); it's not yet been extended to *schema* migrations.

### 1.2 The shape of the gap
- **No discovery path.** Scripts are findable via `MIGRATION_NOTES.md` or `CHANGELOG.md`, which depend on the maintainer remembering to add an entry. No MCP tool, no CLI verb, no `yadgar migrate --status` to ask "what's my DB at?".
- **No dry-run at the framework level.** The framework always writes. The only `--dry-run` flag in the entire one-off-script inventory lives in `scripts/migrate_v5_7_to_v5_8.py` — which duplicates the framework's migration 008 body verbatim (drift hazard).
- **No apply gate.** Migrations run automatically on first daemon start, on every subsequent start (idempotent-skip), with no user-visible prompt and no pre-flight report of "these N migrations will run; expect these row counts to change".
- **Stale script bodies.** TABLES lists frozen at v5.41, BC-ST2 untested, dead `migrate_adr_monolith.py` still referenced in CHANGELOG (ADR-0124 says remove).
- **No I32 coverage of `scripts/migrate_*`.** The CAPABILITY_REGISTRY catalogues the framework runner (`CAP-STOR-002`) and every individual migration (`CAP-STOR-003..027`); the parallel `scripts/` universe is uncatalogued, so the I32 lint doesn't catch drift between script bodies and framework bodies.
- **No authorship assist.** Writing a new `_migration_NNN.fn()` is high-context work (read prior migration in same area, match house style, backfill safely, write the test, update CAPABILITY_REGISTRY, update MIGRATION_NOTES.md). Currently 100% hand-written by the maintainer.

## 2. Survey of what's there today

### 2.1 Framework — `yadgar/_shared/storage/migrations.py`
- **25 effective `_migration_NNN_*` functions** (001_hnsw_indexes → 027_runtime_config_table; slot 017 reserved for the never-shipped wiki_source_hash table).
- **Runner:** `_MigrationsMixin._run_migrations_locked()`:
  1. `fcntl.flock(STATE_DIR/.migration.lock, LOCK_EX)` — serialises concurrent daemon starts.
  2. `DEFINE TABLE IF NOT EXISTS schema_version SCHEMALESS;` (line 1202).
  3. Iterate `_MIGRATIONS` list in declared order. For each: `SELECT version FROM schema_version WHERE version = $v` — if found, skip.
  4. Else: `migration["fn"](self)`; then `CREATE schema_version SET version=$v, applied_at=ts` (line 1210-1213).
- **Tracking:** single `schema_version` table (SCHEMALESS), rows `{version, applied_at}`. Sentinel is the version string only — no hash, no checksum, no body fingerprint.
- **Idempotency:** per-migration. Most use `DEFINE FIELD/TABLE/INDEX IF NOT EXISTS` + backfill `WHERE field IS NONE` filters in single TXs. Migrations 016, 018, 023 use `relax→update→re-tighten` to dodge SurrealDB v3 ASSERT-on-UPDATE. Migration 013 has an explicit "skip if version row exists" guard.
- **Failure semantics:** if `fn` raises, exception propagates, `schema_version` row is not written, next startup re-runs from scratch (safe iff the migration is idempotent). Lock released in `finally`. No per-migration rollback, no down-migrations. **I16 is deferred** ("better as documented rollback procedure — restore-from-backup OR forward-fix script" — `ARCHITECTURE_INVARIANTS.md:626`).
- **Server-mode only.** `_run_migrations` returns immediately if `_db_url` unset. Embedded (v2 SDK) mode never runs migrations.
- **Trigger sites:** `StorageEngine.__init__` (server mode) → `_init_schema()` → `_run_migrations()` (line 1363). `make setup`, `yadgar setup`, `install`, `seed-anchors` do **not** explicitly invoke migrations; they trigger them transitively via `_wait_for_daemon` in `scripts/install/yadgar-setup.sh:611-656`.
- **DELIBERATELY not replaced with a library** (per `docs/plans/archive/build-vs-buy-library-audit-2026-07-12.md` §6): "Alembic = SQLAlchemy-only; yoyo/Flyway = SQL dialects. No SurrealQL migration framework exists in Python; writing an adapter costs more than the 30-LOC runner. KEEP-CUSTOM." That conclusion holds.

### 2.2 One-off scripts — `scripts/migrate_*`

| Script | Status | Idempotent | Framework-routed | TABLES-list current | Catalogued in I32 | Purpose |
|---|---|---|---|---|---|---|
| `migrate_v5_7_to_v5_8.py` | live | **YES** (`tier IS NONE` filter) | **NO** (embeds `StorageEngine` directly — conflicts with running server per ADR-0124) | n/a | **NO** | Pre-migration of `_migration_008_anchor_tier`. Verbatim duplicate of the framework body; only added value = `--dry-run` flag. |
| `migrate_v235_to_v204.py` | live | **NO** (UPSERT re-stamps timestamps) | **NO** (subprocesses + `surrealdb` SDK) | **STALE** (12 tables, pre-v5.41) | **NO** | SurrealDB binary-version downgrade v2.3.5 → v2.0.4 export/reimport. Single-operator. |
| `migrate_to_server.py` | live | **NO** (same UPSERT caveat) | **NO** (subprocesses + `surrealdb` SDK) | **STALE** (same 12) | **NO** | Embedded `surrealkv` → SurrealDB v2.3.5 server upgrade. Single-operator. |
| `migrate-yadgar-xdg.sh` | live | **YES** (`mv ... 2>/dev/null \|\| true`) | **NO** (filesystem move, not DB) | n/a | **NO** | POSIX `mv` for the 7 v5.47.0 XDG dirs. Legitimately one-off. |
| `migrate_adr_monolith.py` | **DEAD** (ADR-0124 verdict, never runnable) | n/a | **NO** (embedded `StorageEngine`) | n/a | **NO** | Replaced by `/tmp/yadgar-migration-hold/migrate_adr_mcp.py`. Should be removed. |
| `migrate_legacy_protected_to_anchor.py` | **PLANNED** (PD-20) | not written | not written | not written | **NO** | Backfill pre-v5.10.x `memorize(is_protected=True)-without-`_anchor` rows. PD says "defer until audit telemetry shows the gap is large." |

**Capability ledger** — the gap that no canonical survey covers today. To be filled in by the Phase 0.5 work below.

### 2.3 Precedents + prior thinking
- **ADR-0124 (accepted 2026-07-15):** "Migrate only via the daemon MCP tools... never StorageEngine. Preserve ids via `wiki_add(branch_hint=master)+wiki_set_metadata(branch=null)`. Shipped `scripts/migrate_adr_monolith.py` is dead, remove/replace it." → This binds the answer for *content* migrations; **schema migrations are not yet covered by any ADR**.
- **ADR-0078:** "Only backend functions/pipelines touch the DB — core limited to HTTP forwards (+ sanctioned debug APIs)." Establishes the "sanctioned path" language. All `scripts/migrate_*.py` violate this (core-side code opening the DB file).
- **ADR-0163:** DB-backed runtime config (`runtime_config` table, `config_set` MCP tool). The model the migration overhaul would mirror: MCP-routed, PTC-cached, warmup + invalidation, both core and backend reachable.
- **BC-ST2** (`BEHAVIOR_CONTRACT.md:107`): "migrations run forward deterministically, no data loss" — `⏳[r] P1`, **no test today**. Natural acceptance target for the overhaul.
- **`docs/plans/db-audit-fix.md`:** "Per confirmed issue: (a) one-off remediation migration (idempotent, dry-run…)" + "No silent mutation of the live store. Every fix is a reviewed migration." Closest current "one-off migration as a system" thinking; about specific DB rows, not the system around them.
- **`docs/plans/v7-team-usability.md` §"Per-migration"** (line 325): aspirational "each schema change reversible via rollback migration." v7 horizon — post v5.46.x close + post v6 + post v7.
- **`docs/plans/security-stack-skeleton-2026-07-13.md`:** multi-tenancy needs the schema migration to apply across N namespaces — flagged **L (large)**. This is the v7 multi-tenant angle; task #0048 is the **v5.x near-term** "2-3 users on the same daemon" angle.
- **`docs/plans/archive/PLAN_V5_10_X_MEMORIZE_ANCHOR_PARITY.md:163` (PD-20):** "One-shot migration script `scripts/migrate_legacy_protected_to_anchor.py` for pre-v5.10.x `memorize(is_protected=True)-without-`_anchor` rows. Runs idempotent backfill matching v5.8 migration_008 pattern." — same pattern as `migrate_v5_7_to_v5_8.py`, same drift hazard, **PLANNED not written**.

### 2.4 I32 / I16 / I25 / I29 touchpoints
- **I32 (CAPABILITY_REGISTRY):** catalogues framework runner + every migration (`CAP-STOR-002..027`). Does NOT cover `scripts/migrate_*`. Expanding I32 to require `scripts/migrate_*` coverage would be a clean enforcement surface.
- **I16 (migration reversibility):** DEFERRED. Migration overhaul should produce the BC-ST2 acceptance test even if down-migrations are still deferred.
- **I25 (config three-way sync):** orthogonal — migrations don't add Settings fields. Worth keeping in mind only because the runtime_config knobs (`MIGRATION_HTTP_TIMEOUT_SEC`) sit in this train's sister plan.
- **I29 (no-dead-capability):** scoped to the EDGE_CONTRACT domain; dead `migrate_adr_monolith.py` is I29-relevant but I29 doesn't currently catch it. Out of scope to broaden.

## 3. Design proposals (under review)

### 3.1 Taxonomy of migration surface — 4 cases

The 6 scripts in `scripts/migrate_*` + 25 framework migrations break cleanly into 4 cases. Different surface for each:

| Case | Description | Current shape | Future shape |
|---|---|---|---|
| **(a)** | Framework-runnable at startup, pure schema | 25 framework migrations (001-027) | Stays as-is; improve the runner per §4.1-§4.3 |
| **(b)** | Framework-runnable + operator-runnable with dry-run (pre-migration preview) | `migrate_v5_7_to_v5_8.py` (verbatim duplicate of 008) | Push `--dry-run` into the framework; **retire the script** |
| **(c)** | Out-of-framework scope (SurrealDB binary-version or filesystem ops) | `migrate_v235_to_v204.py`, `migrate_to_server.py`, `migrate-yadgar-xdg.sh` | Document as LEGITIMATELY one-off; I32 catalogue entry per script (file, purpose, idempotent, tested) |
| **(d)** | Replaceable with daemon-MCP-routed migration | `migrate_adr_monolith.py` (per ADR-0124) | **Remove** the script + remove its CHANGELOG reference; leave the working `/tmp/yadgar-migration-hold/migrate_adr_mcp.py` pattern as a wiki page |

This taxonomy is the lever for the recommendation. Each case has a different migration surface; conflating them is what's produced the current "pile of scripts" state.

### 3.2 Borrowed-from-Flyway verbs (case-a runner improvements)

| Verb | yadgar proposal | Solves which gap |
|---|---|---|
| **`yadgar migrate status`** (MCP + CLI) | Query `schema_version` vs `_MIGRATIONS`; list pending with per-migration `{expected_row_delta, ops_preview}` from a registry annotation. | "No discovery path" |
| **`yadgar migrate --dry-run`** (MCP + CLI) | Push a `dry_run` flag into the framework; each `_migration_NNN.fn()` accepts `(storage, dry_run=False)`, returns `{would_touch, ops}` when dry-run, no writes. | "No framework-level dry-run" + "duplicate of 008 in `migrate_v5_7_to_v5_8.py`" |
| **`yadgar migrate repair`** (MCP + CLI) | Reconcile `schema_version` to runner's truth. Delete rows for which `fn` no longer exists; surface (don't auto-create) rows for `fn`s present but unrecorded. Operator gates the action. | "Dead `migrate_adr_monolith.py` still leaves residue" + "stale TABLES list" |
| **`yadgar migrate baseline <version>`** (MCP + CLI) | Insert `schema_version` rows for everything ≤ the given version, **without** running any `fn`. For users adopting yadgar on a pre-existing DB. | "Fresh install vs. pre-existing DB" |
| **Apply gate** (UX, no new tool) | Before applying a non-recorded migration, surface a CLI prompt or MCP confirmation: "migration 028 will backfill 142 rows in `memory.tier`. apply? [y/N]". | "No pre-flight" |

Total scope: ~3 PRs, additive, **no existing migration bodies change**. The framework runner grows a `dry_run` parameter; each `fn` is updated to accept (and ignore, for now) the new param. The `_MIGRATIONS` list gains a per-entry annotation registry (`expected_row_delta`, `ops_preview`) populated as a follow-up per-migration PR per migration (25 small PRs OR a script that introspects each `fn` body and proposes values).

### 3.3 Authoring prompt (case-a, future)

Once 3.2 is shipped (so a validator exists), an LLM authoring tool that proposes `_migration_NNN.fn()` bodies in repo-house style, validated by `yadgar migrate status`:

- **CLI:** `yadgar migrate author --add-column memory.tier last_audit_at`
- **MCP:** `yadgar_author_migration(change_description: str)`
- **Sources of context** (via recall):
  - All prior migrations in the same table (migrations.py + `migrate_*.py`)
  - Current table schema (`_init_schema` + `schema_version`)
  - CAPABILITY_REGISTRY.md entries for the table
  - MIGRATION_NOTES.md for the version's planned changes
  - House style: existing migration bodies, esp. the `relax→update→re-tighten` pattern (016, 018, 023)
- **Output:** a proposed `_migration_NNN_<desc>.py` body + a backfill UPDATE + a `test_migration_NNN_<desc>.py` test skeleton + a CAPABILITY_REGISTRY.md diff + a MIGRATION_NOTES.md diff.
- **Validation:** runs `yadgar migrate status --dry-run` to confirm the proposed migration would apply cleanly to a copy of the DB; refuses to write if any of the 4 outputs are empty.
- **Hard NO:** LLM never *applies* a migration. The tool is "draft-and-validate", the operator is "review-and-apply". Aligns with HARD RULE Apply/Import.

### 3.4 What we are NOT proposing (review rejection list)

| Proposal | Why NOT (for now) |
|---|---|
| Replace the framework runner with a third-party lib (Flyway, yoyo, Alembic) | None speak SurrealQL. Writing an adapter costs more than 30-LOC runner + 25 case-tested migrations (`build-vs-buy-library-audit-2026-07-12.md` §6 verdict still holds). |
| Down-migrations / per-migration rollback (I16) | Deferred. BC-ST2 acceptance test is the right first step; rollback machinery is L-scale work. |
| Auto-apply migrations at runtime / agent-driven apply | Violates HARD RULE Apply/Import, ADR-0078, ADR-0124. Operator-gated always. |
| Move `migrate_v235_to_v204.py` + `migrate_to_server.py` into the framework | These are SurrealDB binary-version export/reimport — out-of-scope for `_init_schema` (which only defines schema, never exports/imports data). Legitimately one-off. Document + I32 catalogue. |
| v7 multi-namespace migration story (L-scale) | Out of scope; this is the v5.x near-term. Feed forward into `docs/plans/v7-team-usability.md`. |
| Expand I32 lint to enforce `scripts/migrate_*` coverage automatically | Easy to do but DEFER until §3.2's `migrate_*` retirement decisions are made. Otherwise we'd catalogue scripts we're about to delete. |

## 4. Proposed phases (under review — scope and ordering are decision points)

### Phase 0 — Decision (no code)
1. **Confirm the taxonomy in §3.1.** Specifically: is `migrate_v5_7_to_v5_8.py` worth retiring, or is the verbatim-duplicate shape intentional (e.g. for offline one-shots against a DB the daemon doesn't have the password to)?
2. **Decide the scope of Phase 1.** All 4 verb additions in one v5.166 train, or one at a time?
3. **Decide the apply-gate UX.** CLI prompt only? MCP confirmation only? Both? Silent for known-safe migrations (001-016, additive only)?
4. **Decide authoring-prompt timing.** Ship §3.3 after Phase 1, or after a separate validation cycle?
5. **Decide BC-ST2 fate.** Fold the acceptance test into Phase 1, or separate?

### Phase 1 — Migration operator experience v1 (proposed scope)
- **1.1:** Add `dry_run=False` param to framework runner; each `_migration_NNN.fn` accepts and ignores it (mechanical change, no behaviour change).
- **1.2:** Add per-migration `expected_row_delta` annotation registry (empty dict; populated per-migration as a follow-up).
- **1.3:** New CLI subcommand `yadgar migrate status` (read-only; lists pending + applied; per-migration `expected_row_delta` if known).
- **1.4:** New CLI subcommand `yadgar migrate --dry-run` (dry-run; reports `{would_touch, ops}` per migration).
- **1.5:** New CLI subcommand `yadgar migrate baseline <version>` (insert `schema_version` rows ≤ version, no `fn` calls).
- **1.6:** New CLI subcommand `yadgar migrate repair` (reconcile `schema_version` to runner; surface unrecorded+recorded-missing cases; operator gates).
- **1.7:** Apply gate before non-recorded migration: CLI prompt or MCP confirmation (decision in Phase 0.3).
- **1.8:** BC-ST2 acceptance test (forward-deterministic, no data loss) — fold into this train.
- **1.9:** I32 catalogue entries for each `scripts/migrate_*` per the §3.1 case.

### Phase 2 — Retire duplicated one-off scripts (proposed scope)
- **2.1:** Delete `scripts/migrate_v5_7_to_v5_8.py` (case-b → case-a; the framework's dry-run now covers it). Add MIGRATION_NOTES.md entry.
- **2.2:** Delete `scripts/migrate_adr_monolith.py` (case-d; per ADR-0124; remove CHANGELOG reference). Leave the `/tmp/yadgar-migration-hold/migrate_adr_mcp.py` pattern as a wiki page documenting the operator handoff.
- **2.3:** Defer `migrate_v235_to_v204.py` + `migrate_to_server.py` deletion (case-c; legitimately one-off; I32 catalogue entry is enough).
- **2.4:** I32 lint expansion: require `scripts/migrate_*` coverage in CAPABILITY_REGISTRY (post-retirement, so we're not cataloguing code about to die).

### Phase 3 — Authoring prompt (proposed scope)
- **3.1:** New CLI subcommand `yadgar migrate author --change <description>` (proposes migration body + backfill + test skeleton + CAPABILITY_REGISTRY diff + MIGRATION_NOTES.md diff).
- **3.2:** New MCP tool `yadgar_author_migration(change_description)` (same logic, callable from agents that want to draft a migration).
- **3.3:** Authoring tool uses recall to load context: prior migrations in same area, current table schema, CAPABILITY_REGISTRY entries, MIGRATION_NOTES.md planned changes.
- **3.4:** Authoring tool runs `yadgar migrate status --dry-run` to validate the proposal; refuses to write if any of the 4 outputs is empty.
- **3.5:** Authoring tool NEVER applies; always "draft-and-validate"; operator reviews and runs `yadgar migrate --apply <version>`.

### Phase 4 — v7 multi-namespace migration (deferred)
- **4.1:** Migration applied across N namespaces per-tenant.
- **4.2:** Per-tenant `schema_version` table (or scoped query).
- **4.3:** Coordinated rollout + rollback per namespace.
- **4.4:** Feed forward into `docs/plans/v7-team-usability.md` and `docs/plans/security-stack-skeleton-2026-07-13.md` (L-scale).

## 5. Open questions for user (Phase 0 decisions)

1. **§3.1 case-b retirement:** confirm we should retire `scripts/migrate_v5_7_to_v5_8.py` once §4.1 ships? Or keep it as a "dataless dry-run preview" surface for users who can't run the framework on their DB?
2. **§4 Phase 1 scope:** all 9 items in one v5.166 train ("migration operator experience v1"), or one PR per item, or grouped (1.1-1.4 runner, 1.5-1.6 ops, 1.7-1.9 polish)?
3. **Apply gate UX (§4.1.7):** CLI prompt only, MCP confirmation only, or both? And what's the "known-safe" threshold — additive-only (no UPDATEs/DELETEs), or read-the-`fn`-body-and-decide?
4. **Authoring prompt timing (§4.3):** ship after Phase 1 (recommended) or earlier as a thin prototype to validate the recall context?
5. **BC-ST2 acceptance test (§4.1.8):** fold into Phase 1, or split into a separate "migration safety" train?
6. **PD-20 `migrate_legacy_protected_to_anchor.py`:** write it now (case-b → retires naturally with §4.1) or defer until audit telemetry says the gap is large (per PD-20's original verdict)?
7. **`docs/contracts/CAPABILITY_REGISTRY.md` update:** require `scripts/migrate_*` entries as part of Phase 2.4, or expand the I32 lint rule more aggressively to catch new script additions at PR-time?

## 6. Risk + reversibility

- **Phase 1 is additive + reversible.** Every new CLI subcommand is read-only or explicitly operator-gated; the existing runner is unchanged for non-migrating users. A knob with no DB row behaves exactly as today. Low blast radius.
- **Phase 2 deletes scripts that the framework now covers.** Reversible: `git revert` + re-adding the script is one commit. Mitigations: MIGRATION_NOTES.md entry per deletion, BC-ST2 acceptance test before deletion, operator handoff to the framework.
- **Phase 3 is a draft tool; it never applies.** Reversible: delete the CLI + MCP tool, no data change. Mitigations: keep the tool out of any default install path; require explicit opt-in.
- **Phase 4 is v7-scale;** defer to v7 planning.
- **I32 lint expansion (§4.2.4):** expanding the I32 ratchet to enforce script coverage is a real invariant change. The I32 ratchet currently has a grandfathered list; any new enforcement needs review against `ARCHITECTURE_INVARIANTS.md §I32` + the CAPABILITY_REGISTRY schema.

## 7. Test plan (review)

- **Per-migration idempotency:** existing per-migration test suites (one per migration) still pass.
- **BC-ST2 acceptance:** new test: spin up a fresh DB at v<N>, apply all migrations N+1..M, snapshot row counts + a sample of rows; assert zero data loss + zero row drift (forward-deterministic). Run in `pytest -m integration` (slow, opt-in).
- **I32 coverage:** `scripts/check_capability_coverage.py` exits 0 with all framework + `scripts/migrate_*` entries present.
- **CLI smoke tests:** `yadgar migrate status`, `yadgar migrate --dry-run`, `yadgar migrate baseline`, `yadgar migrate repair` all have happy-path + error-path test cases.
- **Authoring prompt (Phase 3):** golden-file tests: `yadgar migrate author` for N hand-crafted change descriptions produces expected output, validated against `yadgar migrate status --dry-run`.

## 8. Rollout

- Phase 1 ships as a v5.x minor (e.g. v5.166). One PR or N PRs, decision in §0.2.
- Phase 2 ships as v5.x minor +1, after Phase 1 is in production ≥2 weeks.
- Phase 3 ships as v5.x minor +2, after Phase 2 is in production ≥2 weeks.
- Phase 4 is v7 planning; defer.

## 9. Decisions log (for ADR)

When the plan is approved, the decisions worth capturing in an ADR (suggested ADR-0168, "Migration operator experience v1"):

- **D1:** The framework runner grows a `dry_run` parameter; each `_migration_NNN.fn(storage, dry_run=False)` accepts and ignores it. Idempotent skip preserved.
- **D2:** New CLI subcommand family `yadgar migrate {status,--dry-run,baseline,repair}` + matching MCP surface.
- **D3:** Apply gate before non-recorded migration (UX, no new tool).
- **D4:** `scripts/migrate_v5_7_to_v5_8.py` is retired (D1 covers its unique value).
- **D5:** `scripts/migrate_adr_monolith.py` is removed (per ADR-0124, still pending).
- **D6:** I32 lint expands to require `scripts/migrate_*` CAP-xxx entries (post-retirement).
- **D7:** Authoring prompt `yadgar migrate author` ships as draft-and-validate only; never auto-apply. Operator-gated.
- **D8:** v7 multi-namespace migration story deferred; this train is v5.x near-term.

## 10. Yadgar findings footer (subagent / handoff contract)

For any subagent that picks up this plan in a follow-up session, the Yadgar findings are:

- **A18 + multi-user** — The schema↔migrations invariant is ratcheted for the framework (25 migrations, all catalogued in `CAP-STOR-002..027`) but the parallel `scripts/migrate_*.py` universe is a **second, uncatalogued migration surface** with 3 active `.py` files + 1 `.sh` + 1 dead-but-still-referenced + 1 planned. New users have no discovery path except `MIGRATION_NOTES.md` / `CHANGELOG.md`.
- **ADR-0124 is the canonical precedent** — "Migrate only via the daemon MCP tools; never StorageEngine." This binds the answer for *content* migrations; **DB schema migrations are not yet covered by any ADR**; this plan proposes ADR-0168 to extend it.
- **`migrate_v5_7_to_v5_8.py` is the exact duplication the user flagged** — its body is a verbatim copy of `_migration_008_anchor_tier`'s backfill. Only added value = `--dry-run`. Fix: push `--dry-run` into the framework (D1) and retire the script (D4).
- **Stale TABLES lists** — `migrate_v235_to_v204.py` + `migrate_to_server.py` enumerate 12 tables, frozen pre-v5.41. Not a problem today (no v2-format user upgrades likely) but a foot-gun if the script ever runs against a current DB. Document as case-c one-off; I32 catalogue entry (case-c retention).
- **`BC-ST2` has no test** — `BEHAVIOR_CONTRACT.md:107` marks it `⏳[r] P1`. The migration operator experience train is the natural place to land the BC-ST2 acceptance test (D1 + Phase 1.8).
- **No wiki page** is titled or tagged "migration script system" — `wiki_query` returns only ADR-0124. The investigation doc (this file) is the first curated entry; promote to wiki page once approved.
