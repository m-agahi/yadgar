# PLAN v5.55 — Complexity policy + debt paydown (unified umbrella)

Status: **PLANNED 2026-06-13.** The single campaign for I13 complexity debt. Folds in the
former **v5.90** grandfathered-cleanup plan (now superseded — see
`docs/PLAN_V5_90.md`). Two problems, one campaign:

1. **The hole** — `.complexity-baseline.json` silently suppresses HARD violations too, so
   a "HARD cap" is advisory: anything paid down can silently regrow. The pre-commit hook
   only checks *changed* files, so grandfathered violations are invisible until a file is
   next edited (this is how `storage/client.py` ambushed v5.54.5).
2. **The debt** — a large grandfathered baseline. Two snapshots:
   - v5.90 (2026-06-09, all files incl. tests/scripts): **11,904** baseline entries —
     cyclo>15: 259, fn-loc>80: 766, file-loc>500: 76.
   - v5.54.5 audit (2026-06-13, `--baseline /dev/null`, **production only**):
     **117 HARD** violations across 51 files (client.py's 3 fixed in v5.54.5 → 114 left).
   Re-scan at the start of each wave; treat both as stale snapshots, not gospel.

Fix BOTH: a **configurable, gated, documented** policy first (Part 1), then pay the debt
down against it (Part 2) — refactoring accidental complexity, **explicitly justifying**
the essential complexity that should be left alone.

---

## Part 1 — Policy + tooling (v5.55.0, lands BEFORE any refactor)

### 1a. Configurable caps — `.complexity-config.json`
Caps move OUT of `scripts/check_complexity.py` into a version-controlled config the hook
and checker both read. Changing a cap = a reviewed PR diff.
```json
{
  "caps": {
    "cyclomatic":  {"soft": 10,  "hard": 15},
    "nesting":     {"soft": null, "hard": 4},
    "params":      {"soft": 5,    "hard": 8},
    "fn_loc":      {"soft": 80,   "hard": 150},
    "file_loc":    {"soft": 500,  "hard": 1000},
    "class_depth": {"soft": null, "hard": 3}
  },
  "per_path_overrides": {
    "yadgar/storage/migrations.py": {"file_loc": {"hard": 1500}}
  }
}
```
`per_path_overrides` covers legitimately-special files (append-only migration ledger)
with the override reviewable in the diff.

### 1b. Gated HARD allowlist — `.complexity-allowlist.json`
Replaces silent HARD baselining. A HARD violation passes ONLY if its
`(path, function, metric)` has an allowlist entry **with a non-empty rationale**.
```json
[
  {
    "path": "yadgar/causal_discovery/pc.py",
    "function": "pc_algorithm",
    "metrics": {"cyclomatic": 53, "nesting": 6, "fn_loc": 175},
    "rationale": "Textbook PC constraint-based causal-discovery loop; the branching is the algorithm. Extraction scatters it and harms readability.",
    "added": "2026-06-13", "added_by": "max"
  }
]
```

### 1c. Checker behavior
- **SOFT** → WARN; keep ratcheting via `.complexity-baseline.json` (soft debt pays down gradually).
- **HARD** → ERROR (block) UNLESS allowlisted-with-rationale → INFO/pass.
- **No HARD in the baseline.** Going forward the baseline is SOFT-only; every current HARD
  baseline entry is triaged during the waves into refactored-under-cap OR allowlisted.

### 1d. New invariant I30 — "complexity-cap integrity" (own pre-commit hook + check_invariants)
Mirrors the I28 allowlist-audit / I29 dead-capability pattern (every exception live + justified):
- **Gate:** no HARD violation exists outside the allowlist.
- **Rationale required:** every allowlist entry has a non-empty rationale (≥ ~40 chars).
- **No stale entries:** every entry still maps to a real HARD violation (refactor under cap → entry removed, else fail).
- **Drift detection:** recorded `metrics` match current within tolerance; growth past recorded value → re-review.

### 1e. Documentation
- `docs/COMPLEXITY_POLICY.md` — caps + why, how to change a cap (config + review), how to
  add an allowlist entry (rationale + review), the **refactor-vs-justify decision guide**,
  migration plan. Rule of thumb: refactor *accidental* complexity (deep nesting from
  missing guard clauses, param explosions, god-functions); allowlist *essential*
  complexity (an algorithm whose branching IS the spec).
- `docs/ARCHITECTURE_INVARIANTS.md` + wiki mirror — add I30.

### 1f. Day-one green
Seed every current HARD violation into the allowlist with a provisional rationale
`"pre-existing; scheduled for v5.55 wave N refactor"`. The gate goes live green and only
blocks NEW HARD violations + silent regrowth; it tightens as waves remove entries.

---

## Part 2 — Pay down the debt (waves against the policy)

Each function ends in exactly one terminal state: **refactored under cap** OR
**allowlisted with a permanent rationale**. Tiered by leverage (from v5.90), hot-path
first within tiers.

### Tier 1 — cyclomatic (highest defect-density per LOC)
Worst production offenders (v5.54.5 audit): `_run_check_invariants` 98, `cmd_stats` 68,
`_memify_prune` 56, `recall` 55, `pc_algorithm` 53 (allowlist candidate), `cmd_daemon` 42,
`insert_memory` 40, `_render_project_brief` 35, `checkpoint` 36, `analyze_query` 29,
`_derive_implied_fact_passages` 46, the four `_reranking_*`, `consolidation/cls.py` ×3, etc.
Batch **10–20 per release**, clustered by module. Hot-path functions (`recall`,
`memorize`, `insert_memory`, `_memify_prune`) run the benchmark suite before merge —
>10% p50 regression = revert.

### Tier 2 — function LOC > 80/150
Many auto-resolve when Tier-1 functions split. Re-scan after each Tier-1 release; then a
dedicated sweep (~150/release at the easy end).

### Tier 3 — file LOC > 500/1000 (file splits)
6 production files over the 1000 HARD cap: `server/tools/project.py` (2185),
`server/http.py` (1842), `config_yaml.py` (1430), `server/tools/wiki.py` (1167),
`storage/migrations.py` (1153 — `per_path_override` candidate), `wiki.py` (1076). Plus the
500-SOFT set. Each split: identify cohesive sub-modules, update imports, keep public API
in the package `__init__`, full-suite green. **Splitting hot files also improves test
isolation** (relevant to v5.56). ~10 files/release. Last — needs import-graph care.

### Per-release contract (from v5.90)
1. Plan doc naming exact functions/files + split strategy + regression-test plan.
2. **TDD on the refactored surface** — characterization/golden tests BEFORE refactor (esp.
   thin-coverage hot paths: `recall`, `insert_memory`, `_memify_prune`), refactor, assert green.
3. Baseline/allowlist shrinks by exactly the entries cleared (never grows; mechanical
   line-shift adjustments excepted).
4. No new violations — a new over-cap function is a hard fail (re-split or data-drive).
5. Same ruff + pre-commit gates. Targeted tests only; **never `pytest -n auto`** locally
   (OOM — see v5.54.5 guardrails).

### Tooling (from v5.90)
- `scripts/check_complexity.py` (exists) — extend to read `.complexity-config.json` + honor the allowlist.
- `scripts/baseline_diff.py` (new) — diff two baseline snapshots; flag net-positive additions (catches creep).
- `scripts/list_grandfathered.py` (new) — pretty-print remaining baseline/allowlist by tier + module; scopes each wave.

---

## Sequencing & versioning
- **v5.55.0** — Part 1 (config + allowlist + I30 + docs + day-one-green seeding). No behavior change.
- **v5.55.1 .. N** — refactor waves: Tier 1 (cyclo) → Tier 2 (fn-loc) → Tier 3 (file splits).
  One wave-slice per sub-version; files independent within a wave → fan out
  (worktree-isolated agents). Pause/resume anytime on user direction.
- Stays on the `v5.55.x` track until the grandfathered baseline is HARD-clean + SOFT-paid;
  then normal cadence resumes.

## Cross-campaign note (v5.56)
This campaign **moves and splits a lot of code** (Tier-3 file splits especially). That
shifts line numbers, module boundaries, and import side-effects — which is exactly what
drives the v5.56 xdist test-isolation problems. **v5.56 must re-audit after v5.55 waves
land** (its plan says so). Coordinate: large Tier-3 splits may resolve some v5.56
pollution for free (better module isolation), or shift it. Re-scan, don't assume.

## Risks (from v5.90)
- **Subtle behavior breakage** → snapshot tests BEFORE refactor; any divergence blocks.
- **Lazy baseline update** → `baseline_diff.py` flags net additions.
- **Hot-path perf regression** → benchmark gate on recall/memorize/insert_memory; >10% p50 = revert.
- **Release fatigue** (15+ patch releases) → clear per-wave plan docs; pausable.
- **Forcing essential complexity under cap** → that's what the allowlist is for; justify, don't contort.

## Non-goals
No behavior changes, no public API changes (signature change → minor bump, not patch),
no optimization-driven changes (correctness > performance), no cross-cutting renames.

## References
`.complexity-baseline.json`, `.complexity-config.json` (new), `.complexity-allowlist.json`
(new), `scripts/check_complexity.py`, I13 spec, superseded `docs/PLAN_V5_90.md`.
