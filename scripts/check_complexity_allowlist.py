#!/usr/bin/env python3
"""I30 — Complexity-cap integrity invariant.

Enforces four properties of the HARD complexity allowlist, plus one of the
soft-violation baseline:

  (a) GATE — no HARD violation exists outside the allowlist.
      Every production HARD violation must be covered by a
      .complexity-allowlist.json entry.

  (b) RATIONALE — every allowlist entry has a non-empty rationale
      (>= MIN_RATIONALE_LEN characters, currently 40).

  (c) NO STALE ENTRIES — every allowlist entry still maps to a real
      current HARD violation.  When a function is refactored under cap,
      its allowlist entry must be removed, or this check fails.

  (d) DRIFT — recorded metrics in each allowlist entry must match
      current measured values within tolerance (DRIFT_TOLERANCE = 0.20).
      Growth beyond the recorded value triggers re-review.

  (e) NO DEAD BASELINE ENTRIES — no .complexity-baseline.json entry
      names a path/symbol that no longer exists (task 395).  Property
      (c) covered the allowlist only, so a baseline entry describing a
      moved-away function sat there forever.  Line drift (same symbol,
      new line number) is NOT a violation — only a symbol that exists
      nowhere in the scanned tree.

Exit codes:
  0  all five properties satisfied
  1  one or more violations found

Usage:
  python scripts/check_complexity_allowlist.py
  python scripts/check_complexity_allowlist.py --repo-root /path/to/repo
  python scripts/check_complexity_allowlist.py --list-all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

_REPO_ROOT = Path(__file__).resolve().parent.parent

from complexity_audit import run_audit  # noqa: E402
from complexity_config import (  # noqa: E402
    AllowlistEntry,
    build_allowlist_index,
    load_allowlist,
    load_caps,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_RATIONALE_LEN = 40  # characters; "pre-existing; scheduled for v5.55 wave N refactor" ≈ 51
# Tightened 2026-08-23 by car C2 (task 282, bug-bag-2 train). The previous
# 0.20 multiplier let a single re-baseline inflate any entry by +20% in
# one shot — http.py recorded 3300, was 3496, check_complexity exited 0
# even though file_loc grew +5.9% because 5.9 < 20. The drift ratchet is
# supposed to catch growth, not absorb it; 0.0 means "any growth above
# the recorded value is a re-review trigger".
DRIFT_TOLERANCE = 0.0

# Metric keys used in the allowlist (canonical vocabulary)
_FN_METRICS = {"cyclomatic", "fn_loc", "params", "nesting"}
_FILE_METRICS = {"file_loc"}
_CLASS_METRICS = {"class_depth"}

# ---------------------------------------------------------------------------
# HARD violation collection (mirrors check_complexity.py HARD checks)
# ---------------------------------------------------------------------------


def _collect_production_hard_violations(
    repo_root: Path,
) -> list[dict]:
    """Scan production Python files in yadgar/ and collect all HARD violations.

    Scope: yadgar/ only (production package). Scripts/ tooling is excluded;
    its complexity is gated by the I13 hook + baseline, not the allowlist.

    Returns a list of dicts:
      {"path": rel_path, "function": name, "metric": metric, "actual": int, "limit": int}
    """
    caps = load_caps(repo_root / ".complexity-config.json")
    results = run_audit(str(repo_root / "yadgar"))

    fns = results["functions"]
    files_r = results["files"]
    cls_r = results["classes"]

    violations: list[dict] = []

    for r in fns:
        if r.is_test:
            continue
        try:
            rel = str(Path(r.filepath).resolve().relative_to(repo_root))
        except ValueError:
            rel = r.filepath

        if r.cyclo > caps.cyclomatic_hard:
            violations.append(
                {
                    "path": rel,
                    "function": r.name,
                    "lineno": r.lineno,
                    "metric": "cyclomatic",
                    "actual": r.cyclo,
                    "limit": caps.cyclomatic_hard,
                }
            )
        if r.loc > caps.fn_loc_hard:
            violations.append(
                {
                    "path": rel,
                    "function": r.name,
                    "lineno": r.lineno,
                    "metric": "fn_loc",
                    "actual": r.loc,
                    "limit": caps.fn_loc_hard,
                }
            )
        if r.params > caps.params_hard:
            violations.append(
                {
                    "path": rel,
                    "function": r.name,
                    "lineno": r.lineno,
                    "metric": "params",
                    "actual": r.params,
                    "limit": caps.params_hard,
                }
            )
        if r.nesting > caps.nesting_hard:
            violations.append(
                {
                    "path": rel,
                    "function": r.name,
                    "lineno": r.lineno,
                    "metric": "nesting",
                    "actual": r.nesting,
                    "limit": caps.nesting_hard,
                }
            )

    for f in files_r:
        if f.is_test:
            continue
        if f.loc > caps.file_loc_hard:
            try:
                rel = str(Path(f.filepath).resolve().relative_to(repo_root))
            except ValueError:
                rel = f.filepath
            violations.append(
                {
                    "path": rel,
                    "function": "<file>",
                    "lineno": 1,
                    "metric": "file_loc",
                    "actual": f.loc,
                    "limit": caps.file_loc_hard,
                }
            )

    for c in cls_r:
        if c.inh_depth > caps.class_depth_hard:
            try:
                rel = str(Path(c.filepath).resolve().relative_to(repo_root))
            except ValueError:
                rel = c.filepath
            violations.append(
                {
                    "path": rel,
                    "function": c.name,
                    "lineno": c.lineno,
                    "metric": "class_depth",
                    "actual": c.inh_depth,
                    "limit": caps.class_depth_hard,
                }
            )

    return violations


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------


def check_gate(
    hard_violations: list[dict],
    allowlist_index: dict,
) -> list[str]:
    """(a) No HARD violation exists outside the allowlist."""
    errors: list[str] = []
    for v in hard_violations:
        key = (v["path"], v["function"], v["metric"])
        if key not in allowlist_index:
            errors.append(
                f"GATE: {v['path']}:{v['lineno']} {v['function']!r} "
                f"{v['metric']}={v['actual']} (limit {v['limit']}) "
                f"is HARD but NOT in allowlist — add an entry with rationale or refactor"
            )
    return errors


def check_rationale(entries: list[AllowlistEntry]) -> list[str]:
    """(b) Every allowlist entry has a non-empty rationale >= MIN_RATIONALE_LEN chars."""
    errors: list[str] = []
    for e in entries:
        stripped = e.rationale.strip()
        if len(stripped) < MIN_RATIONALE_LEN:
            errors.append(
                f"RATIONALE: {e.path} {e.function!r}: rationale is too short "
                f"({len(stripped)} chars < {MIN_RATIONALE_LEN} required): {stripped!r}"
            )
    return errors


def check_stale(
    entries: list[AllowlistEntry],
    hard_violations: list[dict],
) -> list[str]:
    """(c) Every allowlist entry still maps to a real current HARD violation."""
    # Build set of current (path, function, metric) triples from hard violations
    current_keys: set[tuple[str, str, str]] = {
        (v["path"], v["function"], v["metric"]) for v in hard_violations
    }

    errors: list[str] = []
    for e in entries:
        for metric in e.metrics:
            key = (e.path, e.function, metric)
            if key not in current_keys:
                errors.append(
                    f"STALE: {e.path} {e.function!r} metric={metric!r} is in allowlist "
                    f"but no longer a HARD violation — remove the entry (or the metric from it)"
                )
    return errors


def check_drift(
    entries: list[AllowlistEntry],
    hard_violations: list[dict],
) -> list[str]:
    """(d) Recorded metrics match current values within DRIFT_TOLERANCE.

    Growth beyond the recorded value triggers re-review.
    Shrinkage (improvement) is fine — a later stale check will flag it.
    """
    # Build lookup: (path, function, metric) → actual current value
    current_actuals: dict[tuple[str, str, str], int] = {
        (v["path"], v["function"], v["metric"]): v["actual"] for v in hard_violations
    }

    errors: list[str] = []
    for e in entries:
        for metric, recorded_val in e.metrics.items():
            key = (e.path, e.function, metric)
            current = current_actuals.get(key)
            if current is None:
                # Already caught by stale check
                continue
            if current > recorded_val:
                growth_pct = (current - recorded_val) / max(recorded_val, 1) * 100
                if growth_pct > DRIFT_TOLERANCE * 100:
                    errors.append(
                        f"DRIFT: {e.path} {e.function!r} metric={metric!r}: "
                        f"recorded={recorded_val}, current={current} "
                        f"(+{growth_pct:.0f}% > {DRIFT_TOLERANCE * 100:.0f}% tolerance) "
                        f"— update allowlist entry and re-review rationale"
                    )
    return errors


def _baseline_scan_root(repo_root: Path) -> Path:
    """Directory whose ``*.py`` files define the live-symbol set.

    Split out so a test can point the scan at the real tree while the
    baseline under test lives elsewhere.
    """
    return repo_root / "yadgar"


def check_dead_baseline(repo_root: Path) -> list[str]:
    """(e) No ``.complexity-baseline.json`` entry names a symbol that is gone.

    Task 395. The allowlist has had a no-stale-entries property since I30 was
    written; the BASELINE never did, so
    ``admin_other.py::_parse_since_duration@146`` survived the function's move
    to ``_recent_memories.py`` two cars earlier with nothing to flag it.

    Scope matches (c)/(d): production ``yadgar/`` excluding ``/tests/``.
    Only DEAD keys are errors — a symbol that merely moved lines is not one
    (see ``check_complexity.dead_baseline_keys`` for why that distinction is
    load-bearing rather than cosmetic).
    """
    from check_complexity import dead_baseline_keys

    scan_root = _baseline_scan_root(repo_root)
    filepaths = [
        str(p)
        for p in scan_root.rglob("*.py")
        if "__pycache__" not in str(p) and ".venv" not in str(p) and "/tests/" not in str(p)
    ]
    baseline_path = repo_root / ".complexity-baseline.json"
    if not baseline_path.exists():
        return []
    dead = dead_baseline_keys(filepaths, str(baseline_path))
    # Remediation names THIS key, deliberately not ``--gc --all-files``: that
    # flag also deletes the ~1576 benign line-drift entries the Car 7 standing
    # decision keeps on purpose, so pointing at it turns a one-key problem
    # into a 1576-entry diff.
    return [
        f"DEAD-BASELINE: {key} names a symbol that no longer exists — "
        f"delete this entry from .complexity-baseline.json"
        for key in sorted(dead)
    ]


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------


def run_check(
    repo_root: Path,
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """Run all five I30 checks.

    Returns (gate_errors, rationale_errors, stale_errors, drift_errors,
    dead_baseline_errors).
    """
    allowlist = load_allowlist(repo_root / ".complexity-allowlist.json")
    allowlist_index = build_allowlist_index(allowlist)
    hard_violations = _collect_production_hard_violations(repo_root)

    # Stale/drift checks are scoped to production yadgar/ only.
    # Scripts/ tooling and yadgar/tests/ are excluded: the I13 hook + baseline
    # govern test-file complexity; _collect_production_hard_violations() already
    # skips is_test functions/files, so test-path allowlist entries would always
    # appear stale when cross-checked against production violations only.
    #
    # Car 7 (2026-08-13) found the side effect: this filter excludes those
    # entries from check_drift too, not just check_stale — 4 entries
    # (scripts/check_complexity.py::_check_function, ::_check_class;
    # yadgar/tests/server/test_integration.py <file>,
    # ::test_clean_startup_and_shutdown) are permanently ungated by drift.
    # Measured that day: all 4 at 0% or shrinking — not an active problem —
    # but nothing will ever flag them if that changes. Not fixed here
    # (would need a separate scripts/tests-scoped drift pass); flagged as a
    # train finding.
    audited_prefix = "yadgar/"
    audited_entries = [
        e for e in allowlist if e.path.startswith(audited_prefix) and "/tests/" not in e.path
    ]

    return (
        check_gate(hard_violations, allowlist_index),
        check_rationale(allowlist),  # rationale applies to all entries
        check_stale(audited_entries, hard_violations),
        check_drift(audited_entries, hard_violations),
        check_dead_baseline(repo_root),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "I30 — complexity-cap integrity: every HARD violation allowlisted, "
            "every allowlist entry live + justified + not drifted."
        ),
    )
    parser.add_argument(
        "--repo-root",
        metavar="DIR",
        default=None,
        help="Override repository root (default: auto-detect from script location).",
    )
    parser.add_argument(
        "--list-all",
        action="store_true",
        help="List all current HARD violations with their allowlist status and exit 0.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(args.repo_root) if args.repo_root else _REPO_ROOT

    if args.list_all:
        allowlist = load_allowlist(repo_root / ".complexity-allowlist.json")
        allowlist_index = build_allowlist_index(allowlist)
        hard_violations = _collect_production_hard_violations(repo_root)
        print(f"Current HARD violations ({len(hard_violations)} total):")
        for v in sorted(hard_violations, key=lambda x: (x["path"], x["function"], x["metric"])):
            key = (v["path"], v["function"], v["metric"])
            status = "ALLOWLISTED" if key in allowlist_index else "UNALLOWLISTED"
            print(
                f"  [{status}] {v['path']}:{v['lineno']} {v['function']!r} {v['metric']}={v['actual']}"
            )
        return 0

    gate_errors, rationale_errors, stale_errors, drift_errors, dead_errors = run_check(repo_root)

    all_errors = gate_errors + rationale_errors + stale_errors + drift_errors + dead_errors
    if all_errors:
        print("I30 VIOLATIONS — complexity-cap integrity broken:", file=sys.stderr)
        for section, errs in [
            ("(a) GATE", gate_errors),
            ("(b) RATIONALE", rationale_errors),
            ("(c) STALE", stale_errors),
            ("(d) DRIFT", drift_errors),
            ("(e) DEAD-BASELINE", dead_errors),
        ]:
            if errs:
                print(f"  {section}:", file=sys.stderr)
                for e in errs:
                    print(f"    {e}", file=sys.stderr)
        print(f"\n{len(all_errors)} violation(s) found.", file=sys.stderr)
        return 1

    total_allowlisted = sum(
        1
        for entry in load_allowlist(repo_root / ".complexity-allowlist.json")
        for _ in entry.metrics
    )
    print(
        f"I30 OK — complexity-cap integrity satisfied "
        f"({total_allowlisted} allowlist metric entries, all live + justified)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
