# PLAN — v5.63: Wiki corpus maintenance tools (SUPERSEDED)

**Status:** SUPERSEDED 2026-06-04 by `docs/PLAN_V5_64_WIKI_EDIT_PRIMITIVES.md`. v5.64 folds in v5.63's `wiki_reclassify_directory` + `wiki_set_branch` into a single parameterized `wiki_set_metadata(slug, field, value)` tool, plus carries over the optional Migration 019. v5.63 is preserved as historical artifact per the "every document on master" workflow rule.

**Original status (2026-06-03):** SKELETON drafted 2026-06-03 night. Not blocking. Adjacent to v5.61 (repo-wiki native) + v5.62 (yadgar CLI).

**Origin:** Post-v5.42.6 wiki corpus audit (2026-06-03) — 372 unique pages, all schema-valid after migration 018 backfill. Found 2 cosmetic anomalies that don't break the knob-ON enforcement path but cause taxonomy drift over time. Tools to address them are not urgent; park here.

## Scope

Two new MCP tools + optional bulk migration:

### Tool 1 — `wiki_reclassify_directory(slug, directory)`

Patches `directory_context` on an existing wiki page. Currently blocked by `wiki_update` field allowlist (`content`, `tags`, `category`, `confidence` only). New tool exposes directory_context as a separately-callable patch.

Use cases:
- Heuristic-misclassified page from migration 018 backfill (e.g., the 1 `aws-vpc` page audit found in `global` that should be `/home/max/aws-work`)
- User reclassifies as project context evolves (page outgrows global scope or vice versa)
- Per-page corrections after `v5.61` repo-wiki regeneration when tag-based heuristic disagrees with intent

Contract:
- Validates new directory is `"global"` or an absolute path
- Requires explicit caller context (`branch_hint` + `directory` of CALLER, not target)
- Rejects if target page's existing `directory_context` already matches (idempotent no-op)
- Logs old + new values + caller_agent

### Tool 2 — `wiki_set_branch(slug, branch)`

Patches `branch` on an existing wiki page (`null` for canonical, non-empty string for branch-scoped). Same exclusion gap as Tool 1 — `wiki_update` does not allow branch field.

Use cases:
- Migrate `~200 legacy pages` from `branch="master"` to `branch=NULL` (canonical) per architecture semantic categories — those pages are branch-invariant facts (AWS infra inventory, project conventions) and should not require a branch_hint to match
- Promote a feature-branch-scoped WIP page to canonical when merged
- Demote a canonical page to branch-scoped when a fork-specific variant is intentional

Contract:
- Accepts `branch=None` (canonical) or non-empty string
- Requires explicit caller `branch_hint` + `directory`
- Idempotent no-op if value already matches

### Optional — Migration 019: bulk-NULL legacy `branch="master"`

For users who want the cleanup applied automatically rather than per-page:
- Scans all wiki_page rows with `branch="master"` AND `directory_context != "global"`
- Sets `branch=NULL` (assumes project-tagged pages are branch-invariant by default)
- Pages with `branch="master"` AND `directory="global"` left alone (global-pages were intentionally not branch-scoped pre-v5.42.5, NULL was the canonical default)
- Optional via env knob `YADGAR_MIGRATION_019_AUTO_NULL_BRANCH=true` (default `false`)

## Audit findings (origin)

Post-v5.42.6 audit (2026-06-03) — see `[[yadgar-wiki-200-page-corpus-analysis-reclassification-plan-2026-]]`:

| Metric | Value |
|---|---|
| Total unique pages | 372 |
| Pages with valid directory_context | 372 (100%) |
| /home/max/aws-work | 153 |
| /home/max/git/yadgar | 18 |
| /home/max/git/ledger | 2 |
| global | 199 |
| Heuristic-misclassified | 1 (`aws-vpc-terraform-...`) |
| Legacy `branch="master"` from pre-v5.42.2 drainer default | ~200 |
| Feature-branch-scoped pages | 0 |

**Knob ON readonly risk: ALREADY ELIMINATED by v5.42.6 migration 018.** v5.63 is taxonomy hygiene, not enforcement repair.

## Out of scope for v5.63

- Auto-classification UI (visualizer tab) — v5.50 viz overhaul
- Repo-wiki-driven reclassification — v5.61
- CLI subcommand for reclassify — covered by v5.62 once tools land

## Effort estimate

~1 calendar day. Two MCP tools + 4-6 tests + optional migration. Mostly mechanical.

## Acceptance

- `wiki_reclassify_directory(slug, directory)` callable via MCP
- `wiki_set_branch(slug, branch)` callable via MCP
- Both validate caller context per v5.42.3 + v5.42.5 contracts
- Audit-found misclassified page corrected: `wiki_reclassify_directory('aws-vpc-terraform-workspace-infrastructure-quinyx-aws-v', '/home/max/aws-work')` succeeds and page is findable in aws-work bucket
- Migration 019 (optional) bulk-cleans `branch="master"` legacy when knob is on

## Cross-references

- `[[yadgar-directory-branch-contract-v5-42-3-5-architecture]]` — semantic categories model
- `[[yadgar-wiki-200-page-corpus-analysis-reclassification-plan-2026-]]` — audit findings
- `docs/PLAN_V5_42_5_DIRECTORY_CONTRACT.md` — original directory contract
- `docs/PLAN_V5_42_6_DIRECTORY_BACKFILL_AND_RESOLUTION_FIX.md` — hotfix that made enforcement safe
- `docs/PLAN_V5_61_REPO_WIKI_YADGAR_NATIVE.md` — adjacent feature (regen-driven reclassification)
- `docs/PLAN_V5_62_YADGAR_CLI.md` — CLI surfaces these tools as `yadgar wiki-reclassify ...`

## Defer rationale

Functional state is correct. v5.63 is polish. Schedule after more pressing work in v5.42.x cleanup cycle settles.
