# PLAN — v5.90: Grandfathered Complexity Cleanup (umbrella)

**Status:** drafted 2026-06-09. Umbrella tracking plan. Implementation spans multiple patch releases (v5.49.5, v5.49.6, ... and onward).

**Scope:** retire ALL entries in `.complexity-baseline.json` so the pre-commit `check-complexity` hook enforces I13 caps with NO grandfathered exceptions.

**Current surface (snapshot 2026-06-09 on master 311de16):**

- Total baseline entries: **11,904**
- Cyclomatic complexity violations (cyclo > 15): **259**
- Function LOC violations (loc > 80): **766**
- File LOC violations (loc > 500): **76**

---

## 1. Strategy

### Three-tier cleanup, ordered by leverage

| Tier | What | Scale | Why first |
|---|---|---|---|
| **T1: cyclomatic** | 259 functions with cyclo > 15 | small-N, high-leverage | Highest defect-density risk per LOC. Refactor improves readability AND reduces bug surface. |
| **T2: function LOC** | 766 functions with loc > 80 | medium-N | Often correlates with T1. Many T1 fixes auto-resolve T2. |
| **T3: file LOC** | 76 files with loc > 500 | largest impact per fix | Splitting hot files improves test isolation, import speed, and review-ability. Last because needs careful import-graph analysis. |

### Per-batch sizing

- **Each patch release tackles 10–20 cyclo violations**, scoped by module proximity (don't mix unrelated modules in one release).
- Tier-2 + Tier-3 batched into separate releases once Tier-1 is done.
- Pre-commit baseline updated incrementally — never wholesale removed (the ratchet IS the safety net).
- Each release MUST include regression tests for refactored functions.

### Non-goals

- No behavior changes. Pure refactor.
- No API changes (public function signatures preserved unless absolutely required; signature change → minor version bump, not patch).
- No optimization-driven changes (correctness > performance).
- No cross-cutting renames (rename PRs go separately).

---

## 2. Release schedule — Tier 1 (cyclomatic)

Each release is a patch (5.49.x). Scoped per-module to bound blast radius. Order picks by leverage × isolation.

| Release | Functions | Module focus | Notes |
|---|---|---|---|
| **v5.49.5** | `memorize@76` (cyclo=114), `memorize@36` (cyclo=84) | `yadgar/server/tools/memorize.py` | Hot path. Highest defect-density. Single file — easier to test in isolation. Likely splits into pre-write gate + write helpers + post-write hooks. |
| **v5.49.6** | `_run_check_invariants@18` (cyclo=98) | `yadgar/server/tools/admin_invariants.py` | Single function. Split per-check-helper pattern. Test fixture per check. |
| **v5.49.7** | `cmd_stats@15` (cyclo=68) | `yadgar/cli/stats.py` | CLI dispatcher — easier than hot-path. Per-subcommand split. |
| **v5.49.8** | `get_full_graph@{19,22,56,57}` (4 instances, cyclo=56–60) | `yadgar/graph_api.py` | Multiple instances of same function name at different line numbers suggests overload/version drift; investigate + consolidate. |
| **v5.49.9** | `_memify_prune@13` (cyclo=56), `recall@20` (cyclo=55) | `yadgar/curation/prune_passes.py` + `yadgar/server/tools/recall.py` | Pair: write-path + read-path complexity. Different modules but related domain. |
| **v5.49.10** | `pc_algorithm@105` (cyclo=53), `install_hooks_impl@48` (cyclo=49) | `yadgar/causal_discovery/pc.py` + `yadgar/install_hooks_lib.py` | Algorithm + install — both have natural phase splits. |
| **v5.49.11** | `_derive_implied_fact_passages@{285,286}` (cyclo=46×2), `cmd_daemon@8` (cyclo=42) | `yadgar/retrieval/query_analysis.py` + `yadgar/cli/daemon.py` | Retrieval helper + daemon CLI dispatcher. |
| **v5.49.12** | `insert_memory@92` (cyclo=40), `_format_restoration@{319,334,339,345}` (cyclo=37×4) | `yadgar/storage/memory.py` + `yadgar/restoration.py` | Storage + restoration formatter. Formatter has 4 same-name instances — likely template-format dispatch, can split per-template. |
| **v5.49.13–v5.49.20** | 8 batches of ~15 cyclo-violations each | mixed | Remaining 200+ cyclo violations. Per-batch module clustering. |

After v5.49.20: **cyclo backlog cleared**. Bump major to v5.50 (next feature release).

---

## 3. Release schedule — Tier 2 (function LOC > 80)

Coordination with Tier 1: many T2 violations auto-resolve when T1 functions are split. Re-scan baseline after each T1 release.

Once cyclo-clean: dedicated T2 sweep over 4–6 patch releases (~150 functions per release at the easier end). Target: v5.50.0+ patch slots.

---

## 4. Release schedule — Tier 3 (file LOC > 500)

76 files. Examples (top by LOC):
- `yadgar/storage/wiki.py` (~806 LOC)
- `yadgar/retrieval/query_analysis.py` (~498 LOC — borderline)
- `yadgar/storage/blocks.py` (~469 LOC — borderline)

Each file split requires:
- Identify cohesive sub-modules (e.g. `wiki.py` → `wiki/{queries,write,crossref,history}.py`).
- Update all imports.
- Re-run full test suite.
- Verify no public API changes (anything imported externally stays in the package `__init__.py`).

~10 files per release. Target: v5.51.x+ (after T2 clean).

---

## 5. Per-release contract

Every refactor release MUST:

1. **Plan doc** at `docs/PLAN_V5_49_X.md` listing the exact functions/files being refactored, the split strategy, and the regression test plan.
2. **TDD on the refactored surface.** Write tests that EXERCISE the function before refactor (golden output snapshots are fine), refactor, assert tests still pass.
3. **Baseline reduction.** `.complexity-baseline.json` shrinks by exactly the number of entries refactored. Update via `python scripts/check_complexity.py --update-baseline`.
4. **No new entries.** Any new function in the refactor that exceeds caps is a hard fail — re-split or push complexity off to data-driven approaches.
5. **Same ruff + pre-commit gates** as feature releases.

## 6. Acceptance per release

- All Tier-1 violations listed in the release's plan doc removed from baseline.
- Tests pass before AND after.
- No new entries added to baseline (other than mechanical line-shift adjustments).
- CHANGELOG entry per release naming the modules touched.
- Hand off to next release (next plan doc drafted before close).

## 7. Tooling

- `scripts/check_complexity.py` — already exists, enforces I13 caps + maintains baseline.
- Add `scripts/baseline_diff.py` (new, v5.49.5 phase 0): given two baseline snapshots, report functions removed AND functions added. Catches accidental new-violation creep.
- Add `scripts/list_grandfathered.py` (new, v5.49.5 phase 0): pretty-print remaining baseline grouped by tier + module. Used to scope each release.

## 8. Risks

- **Refactor breaks subtle behavior.** Mitigation: snapshot tests on the function's input→output mapping BEFORE refactor. Any divergence = blocker.
- **Baseline ratchet bypassed by lazy update.** Mitigation: `scripts/baseline_diff.py` (item 7) flags net-positive additions.
- **Release fatigue.** 15+ patch releases tackling debt is grind. Mitigation: clear plan docs per release; user can pause anytime + resume.
- **Hot-path performance regression.** Mitigation: any T1 function in `recall`, `memorize`, `insert_memory` runs through the benchmark suite (`benchmarks/longmemeval_*`) before merge. Performance regression > 10% on p50 latency = revert.

## 9. Open questions

- Should T1+T2+T3 ship continuously into v5.49.x (current plan), OR bump to v5.50.x once T1 done? Current plan: stay in 5.49.x until tier 1 cleared, then v5.50 for next feature release.
- Should `scripts/list_grandfathered.py` print a markdown-formatted release-plan stub for the next batch? Defer to v5.49.5 phase 0 decision.

## 10. References

- `.complexity-baseline.json` (current state)
- `scripts/check_complexity.py` (enforcement)
- I13 spec — `docs/architecture.md` § Invariants
- v5.49.0–v5.49.4 release history (CHANGELOG.md) — shows the pre-commit baseline ratchet pattern in action
