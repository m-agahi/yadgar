# MIGRATION_NOTES.md Trim Plan

**Status:** DRAFT — awaiting audit
**Issue:** #52
**Date:** 2026-07-13
**File:** `MIGRATION_NOTES.md` (repo root, 7 436 lines as of 2026-07-13)

---

## BLUF

The file is ~75 % genuine operator migration content (keep), ~5 % Max-personal/nix-specific bullets (strip — subsection surgery, not whole-section delete), and ~20 % stale CI-infra iteration noise that operators never need post-merge (prune — whole sections). Estimated outcome: **~1 350–1 550 lines removed**, landing around **5 900–6 100 lines**.

Root stays at `MIGRATION_NOTES.md` (no rename, no move). Five live inbound references (`README.md:213`, `README.md:399`, `AGENTS.md:238`, `docs/configuration.md:690`, `ci-pr.yaml:24`, `ci-release.yaml:24`) are all satisfied by the file existing at the root with the same name. Zero ref-update cost.

---

## Per-Section Action Table

> **Columns:** `Heading` | `Verdict` | `Line range` | `Why`
>
> Line ranges verified against the 2026-07-13 file. Adjacent `---` horizontal rules are included in the range of whichever section owns them (the rule precedes the next section header).

| # | Heading | Verdict | Lines | Why |
|---|---------|---------|-------|-----|
| 1 | `## T4 Ettin CE-swap train` | **KEEP** | 3–56 | Active post-merge ops; current train |
| 2 | `## Deps-modernization train` | **KEEP** | 57–98 | Active train CI rebuild note |
| 3 | `## Hardening Car 3 — CI image rebuild` | **PRUNE** | 99–114 | Merged PR #179; image `yadgar-ci:5.121.0` already pushed; no post-merge operator action remains |
| 4 | `## R3 — CROSS_ENCODER_BACKEND knob removed` | **KEEP** | 115–132 | Env-var removal notice; operators with stale config still need this |
| 5 | `## v5.53.0 — Bootstrap catalog` | **KEEP (strip sub)** | 133–167 | Section is valid; strip `### D-personal TODO for Max` subsection (lines 135–161) — nix-repo, home-manager switch, `~/git/nix/dotfiles` paths are Max-personal, not operator-actionable |
| 6 | `## v5.50.2 — Control tab` | **KEEP** | 168–278 | New env var; operator-actionable |
| 7 | `## v5.50.0 — Tab router` | **KEEP** | 279–321 | Operator deploy notes |
| 8 | `## v5.49.0 — Upgrade orchestrator` | **KEEP** | 322–452 | Large operator guide; update mechanism |
| 9 | `## v5.48.0 — Update mechanism` | **KEEP** | 453–531 | Operator install flow |
| 10 | `## v5.46.20 — Comprehensive install path fixes` | **KEEP** | 532–626 | Last point-fix in the v5.46 train; install path changes operators need |
| 11 | `## v5.46.19 — Rocky Linux SELinux` | **KEEP** | 627–692 | Platform-specific operator steps; non-trivial |
| 12 | `## v5.46.18 — yadgar --version flag` | **KEEP** | 693–732 | Short; useful probe for operators |
| 13 | `## v5.46.17 — secrets dedup` | **KEEP** | 733–769 | Env-var removal; operator-actionable |
| 14 | `## v5.46.16 — except-tuple py2 syntax sweep` | **PRUNE** | 770–804 | Pure code-quality change; no operator action; "no user action required" |
| 15 | `## v5.46.15 — seed anchors via daemon REST` | **KEEP** | 805–855 | Changes MCP tool behavior; operator-facing |
| 16 | `## v5.46.14 — step 9 install-rules venv fix` | **KEEP** | 856–906 | Install-path fix; operators who hit the bug need the steps |
| 17 | `## v5.46.13 — step 8 config init-if-missing` | **KEEP** | 907–955 | Install-path fix |
| 18 | `## v5.46.12 — backend version canonical source` | **KEEP** | 956–1035 | Version-source change; affects probe scripts |
| 19 | `## v5.46.11 — pipx CLI invocation fix` | **KEEP** | 1036–1095 | Breaks fresh installs; operator must upgrade |
| 20 | `## v5.46.10 — pipx wheel bundle gap fix` | **KEEP** | 1096–1183 | Breaks fresh hosts; `IMPACT` tag present |
| 21 | `## v5.46.9 — CI bake speedup + F1/F6 test regressions` | **PRUNE** | 1184–1230 | "No user action required"; pure CI infra + test isolation fix |
| 22 | `## v5.46.8 — Forgejo workflow trigger gate` | **PRUNE** | 1231–1260 | "No user action required"; internal CI gate; gating logic already shipped |
| 23 | `## v5.46.7 — CI branch wiring hotfix` | **PRUNE** | 1261–1292 | "No user action required"; test harness fix, no runtime change |
| 24 | `## v5.46.6 — Circuit breaker clock fix` | **KEEP** | 1293–1318 | `insert_memory` behavior change (empty `directory_context` normalisation) — affects operators who query this field |
| 25 | `## v5.46.5 — Missing functions, endpoints` | **PRUNE** | 1319–1332 | "No user action required"; test-compat restore only |
| 26 | `## v5.46.4 — Test fixture refactor layer` | **PRUNE** | 1333–1351 | "No user action required"; test fixtures + signals payload trim (≤8 tokens) |
| 27 | `## v5.46.3 — CI infrastructure layer` | **PRUNE** | 1352–1373 | "No user action required"; CI runner image change + env var wiring — no runtime change |
| 28 | `## v5.46.2 — Runtime detection UX hotfix` | **KEEP** | 1374–1441 | UX fix on fresh installs; pipx path |
| 29 | `## v5.46.1 — Distribution infrastructure prep` | **KEEP** | 1442–1504 | Prereqs for v5.46 rollout |
| 30 | `## v5.46.0 — Distribution: pipx + Homebrew + Nix flake` | **KEEP** | 1505–1581 | Major install surface; operator-actionable |
| 31 | `## v5.45.1 — macOS launchd plist` | **KEEP** | 1582–1642 | macOS deploy steps; referenced by DECISIONS.md PD-38 |
| 32 | `## v5.45.0 — Setup Foundation` | **KEEP** | 1643–1701 | Foundation for multi-runtime install |
| 33 | `## v5.44.0 — Subagent MCP wiring` | **KEEP (strip sub)** | 1702–1782 | Valid operator section; strip the "Nix users (this project):" bullet at line 1708 — it is project-internal, not general operator guidance |
| 34 | `## Yadgar Findings` (stray, inside v5.44.0 code block) | **STRIP** | 1750–1755 | Artifact of SubagentStop directive parsing — stray markdown headings inside a fenced code block; visually confusing; harmless but confusing |
| 35 | `## v5.43.0 — MCP schema discipline` | **KEEP** | 1784–1846 | `branch_hint` + `directory` enforcement; operators who call MCP tools need this |
| 36 | `## v5.42.6 — directory backfill repair` | **KEEP** | 1847–1885 | Repair guide; references DLQ |
| 37 | `## v5.42.5 — directory contract` | **KEEP** | 1886–1953 | Contract change; operator-facing MCP behavior |
| 38 | `## v5.42.3 — drainer branch enforcement` | **KEEP** | 1954–1987 | Breaking behavior change in branch detection |
| 39 | `## v5.42.2 — wiki branch-default scope mismatch` | **KEEP** | 1988–2035 | Wiki behavior fix; operator-relevant if using wiki |
| 40 | `## v5.42.1 — wiki_page embedding backfill` | **KEEP** | 2036–2100 | One-time backfill; some operators may still need |
| 41 | `## v5.42.0 — DLQ-based async rejection` | **KEEP** | 2101–2183 | New DLQ surface; operator-actionable |
| 42 | `## v5.41.5 — similarity gate moved to drainer` | **KEEP** | 2184–2261 | Behavior change; dedup logic moved |
| 43 | `## v5.41.4 — roadmap-update-lag signal` | **KEEP** | 2262–2316 | New signal; no action but useful |
| 44 | `## v5.41.3 — MCP-handler perf test` | **KEEP** | 2317–2355 | I9 framing correction |
| 45 | `## v5.41.2 — wiki_add wait flag` | **KEEP** | 2356–2393 | New `wait=` param; operator-actionable |
| 46 | `## v5.41.1 — Wiki versioning transactional atomicity` | **KEEP** | 2394–2439 | Behavior fix; wiki conflict resolution |
| 47 | `## v5.41.0 — Wiki versioning + section-patching` | **KEEP** | 2440–2485 | Major wiki surface; operator-facing |
| 48 | `## v5.39.0 — Wiki similarity gate` | **KEEP** | 2486–2552 | New gate; config knob |
| 49 | `## v5.37.0 — Viz integration testing infra` | **KEEP** | 2553–2596 | Viz test surface |
| 50 | `## v5.35.1 — Memory block follow-ups` | **KEEP** | 2597–2633 | Block API follow-ups |
| 51 | `## v5.35.0 — JavaScript/TypeScript SDK` | **KEEP** | 2634–2668 | New SDK; operator-actionable |
| 52 | `## v5.33.0 — In-context memory blocks` | **KEEP** | 2669–2729 | New block API |
| 53 | `## v5.31.1 — Graph filter + MCP recall kwargs` | **KEEP** | 2730–2748 | New kwargs |
| 54 | `## v5.31.0 — Recall pipeline plugin architecture` | **KEEP** | 2749–2814 | Major recall change |
| 55 | `## v5.29.0 — Bi-temporal edges` | **KEEP** | 2815–2865 | Schema change; operator-relevant |
| 56 | `## v5.27.0 — DuckDB analytics export` | **KEEP** | 2866–2907 | New export surface |
| 57 | `## v5.26.0 — LongMemEval Sonnet 4.6 Full 500q` | **KEEP** | 2908–2959 | Benchmark results; operator-visible quality gate |
| 58 | `## v5.25.6 — README white-bg HTML table` | **PRUNE** | 2960–2978 | Docs/branding only; no operator deploy action; "Rebuild required" is implicit for all releases |
| 59 | `## v5.25.5 — Branding: pivot SVG hero → PNG` | **PRUNE** | 2979–3000 | Branding asset swap; no operator steps beyond standard image rebuild |
| 60 | `## v5.25.4 — Branding: SVG logo + favicon` | **PRUNE** | 3001–3022 | Branding only; no config change |
| 61 | `## v5.25.3 — Fast Profile Follow-up` | **KEEP** | 3023–3050 | Fixes CPU burst; operator-relevant if observing high CPU |
| 62 | `## v5.25.2 — CPU Burst Hotfix** | **KEEP** | 3051–3112 | Root-cause + fix; operator-relevant |
| 63 | `## v5.25.1 — Benchmark Phase 1: surreal subprocess` | **KEEP** | 3113–3187 | Benchmark infra change |
| 64 | `## v5.25.0 — Benchmark Phase 1: retrieval infra` | **KEEP** | 3188–3258 | Benchmark infra |
| 65 | `## v5.24.2 — Bookmarks hotfix` | **KEEP** | 3259–3291 | Bug fix; operator should upgrade |
| 66 | `## v5.24.1 — Bookmarks hotfix` | **KEEP** | 3292–3331 | Bug fix |
| 67 | `## v5.24.0 — Wiki Bookmarks frontend` | **KEEP** | 3332–3408 | New surface |
| 68 | `## v5.23.0 — Wiki Bookmarks backend` | **KEEP** | 3409–3477 | New surface |
| 69 | `## v5.21.0 — Cross-project anchor dedup` | **KEEP** | 3478–3575 | Behavior change; operator-relevant |
| 70 | `## v5.20.0 — DB-lockdown PreToolUse hook` | **KEEP** | 3576–3634 | Hook path moved; operators with custom installs affected |
| 71 | `## v5.19.0 — Scope-aware anchor surfacing` | **KEEP** | 3635–3675 | Behavior change |
| 72 | `## v5.17.0 — Write-time contradiction detection` | **KEEP** | 3676–3715 | Default-on behavior change |
| 73 | `## v5.15.0 — CPU burst detection` | **KEEP** | 3716–3777 | New feature; gate plumbing |
| 74 | `## v5.13.1 — Integration test backend version pin` | **PRUNE** | 3778–3804 | Test-infra fix; "no user action required" |
| 75 | `## v5.13.0 — Secret-gate context-awareness` | **KEEP** | 3805–3894 | Config allowlist change; operator-actionable |
| 76 | `## v5.11.0 — Viz knobs configurable via config.yaml` | **KEEP** | 3895–3940 | New config surface; operators use viz |
| 77 | `## v5.10.11 — Viz polish (3D-only)` | **PRUNE** | 3941–3972 | Cosmetic tweak; no operator action; subsumed by v5.11.0 config knobs |
| 78 | `## v5.10.10 — Viz polish: 2x 3D node size` | **PRUNE** | 3973–4035 | Cosmetic; no operator action |
| 79 | `## v5.10.9 — Viz orphan-edge filter` | **PRUNE** | 4036–4117 | Cosmetic; no operator action |
| 80 | `## v5.10.8 — Viz physics hang + mesh leak fix` | **PRUNE** | 4118–4194 | Bug fix embedded in a series of viz iterations; no forward operator action |
| 81 | `## v5.10.7.3 — Revert v5.10.7 custom 3D node geometry` | **PRUNE** | 4195–4235 | Revert of a revert; historical churn; final state is v5.11.0 |
| 82 | `## v5.10.7.2 — 3D viz transparent flag fix` | **PRUNE** | 4236–4278 | Cosmetic fix; no operator action |
| 83 | `## v5.10.7.1 — Bundled hotfix: sentinel filter + viz lighting` | **PRUNE** | 4279–4314 | Cosmetic; no operator action |
| 84 | `## v5.10.7 — Viz UX fixes` | **PRUNE** | 4315–4345 | Cosmetic; no operator action |
| 85 | `## v5.10.6 — SESSION_END_CAPTURE sentinel-marker pattern` | **KEEP** | 4346–4405 | Protocol change; operators using hooks need this |
| 86 | `## v5.10.5 — nightly cycle remaining bugs` | **KEEP** | 4406–4445 | Bug fixes affecting nightly cycle |
| 87 | `## v5.10.4 — consolidate_now mode parameter` | **KEEP** | 4446–4478 | New param; operator-facing |
| 88 | `## v5.10.3 — scan_db_for_secrets.py e2e fix` | **KEEP** | 4479–4513 | Script fix |
| 89 | `## v5.10.2 — Secret-gate architecture` | **KEEP** | 4514–4566 | Architecture change |
| 90 | `## v5.10.1 — _active_work soft warning tier` | **KEEP** | 4567–4629 | New warning tier; operator-visible |
| 91 | `## backend v5.4.0 — Recall hot-path caching` | **KEEP** | 4630–4740 | Backend cache change; performance-relevant |
| 92 | `## v5.10.0 — Test Harness Hardening` | **KEEP** | 4741–4817 | Harness change; affects contributors |
| 93 | `## v5.9.0 — Anchor Audit` | **KEEP** | 4818–4931 | New tool; operator-actionable |
| 94 | `## v5.8.0 — Anchor Hygiene Foundation` | **KEEP** | 4932–5024 | New anchor tier system |
| 95 | `## v5.7.12 — project_brief two-audience split` | **KEEP** | 5025–5105 | API shape change |
| 96 | `## v5.7.11 + backend v5.3.1 — Yamlify OTLP + DBSIZE knobs` | **KEEP** | 5106–5173 | Config change |
| 97 | `## v5.7.10 — Container yaml loading + I25 invariant` | **KEEP (strip sub)** | 5174–5286 | Valid section; strip Max-personal nix deploy bullet (see Strip List §A) |
| 98 | `## Backend v5.3.0 — dbsize cache + restart attribution` | **KEEP** | 5287–5358 | Backend behavior change |
| 99 | `## v5.7.9 — Source-aware SessionStart response` | **KEEP** | 5359–5397 | Hook behavior change |
| 100 | `## v5.7.8 — /mcp trace_id wiring` | **KEEP** | 5398–5428 | Trace wiring |
| 101 | `## v5.7.7 — VIZ_HEALTH_REFRESH_SEC env knob` | **KEEP** | 5429–5454 | New env knob |
| 102 | `## v5.7.6 — OTLP/HTTP span exporter to Tempo` | **KEEP** | 5455–5499 | New exporter; operator config |
| 103 | `## v5.7.5 — I24 @trace_span AST lint` | **KEEP** | 5500–5540 | Lint enforcement |
| 104 | `## v5.7.4 — Hook observability extension` | **KEEP** | 5541–5576 | Hook change |
| 105 | `## v5.7.3 — DB metric dedup` | **KEEP** | 5577–5606 | Metric dedup |
| 106 | `## v5.7.2 — CROSS_ENCODER_TOP_K cut 20→10` | **KEEP** | 5607–5642 | Performance tuning; operator-relevant |
| 107 | `## v5.7.1 — Consolidation systemctl container fix` | **KEEP** | 5643–5675 | Container fix |
| 108 | `## v5.7.0 — Nightly Cycle Redesign` | **KEEP (strip sub)** | 5676–5781 | Major behavior change; strip Max-personal nix deploy bullets (see Strip List §B) |
| 109 | `## v5.6.7 PR-M — Optional log-dir relocation` | **KEEP** | 5782–5858 | New env knob `YADGAR_LOG_DIR`; operator-actionable |
| 110 | `## v5.6.1 — V1c bug fixes` | **KEEP** | 5859–5886 | Bug fixes |
| 111 | `## v5.6.0 — V1c viz daemon sidebar` | **KEEP** | 5887–5926 | New UI surface |
| 112 | `## v5.5.3 — V1b CB-1 state gauge` | **KEEP** | 5927–5954 | New gauge |
| 113 | `## v5.5.2 — backend log metric wiring fix` | **KEEP** | 5955–5982 | Metric fix |
| 114 | `## v5.5.1 — log rotation + rate limiter` | **KEEP** | 5983–6058 | Operator-actionable |
| 115 | `## v5.4.8 — middleware request-log visibility fix` | **KEEP** | 6059–6103 | Behavior fix |
| 116 | `## v5.4.7 — I14 ratchet cleanup` | **KEEP** | 6104–6153 | Ratchet change |
| 117 | `## v5.4.6 — LOW-risk complexity refactor batch` | **KEEP** | 6154–6188 | Code quality notes |
| 118 | `## v5.3.9 — Crash hotfix` | **KEEP** | 6189–6305 | Crash fix; important |
| 119 | `## v4.8 backup retention` | **KEEP** | 6306–6361 | Still-relevant retention policy |
| 120 | `## v4.8 weekly SurrealKV vacuum` | **KEEP** | 6362–6416 | Vacuum setup |
| 121 | `## v4.8 log-level config` | **KEEP** | 6417–6425 | Config reference |
| 122 | `## v4.8 consolidation cooldown` | **KEEP** | 6426–6442 | Config reference |
| 123 | `## v5.0 Stage 1 — Credentials Hardening + MCP Auth` | **KEEP** | 6443–6545 | Major auth surface; still referenced |
| 124 | `## v5.0.1 — durable MCP token wiring in nix module` | **KEEP (strip sub)** | 6546–6631 | Valid; strip the nix-specific inline action (see Strip List §C) |
| 125 | `## v5.1 — yadgar-vacuum.service ExecStart fix (A2)` | **KEEP** | 6632–6695 | Systemd unit fix |
| 126 | `## v5.4.3 — I14 framework-logger coverage` | **KEEP** | 6696–6745 | Logger coverage |
| 127 | `## v5.4.2 — CB-1 probe fixes + F5-A saturation fix` | **KEEP** | 6746–6887 | Probe fixes |
| 128 | `## v5.5.0 — V1a backend /metrics endpoint` | **KEEP** | 6888–6964 | New metrics endpoint |
| 129 | `## v5.7.0 PR-4 — vacuum_now() trigger-file pattern` | **KEEP** | 6965–7044 | MCP vacuum trigger |
| 130 | `## v5.7.0 PR-5 — VACUUM_AUTO_THRESHOLD_BYTES backstop` | **KEEP** | 7045–7083 | Mental-model clarification; operator-relevant |
| 131 | `# Orchestration safety net (2026-06-14)` + sub-headings `## 1.` `## 2.` `## 3.` | **KEEP** | 7084–7118 | Watchdog install; operator must run manually |
| 132 | `## v5.56.0 release` | **KEEP** | 7120–7134 | Release orchestration note |
| 133 | `## v5.57.0` | **KEEP** | 7136–7168 | Branch protection + nix bump steps |
| 134 | `## SurrealDB server upgrade v3.0.5 → v3.1.5` | **KEEP** | 7170–7282 | Critical upgrade sequence; rollback instructions |
| 135 | `## Phase 2b: stdio transport dropped` | **KEEP** | 7284–7320 | Breaking change; MCP client config migration |
| 136 | `## R3 — shared queue volume for the backend drainer` | **KEEP** | 7322–7346 | Prod deploy action |
| 137 | `## R3 — deployment prerequisites summary` | **KEEP** | 7348–7368 | Deploy prereq checklist |
| 138 | `## Data-dir hygiene — one-time migration` | **KEEP (strip sub)** | 7370–7436 | Valid; strip hardcoded June incident artifact filenames (see Strip List §D) |

---

## Strip List (Subsection Surgery — Not Whole-Section Deletes)

These are targeted bullet/paragraph removals inside KEEP sections. The surrounding section stays intact.

### §A — v5.7.10 (line 5174): strip nix-personal deploy paragraph

**Location:** `## v5.7.10 — Container yaml loading + I25 invariant` section, under `### Deploy steps` or equivalent. Identify and remove any bullet/sentence that:
- References `~/git/nix` or `home-manager switch`
- Is scoped to "this project's nix install" specifically
- Uses hardcoded paths like `/home/max`

**Rule:** Keep generic deploy steps (docker pull, systemd restart). Remove steps that only apply to Max's nix repo.

### §B — v5.7.0 (lines 5707–5733): strip Max-personal nix deploy bullets

**Location:** `## v5.7.0 — Nightly Cycle Redesign`, `### Deploy steps`, items 2–4 (lines 5707–5736 inclusive).

**Specific content to strip:**
- Step 2 (`Bump yadgar_core_version in ~/git/nix/modules/home/yadgar.nix`) — nix-repo personal step
- Step 3 (`Apply nix changes … cd ~/git/nix && nix-update`) and its sub-bullets listing the activated systemd units — personal nix workflow
- Step 4 (`Re-run the pipx editable install so yadgar-nightly-cycle console-script entry registers`) — personal dev-mode install step

**Keep:** Step 1 (rebuild + push image), `### Verification`, `### Behavior change`, `### Known gaps / followups`.

**Rationale:** General operators install via `pip install yadgar` or `pipx install yadgar`; the nix-home-manager flow is Max's personal workstation setup, not a user-facing deploy step.

### §C — v5.0.1 (line 6546): strip nix-module-specific paragraph

**Location:** `## v5.0.1 — durable MCP token wiring in nix module`. If the section body consists primarily or entirely of nix-module steps (`home-manager switch`, `~/git/nix` paths), strip those steps. Keep any generic MCP token verification steps or config.yaml guidance.

**Note:** If the entire section is nix-only with no general content, escalate to PRUNE and include in counts below.

### §D — Data-dir hygiene (line 7403–7409): strip hardcoded June-incident artifact names

**Location:** `## Data-dir hygiene — one-time migration`, within `### One-time migration` code block, step 4.

**Content to strip** (inside the bash script, line 7403–7409):
```bash
rm -rf \
  "$DATA/surreal_db.bak" \
  "$DATA/surreal_db.bloated-20260614_021847" \
  "$DATA/surreal_db.bloated-20260616_190014" \
  "$DATA/surreal_db.EMPTY-postvacuum-20260616"
```
And the accompanying comment lines listing the specific file sizes (lines ~7399–7409 including the `# 4. DELETE June incident debris (~1.4 GB — confirm sizes below before running)` header).

**Also strip** the size table row for these four artifacts (lines ~7422–7427, rows for `surreal_db.bak`, `surreal_db.bloated-*`, `surreal_db.EMPTY-postvacuum-*`). Update "Total freed" row accordingly.

**Keep:** Steps 1–3, 5–7 (generic path layout + orphaned export file cleanup + old-dir retention). Keep the general inventory table structure; strip only the incident-specific rows.

**Rationale:** These artifact names are June 2026 incident debris on Max's specific machine. General operators will not have these files. Leaving them suggests operators should delete files that don't exist on their installs.

### §E — v5.44.0 (line 1708): strip "Nix users (this project):" bullet

**Location:** `## v5.44.0`, `### Who needs to act`, first bullet at line 1708.

**Strip exactly:**
```
**Nix users (this project):** No action required. Agent templates and hook config are delivered via the nix repo (home-manager module update). Nix rebuild picks up the new `general-purpose.md` etc.
```

**Keep:** The "Non-nix users:" bullet and remainder of section.

### §F — v5.53.0 `### D-personal TODO for Max` (lines 135–161): strip entire subsection

**Location:** `## v5.53.0`, lines 135–161.

**Strip exactly:** the heading `### D-personal TODO for Max (nix-managed Claude config — NOT general)` and all content through to (but not including) `### No config knob changes in this release` at line 162.

This includes:
- The `**Do NOT apply this yourself...`** warning
- The `~/git/nix/dotfiles/common/claude.md` path reference
- The numbered action list (edit nix claude.md, run `home-manager switch`, verify)
- The `**Action required:**` block

---

## Resulting Trimmed Table of Contents

After applying all PRUNE and STRIP actions, the top-level section order (all `## …` headings) will be:

```
## T4 Ettin CE-swap train — post-merge ops
## Deps-modernization train — CI images rebuild required BEFORE merging
## R3 — CROSS_ENCODER_BACKEND knob removed
## v5.53.0 — Bootstrap catalog + read-first contract        [§F stripped]
## v5.50.2 — Control tab + backend control APIs
## v5.50.0 — Tab router, viz Variant C, bookmarks redirect
## v5.49.0 — Upgrade orchestrator + memory archive retention
## v5.48.0 — Update mechanism
## v5.46.20 — Comprehensive install path fixes
## v5.46.19 — Rocky Linux SELinux + restart-on-regen hotfix
## v5.46.18 — yadgar --version flag
## v5.46.17 — secrets dedup
## v5.46.15 — seed anchors via daemon REST endpoint
## v5.46.14 — step 9 install-rules venv python fix
## v5.46.13 — step 8 config init-if-missing fix
## v5.46.12 — backend version canonical source fix
## v5.46.11 — pipx CLI invocation fix
## v5.46.10 — pipx wheel bundle gap fix
## v5.46.6 — Circuit breaker clock fix, NLI spy wiring, SurrealDB install, carryover
## v5.46.2 — Runtime detection UX hotfix
## v5.46.1 — Distribution infrastructure prep
## v5.46.0 — Distribution: pipx + Homebrew + Nix flake + SBOM + release automation
## v5.45.1 — macOS launchd plist generation + install
## v5.45.0 — Setup Foundation: make-canonical, multi-runtime, seed anchors
## v5.44.0 — Subagent MCP wiring + automation extensions    [§E stripped]
## v5.43.0 — MCP schema discipline: caller-context enforcement
## v5.42.6 — directory backfill repair + resolution hole fix
## v5.42.5 — directory contract
## v5.42.3 — drainer branch enforcement + memory branch_hint parity
## v5.42.2 — wiki branch-default scope mismatch fix
## v5.42.1 — wiki_page embedding backfill + embed-failure surfacing
## v5.42.0 — DLQ-based async rejection tracking
## v5.41.5 — similarity gate moved to drainer
## v5.41.4 — roadmap-update-lag signal + wiki_append_section convention
## v5.41.3 — MCP-handler perf test + I9 attribution correction
## v5.41.2 — wiki_add wait flag for read-your-writes consistency
## v5.41.1 — Wiki versioning transactional atomicity hotfix
## v5.41.0 — Wiki versioning + section-patching
## v5.39.0 — Wiki similarity gate
## v5.37.0 — Viz integration testing infrastructure
## v5.35.1 — Memory block follow-ups
## v5.35.0 — JavaScript/TypeScript SDK @yadgar/sdk v0.1.0
## v5.33.0 — In-context memory blocks
## v5.31.1 — Graph filter + MCP recall kwargs
## v5.31.0 — Recall pipeline plugin architecture
## v5.29.0 — Bi-temporal edges extension
## v5.27.0 — DuckDB analytics export
## v5.26.0 — LongMemEval Sonnet 4.6 Full 500q
## v5.25.3 — Fast Profile Follow-up: instructions_loaded + viz_search
## v5.25.2 — CPU Burst Hotfix: subagent_start fast profile + action-log poison-pill skip
## v5.25.1 — Benchmark Phase 1: spawn surreal-server subprocess
## v5.25.0 — Benchmark Phase 1: retrieval infra + reproducibility metadata
## v5.24.2 — Bookmarks hotfix
## v5.24.1 — Bookmarks hotfix
## v5.24.0 — Wiki Bookmarks frontend
## v5.23.0 — Wiki Bookmarks backend
## v5.21.0 — Cross-project anchor dedup detection + PD-23 migration_grace handler
## v5.20.0 — DB-lockdown PreToolUse hook migrated + Claude Code 2026 schema fix
## v5.19.0 — Scope-aware anchor surfacing in HippocampalReplay
## v5.17.0 — Write-time contradiction detection default-on
## v5.15.0 — CPU burst detection + secret-gate plumbing
## v5.13.0 — Secret-gate context-awareness + allowlist
## v5.11.0 — Viz knobs configurable via config.yaml
## v5.10.6 — SESSION_END_CAPTURE sentinel-marker pattern
## v5.10.5 — nightly cycle remaining bugs
## v5.10.4 — consolidate_now mode parameter
## v5.10.3 — scan_db_for_secrets.py end-to-end fix
## v5.10.2 — Secret-gate architecture + memorize parity + nightly cycle hotfix
## v5.10.1 — _active_work soft warning tier + watchdog timer
## backend v5.4.0 — Recall hot-path caching
## v5.10.0 — Test Harness Hardening
## v5.9.0 — Anchor Audit
## v5.8.0 — Anchor Hygiene Foundation
## v5.7.12 — project_brief two-audience split + signals/restore modes
## v5.7.11 + backend v5.3.1 — Yamlify OTLP + DBSIZE knobs, drop dead LOG_LEVEL
## v5.7.10 — Container yaml loading + I25 invariant + nix -e cleanup  [§A stripped]
## Backend v5.3.0 — dbsize cache + restart attribution
## v5.7.9 — Source-aware SessionStart response
## v5.7.8 — /mcp trace_id wiring
## v5.7.7 — VIZ_HEALTH_REFRESH_SEC env knob
## v5.7.6 — OTLP/HTTP span exporter to Tempo
## v5.7.5 — I24 @trace_span AST lint
## v5.7.4 — Hook observability extension
## v5.7.3 — DB metric dedup
## v5.7.2 — CROSS_ENCODER_TOP_K cut 20 → 10
## v5.7.1 — Consolidation systemctl container fix
## v5.7.0 — Nightly Cycle Redesign                          [§B stripped]
## v5.6.7 PR-M — Optional log-dir relocation
## v5.6.1 — V1c bug fixes
## v5.6.0 — V1c viz daemon sidebar
## v5.5.3 — V1b CB-1 state gauge
## v5.5.2 — backend log metric wiring fix
## v5.5.1 — log rotation + rate limiter
## v5.4.8 — middleware request-log visibility fix
## v5.4.7 — I14 ratchet cleanup
## v5.4.6 — LOW-risk complexity refactor batch
## v5.3.9 — Crash hotfix
## v4.8 backup retention
## v4.8 weekly SurrealKV vacuum
## v4.8 log-level config
## v4.8 consolidation cooldown
## v5.0 Stage 1 — Credentials Hardening + MCP Auth
## v5.0.1 — durable MCP token wiring in nix module          [§C stripped]
## v5.1 — yadgar-vacuum.service ExecStart fix (A2)
## v5.4.3 — I14 framework-logger coverage + ruff grandfathering
## v5.4.2 — CB-1 probe fixes + F5-A saturation fix
## v5.5.0 — V1a backend /metrics endpoint
## v5.7.0 PR-4 — vacuum_now() trigger-file pattern
## v5.7.0 PR-5 — VACUUM_AUTO_THRESHOLD_BYTES is an emergency backstop
# Orchestration safety net (2026-06-14)
## v5.56.0 release
## v5.57.0
## SurrealDB server upgrade v3.0.5 → v3.1.5
## Phase 2b: stdio transport dropped — MCP client config migration required
## R3 — shared queue volume for the backend drainer
## R3 — deployment prerequisites summary
## Data-dir hygiene — one-time migration                    [§D stripped]
```

**Sections removed vs original:** Hardening Car 3; v5.46.16; v5.46.9; v5.46.8; v5.46.7; v5.46.5; v5.46.4; v5.46.3; v5.25.6; v5.25.5; v5.25.4; v5.13.1; v5.10.11; v5.10.10; v5.10.9; v5.10.8; v5.10.7.3; v5.10.7.2; v5.10.7.1; v5.10.7 — **20 sections pruned**.

---

## Root-vs-Move Decision

**Decision: Keep at repo root as `MIGRATION_NOTES.md`. Do not rename.**

**Justification:**

1. `README.md:213` — prose sentence `See MIGRATION_NOTES.md` with no path prefix
2. `README.md:399` — markdown link `[Migration notes](MIGRATION_NOTES.md)`
3. `AGENTS.md:238` — markdown link `[MIGRATION_NOTES.md](MIGRATION_NOTES.md)`
4. `docs/configuration.md:690` — prose `See MIGRATION_NOTES.md`
5. `.forgejo/workflows/ci-pr.yaml:24` — path filter `'MIGRATION_NOTES.md'` triggers CI on changes
6. `.forgejo/workflows/ci-release.yaml:24` — same path filter

Moving the file requires updating all six references. The CI path filter is the highest-risk change — a missed update silently stops CI triggering on operator note updates.

Renaming to `OPERATOR_NOTES.md` provides cosmetic clarity at the cost of breaking six references and risking stale CI gating. Net value: negative. Skip.

---

## Acceptance Criteria

A builder executing this plan passes acceptance when:

1. **Inbound refs still resolve:** verify `grep -r "MIGRATION_NOTES" . --include="*.md" --include="*.yaml"` returns the same 6 files hitting `MIGRATION_NOTES.md` at root. No dead links.
2. **No operator-actionable content lost:** all KEEP sections present in trimmed file; all STRIP edits only remove nix-personal / incident-artifact-specific bullets, not surrounding operator steps.
3. **Estimated line reduction achieved:** trimmed file is between 5 800 and 6 100 lines (measured via `wc -l`). See breakdown below.
4. **No section-order change:** sections appear in same relative order as the original. Do not reorder to fix chronology — risk of confusion.
5. **CI path filter still fires:** `ci-pr.yaml` and `ci-release.yaml` path filters for `MIGRATION_NOTES.md` still match the file. Confirmed by: file still at `MIGRATION_NOTES.md` in repo root.
6. **No version bump required:** this is docs-only. `pyproject.toml` version is unchanged.

### Line reduction breakdown (estimated)

| Category | Sections / bullets | Estimated lines removed |
|---|---|---|
| PRUNE — whole sections (20 sections) | v5.46.16; v5.46.9; v5.46.8; v5.46.7; v5.46.5; v5.46.4; v5.46.3; v5.25.6; v5.25.5; v5.25.4; v5.13.1; v5.10.11; v5.10.10; v5.10.9; v5.10.8; v5.10.7.3; v5.10.7.2; v5.10.7.1; v5.10.7; Hardening Car 3 | ~1 250 |
| STRIP §F — D-personal TODO for Max subsection | 27 lines | ~27 |
| STRIP §B — v5.7.0 nix deploy bullets | ~30 lines | ~30 |
| STRIP §E — v5.44.0 "Nix users (this project):" bullet | ~2 lines | ~2 |
| STRIP §A — v5.7.10 nix paragraph | ~5 lines | ~5 |
| STRIP §C — v5.0.1 nix-specific steps | ~10 lines | ~10 |
| STRIP §D — data-dir June incident artifact names + table rows | ~20 lines | ~20 |
| **Total** | | **~1 344** |

**Final estimated line count:** 7 436 − 1 344 ≈ **6 092 lines**

---

## Scope

- **In scope:** `MIGRATION_NOTES.md` edits only (delete lines / remove paragraphs).
- **Out of scope:** `CHANGELOG.md`, `README.md`, `AGENTS.md`, `docs/configuration.md`, CI YAML files, any source code, version bumps.
- **Not a refactor:** do not reorganize chronology, add new headings, or rewrite prose. Delete or leave as-is.

---

## Version Impact

Docs-only change. No version bump required.

**CI path-filter note:** both `ci-pr.yaml` and `ci-release.yaml` path filters include `MIGRATION_NOTES.md`. Edits to this file **will** trigger CI test jobs on the PR. This is expected behavior and is not a problem — it ensures CI validates the repo state after the trim. Do not remove the path filter.

---

## AUDIT (2026-07-13)

Independent adversarial audit. Verified against the live `MIGRATION_NOTES.md` (7 436 lines) and inbound-ref files as of this date. **Status: AUDITED — ready.** The plan is buildable; no false-prunes found. Two required additions before build (one execution guardrail, one rationale correction) — neither is a soundness defect.

### Verification table (load-bearing claims)

| # | Claim | Verdict | Evidence |
|---|-------|---------|----------|
| A | File is 7 436 lines | **VERIFIED** | `wc -l` = 7436 |
| B | PD-23 deadline section (2026-08-26) preserved | **VERIFIED** | Authoritative PD-23 section is `## v5.21.0` at line 3478 (`### PD-23 Deadline (CRITICAL)` line 3483, deadline 3485, handler steps 3522-3562), marked **KEEP** (row 69). Deadline survives the trim. |
| C | PD-23 reminders inside PRUNE'd sections | **VERIFIED — no unique loss** | Two *redundant* one-line reminders sit in PRUNE'd viz sections: `v5.10.8` line 4191 and `v5.10.7.3` line 4232 (both `"PD-23 migration_grace expiry 2026-08-26 still requires v5.11.x handler before then"`). These are duplicates of the authoritative KEEP section — pruning them loses no unique operator info. Noted, not a blocker. |
| D | All 6 inbound refs resolve to root `MIGRATION_NOTES.md` | **VERIFIED (1 line-number nit)** | `AGENTS.md:238`, `README.md:213`, `README.md:399`, `docs/configuration.md:690`, `ci-release.yaml:24` all confirmed. **Correction:** `ci-pr.yaml` hit is at **line 18**, not `:24` as the BLUF/§Root-vs-Move state. Both YAML path filters still fire (file stays at root, same name) — the ref itself is fine; only the cited line number is wrong. |
| E | PRUNE header line ranges accurate | **VERIFIED** | Spot-checked 15 PRUNE headers (rows 3,14,21,22,23,25,26,27,58,59,60,74,77,80,84) — every `sed -n '<start>p'` matched the claimed heading exactly. |
| F | STRIP §F (v5.53.0 D-personal TODO) target | **VERIFIED** | Lines 133-167 are exactly the `### D-personal TODO for Max` nix-personal subsection (`~/git/nix/dotfiles/common/claude.md`, `home-manager switch`); strip bound (up to `### No config knob changes` at 162) is correct. Genuinely non-operator. |
| G | STRIP §D (data-dir June incident names) target | **VERIFIED** | Hardcoded artifact names (`surreal_db.bak`, `surreal_db.bloated-20260614_021847`, etc.) at lines 7399-7412 + size table; plan correctly KEEPS the generic glob-based steps (`vacuum_export_*`, `surreal_db.old-*`) and strips only the machine-specific rows. |
| H | Line-reduction estimate (~1 344, land ~6 092) | **PLAUSIBLE** | Estimate is internally consistent (20 sections ≈ 1 250 lines + strips). Not independently line-counted; the ±200 acceptance band (5 800–6 100) absorbs the uncertainty. |

### False-prune sweep (the discriminating check)

Every PRUNE section was tested against "is anything here still operator-forward-relevant?"

- **Row 3 `Hardening Car 3` — the only PRUNE about the CURRENT CI image (`yadgar-ci:5.121.0`), so audited hardest.** Verdict **PRUNE holds.** PR #179 is merged (commit `1d823918`, shipped v5.121.0); current repo version is **5.133.0** — the "rebuild BEFORE merging #179" step is long spent, the tag exists. The generic build command (`podman build -f Dockerfile.ci -t …:5.121.0 && podman push`) is trivially reproducible from `Dockerfile.ci` (present in tree), so pruning does not destroy the *only* copy of how to build the CI image. Not a sole-home loss. **NOT a false-prune.**
- All other CI/test-infra PRUNEs (rows 14, 21–27, 74) concern **superseded** image tags (`yadgar-ci:5.46.3/5.46.9`) — dead. Viz PRUNEs (rows 77–84) are cosmetic iterations whose final state is the KEPT `v5.11.0` config-knob section. Branding PRUNEs (rows 58–60) are asset swaps. None forward-actionable.

**Result: zero false-prunes.**

### Required corrections before build (fold into plan)

1. **[RATIONALE FIX — not a scope change]** Row 21 (`v5.46.9`) rationale says *"No user action required; pure CI infra + test isolation fix."* This is **factually false**: the section contains an operator-facing `### Build and push commands (operator, not CI)` block with `docker buildx` invocations and a `### CI image rebuild required` note. The PRUNE *verdict* is still correct (5.46.9 is superseded → not forward-relevant), but the stated reason is wrong. This exposes the trim's real operating principle, which the plan should state explicitly: **PRUNE = "not forward-relevant for any operator on a current install," NOT "no operator action ever existed."** Several PRUNE'd sections did carry one-time operator steps; they are pruned because those steps are spent, not because they never existed. Correct the row-21 "Why" and add this principle to the BLUF so a future reader does not trust a wrong per-row rationale.

2. **[REQUIRED GUARDRAIL — execution hazard]** The line ranges are accurate **against the pristine 7 436-line file only**. A mechanical builder deleting 20 sections **top-down invalidates every later range** (each deletion shifts all subsequent lines up). This is the most likely way the build corrupts the file, and the Acceptance Criteria do **not** close it. Add to the plan a mandatory execution rule: **delete by header-anchored match (grep the `## <heading>` line, delete to the next `## `/`# ` boundary), OR process sections strictly bottom-up (highest line number first).** Never delete by the tabulated absolute ranges in source order. Same applies to the STRIP edits (anchor on the verbatim text, not the line number).

### Minor notes (non-blocking)

- `ci-pr.yaml` ref line number is `:18` not `:24` (fix in BLUF + §Root-vs-Move). Does not affect the keep-at-root decision.
- Acceptance criterion 3's line band (5 800–6 100) is a good hedge given the estimate is not independently counted. Keep it.
- Section-count claim ("20 sections pruned") matches the enumerated PRUNE rows — verified.

### Status

**AUDITED — ready.** Buildable as written once corrections #1 (rationale/principle) and #2 (header-anchored/bottom-up deletion guardrail) are folded in. Both are additive; neither changes the keep/prune set. No false-prune, PD-23 deadline preserved, all 6 inbound refs resolve at root.

**User-decision items:** none required — the two corrections are mechanical improvements to the plan, not open questions. (Optional: confirm you're content leaving the two redundant PD-23 reminder lines pruned — the authoritative copy survives, so this is safe by default.)
