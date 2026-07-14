# Complexity Policy — yadgar

**Status:** Active (v5.55.0+). Enforced by I13 (pre-commit check-complexity) and I30 (pre-commit
check-complexity-allowlist).

---

## Scope — production code only

I13 (`check-complexity`) enforces caps on **production code only**:

- `yadgar/` — production package, excluding `yadgar/tests/`
- Everything else is **exempt**: `yadgar/tests/` (test suite) and `scripts/` (one-off tooling)

This matches the scope of I30 (`check-complexity-allowlist`), which also scans
`yadgar/` only and skips test files. The two invariants agree by design.

Test and script files are expected to contain longer, more complex functions and
are governed by review rather than automated caps.

---

## Caps

Caps live in `.complexity-config.json` at the repo root. Changing a cap requires a reviewed PR diff —
the config file is version-controlled precisely so cap changes are visible and intentional.

| Metric | Soft cap | Hard cap |
|---|---|---|
| Cyclomatic complexity (per function) | 10 | 15 |
| Nesting depth (per function) | — | 4 |
| Parameter count (per function, non-test) | 5 | 8 |
| Function LOC (non-test) | 80 | 150 |
| File LOC | 500 | 1000 |
| Class inheritance depth | — | 3 |

**Soft cap** violations warn to stderr but do not block commits.
**Hard cap** violations block commits **unless** the `(file, function, metric)` triple is in
`.complexity-allowlist.json` with a non-empty rationale.

### Why these numbers?

- **Cyclomatic 15 / nesting 4:** McCabe's classic "dangerous complexity" thresholds. Above these,
  defect density and test escape rates rise sharply. Covered by `ruff C901` (cyclomatic) and the
  custom hook (nesting).
- **Function LOC 150:** Beyond 150 lines a function rarely fits in working memory in one pass.
- **Params 8:** Beyond 8 parameters, callers must look at the signature to understand call sites;
  the function usually wants a config dataclass.
- **File LOC 1000:** A signal the file has grown beyond a single cohesive concept; a Tier-3 split
  candidate.
- **Class depth 3:** Deep inheritance hierarchies are hard to reason about and change safely.

---

## Enforcement model

### SOFT violations

1. The baseline ratchet (`complexity-baseline.json`) records current metrics for functions that
   already exceed a soft cap.
2. The pre-commit hook warns on any soft violation **only if** it's new or worsened (current metric
   > baseline metric).  Pre-existing soft violations at or below their baseline value are silently
   allowed.
3. To pay down a soft violation: refactor the function under cap, then regenerate the baseline
   (`python scripts/check_complexity.py --update-baseline`).

### HARD violations

Hard violations **must** end in exactly one of:

1. **Refactored under cap** — remove the allowlist entry; the I30 stale check will fail if you
   forget.
2. **Allowlisted with permanent rationale** — add an entry to `.complexity-allowlist.json`
   explaining *why* this complexity is essential (see below).

The baseline ratchet does **not** apply to HARD violations. Only the allowlist can gate them.

---

## How to change a cap

1. Edit `.complexity-config.json`.
2. If lowering a cap (tightening), run `python scripts/check_complexity_allowlist.py` locally
   to see how many new HARD violations appear.  Add allowlist entries or refactor as needed.
3. If raising a cap (loosening), ensure you have a justified reason in the PR description.
4. Open a PR. The diff on `.complexity-config.json` is the change record.

---

## How to add an allowlist entry

Add an object to `.complexity-allowlist.json`:

```json
{
  "path": "yadgar/causal_discovery/pc.py",
  "function": "pc_algorithm",
  "lineno": 105,
  "metrics": {"cyclomatic": 53, "nesting": 6, "fn_loc": 175},
  "rationale": "Textbook PC constraint-based causal-discovery loop; the branching is the algorithm. Extraction scatters it and harms readability.",
  "added": "2026-06-13",
  "added_by": "max"
}
```

**Required fields:**
- `path` — repo-relative path (e.g. `yadgar/foo.py`)
- `function` — exact function/class name or `<file>` for file-level violations
- `lineno` — line number (for disambiguation when multiple same-named functions exist)
- `metrics` — dict of `{metric_name: measured_value}` for each HARD metric being allowlisted
- `rationale` — ≥ 40 characters explaining why this complexity is essential
- `added` — date added (ISO 8601)
- `added_by` — who added it

**Metric names:** `cyclomatic`, `fn_loc`, `params`, `nesting`, `file_loc`, `class_depth`

After adding the entry, `python scripts/check_complexity_allowlist.py` must return exit 0.

### I30 stale detection

When you refactor a function under cap, **remove** its allowlist entry. The I30 `check-complexity-allowlist`
hook runs on every affected commit and will fail with a STALE error if the entry remains after the
function is under cap. This is intentional — the allowlist must shrink as the debt pays down.

---

## Refactor-vs-justify decision guide

Ask these questions in order:

1. **Is this complexity accidental or essential?**
   - *Accidental*: deep nesting from missing guard clauses, param explosion from missing dataclass,
     long function from missing helpers that are independently testable.
     → **Refactor**.
   - *Essential*: the branching *is* the spec (a constraint-satisfaction algorithm, a dispatch
     table, a migration runner), or splitting would scatter the invariant across files.
     → **Allowlist**.

2. **Can it be split without topology breakage?**
   - If the function crosses an async/thread/queue boundary, decomposition requires a topology
     proof (I5). Is the proof possible and worth the risk?
     → If not: **Allowlist** with that rationale.
   - If the function is pure computation (no async, no shared state): split it.
     → **Refactor**.

3. **Will splitting harm readability?**
   - If the logic is a linear pipeline of independent steps, helpers improve readability.
   - If it's a tightly coupled state machine where every branch touches shared local state,
     helpers create invisible coupling.
     → Tight coupling: **Allowlist** with that rationale.

4. **Is there a benchmark risk?**
   - Hot paths (`recall`, `memorize`, `insert_memory`, `_memify_prune`) need a before/after
     benchmark run (>10% p50 regression = revert). Is the refactor safe?
     → If unsure: defer to a wave where the benchmark tooling can be run.

---

## Migration plan (v5.55 campaign)

The v5.55 campaign pays down the debt in waves:

- **v5.55.0** (this release): governance tooling + allowlist seeded with 84 provisional entries.
  Gate live and green. No behavior change.
- **v5.55.1+**: Tier-1 (cyclomatic) → Tier-2 (fn_loc) → Tier-3 (file splits). Each wave removes
  entries from the allowlist or adds permanent rationale. The allowlist only ever shrinks.

See `docs/PLAN_V5_55_COMPLEXITY_PAYDOWN.md` for the full wave plan.

---

## Per-path overrides

Some files have legitimately-special constraints (e.g. an append-only migration ledger that grows
by design). These get a `per_path_overrides` entry in `.complexity-config.json` rather than an
allowlist entry, because the override applies permanently at a file-structural level.

```json
"per_path_overrides": {
  "yadgar/storage/migrations.py": {"file_loc": {"hard": 1500}}
}
```

Per-path overrides must be justified in the PR that introduces them.
