# Module standardization train — ADR-0066 remainder (I13 internal splits + shim removal)

**Status: AUDITED — needs rework** (2026-07-13; see `## AUDIT (2026-07-13)` at end — 3 WRONG, 2 STALE; rework = Version-math + `.complexity-allowlist.json` C1 scope, not a redesign). No code changed; this doc is the only artifact.
**Date:** 2026-07-13. **Task:** #18 (ADR-0066 "PR B + C1–C6", `wiki:yadgar-adr-log`).
**Baseline:** master at core **5.132.0** / backend **5.43.0** (`yadgar/__init__.py:21` BACKEND_VERSION; `server.json:10-11`), tip `471eba13` (T4 Ettin #191).
**Standard enforced:** `docs/ARCHITECTURE_INVARIANTS.md` §I34 (modular layer coherence + forward-only, ADR-0062) lines 546–622; I13 file caps line 80 (≤1000 hard / ≤500 soft LOC), function caps line 78.

---

## BLUF

ADR-0066 (dated 2026-07-08, `wiki:yadgar-adr-log`) scoped a train that did TWO things per target file:
**(a) promote** the loose top-level `.py` into a package dir, and **(b) split** the oversized file into
cohesive ≤500-LOC modules with per-file-ignores removed and import-linter green.

**Part (a) — promotion — is already DONE**, but not by this train: the **layer-boundary train T2 (#182,
`5b9c8ca1`)** codified ADR-0084 law-(3) "NO lone files — every module a package dir" and promoted every
ADR-0066 target into a package via **PEP-562 back-compat shims** (27 shims repo-wide; `grep -rl "Back-compat shim"`).
So all six C-targets + PR-B files now live in package dirs.

**Part (b) — the I13 internal split — is OUTSTANDING for 5 of 6 C-targets.** The promotion moved the LOC; it
did not shrink it. Several targets even GREW post-ADR (Ettin/deps trains): `embed_service.py` 1194→**1580**,
`cache.py` 976→**980**. This train does the split-the-file work ADR-0066 actually exists for, INSIDE the
now-existing package dirs.

**Plus a forward-only cleanup ADR-0066 didn't foresee:** I34 (line 578) forbids re-export shims in refactor
trains. The 27 PEP-562 shims T2 left are exactly that debt. Removing the shims for THIS train's targets (migrate
importers, delete shim, forward-only) folds naturally into each car — but the full 27-shim removal is a scope
decision (see Open Questions Q3).

Train shape: `feat/vX.Y` + worktree-parallel cars (seams disjoint) + audited-plan-per-car + **ONE PR** +
**one version bump** (core + backend independently). Estimated **~5 cars** of real splitting + 1 cleanup car.

---

## Current-state-vs-outstanding table (VERIFIED against tree at `471eba13`)

Every LOC below = live `wc -l` on 2026-07-13. "Promoted" = loose file is now a PEP-562 shim + real impl in a pkg dir.

| ADR car | ADR target (2026-07-08 LOC) | CURRENT real-impl path | CURRENT LOC | Promoted (T2)? | >500 soft? | grandfather entry | Split done? | Status |
|---|---|---|---|---|---|---|---|---|
| **PR B** | auth_middleware → core/server/; backup → core/export/ | `core/auth_middleware/auth_middleware.py`; `core/backup/backup.py` | 238; 304 | ✅ own dirs | no; no | none | n/a (under cap) | **DONE (placement differs — Q1)** |
| **C1** | embed_service.py 1194 → backend/embed_service/ | `backend/embed_service/embed_service.py` | **1580** | ✅ | YES (also HARD >1000) | none in ruff¹ | ❌ | **OUTSTANDING** |
| C1-metrics | metrics.py keeps own CollectorRegistry | `backend/embed_service/embed_service_metrics.py` | **503** | ✅ | marginally (503) | none | ❌ (barely) | OUTSTANDING (minor) |
| **C2a** | cache.py 976 → backend/cache/ | `backend/cache/cache.py` | **980** | ✅ | YES | none in ruff¹ | ❌ | **OUTSTANDING** |
| **C2b** | ml_client.py 817 → backend/ml_client/ | `backend/ml_client/ml_client.py` | **770** | ✅ | YES | none in ruff¹ | ❌ | **OUTSTANDING** |
| **C3** | daemon.py 948 → core/daemon/ (+lifecycle/daemons/drain/sd_notify) | `core/daemon/daemon.py` | **948** | ✅ (pkg has daemon/daemons/drain/sd_notify/system_metrics) | YES | none² | ❌ | **OUTSTANDING** |
| **C4** | graph_api 880 split + viz cluster → core/viz/ | `backend/graph/graph_api.py` (moved to BACKEND by T2 Car E3); viz → `core/viz/` | 641; viz all <500 | ✅ | graph_api YES (641); viz no | **STALE**³ line 128 | ❌ (graph_api only) | **OUTSTANDING (graph_api); viz DONE** |
| **C5** | install_hooks_lib.py 463 → core/install/ | `core/install/install_hooks_lib.py` | **662** (grew) | ✅ | YES | ✅ `C901` line 129 (live) | ❌ | **OUTSTANDING** |
| **C6** | predictive_coding (optional) | `backend/predictive_coding/predictive_coding.py` | **641** | ✅ | YES | ✅ `C901` line 133 (live) | ❌ | **OUTSTANDING (optional)** |

¹ `cache.py`/`ml_client.py`/`embed_service.py` are **file-LOC** violators (I13 line 80, tracked in
`.complexity-baseline.json`), NOT ruff-C901 per-file-ignores — so "remove the per-file-ignore" from ADR-0066 does
not literally apply; the debt lives in the complexity baseline instead. Verify each with
`python scripts/check_complexity.py`.
² `pyproject.toml:121` grandfathers `core/cli/daemon.py` (277 LOC, the CLI entry) — a DIFFERENT file from
`core/daemon/daemon.py` (948, the supervisor). Do not conflate.
³ `pyproject.toml:128` = `yadgar/core/graph/graph_api.py` — **that path no longer exists** (T2 Car E3 moved it to
`backend/graph/graph_api.py`). The entry is dead; delete it in this train (Car 4). Verified: `ls yadgar/core/graph/` → No such directory.

**All files currently >500 LOC (I13 soft) — for scope discipline (ADR-0066 targets are a SUBSET):**

| path | LOC | in ADR-0066 scope? |
|---|---|---|
| `core/server/tools/project.py` | 2769 | NO (out — see Scope OUT) |
| `core/server/http.py` | 2390 | NO |
| `_shared/wiki/store.py` | 2303 | NO |
| `_shared/config/config_yaml.py` | 2129 | NO |
| `backend/embed_service/embed_service.py` | **1580** | **C1** |
| `core/server/tools/wiki.py` | 1523 | NO |
| `_shared/storage/memory.py` | 1377 | NO |
| `_shared/storage/migrations.py` | 1367 | NO |
| `core/vacuum/__init__.py` | 1280 | NO |
| `_shared/observability/metrics.py` | 1202 | NO |
| `_shared/storage/wiki.py` | 1119 | NO |
| `_shared/config/config.py` | 1053 | NO |
| `_shared/observability/log_config.py` | 1002 | NO |
| `backend/cache/cache.py` | **980** | **C2a** |
| `core/daemon/daemon.py` | **948** | **C3** |
| `backend/admin_exec/invariants.py` | 889 | NO |
| `backend/consolidation/cls.py` | 848 | NO |
| `core/server/tools/audit.py` | 823 | NO |
| `_shared/observability/tracing.py` | 787 | NO |
| `core/cli/stats.py` | 780 | NO |
| `_shared/storage/ops.py` | 777 | NO |
| `backend/ml_client/ml_client.py` | **770** | **C2b** |
| `core/install/install_hooks_lib.py` | **662** | **C5** |
| `backend/graph/graph_api.py` | **641** | **C4** |
| `backend/predictive_coding/predictive_coding.py` | **641** | **C6** |

ADR-0066 scope = 7 named files (bold). The other ~18 are pre-existing/grown debt, **explicitly OUT** (Scope OUT §).

---

## Per-car design

**Seam law:** each car edits ONE package dir + its mirrored test dir + (its own) `pyproject.toml`/baseline lines.
Backend cars (C1/C2/C6) and core cars (C3/C4/C5) are in disjoint layers → worktree-parallel-safe. The only shared
files are `pyproject.toml` (per-file-ignores + baseline) and `yadgar/__init__.py`/`server.json` (version) — the
integration merge resolves those; agents touch ONLY their own baseline lines to minimize collision (see Risks).

Split method (all cars, per ADR-0066 decision + I34 forward-only): break the oversized `.py` into cohesive
≤500-LOC sibling modules **inside the existing package dir**; the package `__init__.py` re-exports the public API so
external importers are byte-unaffected; **no new shim files** (I34 line 578 — the split is internal, importers use
the package). Delete stale/live per-file-ignore + complexity-baseline entries as the file drops under cap. Move any
mirrored test file into the pkg-mirrored tests subdir. Run `lint-imports` (I34 line 605) + full unit suite green.

### Car C1 — `backend/embed_service/` split (biggest, backend)
- **What:** split `embed_service.py` **1580 LOC** → cohesive modules (candidate seams: FastAPI app/routes,
  the rerank semaphore + `/rerank` handler, `/embed` + model-load/idle-evict lifecycle, health/metrics wiring).
  Target each ≤500. `embed_service_metrics.py` (503) keeps its isolated `CollectorRegistry` (ADR-0066 explicit);
  trim to <500 opportunistically or leave (503 is 3 over soft — advisory only, not blocking).
- **Files:** `yadgar/backend/embed_service/*` (+ new split modules + `__init__.py` re-exports).
- **Seam:** backend layer, embed_service pkg only. Disjoint from all core cars.
- **Tests:** `yadgar/tests/backend/` embed/rerank tests; `test_ml_client_rerank_gate.py` touches the rerank
  contract — run it. Add characterization: `/embed` + `/rerank` response parity pre/post split.
- **Version:** BACKEND bump. Delete any baseline entry for the file.

### Car C2 — `backend/cache/` + `backend/ml_client/` splits (backend)
- **Revisit-trigger CLEARED:** ADR-0066 said "sequence C2 after the backend-caching train (task #3)". That train
  SHIPPED; `cache.py` last touched by T2 #182 (`git log -- yadgar/backend/cache/`). Safe to split.
- **What:** `cache.py` **980** → split the two cache shapes it documents (`LRUCache` count-capped vs the
  byte-budget cache + registry/snapshot) into siblings; `ml_client.py` **770** → split (Local vs Remote client,
  the rerank/embed Protocol impls, circuit-breaker). Both ≤500.
- **Files:** `yadgar/backend/cache/*`, `yadgar/backend/ml_client/*`.
- **Seam:** backend; two sibling pkgs, both disjoint from C1 (different dirs) → can be its own worktree.
- **Tests:** `test_backend_cache_car1_ce_dedup.py`, `test_backend_cache_car4_graph.py`, `test_ml_client_rerank_gate.py`.
  CE-cache `_ckpt` split-brain (T4 Car0 #188) lives near here — assert cache key/ckpt behavior unchanged.
- **Version:** BACKEND bump.

### Car C3 — `core/daemon/daemon.py` split (core)
- **What:** `daemon.py` **948** → split the supervisor. The pkg ALREADY holds `daemons.py`(239),
  `drain.py`(114), `sd_notify.py`(66), `system_metrics.py`(266) — so the promotion+absorption T2 did; this car
  splits the remaining 948-LOC supervisor into cohesive units (lifecycle/start-stop, backend spawn+health,
  vacuum-swap orchestration, signal handling). Each ≤500.
- **Files:** `yadgar/core/daemon/*`.
- **Seam:** core layer, daemon pkg. Disjoint from all backend cars + core viz/install.
- **Tests:** `test_daemon_module.py`, `test_daemon_obs_gauges.py`.
- **Version:** CORE bump. (No ruff entry to remove — `pyproject.toml:121` is the DIFFERENT `cli/daemon.py`.)

### Car C4 — `backend/graph/graph_api.py` split + stale-grandfather delete (backend + pyproject)
- **What:** `graph_api.py` **641** (moved to BACKEND by T2 Car E3) → split graph-JSON assembly from
  layout-attach helpers, ≤500. **Delete the STALE `pyproject.toml:128` entry** (`core/graph/graph_api.py` — path
  gone). Viz cluster (`core/viz/viz_meta|viz_server|viz_daemon_health`) is already <500 → **no split needed**;
  the C4-viz half of ADR-0066 is effectively DONE by T2.
- **Files:** `yadgar/backend/graph/*`, `pyproject.toml` (line 128 delete).
- **Seam:** backend graph pkg (disjoint from C1/C2 embed/cache/ml pkgs). Note: pyproject line-128 delete is a
  1-line shared-file edit — batch into integration or this car's baseline-only diff.
- **Tests:** `test_graph_api.py`, `test_graph_api_layout_attach.py`, `test_graph_layout.py`.
- **Version:** BACKEND bump.

### Car C5 — `core/install/install_hooks_lib.py` split (core)
- **What:** `install_hooks_lib.py` **662** (grew from ADR's 463; C901 grandfathered `pyproject.toml:129`) →
  split hook-file generation from settings.json patching from the assets/templates writer, ≤500. **Remove the
  live `pyproject.toml:129` C901 entry** once cyclomatic drops under 15.
- **Files:** `yadgar/core/install/*`, `pyproject.toml` (line 129).
- **Seam:** core install pkg. Disjoint from daemon/viz/backend.
- **Tests:** install-hook tests under `yadgar/tests/core/`; smoke `install_hooks` MCP tool.
- **Version:** CORE bump.

### Car C6 — `backend/predictive_coding/` split (OPTIONAL, backend)
- **What:** `predictive_coding.py` **641** (C901 grandfathered `pyproject.toml:133`) → split if the audit
  confirms cohesive seams; ADR-0066 marks C6 optional. Remove `pyproject.toml:133` on success.
- **Files:** `yadgar/backend/predictive_coding/*`, `pyproject.toml:133`.
- **Seam:** backend predictive_coding pkg.
- **Tests:** `test_predictive_coding.py`, `test_predictive_coding_cache.py`.
- **Version:** BACKEND bump. **Q4: include or defer C6.**

### Car SHIM — forward-only shim removal (cleanup, LAST car; cross-layer)
- **What:** delete the PEP-562 back-compat shims for THIS train's targets and migrate their importers to the
  package path (I34 forward-only, line 578: "do NOT keep re-export shims"). Minimum set: the shims for the split
  targets. **Q3 decides** whether to also clear the other ~20 T2 shims (`_shared/config*`, `models`, `protocols`,
  `metrics`, `tracing`, `wiki_meta`, etc.) in this train or leave to a dedicated forward-only cleanup train.
- **Files:** shim files (delete) + every importer using the old path (codemod `from yadgar.X import` →
  `from yadgar.<layer>.<pkg>.X import`). ADR-0066 rationale: `embed_service` had ~110 importers — measure current
  churn with `grep -rl` before committing.
- **Seam:** cross-layer, high churn → runs LAST (after all splits land) on the integration branch, not parallel.
- **Tests:** full suite (import surface); `python -c "import yadgar._shared; import yadgar.core; import yadgar.backend"`.
- **Version:** both bumps (touches both layers). **This car is the I34-compliance payoff of the train.**

---

## Acceptance criteria

- [unit] Each split target file ≤500 LOC (soft) — `python scripts/check_complexity.py --all-files` shows the
  target no longer in the >500 set; embed_service also drops under the 1000 HARD cap. [C1–C6]
- [unit] `lint-imports` = 4 kept / 0 broken, no new waivers (I34 line 603). [all cars]
- [unit] Every removed per-file-ignore / baseline entry: the file passes the cap without it; stale `pyproject.toml:128`
  deleted (path gone). [C4/C5/C6]
- [unit] Public-API parity: package `__init__` re-exports every previously-importable symbol (import-surface test
  per split pkg). [C1–C6]
- [unit] Full unit suite green across all 5 CI shards, 0 skips/warnings (test-suite-hardening #179 discipline). [all]
- [e2e] `/embed` + `/rerank` response parity pre/post C1 (characterization). [C1]
- [e2e] recall + restore + daemon-supervisor smoke unchanged (behavior-neutral guarantee). [C1/C3]
- [e2e] After SHIM car: fresh `import yadgar.*` clean; MCP server boots; 0 remaining "Back-compat shim" files for
  train targets (`grep -rl "Back-compat shim"`). [SHIM]
- [unit] Version consistency test green (`test_v5_46_12` canonical + `sync_version.py` propagation to
  `flake.nix` + `docker-compose.yml`). [integration]

## Test plan

1. **Per car, red→green:** splitting is a mechanical refactor (I34 line 555 rip-and-replace), so the discipline is
   **characterization parity**, not new TDD tests: pin current public-symbol set + a behavior smoke BEFORE the
   split (red = import error / behavior drift), split, green. Add the import-surface test as the failing test first.
2. **Blast-radius discipline (memory 531809, obs-train #159):** a codebase-wide structural change surfaced ~11
   serial bugs because pushes preceded a COMPLETE local full-suite run. **Rule: run the FULL unit suite (all 5
   shards, `-n auto`, `--timeout` thread-method) + e2e to 0 failures LOCALLY before the first push per car.** A
   partial/killed run is NOT a pass.
3. **Integration:** after each car merges to `feat/vX.Y`, run `pytest yadgar/tests/ -x --timeout=60 -q`
   (claude-workflow §2) + `lint-imports`.
4. **Pre-PR:** full 3-pass audit (security + quality + cavecrew) on the WHOLE `feat/vX.Y..master` diff
   (claude-workflow §165); diffs computed vs feature-branch tip (avoid stale-diff audits, claude-workflow line 193).

## Risks (blast radius)

- **Reorg-sweep hazard (memory 531809 — cited):** structural codebase-wide edits have BROAD blast radius that
  surfaces serially in CI if you push before a complete local suite. The SHIM car (cross-layer importer codemod) is
  the highest-risk single car here — same failure mode. Mitigation: SHIM runs LAST, local full-suite-green gate,
  codemod verified by import test before commit.
- **Targets grew/moved since ADR (2026-07-08):** ADR-0066 LOC are stale (`embed_service` 1194→1580 via Ettin,
  `install_hooks_lib` 463→662, `graph_api` moved core→backend). ADR revisit_trigger: "re-verify file hasn't been
  refactored meanwhile" — this plan did (table is live `wc -l`). Auditor must re-verify at build time too; T4/deps
  trains are still landing.
- **`pyproject.toml` merge collisions:** 3+ parallel cars each edit per-file-ignores/baseline. Mitigation: each
  agent touches ONLY its own file's line; integration merge resolves; regenerate baseline once at integration
  (`check_complexity.py --update-baseline`) rather than per-car to avoid churn conflicts.
- **git-blame breakage across splits** (ADR-0066 consequence) — accepted; `git log --follow` mitigates.
- **Split introduces a circular import** inside a pkg (the `_shared` leaf cluster is mutually referential per the
  folder-split plan) — less likely intra-pkg but possible; mitigation: `python -c "import <pkg>"` per car.
- **C6 optional** — if predictive_coding has no clean seam, forcing a split scatters complexity (the audit-agent's
  original LEAVE-all worry). Defer rather than force.

## Scope

**IN:** internal I13 splits of the 7 ADR-0066 named files (C1 embed_service, C2 cache+ml_client, C3 daemon, C4
graph_api, C5 install_hooks_lib, C6 predictive_coding optional); delete stale + live grandfather/baseline entries
for those; forward-only shim removal for train targets (min set; Q3 for the rest); import-linter green.

**OUT:**
- The ~18 other >500-LOC files (project.py 2769, http.py 2390, wiki/store.py 2303, config_yaml.py 2129,
  wiki.py 1523, storage/memory.py 1377, migrations.py 1367, vacuum 1280, metrics.py 1202, storage/wiki.py 1119,
  config.py 1053, log_config.py 1002, admin_exec/invariants 889, cls.py 848, audit.py 823, tracing.py 787,
  stats.py 780, storage/ops.py 777) — pre-existing/grown debt, NOT ADR-0066 targets. A separate standardization
  round if wanted.
- PR-B packaging (auth_middleware/backup) — already promoted by T2; only Q1 (placement) is open.
- `_shared/` loose files (ADR-0066: "single-file by design, out of scope") — but note T2 already packaged them
  with shims; only the shim-removal (Q3) touches `_shared`.
- Function-level decomposition beyond what a file-split naturally yields (no forced cyclomatic surgery).
- Any behavior change, new feature, new endpoint.

## Open questions (need user decision)

- **Q1 — PR B placement divergence:** ADR-0066 said `auth_middleware → core/server/`, `backup → core/export/`.
  T2 instead promoted them to their OWN dirs `core/auth_middleware/` + `core/backup/` (both <500, done). Accept the
  T2 placement (recommend: yes — no benefit to re-moving) or honor ADR's original target dirs? If accept → PR B is
  fully DONE, drop from train. NOTE: `core/export/` (duckdb_exporter/schema, 848 LOC) is a SEPARATE unrelated
  subsystem — NOT the ADR-0066 backup destination; `backup.py` is in `core/backup/backup.py` (304 LOC), verified.
- **Q2 — C1 embed_service seam count:** 1580 LOC needs 3–4 sibling modules. Approve a splitting seam map before
  build, or let the C1 build-agent propose (audited per-car)?
- **Q3 — SHIM removal scope:** remove only the ~7 shims for train targets, OR clear all 27 T2 PEP-562 shims
  (full I34 forward-only compliance) in this train's SHIM car? Full clear = large importer codemod (embed_service
  alone ~110 sites) but retires the whole re-export-shim debt I34 forbids. Recommend: **train-targets only**; a
  dedicated "forward-only shim purge" train for the rest (keeps this train's blast radius bounded).
- **Q4 — C6 include or defer:** ADR marks predictive_coding optional. Include (641 LOC, has grandfather entry) or
  defer to keep the train tighter?
- **Q5 — version math** (below): confirm the bump policy.

## Version math

Two independent release units (I34 note: "versions belong to the 2 release units", ADR-0084 rejected per-module
versioning). One PR → **one bump per unit that the PR touched**, regardless of car count:

- **Core cars:** C3 (daemon), C5 (install), + SHIM core half → **CORE 5.132.0 → 5.133.0** (one minor bump).
- **Backend cars:** C1 (embed_service), C2 (cache/ml_client), C4 (graph_api), C6 (predictive_coding), + SHIM
  backend half → **BACKEND 5.43.0 → 5.44.0** (one minor bump).
- Bump both in the final integration commit; `sync_version.py` propagates BACKEND to `flake.nix` +
  `docker-compose.yml`; update `server.json` (`version` + `backend_version`) + the canonical
  `test_v5_46_12` expected value. Pure refactor (behavior-neutral) → **minor**, not major; no patch since it's a
  deliberate multi-car train, not a hotfix.
- If Q3 = full-shim-purge or Q1 = re-move PR-B files, no additional version delta (still one bump per unit).

---

*Sources: `wiki:yadgar-adr-log` ADR-0066 (verbatim decision/consequences/revisit_trigger) + ADR-0084 (lone-files
law) + ADR-0062 (I34); `docs/ARCHITECTURE_INVARIANTS.md:78,80,546-622`; `docs/plans/archive/layer-boundary-train-2026-07-09.md`
(T2 car table, task-#18 pointer line 51/107); `docs/plans/archive/core-backend-folder-split-2026-07-06.md`
(shim precedent); `docs/claude-workflow.md:34-119,163-202` (train workflow); `pyproject.toml:115-156`
(per-file-ignores); live `wc -l` census 2026-07-13; memory 531809 (blast-radius discipline).*

---

## AUDIT (2026-07-13)

**Status: AUDITED — needs rework.** The core approach (internal split inside the existing
package dirs, forward-only, shim-removal-last, worktree-parallel cars) is **sound and matches
I34**. Rework is edits to **two sections** (Version math + the `.complexity-allowlist.json`
omission that widens C1 scope/acceptance), **not a redesign**. 3 WRONG claims, 2 STALE, rest
VERIFIED. Audited by a read-only pass over the live tree (all `wc -l` / grep re-run 2026-07-13).

### Verification table

| # | Load-bearing claim | Plan says | Live evidence | Verdict |
|---|---|---|---|---|
| 1 | Baseline tip | `471eba13` (T4 #191) | `git rev-parse HEAD` → `bb515e4b`; `471eba13` is **7 commits back** | **STALE** (versions 5.132.0/5.43.0 still correct — `server.json:10-11`, `yadgar/__init__.py:21`) |
| 2 | embed_service.py LOC | 1580 | `wc -l` = **1580** | VERIFIED |
| 3 | embed_service_metrics.py | 503 | `wc -l` = **503** | VERIFIED |
| 4 | cache.py LOC | 980 | `wc -l` = **980** | VERIFIED |
| 5 | ml_client.py LOC | 770 | `wc -l` = **770** | VERIFIED |
| 6 | core/daemon/daemon.py LOC | 948 | `wc -l` = **948** | VERIFIED |
| 7 | backend/graph/graph_api.py LOC | 641 | `wc -l` = **641** | VERIFIED |
| 8 | install_hooks_lib.py LOC | 662 | `wc -l` = **662** | VERIFIED |
| 9 | predictive_coding.py LOC | 641 | `wc -l` = **641** | VERIFIED |
| 10 | auth_middleware / backup LOC | 238 / 304 | `wc -l` = **238 / 304**; both in own dirs `core/auth_middleware/`, `core/backup/` | VERIFIED |
| 11 | daemon pkg siblings absorbed by T2 | daemons 239 / drain 114 / sd_notify 66 / system_metrics 266 | `wc -l` = **239 / 114 / 66 / 266** | VERIFIED (exact) |
| 12 | 27 PEP-562 "Back-compat shim" files | 27 | `grep -rl "Back-compat shim" yadgar/` = **27** | VERIFIED |
| 13 | I34 (ADR-0062) forbids re-export shims | line 578 | `ARCHITECTURE_INVARIANTS.md:577-579` "Do **not** keep … re-export shims"; §I34 header line 546; ADR-0062 mapping confirmed line 546/994 | VERIFIED |
| 14 | `pyproject.toml:128` stale grandfather (`core/graph/graph_api.py`, path gone) | delete in Car 4 | `pyproject.toml:128` = `"yadgar/core/graph/graph_api.py" = ["C901"]`; `ls yadgar/core/graph` → **No such directory**. Entry is dead. | VERIFIED |
| 15 | `pyproject.toml:129` = live C5 install grandfather | C5 removes it | `pyproject.toml:129` = `"yadgar/core/install/install_hooks_lib.py" = ["C901"]` (live) | VERIFIED |
| 16 | `pyproject.toml:133` = live C6 predictive grandfather | C6 removes it | `pyproject.toml:133` = `"yadgar/backend/predictive_coding/predictive_coding.py" = ["C901"]` (live) | VERIFIED |
| 17 | `pyproject.toml:121` = DIFFERENT `cli/daemon.py` (277 LOC), don't conflate C3 | footnote 2 | `pyproject.toml:121` = `"yadgar/core/cli/daemon.py" = ["C901"]`; distinct from `core/daemon/daemon.py`. C3 has **no** per-file-ignore to remove — correct. | VERIFIED |
| 18 | Only C5/C6 are per-file-ignores; C1/C2a/C2b/C3 are NOT ruff C901, tracked in `.complexity-baseline.json` (footnote 1) | debt "lives in the complexity baseline instead" | embed_service/cache/ml_client/core-daemon are **NOT** in `[per-file-ignores]` — correct. BUT: **embed_service's HARD >1000 file-LOC waiver lives in `.complexity-allowlist.json:25`, a file the plan NEVER names** — not `.complexity-baseline.json`. `.complexity-baseline.json` tracks SOFT violations only (`::__file__` LOC + per-function keys). | **WRONG** (see Correction A) |
| 19 | embed_service SHIM importer count | "~110 importers" (ADR-era historical; "measure with grep before committing") | Live: **11** non-test module importers, **47** total refs, **0** old-`yadgar.embed_service`-string sites. Only embed_service shim is `embed_service_metrics.py`; **no** top-level `embed_service.py` shim exists. | VERIFIED-with-context (plan already flagged it as historical + measure-first; SHIM blast radius far below 110 → strengthens Q3=train-targets-only) |
| 20 | ADR-0084 lone-files law codified by T2 #182 | promotion done | ADR-0062/0084/0066 all resolve in docs + wiki; auth/backup/daemon-pkg confirm promotion | VERIFIED |
| 21 | Version math: core 5.132→5.133, backend 5.43→5.44 | lines 273-280 | `origin/feat/obs-quickwins-train` (#195) ships **5.133.0/5.44.0**, is **NOT merged** into master, and is ahead. Two trains cannot both claim 5.133/5.44. | **WRONG** (see Correction B) |
| 22 | cache.py debt cleared by the split | C2 "split … both ≤500" | `.complexity-allowlist.json:440` = **PLR0913 9-param `__init__` waiver** on cache.py — orthogonal to LOC. Splitting <500 does **not** clear it; Scope OUT ("no forced param surgery") means it intentionally **survives**. | **WRONG (implied)** (see Correction C) |
| 23 | Seams disjoint → worktree-parallel; only shared files = `pyproject.toml` + version files | lines 98-100, 224 | Layers are disjoint (backend vs core). BUT `.complexity-allowlist.json` is a **THIRD shared file**: C1 edits line 25, C2 edits line 440 → collision the plan's mitigation (line 224 names only pyproject+baseline) does not cover. | STALE/incomplete (see Correction D) |
| 24 | Invariant line cites 78 (fn caps) / 80 (file caps) | header line 6 | `ARCHITECTURE_INVARIANTS.md:78` = function caps; `:80` = file caps (≤1000 hard/≤500 soft). | VERIFIED |

### Corrections (fold into the plan before build)

**A — Footnote 1 & Acceptance line 188 (C1 scope gap).** The embed_service HARD file-LOC (>1000)
waiver is `.complexity-allowlist.json:25` (stale rationale: ends at "1194 → next structural
addition should split"; baseline `::__file__` says 1423; live 1580 — drifted twice). Removing
it is a **C1 deliverable** — the same dead-waiver debt-class as the stale `pyproject.toml:128`
you already delete in C4. Acceptance criterion line 188 ("every removed per-file-ignore / baseline
entry") **omits the allowlist** → C1 could pass acceptance while leaving a dead HARD waiver. Fix:
add `.complexity-allowlist.json:25` removal to Car C1's deliverables **and** to line 188.

**B — Version math (BLOCKER, lines 273-280).** Base on **post-#195** master. #195
(`feat/obs-quickwins-train`) already occupies 5.133.0/5.44.0 and is unmerged-but-ahead. After it
lands, this train's base = 5.133.0/5.44.0 → **targets CORE 5.133.0 → 5.134.0, BACKEND 5.44.0 →
5.45.0**. The audit mission flagged this; the plan body contradicts the flag by hardcoding the
same 5.133/5.44. Also update `test_v5_46_12_backend_version_canonical.py` expected value +
`sync_version.py` propagation (server.json + flake.nix + docker-compose.yml — 3 sites, all verified
present). Add an explicit sequencing precondition: **"do not start version bump until #195 has
merged; re-read master `server.json` at integration."**

**C — cache param waiver clarity (Scope + C2).** State in C2 that `cache.py`'s
`.complexity-allowlist.json:440` PLR0913 9-param `__init__` waiver is **out of scope and survives
the split** (it's a param-count waiver, not LOC; Scope OUT already forbids forced param surgery).
Without this note the plan implies C2 retires cache's debt — it retires only the LOC-soft part.

**D — Seam risk (line 224).** Add `.complexity-allowlist.json` to the list of shared files that
collide across parallel cars (currently only `pyproject.toml` + baseline named). C1 (line 25) and
C2 (line 440) both edit it. Same mitigation applies (each agent touches only its own line;
integration resolves) but it must be **named**, and "agents touch ONLY their own pyproject line"
(line 100) should read "own pyproject **and** allowlist line."

### Assessed but OK (no change needed)

- **Cars disjoint (mission 5):** backend (C1/C2/C4/C6) vs core (C3/C5) layers are import-disjoint;
  `backend/__init__.py` (6 LOC) + `core/__init__.py` (9 LOC) are thin, no per-pkg re-export churn
  there. Worktree-parallel viable modulo the 3 shared metadata files (pyproject/baseline/allowlist)
  the integration merge resolves. SHIM-car-runs-last is correct.
- **Acceptance criteria (mission 6):** testable — LOC via `check_complexity.py --all-files`,
  `lint-imports` 4-kept/0-broken (I34 enforcement confirmed `pyproject [tool.importlinter]`),
  import-surface parity test, version-consistency test all exist. **Only gap = the allowlist
  omission in Correction A.**
- **Scope IN/OUT:** clean, except cache-param clarity (Correction C). The ~18 OUT files match the
  live >500 census. PR-B correctly identified as done-modulo-Q1.
- **Blast-radius mitigation (mission 5, memory 531809):** SHIM-last + full-local-suite-green gate
  is the right discipline; the actual embed_service churn (11 sites, not 110) makes it **lower**
  risk than the plan fears — the mitigation is if anything over-provisioned, which is fine.

### New user-decision item (add to Open Questions)

- **Q6 — allowlist strip:** does C1 also strip `.complexity-allowlist.json:25` (embed_service HARD
  file-LOC waiver)? **Recommend: YES** — it's dead debt once the file drops <1000, same as the
  stale `:128` delete. cache's `:440` param waiver is a **no-op** for this train (survives; out of
  scope per Correction C).

### Sequencing verdict

**This train is blocked on #195.** Do not finalize version numbers or start the integration bump
until `feat/obs-quickwins-train` merges to master. Splitting work (C1–C6) can proceed in parallel
worktrees off current master, but the version-math section and `test_v5_46_12` expected value must
be recomputed against post-#195 master (5.133.0/5.44.0 → 5.134.0/5.45.0).
