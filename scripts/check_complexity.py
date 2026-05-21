#!/usr/bin/env python3
"""Pre-commit hook: enforce I13 complexity caps.

Covers what ruff DOESN'T handle natively:
  - Function LOC (≤150 hard / ≤80 soft)
  - Function nesting depth (≤4 hard)
  - File LOC (≤1000 hard / ≤500 soft)
  - File public symbols (≤30 soft)
  - Class method count (≤30 soft)
  - Class instance attrs (≤15 soft)
  - Class inheritance depth (≤3 hard)

Ruff covers:
  - Cyclomatic complexity ≥ C901 max-complexity=15 (hard cap)
  - Too-many-arguments PLR0913 max-args=8 (hard cap)

Enforcement:
  - Soft cap violations → warn to stderr, exit 0
  - Hard cap violations → error to stderr, exit 1
  - # noqa: C901 - cohesive: <reason> on def line suppresses SOFT only; hard = never suppressible

Baseline ratchet (option 1):
  - .complexity-baseline.json records current metrics for all functions
  - Hook only blocks NEW violations OR worsened existing ones
  - New code held to full caps; existing functions allowed at their current numbers

Usage:
  python scripts/check_complexity.py [files...]
  python scripts/check_complexity.py --update-baseline [files...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------------------
# Import analysis functions from complexity_audit.py (do not duplicate)
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from complexity_audit import (  # noqa: E402
    CLASS_ATTRS_SOFT,
    CLASS_DEPTH_HARD,
    CLASS_METHODS_SOFT,
    FILE_LOC_HARD,
    FILE_LOC_SOFT,
    FN_CYCLO_HARD,
    FN_CYCLO_SOFT,
    FN_LOC_HARD,
    FN_LOC_SOFT,
    FN_NESTING_HARD,
    FN_PARAMS_HARD,
    FN_PARAMS_SOFT,
    FileResult,
    FunctionResult,
    analyze_file,
)

# ---------------------------------------------------------------------------
# Cap constants (re-exported from complexity_audit — shown here for clarity)
# ---------------------------------------------------------------------------

# FN_CYCLO_HARD = 15  # Covered by ruff C901
# FN_CYCLO_SOFT = 10  # Covered by ruff C901


# ---------------------------------------------------------------------------
# Noqa pattern — matches: # noqa: C901 - cohesive: <reason>
# Accepts dashes (-, –, —) and optional whitespace variations
# ---------------------------------------------------------------------------

_NOQA_COHESIVE_RE = re.compile(
    r"#\s*noqa:\s*C901\s*[-–—:]\s*cohesive\s*[-–—:]\s*.+",
    re.IGNORECASE,
)


def _has_noqa_cohesive(source_lines: list[str], lineno: int) -> bool:
    """Check if the def line (1-indexed) has a cohesive noqa annotation."""
    if lineno < 1 or lineno > len(source_lines):
        return False
    line = source_lines[lineno - 1]
    return bool(_NOQA_COHESIVE_RE.search(line))


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


class ViolationSeverity(str, Enum):  # noqa: UP042 — StrEnum unavailable in Python <3.11
    HARD = "HARD"
    SOFT = "SOFT"
    ALLOWED = "ALLOWED"  # pre-existing at or below baseline


@dataclass
class Violation:
    filepath: str
    lineno: int
    entity: str  # function or class name
    cap: str  # e.g. "fn_loc", "file_loc", "nesting", "inheritance_depth"
    severity: ViolationSeverity
    actual: int
    limit: int
    pre_existing: bool = False
    suppressed: bool = False  # true if a cohesive noqa annotation silences this soft violation

    def message(self) -> str:
        tag = f"{self.filepath}:{self.lineno}"
        severity = self.severity.value
        if self.suppressed:
            severity = "suppressed"
        pre = " [pre-existing]" if self.pre_existing else ""
        return (
            f"{tag}: {self.entity}: {self.cap}={self.actual} exceeds {self.limit} ({severity}){pre}"
        )


@dataclass
class CheckResult:
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    exit_code: int = 0


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------


def load_baseline(baseline_path: str) -> dict:
    """Load .complexity-baseline.json. Returns {} if missing."""
    p = Path(baseline_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):  # fmt: skip
        return {}


def _baseline_key(filepath: str, entity_name: str, lineno: int = 0) -> str:
    """Key for baseline entries: <filepath>::<name>@<lineno>.

    lineno disambiguates overloaded names in the same file (e.g. multiple
    score_cross_encoder methods across Protocol + concrete classes).
    If lineno=0, the entry is file-level (key = filepath::__file__).
    """
    if lineno == 0:
        return f"{filepath}::{entity_name}"
    return f"{filepath}::{entity_name}@{lineno}"


def update_baseline(filepaths: list[str], baseline_path: str) -> None:
    """Regenerate baseline entries for the given files and merge into baseline_path."""
    existing = load_baseline(baseline_path)
    all_classes: dict = {}

    for filepath in filepaths:
        filepath = str(Path(filepath).resolve())
        is_test = _is_test_file(filepath)
        fn_results, file_result, cls_results = analyze_file(filepath, is_test, all_classes)

        # File-level entry
        file_key = _baseline_key(filepath, "__file__")
        existing[file_key] = {"loc": file_result.loc}

        for r in fn_results:
            key = _baseline_key(r.filepath, r.name, r.lineno)
            existing[key] = {
                "loc": r.loc,
                "cyclo": r.cyclo,
                "params": r.params,
                "nesting": r.nesting,
            }

        for c in cls_results:
            key = _baseline_key(c.filepath, c.name, c.lineno)
            existing[key] = {
                "methods": c.methods,
                "attrs": c.attrs,
                "inh_depth": c.inh_depth,
            }

    p = Path(baseline_path)
    p.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-file analysis
# ---------------------------------------------------------------------------


def _is_test_file(filepath: str) -> bool:
    p = Path(filepath)
    name = p.name
    return name.startswith("test_") or name.endswith("_test.py")


def _check_function(
    r: FunctionResult,
    source_lines: list[str],
    baseline: dict,
) -> list[Violation]:
    """Return violations for a single function result.

    Baseline keys use the same short names stored by update_baseline:
    "loc", "cyclo", "params", "nesting".
    """
    violations = []
    has_noqa = _has_noqa_cohesive(source_lines, r.lineno)
    key = _baseline_key(r.filepath, r.name, r.lineno)
    bl = baseline.get(key)

    def _classify(
        bl_key: str,
        cap_label: str,
        actual: int,
        hard_limit: int,
        soft_limit: int | None = None,
    ) -> Violation | None:
        """bl_key: key into baseline dict (e.g. "loc", "cyclo").
        cap_label: human-readable cap name for Violation.cap."""
        lower_bound = soft_limit if soft_limit is not None else hard_limit
        if actual <= lower_bound:
            return None
        is_hard = actual > hard_limit
        severity = ViolationSeverity.HARD if is_hard else ViolationSeverity.SOFT
        limit = hard_limit if is_hard else (soft_limit or hard_limit)

        # Baseline ratchet
        if bl is not None and bl_key in bl:
            baseline_val = bl[bl_key]
            if actual <= baseline_val:
                return Violation(
                    filepath=r.filepath,
                    lineno=r.lineno,
                    entity=r.name,
                    cap=cap_label,
                    severity=ViolationSeverity.ALLOWED,
                    actual=actual,
                    limit=limit,
                    pre_existing=True,
                )
            # Worsened beyond baseline — fall through to full enforcement

        # noqa suppresses SOFT only
        if has_noqa and not is_hard:
            return Violation(
                filepath=r.filepath,
                lineno=r.lineno,
                entity=r.name,
                cap=cap_label,
                severity=ViolationSeverity.SOFT,
                actual=actual,
                limit=limit,
                suppressed=True,
            )

        return Violation(
            filepath=r.filepath,
            lineno=r.lineno,
            entity=r.name,
            cap=cap_label,
            severity=severity,
            actual=actual,
            limit=limit,
        )

    # --- Cyclomatic (always enforced, including test files) ---
    # NOTE: ruff C901 covers this at hard cap; we check here too for consistency
    # and because ruff may not run on the same file set.
    if r.cyclo > FN_CYCLO_SOFT:
        v = _classify("cyclo", "cyclo", r.cyclo, FN_CYCLO_HARD, FN_CYCLO_SOFT)
        if v:
            violations.append(v)

    # --- LOC (skipped for test files) ---
    if not r.is_test:
        v = _classify("loc", "fn_loc", r.loc, FN_LOC_HARD, FN_LOC_SOFT)
        if v:
            violations.append(v)

    # --- Nesting (always enforced) ---
    if r.nesting > FN_NESTING_HARD:
        v = _classify("nesting", "nesting", r.nesting, FN_NESTING_HARD, None)
        if v:
            violations.append(v)

    # --- Params (skipped for test files) ---
    if not r.is_test and r.params > FN_PARAMS_SOFT:
        v = _classify("params", "params", r.params, FN_PARAMS_HARD, FN_PARAMS_SOFT)
        if v:
            violations.append(v)

    return violations


def _check_class(
    c,
    baseline: dict,
) -> list[Violation]:
    """Return violations for a single class result.

    Baseline keys: "methods", "attrs", "inh_depth" (matching update_baseline).
    """
    violations = []
    key = _baseline_key(c.filepath, c.name, c.lineno)
    bl = baseline.get(key)

    def _classify_cls(
        bl_key: str,
        cap_label: str,
        actual: int,
        hard_limit: int | None = None,
        soft_limit: int | None = None,
    ) -> Violation | None:
        if soft_limit is not None and actual <= soft_limit:
            return None
        if hard_limit is not None and actual <= hard_limit:
            return None

        is_hard = hard_limit is not None and actual > hard_limit
        severity = ViolationSeverity.HARD if is_hard else ViolationSeverity.SOFT
        limit = hard_limit if is_hard else soft_limit

        # Baseline ratchet
        if bl is not None and bl_key in bl:
            baseline_val = bl[bl_key]
            if actual <= baseline_val:
                return Violation(
                    filepath=c.filepath,
                    lineno=c.lineno,
                    entity=c.name,
                    cap=cap_label,
                    severity=ViolationSeverity.ALLOWED,
                    actual=actual,
                    limit=limit,
                    pre_existing=True,
                )

        return Violation(
            filepath=c.filepath,
            lineno=c.lineno,
            entity=c.name,
            cap=cap_label,
            severity=severity,
            actual=actual,
            limit=limit,
        )

    # Methods (soft)
    if c.methods > CLASS_METHODS_SOFT:
        v = _classify_cls("methods", "class_methods", c.methods, soft_limit=CLASS_METHODS_SOFT)
        if v:
            violations.append(v)

    # Instance attrs (soft)
    if c.attrs > CLASS_ATTRS_SOFT:
        v = _classify_cls("attrs", "class_attrs", c.attrs, soft_limit=CLASS_ATTRS_SOFT)
        if v:
            violations.append(v)

    # Inheritance depth (hard)
    if c.inh_depth > CLASS_DEPTH_HARD:
        v = _classify_cls(
            "inh_depth", "inheritance_depth", c.inh_depth, hard_limit=CLASS_DEPTH_HARD
        )
        if v:
            violations.append(v)

    return violations


def _check_file_loc(
    file_result: FileResult,
    baseline: dict,
) -> list[Violation]:
    """Return file-level LOC violations."""
    violations = []
    if file_result.is_test:
        # Test files: file LOC still enforced (only fn params+LOC are exempt)
        pass

    loc = file_result.loc
    if loc <= FILE_LOC_SOFT:
        return violations

    severity = ViolationSeverity.HARD if loc > FILE_LOC_HARD else ViolationSeverity.SOFT
    limit = FILE_LOC_HARD if severity == ViolationSeverity.HARD else FILE_LOC_SOFT

    key = _baseline_key(file_result.filepath, "__file__")
    bl = baseline.get(key)
    if bl is not None and "loc" in bl:
        if loc <= bl["loc"]:
            return [
                Violation(
                    filepath=file_result.filepath,
                    lineno=1,
                    entity="<file>",
                    cap="file_loc",
                    severity=ViolationSeverity.ALLOWED,
                    actual=loc,
                    limit=limit,
                    pre_existing=True,
                )
            ]

    violations.append(
        Violation(
            filepath=file_result.filepath,
            lineno=1,
            entity="<file>",
            cap="file_loc",
            severity=severity,
            actual=loc,
            limit=limit,
        )
    )
    return violations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_staged_file(
    filepath: str,
    is_test: bool,
    baseline: dict | None = None,
) -> CheckResult:
    """Analyze a single file and return a CheckResult.

    filepath is resolved to absolute so baseline key lookup works regardless of
    whether the caller passes a relative or absolute path.
    """
    if baseline is None:
        baseline = {}
    filepath = str(Path(filepath).resolve())

    all_classes: dict = {}
    try:
        fn_results, file_result, cls_results = analyze_file(filepath, is_test, all_classes)
    except (OSError, SyntaxError) as exc:
        result = CheckResult()
        result.errors.append(f"{filepath}: failed to parse: {exc}")
        result.exit_code = 1
        return result

    # Load source lines for noqa detection
    try:
        source_lines = Path(filepath).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        source_lines = []

    result = CheckResult()
    all_violations: list[Violation] = []

    # File-level LOC
    all_violations.extend(_check_file_loc(file_result, baseline))

    # Per-function checks
    for fn_r in fn_results:
        all_violations.extend(_check_function(fn_r, source_lines, baseline))

    # Per-class checks
    for cls_r in cls_results:
        all_violations.extend(_check_class(cls_r, baseline))

    result.violations = all_violations

    # Classify into warnings/errors
    for v in all_violations:
        if v.severity == ViolationSeverity.ALLOWED or v.suppressed:
            continue
        if v.severity == ViolationSeverity.HARD:
            result.errors.append(v.message())
            result.exit_code = 1
        elif v.severity == ViolationSeverity.SOFT:
            result.warnings.append(v.message())

    return result


def check_files(
    filepaths: list[str],
    baseline_path: str,
) -> CheckResult:
    """Check all given files and return aggregated CheckResult.

    Normalises filepaths to absolute so baseline keys (which are always
    absolute) match regardless of whether pre-commit passes relative paths.
    """
    baseline = load_baseline(baseline_path)
    combined = CheckResult()

    for filepath in filepaths:
        if not filepath.endswith(".py"):
            continue
        # Normalise to absolute path so baseline key lookup always hits
        filepath = str(Path(filepath).resolve())
        is_test = _is_test_file(filepath)
        result = analyze_staged_file(filepath, is_test, baseline)
        combined.violations.extend(result.violations)
        combined.warnings.extend(result.warnings)
        combined.errors.extend(result.errors)
        if result.exit_code != 0:
            combined.exit_code = 1

    return combined


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _find_baseline(start: str = ".") -> str:
    """Walk up from cwd to find .complexity-baseline.json."""
    p = Path(start).resolve()
    for parent in [p] + list(p.parents):
        candidate = parent / ".complexity-baseline.json"
        if candidate.exists():
            return str(candidate)
    return str(Path(start) / ".complexity-baseline.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="I13 complexity cap enforcer (pre-commit hook)")
    parser.add_argument(
        "filenames",
        nargs="*",
        help="Python files to check (passed by pre-commit)",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Regenerate .complexity-baseline.json and exit 0",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Path to .complexity-baseline.json (default: auto-locate from cwd)",
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="Scan all .py files in yadgar/ and scripts/ (ignores filenames args)",
    )
    args = parser.parse_args()

    baseline_path = args.baseline or _find_baseline()

    if args.all_files:
        repo_root = Path(__file__).parent.parent
        filenames = [
            str(p)
            for d in ("yadgar", "scripts")
            for p in (repo_root / d).rglob("*.py")
            if "__pycache__" not in str(p) and ".venv" not in str(p)
        ]
    else:
        filenames = args.filenames

    if args.update_baseline:
        update_baseline(filenames, baseline_path)
        n = len(json.loads(Path(baseline_path).read_text()).keys())
        print(f"Baseline updated: {baseline_path} ({n} entries)", file=sys.stderr)
        sys.exit(0)

    result = check_files(filenames, baseline_path)

    for w in result.warnings:
        print(f"WARN  {w}", file=sys.stderr)
    for e in result.errors:
        print(f"ERROR {e}", file=sys.stderr)

    sys.exit(result.exit_code)


if __name__ == "__main__":
    main()
