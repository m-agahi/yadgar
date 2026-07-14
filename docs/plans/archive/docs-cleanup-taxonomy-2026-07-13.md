# docs/ Cleanup — Taxonomy + Migration Plan (2026-07-13)

**Status:** AUDITED-READY — pending final human audit. All 4 user decisions folded in
(aggressive path chosen). Pins + inbound refs verified against observed repo state
2026-07-13. No moves/deletes/commits executed — this doc is the executable spec only.

> **Provenance note:** Yadgar MCP was **unreachable** while this revision was written
> (`recall` returned `Unable to connect`, retried twice). This plan is built **purely on
> observed repo state** (grep/read of the working tree) — which is authoritative per the
> observed-state-wins contract. When MCP returns, a cross-check against any prior
> `docs-reorg` wiki/memory is worthwhile but not blocking; nothing here depends on it.

---

## BLUF

`docs/` has **49 loose `.md` files in the root** (plus one loose `.json`/HTML stragglers)
intermingling stable canonical reference docs with **~17 dated point-in-time reports**.
Three naming schemes coexist (`SCREAMING_SNAKE`, `kebab`, `V5_NN_` / `_YYYY_MM_DD`). No
`docs/README.md` index exists.

The user chose the **aggressive, full-consistency path** (4 decisions below), overriding the
draft's earlier "minimal / go-forward-only" recommendations:

1. **Move the 4 contract docs** → `docs/contracts/` — requires updating **19 HARD pins**
   same-car or CI breaks.
2. **Remove 124 generated diagram artifacts from git** + `.gitignore` the generated path;
   keep source. Fix the empty-`specs/` / README drift.
3. **Rename existing docs too** (retroactive, not go-forward-only): every loose root doc →
   its taxonomy dir + normalized name. Contracts keep `SCREAMING_SNAKE`; other reference
   docs → `kebab`; dated reports → `<topic>-YYYY-MM-DD.md`.
4. **Consolidate overlaps**: dedup `benchmarks-current.md` → `BENCHMARK_RESULTS.md`;
   clarify (not merge) the roadmap sources.
Plus: create `docs/README.md` taxonomy index. ADRs stay wiki-native (confirmed — no ADR
files enter `docs/`).

**Hard constraint that dominates sequencing:** four contract docs are **path-anchored** in
`.pre-commit-config.yaml` `files:` regexes + five lint scripts + four test files — and the
retroactive rename (#3) adds **four more HARD code/Makefile pins** on non-contract docs
(`configuration.md`, `RECOMMENDED_CLAUDE_RULES.md`, `V5_41_5_PROFILING_REPORT.md`,
`UPGRADE_TEST.md`). Every one of these must be updated **in the same car as its move** or
pre-commit / CI / `make upgrade-test` reddens.

**Execution:** a **sequential 5-car `feat/docs-reorg` train** (NOT worktree-parallel —
`README.md` + `AGENTS.md` are shared ref-update surfaces every car touches, so parallel
worktrees collide). `git mv` only (history preserved), inbound-ref updates same-car,
**zero content rewrites** except the two consolidation merges. CI must be green at the end
of every car.

---

## Observed docs/ tree + counts (verified 2026-07-13)

```
docs/                                    49 loose .md (root)  ← chaos lives here
├── assets/                    1   (yadgar.svg)
├── audits/                    1   (silent-breakage-2026-06-16.md)   ← single-file dir
├── maintainer-notes/          2   (nix-integration.md, todo.md)
├── migrations/                1   (wiki-restamp-2026-06-16-manifest.json)  ← single-file dir
├── observability/             2   (alerts.yaml, dashboard.json)
├── roadmap/                   6   (v4.8 v4.9 v5 v5.1 v6 v7 .md)  ← per-version roadmaps
├── testing/                   3   (recall-perf-checklist + 2 dated 2026-07-13 breakdowns)
├── plans/                    39   active/open plans (slug-dated) + ROADMAP.md
│   ├── archive/             146   shipped/dead plans (2.6 MB) — do-not-edit; FROZEN
│   └── *.mockup.html          2   (viz-config-control-panel, viz-trace-replay)
└── diagrams/
    ├── specs/                 0   ← EMPTY, but README says specs live here (DRIFT)
    ├── mcp-traces/           32   generated per-tool SVGs
    ├── out/                 124   GENERATED artifacts — TRACKED in git (git ls-files=124)
    ├── archive/               4   dated diagram snapshots (NOT ~52 — draft was stale)
    ├── mcp-tool-traces-2026-07-09.md   ← loose dated .md at diagrams/ root
    ├── __pycache__/               .pyc — untracked
    └── *.py + README.md       6   (generate.py, capture_trace.py, simplify_trace.py, …)
```

**Drift vs the draft, corrected here:** (a) root has **49** `.md` incl.
`MACOS_LAUNCHD_PORT_DESIGN.md` (draft's table omitted it); (b) `diagrams/archive` = **4**,
not ~52; (c) `diagrams/mcp-tool-traces-2026-07-09.md` is a loose dated `.md` the draft
didn't classify.

---

## The 4 folded decisions (were "open questions" — now RESOLVED)

### Decision 1 — MOVE the 4 contract docs → `docs/contracts/` (aggressive path)

`ARCHITECTURE_INVARIANTS.md`, `BEHAVIOR_CONTRACT.md`, `CAPABILITY_REGISTRY.md`,
`EDGE_CONTRACT.md` move to `docs/contracts/` (name unchanged — `SCREAMING_SNAKE` kept;
they're referenced by exact basename and are the enforcement source-of-truth). This is the
**CI-risk car (Car B)** — **19 HARD pins** must change same-car. Full checklist below.

### Decision 2 — Remove generated diagram artifacts from git + gitignore

`git rm --cached` the **124** tracked files under `docs/diagrams/out/` (keep source specs +
`generate.py`). Add a `.gitignore` rule for the generated path. A follow-up task regenerates
+ tracks the latest set (see note). Fix the `specs/` / README drift same car.

### Decision 3 — Rename existing docs retroactively (full consistency)

Every loose root doc moves to its taxonomy dir **and** gets a normalized name. Casing rule:

- **Contracts** → `docs/contracts/` , keep `SCREAMING_SNAKE` (path changes, name doesn't).
- **Dated reports** → `docs/reports/<cat>/<topic>-YYYY-MM-DD.md` (hyphen date delimiter).
- **Other reference docs currently `SCREAMING_SNAKE`** → **kebab-case** under `reference/`
  (`HOOKS.md`→`hooks.md`, `INSTALL.md`→`install.md`, `RELEASE.md`→`release.md`,
  `VERSIONING.md`→`versioning.md`, `PRIVACY.md`→`privacy.md`, `AGENT_PROMPTS.md`→
  `agent-prompts.md`, `CONFLICT_RESOLVER.md`→`conflict-resolver.md`, `VIZ_CONFIG.md`→
  `viz-config.md`, `COMPLEXITY_POLICY.md`→`complexity-policy.md`,
  `RECOMMENDED_CLAUDE_RULES.md`→`recommended-claude-rules.md`,
  `CLAUDE_SUBAGENT_CONTRACT.md`→`claude-subagent-contract.md`,
  `WORKFLOW_ROADMAP_UPDATE.md`→`workflow-roadmap-update.md`,
  `DECISIONS.md`→`decisions.md`, `MACOS_LAUNCHD_PORT_DESIGN.md`→
  `reports/releases/macos-launchd-port-design-2026-06-07.md` — it's a dated design report,
  not a stable reference; classify as a report).
- **Already-kebab reference docs** keep their name (`architecture.md`, `retrieval.md`,
  `memory-lifecycle.md`, `configuration.md`, `sdk-js.md`, `dogfood.md`,
  `claude-workflow.md`) — path changes to `reference/` only.
- **Exception — keep uppercase:** `CHANGELOG.md` stays `docs/CHANGELOG.md` (standard
  root-level filename like `README`/`LICENSE`; README-linked, conventionally root).
- **Scope guard:** the retroactive rename covers the **~50 ROOT files ONLY**.
  `plans/*.md` (39) already convention-compliant → **STAY**. `plans/archive/*` (146) is
  frozen/do-not-edit → **STAY** (renaming the archive would break history refs for zero
  value). Do not let "full consistency" balloon into the archive.

### Decision 4 — Consolidate overlaps

- **Benchmarks (clean merge):** `docs/BENCHMARK_RESULTS.md` (176 L) is **canonical**.
  `docs/benchmarks-current.md` (57 L) self-describes as a pointer ("See
  `docs/BENCHMARK_RESULTS.md` for full per-type breakdown") — its unique content is a
  small "Status + suite table". **Fold** that table into a `## Current status` section at
  the top of `BENCHMARK_RESULTS.md`, then **delete** `benchmarks-current.md` and repoint
  its 2 inbound refs (both `CHANGELOG.md:2655`) to the canonical file.
- **Roadmap trio — CLARIFY, do NOT merge (brutal-honesty correction):** the three are
  **distinct roles**, not overlapping copies:
  - `docs/plans/ROADMAP.md` (97 L) = **open-plans index / plans SSOT** — NOT a version
    roadmap. **Stays distinct.** Do not fold into any roadmap merge.
  - `docs/roadmap/vN.md` (6 files) = **historical per-version plans** — stay as historical
    record under `roadmap/`.
  - `docs/yadgar-roadmap-future-improvements.md` (344 L) = a **file-canonical MIRROR of the
    `yadgar-roadmap-future-improvements` wiki page** (RMW-safety pattern; self-declares
    "file-canonical mirror" at line 13). It is a wiki-sync artifact, **not** a mergeable
    doc. **Keep as-is at root** (or move under `roadmap/` with its 4 inbound refs updated);
    do **not** fold it into anything — folding would desync the wiki mirror. Recommend:
    move to `docs/roadmap/future-improvements.md`, update the 4 refs + the wiki mirror's
    self-pointer at line 13.
  - Net: no destructive roadmap merge. One canonical *forward* roadmap = the wiki page (its
    file mirror); `plans/ROADMAP.md` stays the plans index; `roadmap/vN.md` stays history.

### Confirmed (not a change)

- **ADRs stay wiki-native** — `wiki:yadgar-adr-log` + `adr_add` MCP (AGENTS.md:173 §I34,
  ADR-0062). No ADR files move into `docs/`.

---

## Contract-pin update checklist (Decision 1 — Car B, CI-risk)

**All pre-commit `files:` regexes are PATH-ANCHORED** (`^…|docs/EDGE_CONTRACT\.md)$`) — they
**break on move**; there are **no basename-only survivors**. Verified line-by-line against
the working tree 2026-07-13. **19 HARD pins** (must change same-car or CI/pre-commit fails):

| # | file:line | current string | doc | fix |
|---|---|---|---|---|
| 1 | `.pre-commit-config.yaml:124` | `…\|docs/EDGE_CONTRACT\.md)$` | EDGE | `docs/contracts/EDGE_CONTRACT\.md` |
| 2 | `.pre-commit-config.yaml:131` | `…\|docs/BEHAVIOR_CONTRACT\.md\|docs/CAPABILITY_REGISTRY\.md)$` | BC, CR | `docs/contracts/…` both |
| 3 | `.pre-commit-config.yaml:145` | `…\|docs/BEHAVIOR_CONTRACT\.md)$` | BC | `docs/contracts/BEHAVIOR_CONTRACT\.md` |
| 4 | `.pre-commit-config.yaml:240` | `…\|^docs/BEHAVIOR_CONTRACT\.md$)` | BC | `^docs/contracts/BEHAVIOR_CONTRACT\.md$` |
| 5 | `scripts/check_capability_coverage.py:60` | `_REGISTRY = _REPO_ROOT / "docs" / "CAPABILITY_REGISTRY.md"` | CR | insert `"contracts" /` |
| 6 | `scripts/check_capability_coverage.py:61` | `_CONTRACT = _REPO_ROOT / "docs" / "BEHAVIOR_CONTRACT.md"` | BC | insert `"contracts" /` |
| 7 | `scripts/check_capability_coverage.py:312` | `repo_root / "docs" / "CAPABILITY_REGISTRY.md"` | CR | insert `"contracts" /` |
| 8 | `scripts/check_capability_coverage.py:332` | `repo_root / "docs" / "BEHAVIOR_CONTRACT.md"` | BC | insert `"contracts" /` |
| 9 | `scripts/check_capability_coverage.py:356` | `repo_root / "docs" / "BEHAVIOR_CONTRACT.md"` | BC | insert `"contracts" /` |
| 10 | `scripts/check_contract_coverage.py:45` | `_CONTRACT = _REPO_ROOT / "docs" / "BEHAVIOR_CONTRACT.md"` | BC | insert `"contracts" /` |
| 11 | `scripts/check_test_weakening.py:38` | `_CONTRACT = _REPO_ROOT / "docs" / "BEHAVIOR_CONTRACT.md"` | BC | insert `"contracts" /` |
| 12 | `scripts/check_test_weakening.py:91` | `_run("git","show","HEAD:docs/BEHAVIOR_CONTRACT.md")` | BC | `HEAD:docs/contracts/BEHAVIOR_CONTRACT.md` |
| 13 | `scripts/check_test_weakening.py:99` | `_run("git","show",":docs/BEHAVIOR_CONTRACT.md")` | BC | `:docs/contracts/BEHAVIOR_CONTRACT.md` |
| 14 | `scripts/check_dead_capability.py:324` | `parse_contract(repo_root / "docs" / "EDGE_CONTRACT.md")` | EDGE | insert `"contracts" /` |
| 15 | `scripts/check_dead_capability.py:388` | `parse_contract(repo_root / "docs" / "EDGE_CONTRACT.md")` | EDGE | insert `"contracts" /` |
| 16 | `yadgar/tests/server/test_contract_coverage.py:41` | `(_REPO_ROOT / "docs" / "BEHAVIOR_CONTRACT.md").read_text(…)` | BC | insert `"contracts" /` |
| 17 | `yadgar/tests/core/test_capability_coverage.py:43` | `ccc.enumerate_bc(_REPO_ROOT / "docs" / "BEHAVIOR_CONTRACT.md")` | BC | insert `"contracts" /` |
| 18 | `yadgar/tests/core/test_tamper_guards.py:434` | `(_REPO_ROOT / "docs" / "BEHAVIOR_CONTRACT.md").read_text(…)` | BC | insert `"contracts" /` |
| 19 | `yadgar/tests/core/test_wiki_handler_phase0_profile.py:227` | asserted literal `"- I9 invariant: \`docs/ARCHITECTURE_INVARIANTS.md\`"` | AI | `docs/contracts/ARCHITECTURE_INVARIANTS.md` |

**`git show HEAD:` / `:` caveat (pins 12–13):** these read the git object at the *old* path
from HEAD/index. In the commit that performs the `git mv`, HEAD still has the old path until
the rename lands — update these strings **in the same commit** as the move so the post-commit
tree is self-consistent. (After the move commit, `git show HEAD:docs/contracts/…` resolves.)

**SOFT contract refs (update for correctness, will NOT redden CI):** `AGENTS.md:89,173,231`;
`scripts/complexity_audit.py:560` (generated output text, not a read path);
`yadgar/_shared/contracts/viz.py:19` (comment); `yadgar/_shared/observability/log_config.py:7`
(docstring); `yadgar/tests/e2e/test_vacuum_backup_safety.py:8`;
`yadgar/tests/e2e/test_wiki_set_metadata_allrows.py:15`;
`yadgar/tests/config_env_only_allowlist.txt:22`; `scripts/check_test_weakening.py:124`;
`scripts/check_dead_capability.py:401,409,416` (error-message strings);
`yadgar/_shared/config/README.md:10`. **Update-in-tandem:**
`yadgar/tests/core/test_capability_coverage.py:85-86` write to a `tmp_path`-rooted
`docs/…` subpath — these must match whatever relative subpath the script constants use, so
update them in lock-step with pins 5–9 (SOFT alone, HARD-coupled to the script change).

---

## Non-contract HARD pins (Decision 3 — the retroactive rename surprise)

The rename of these four **non-contract** docs also touches code/Makefile — they redden CI /
break `make` if the move isn't matched same-car. **Verified 2026-07-13.** Distribute each to
the car that moves the file:

| moved file → new path | HARD ref | breaks |
|---|---|---|
| `configuration.md` → `reference/configuration.md` | `yadgar/tests/core/test_config_docs_phantom.py:27` (`_DOCS_PATH = _REPO_ROOT/"docs"/"configuration.md"`, used ×5) | phantom-config-knob test opens the file |
| `RECOMMENDED_CLAUDE_RULES.md` → `reference/recommended-claude-rules.md` | `yadgar/tests/core/test_project_brief_catalog_v5530.py:443,450,463` (3 `Path` builds) | project-brief-catalog test opens the file |
| `V5_41_5_PROFILING_REPORT.md` → `reports/releases/v5-41-5-profiling-report.md` | `yadgar/tests/core/test_wiki_handler_phase0_profile.py:275` | test asserts on the path |
| `UPGRADE_TEST.md` → `testing/upgrade-test.md` | `Makefile:331` (`@cat docs/UPGRADE_TEST.md`) + `:328,330` comments | `make upgrade-test` target errors |

`MACOS_LAUNCHD_PORT_DESIGN.md` → `reports/releases/…` has one SOFT ref
(`scripts/install/launchd/yadgar-vacuum-trigger-wrapper.sh:8`, a comment — runtime-safe,
update for hygiene).

---

## Complete per-file migration + rename + ref-update table

Inbound-ref counts verified 2026-07-13 (exclude `plans/`+`archive/`; SELF-refs and
`CHANGELOG` historical mentions noted but **CHANGELOG entries are historical — do NOT
rewrite them**, they record what the path was at ship time). "Refs to update" = live links
outside CHANGELOG that must repoint.

### Bulk rules (do NOT enumerate)

| Bucket | Count | Action | Reason |
|---|---|---|---|
| `plans/archive/*` | 146 | **STAY / FROZEN** | do-not-edit-per-convention; renaming breaks history refs |
| `plans/*.md` (active) | 39 | **STAY** | already convention-compliant (slug-dated) |
| `plans/*.mockup.html` | 2 | **STAY** (or `plans/mockups/`) | minor; out of scope |
| `diagrams/out/*` | 124 | **`git rm --cached` + gitignore** (Car C) | generated artifacts |
| `diagrams/mcp-traces/*`, `diagrams/archive/*` | 36 | **STAY** | generated/snapshot |
| `roadmap/v*.md` | 6 | **STAY** | historical per-version record |
| `observability/`, `maintainer-notes/`, `assets/` | 5 | **STAY** | already categorized |

### Per-file rows (the ~50 root files + 2 single-file dirs + 1 loose diagrams .md)

| Current path | New path | Refs to update (live, non-CHANGELOG) |
|---|---|---|
| `docs/architecture.md` | `docs/reference/architecture.md` | README:118,393; AGENTS:230; **HARD** `core/server/tools/project.py` docstring + `tests/core/test_wiki_refresh_stale.py`; dogfood/V5_46_AUDIT |
| `docs/retrieval.md` | `docs/reference/retrieval.md` | README:118,396; AGENTS:231; dogfood.md |
| `docs/memory-lifecycle.md` | `docs/reference/memory-lifecycle.md` | README:118,395; AGENTS:232; dogfood.md |
| `docs/configuration.md` | `docs/reference/configuration.md` | README:325,396; AGENTS:64,232; ARCHITECTURE_INVARIANTS:54; **HARD** `tests/core/test_config_docs_phantom.py:27` (×5); `config_yaml.py` comment; `agent_prompts.yaml`; `test_check_backend_bump.py` |
| `docs/HOOKS.md` | `docs/reference/hooks.md` | AGENTS:235 |
| `docs/INSTALL.md` | `docs/reference/install.md` | README:170,397; V5_46_AUDIT; roadmap-future |
| `docs/RELEASE.md` | `docs/reference/release.md` | README:399; AGENTS:236; roadmap/v5.md:309,333 |
| `docs/VERSIONING.md` | `docs/reference/versioning.md` | **0 live refs** — move freely |
| `docs/sdk-js.md` | `docs/reference/sdk-js.md` | README:12,314,398 |
| `docs/PRIVACY.md` | `docs/reference/privacy.md` | DECISIONS:822 |
| `docs/RECOMMENDED_CLAUDE_RULES.md` | `docs/reference/recommended-claude-rules.md` | **HARD** `tests/core/test_project_brief_catalog_v5530.py:443,450,463` (×3) |
| `docs/CLAUDE_SUBAGENT_CONTRACT.md` | `docs/reference/claude-subagent-contract.md` | README:346,399; AGENTS:225,237 |
| `docs/AGENT_PROMPTS.md` | `docs/reference/agent-prompts.md` | **0 live refs** — move freely |
| `docs/CONFLICT_RESOLVER.md` | `docs/reference/conflict-resolver.md` | **0 live refs** — move freely |
| `docs/VIZ_CONFIG.md` | `docs/reference/viz-config.md` | **0 live refs** — move freely |
| `docs/dogfood.md` | `docs/reference/dogfood.md` | **0 live refs** — move freely |
| `docs/claude-workflow.md` | `docs/reference/claude-workflow.md` | RELEASE.md:112; roadmap/v5.1.md:365 |
| `docs/COMPLEXITY_POLICY.md` | `docs/reference/complexity-policy.md` | ARCHITECTURE_INVARIANTS:414 |
| `docs/tributes.md` | `docs/reference/tributes.md` | README:415 |
| `docs/DECISIONS.md` | `docs/reference/decisions.md` | ~29 hits, mostly V5_46_AUDIT/roadmap-future/VERSIONING; `.forgejo/workflows/ci-pr.yaml:28` (comment only). **High count → sed recipe below**, not enumerated |
| `docs/WORKFLOW_ROADMAP_UPDATE.md` | `docs/reference/workflow-roadmap-update.md` | roadmap-future (×3) |
| `docs/CHANGELOG.md` | **STAY** `docs/CHANGELOG.md` | keep-uppercase exception; README-linked |
| `docs/ARCHITECTURE_INVARIANTS.md` | `docs/contracts/ARCHITECTURE_INVARIANTS.md` ⚠ | **Car B** — pins #19 + SOFT (log_config, config allowlist, AGENTS:173) |
| `docs/BEHAVIOR_CONTRACT.md` | `docs/contracts/BEHAVIOR_CONTRACT.md` ⚠ | **Car B** — pins #2,3,4,6,8,9,10,11,12,13,16,17,18 |
| `docs/CAPABILITY_REGISTRY.md` | `docs/contracts/CAPABILITY_REGISTRY.md` ⚠ | **Car B** — pins #2,5,7; AGENTS:89,231; `_shared/config/README.md:10` |
| `docs/EDGE_CONTRACT.md` | `docs/contracts/EDGE_CONTRACT.md` ⚠ | **Car B** — pins #1,14,15; viz.py:19 |
| `docs/BENCHMARK_RESULTS.md` | `docs/benchmarks/BENCHMARK_RESULTS.md` | README:364; benchmarks-current:10 (that file is being deleted — see #4); BENCHMARK_LICENSE:57 |
| `docs/BENCHMARK_LICENSE.md` | `docs/benchmarks/BENCHMARK_LICENSE.md` | BENCHMARK_RESULTS:166 |
| `docs/benchmarks-current.md` | **DELETE** (fold → `benchmarks/BENCHMARK_RESULTS.md`) | repoint CHANGELOG:2655? (historical — leave); no live refs beyond CHANGELOG |
| `docs/UPGRADE_TEST.md` | `docs/testing/upgrade-test.md` | **HARD** `Makefile:331` (`cat`) + :328,330 comments |
| `docs/VIZ_TESTING.md` | `docs/testing/viz-testing.md` | **0 live refs** (CHANGELOG only) — move freely |
| `docs/CI_ISSUES_2026_06_05.md` | `docs/reports/ci/ci-issues-2026-06-05.md` | DECISIONS:238; CI_ISSUES_06_06:5 |
| `docs/CI_ISSUES_2026_06_06.md` | `docs/reports/ci/ci-issues-2026-06-06.md` | V5_46_9_HOTFIX:5 |
| `docs/CI_SPEEDUP_AUDIT_2026_06_06.md` | `docs/reports/ci/ci-speedup-audit-2026-06-06.md` | V5_46_9_HOTFIX:6,170 |
| `docs/competitor-audit-2026-05-30.md` | `docs/reports/audits/competitor-audit-2026-05-30.md` | DECISIONS:466,496; roadmap-future:63; `plans/v6-extract-on-ingest.md:101` |
| `docs/competitor-graphify-2026-05-31.md` | `docs/reports/audits/competitor-graphify-2026-05-31.md` | roadmap-future:64 |
| `docs/LICENSE_COMPLIANCE_AUDIT_2026-05-30.md` | `docs/reports/audits/license-compliance-audit-2026-05-30.md` | **README:428**; BENCHMARK_LICENSE:57 |
| `docs/complexity-audit.md` | `docs/reports/audits/complexity-audit-2026-06-01.md` | ARCHITECTURE_INVARIANTS:92,892. NOTE: static committed doc — `complexity_audit.py` does NOT write it (verified: line 560 is generated *text*, no output-path to this file). Safe to move. |
| `docs/V5_46_AUDIT_2026_06_04.md` | `docs/reports/audits/v5-46-audit-2026-06-04.md` | roadmap-future:85 |
| `docs/audits/silent-breakage-2026-06-16.md` | `docs/reports/audits/silent-breakage-2026-06-16.md` | **0 refs**; collapses single-file `audits/` dir |
| `docs/V5_42_1_GATE_VERIFICATION.md` | `docs/reports/releases/v5-42-1-gate-verification.md` | **0 refs** — move freely |
| `docs/V5_45_0_DP_OVERRIDES.md` | `docs/reports/releases/v5-45-0-dp-overrides.md` | V5_46_AUDIT:154 (+ self) |
| `docs/V5_45_0_DP_RESOLUTIONS.md` | `docs/reports/releases/v5-45-0-dp-resolutions.md` | V5_45_0_DP_OVERRIDES:173,176 (+ self) |
| `docs/V5_46_9_HOTFIX_SCOPE.md` | `docs/reports/releases/v5-46-9-hotfix-scope.md` | **0 refs** — move freely |
| `docs/V5_55_REFACTOR_CONTRACT.md` | `docs/reports/releases/v5-55-refactor-contract.md` | **0 refs** — move freely |
| `docs/V5_41_5_PROFILING_REPORT.md` | `docs/reports/releases/v5-41-5-profiling-report.md` | **HARD** `tests/core/test_wiki_handler_phase0_profile.py:275`; V5_46_9_HOTFIX (×2) |
| `docs/PRE_EXISTING_TEST_FAILURES_V5_49_4.md` | `docs/reports/releases/pre-existing-test-failures-v5-49-4.md` | CHANGELOG only (historical) — move freely |
| `docs/UNTESTED_MODULES_V5_49_6.md` | `docs/reports/releases/untested-modules-v5-49-6.md` | CHANGELOG only (historical) — move freely |
| `docs/MACOS_LAUNCHD_PORT_DESIGN.md` | `docs/reports/releases/macos-launchd-port-design-2026-06-07.md` | SOFT `scripts/install/launchd/yadgar-vacuum-trigger-wrapper.sh:8` (comment) |
| `docs/yadgar-roadmap-future-improvements.md` | `docs/roadmap/future-improvements.md` | DECISIONS:130,144; **self-pointer line 13** ("Roadmap sync source") + update the **wiki mirror**; competitor/V5_46 refs are inbound TO it |
| `docs/migrations/wiki-restamp-2026-06-16-manifest.json` | `docs/reports/releases/wiki-restamp-2026-06-16-manifest.json` | **0 refs**; collapses single-file `migrations/` dir |
| `docs/diagrams/mcp-tool-traces-2026-07-09.md` | `docs/reports/releases/mcp-tool-traces-2026-07-09.md` (or `diagrams/`) | **0 refs** — move freely |

### High-inbound sed recipes (do not enumerate 29+ rows)

For `DECISIONS.md` (→`reference/decisions.md`) and any moved file with many docs-internal
links, run per file **after** the `git mv`, excluding frozen/historical trees:

```bash
# preview
git grep -l 'docs/DECISIONS\.md' -- ':!docs/plans/archive' ':!docs/CHANGELOG.md'
# apply (path + basename both change → full old→new path)
git grep -l 'docs/DECISIONS\.md' -- ':!docs/plans/archive' ':!docs/CHANGELOG.md' \
  | xargs sed -i 's#docs/DECISIONS\.md#docs/reference/decisions.md#g'
```

**Never rewrite `docs/CHANGELOG.md` or `plans/archive/`** — they record historical paths
at ship time; changing them corrupts the record for zero value.

---

## Diagram cleanup spec (Decision 2 — Car C)

1. **Untrack generated output (user runs — do NOT auto-execute):**
   ```bash
   git rm -r --cached docs/diagrams/out/          # 124 files, keeps them on disk
   ```
2. **`.gitignore` rule** (repo has NO diagram rule today — verified):
   ```
   docs/diagrams/out/
   ```
   Keep tracked: `generate.py`, `capture_trace.py`, `simplify_trace.py`, `trace_to_boxes.py`,
   `README.md`, `specs/` source YAML, `mcp-traces/`, `archive/`.
3. **Follow-up regen (referenced, not in this train):** a separate task regenerates + tracks
   the latest canonical diagram set. **This plan does not name that artifact** (it is created
   outside this plan); Car C only untracks + gitignores. If the follow-up produces a
   committed "latest" subset, it lands under a distinct tracked path (e.g. a curated
   `diagrams/published/`), NOT `out/`.
4. **Fix `specs/` / README drift** (pick ONE — recommend the second):
   - (a) Move the YAML specs out of `diagrams/archive/` into the empty `diagrams/specs/` so
     the README's `python …/generate.py docs/diagrams/specs/<name>.yaml` instructions become
     true; OR
   - (b) Edit `diagrams/README.md` to point at the actual spec location.
   **Recommend (a)** — the README already documents `specs/` as the authoring dir in 5 places
   (lines 4,64,82,84,92); making reality match the docs is lower-drift than rewording 5 refs.
   Verify no generator hardcodes `archive/` before moving.

---

## Consolidation merge spec (Decision 4 — Car D)

**Benchmarks (do the merge):**
1. Add a `## Current status` section at the top of
   `docs/benchmarks/BENCHMARK_RESULTS.md` containing the unique content from
   `benchmarks-current.md` (the "Status (2026-06-01, v5.26.0)" paragraph + the suite table).
2. `git rm docs/benchmarks-current.md` (or `git mv` then delete after fold — fold first to
   preserve nothing-lost).
3. Repoint the one live inbound (`BENCHMARK_RESULTS:10` self-links to it — remove that link;
   CHANGELOG:2655 is historical, leave).

**Roadmap (clarify only — NO destructive merge):** per Decision 4 above. Add a one-paragraph
"Roadmap sources" note to `docs/README.md` disambiguating the three roles. Move
`yadgar-roadmap-future-improvements.md` → `roadmap/future-improvements.md`, update its 2
`DECISIONS.md` inbound refs + its own line-13 self-pointer, and update the corresponding
**wiki page mirror** so file and wiki stay in sync (the file is the RMW-safe mirror of the
wiki).

---

## docs/README.md index (NEW — Car E, finalized LAST)

Create `docs/README.md` with: (1) the target taxonomy tree; (2) the naming convention
(contracts=`SCREAMING_SNAKE` under `contracts/`, reference=`kebab` under `reference/`,
reports=`<topic>-YYYY-MM-DD.md` under `reports/{ci,audits,releases}/`, plans=existing
`plans/ROADMAP.md` convention); (3) the "Roadmap sources" disambiguation; (4) a pointer that
ADRs are wiki-native. **Author it in the FINAL car** so every path it lists is already final.

---

## Target taxonomy (post-reorg)

```
docs/
├── README.md              ← NEW index + convention (Car E)
├── CHANGELOG.md           ← STAY (keep-uppercase exception)
├── contracts/             ← ARCHITECTURE_INVARIANTS, BEHAVIOR_CONTRACT,
│                             CAPABILITY_REGISTRY, EDGE_CONTRACT (SCREAMING_SNAKE, pinned)
├── reference/             ← architecture, retrieval, memory-lifecycle, configuration,
│                             sdk-js, dogfood, claude-workflow (kebab already) +
│                             hooks, install, release, versioning, privacy, agent-prompts,
│                             conflict-resolver, viz-config, complexity-policy,
│                             recommended-claude-rules, claude-subagent-contract,
│                             workflow-roadmap-update, decisions, tributes (renamed→kebab)
├── benchmarks/            ← BENCHMARK_RESULTS (+folded status), BENCHMARK_LICENSE
├── testing/              ← recall-perf-checklist, upgrade-test, viz-testing, dated breakdowns
├── reports/
│   ├── ci/               ← ci-issues-*, ci-speedup-audit-*
│   ├── audits/           ← competitor-*, license-compliance-audit-*, complexity-audit-*,
│   │                        v5-46-audit-*, silent-breakage-*
│   └── releases/         ← v5-4x snapshots, macos-launchd-port-design-*, profiling-report,
│                            pre-existing-test-failures-*, untested-modules-*, wiki-restamp-*
├── plans/                ← unchanged (39 active + ROADMAP.md + archive/ frozen)
├── roadmap/              ← v4.8…v7 (historical) + future-improvements.md (wiki mirror)
├── diagrams/             ← source + specs/ (populated) ; out/ untracked+gitignored
├── observability/, maintainer-notes/, assets/   ← unchanged
```

---

## Car breakdown + sequencing (`feat/docs-reorg` train)

**Sequential, NOT worktree-parallel-disjoint.** Reason: `README.md`, `AGENTS.md`, and
several `docs/*.md` are **shared ref-update surfaces** that nearly every car edits — parallel
worktrees would collide on the same files and produce merge conflicts. Cars run in order;
each is self-contained (its moves + its ref-updates + green CI *within the same car*).
**All renames use `git mv`** (history-preserving); a plain `mv`+add loses `--follow` history.

| Car | Scope | CI risk | Gate |
|---|---|---|---|
| **A — taxonomy scaffold + bulk root moves** | Create `contracts/ reference/ benchmarks/ reports/{ci,audits,releases}/`. `git mv` all NON-contract, NON-consolidated root docs to their new paths + rename to kebab/dated. Update their ref sites incl. the 4 non-contract HARD pins (`test_config_docs_phantom`, `test_project_brief_catalog_v5530`, `test_wiki_handler_phase0_profile`, `Makefile`). Collapse single-file `audits/`+`migrations/`. Move `yadgar-roadmap-future-improvements.md`→`roadmap/`. | MEDIUM (4 non-contract HARD pins + Makefile) | pytest on the 3 touched tests + `make upgrade-test` dry pass + `git grep -n 'docs/<oldpath>'` empty for Car-A files |
| **B — contract docs move + ALL 19 pins** | `git mv` the 4 contract docs → `contracts/`. Apply the **19 HARD pins** + coupled SOFT (tmp_path fixtures, AGENTS, comments) **in the same commit(s)**. | **HIGH — the CI-risk car** | `pre-commit run --all-files` green + run all 5 lint scripts (`check_capability_coverage`, `check_contract_coverage`, `check_test_weakening`, `check_dead_capability`, `complexity_audit`) + the 4 pinned tests pass |
| **C — diagram untrack + gitignore + specs fix** | `git rm --cached docs/diagrams/out/` (documented as USER command in MIGRATION_NOTES — not auto-run), add `.gitignore` rule, fix `specs/` drift (option a). | LOW | `git status` shows out/ untracked; README instructions resolve to real specs |
| **D — consolidation merges** | Fold `benchmarks-current.md` → `BENCHMARK_RESULTS.md` + delete + repoint; add roadmap-sources note. | LOW | no dangling link to deleted file (`git grep`) ; benchmarks page renders |
| **E — docs/README.md index** | Author the index with final paths + convention + roadmap disambiguation + ADR-wiki pointer. | NONE | index links all resolve (`git grep` each path exists) |

**Sequencing rationale:** A before B (scaffold dirs exist; B only adds the risky contract
pins on a clean tree). B is isolated so a pin regression is bisectable to one car. C/D/E are
low-risk and could combine, but kept discrete for clean review. **E last** — it names final
paths.

**Failure mode to flag loudly:** a single missed pin in Car B (esp. the path-anchored
pre-commit `files:` regexes or the `git show HEAD:` strings) leaves pre-commit / CI **red**,
and because the regexes are path-anchored, a *silent* failure mode also exists — the
contract-coverage hooks would simply **stop firing** on the contract docs (regex no longer
matches the new path) without erroring. Mitigation: after Car B, deliberately touch a
contract doc and confirm the relevant hook *runs* (not just "passes").

---

## Acceptance criteria (per car + train)

Per car:
- `pre-commit run --all-files` exits 0 (Car B especially).
- `git grep -n 'docs/<oldpath>'` returns empty for every file that car moved (excluding
  `docs/CHANGELOG.md` + `plans/archive/`, which are intentionally frozen).
- `git log --follow <newpath>` shows pre-move history (proves `git mv`, not `mv`+add).
- Car A: the 3 pinned tests + `make upgrade-test` pass.
- Car B: all 5 lint scripts + 4 pinned tests pass; contract hooks confirmed *firing* on the
  new paths.

Train end:
- Full CI green (`.forgejo/workflows/*` — no path-filters/mkdocs to break, verified).
- No broken markdown links: `git grep` finds zero live refs to any old path outside frozen
  trees.
- All contract pins resolve to `docs/contracts/…`; all renamed-doc HARD pins resolve.
- `docs/README.md` exists and every path it lists is real.

---

## Notes / residual decisions

- **This plan doc** should be registered in `docs/plans/ROADMAP.md` per the plans convention
  (doc-only edit — in Car A or a trailing chore).
- **`plans/*.mockup.html`** (2) left in place — trivial, out of scope.
- **Only remaining judgment call:** whether `yadgar-roadmap-future-improvements.md` moves to
  `roadmap/future-improvements.md` (recommended, requires wiki-mirror sync) or stays at root
  as an explicit wiki-mirror exception. Everything else is fully specified above.
- **MCP cross-check** (when Yadgar returns): confirm no prior `docs-reorg` wiki page
  contradicts this taxonomy; none was readable at authoring time.
```
