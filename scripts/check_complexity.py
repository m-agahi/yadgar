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
  - Hard cap violations → error to stderr, exit 1, UNLESS the
    (path, function, metric) triple has an allowlist entry with a
    non-empty rationale in .complexity-allowlist.json → INFO/pass
  - # noqa: C901 - cohesive: <reason> on def line suppresses SOFT only;
    hard = never suppressible via noqa (use the allowlist instead)

Baseline ratchet:
  - .complexity-baseline.json records current metrics for SOFT violations
  - Hook only blocks NEW soft violations OR worsened existing ones
  - HARD violations bypass the ratchet; they require the allowlist

Caps are read from .complexity-config.json (repo root); defaults match
the previously hardcoded constants if the file is missing.

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

# Repo root — used to produce portable (repo-relative) baseline keys.
# scripts/ is one level below repo root, so parent.parent gives repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent

from complexity_audit import (  # noqa: E402
    CLASS_ATTRS_SOFT,
    CLASS_METHODS_SOFT,
    FileResult,
    FunctionResult,
    analyze_file,
)
from complexity_config import (  # noqa: E402
    build_allowlist_index,
    get_allowlist_entry,
    is_allowlisted,
    load_allowlist,
    load_caps,
)

# ---------------------------------------------------------------------------
# Cap constants — loaded from .complexity-config.json at module init.
# Kept as module-level for import compatibility; callers should prefer
# using _CAPS directly.  Default: identical to previously hardcoded values.
# ---------------------------------------------------------------------------

_CAPS = load_caps()

# Legacy aliases so external importers and tests don't break
FN_CYCLO_HARD = _CAPS.cyclomatic_hard
FN_CYCLO_SOFT = _CAPS.cyclomatic_soft or 10
FN_LOC_HARD = _CAPS.fn_loc_hard
FN_LOC_SOFT = _CAPS.fn_loc_soft or 80
FN_NESTING_HARD = _CAPS.nesting_hard
FN_PARAMS_HARD = _CAPS.params_hard
FN_PARAMS_SOFT = _CAPS.params_soft or 5
FILE_LOC_HARD = _CAPS.file_loc_hard
FILE_LOC_SOFT = _CAPS.file_loc_soft or 500
CLASS_DEPTH_HARD = _CAPS.class_depth_hard

# ---------------------------------------------------------------------------
# Allowlist — loaded once at module init.  Tests may override by injecting
# a pre-built index into check functions via the allowlist_index parameter.
# ---------------------------------------------------------------------------

_ALLOWLIST_PATH = _REPO_ROOT / ".complexity-allowlist.json"
_ALLOWLIST_ENTRIES = load_allowlist(_ALLOWLIST_PATH)
_ALLOWLIST_INDEX = build_allowlist_index(_ALLOWLIST_ENTRIES)

# ---------------------------------------------------------------------------
# Metric name mapping: Violation.cap  →  allowlist metrics key
#
# Violation.cap uses the hook's internal vocabulary; the allowlist uses the
# canonical external vocabulary defined in .complexity-config.json.
# Keeping Violation.cap labels unchanged preserves existing test assertions
# (which filter by substring e.g. "cyclo" in v.cap, "inheritance" in v.cap).
# ---------------------------------------------------------------------------

_CAP_TO_ALLOWLIST_METRIC: dict[str, str] = {
    "cyclomatic": "cyclomatic",  # fn cyclomatic complexity
    "fn_loc": "fn_loc",  # function LOC
    "nesting": "nesting",  # function nesting depth
    "params": "params",  # function parameter count
    "file_loc": "file_loc",  # file LOC
    "inheritance_depth": "class_depth",  # class inheritance depth (cap name → allowlist key)
    # soft-only (never HARD → never looked up in allowlist, but listed for completeness)
    "class_methods": "class_methods",
    "class_attrs": "class_attrs",
}


# ---------------------------------------------------------------------------
# Noqa pattern — matches: # noqa: C901 - cohesive: <reason>
# Accepts dashes (-, –, —) and optional whitespace variations
# ---------------------------------------------------------------------------

_NOQA_COHESIVE_RE = re.compile(
    r"#\s*noqa:\s*C901\s*[-–—:]\s*cohesive\s*[-–—:]\s*.+",
    re.IGNORECASE,
)


def _rel_path(filepath: str) -> str:
    """Return filepath relative to repo root, for portable baseline keys.

    Falls back to the absolute path if filepath is outside the repo (e.g. tmp
    paths created in tests). This ensures test paths don't pollute the baseline
    and that baseline keys are machine-independent.
    """
    try:
        return str(Path(filepath).resolve().relative_to(_REPO_ROOT))
    except ValueError:
        # Path outside repo — use absolute as-is (tests with tmp_path land here)
        return str(Path(filepath).resolve())


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
    ALLOWED = "ALLOWED"  # pre-existing at or below baseline, or HARD allowlisted


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
    allowlisted: bool = False  # true if a HARD violation is in the allowlist
    recorded: int | None = None  # allowlist's recorded ceiling, set only on a ratchet failure

    def message(self) -> str:
        tag = f"{self.filepath}:{self.lineno}"
        if self.recorded is not None and self.severity == ViolationSeverity.HARD:
            # Ratchet failure: (path, function, metric) IS in the allowlist, but
            # `actual` has drifted past the recorded ceiling. Name both numbers —
            # this is the whole point of the ratchet (a bare "exceeds limit"
            # message can't be told apart from "never allowlisted at all").
            return (
                f"{tag}: {self.entity}: {self.cap}={self.actual} exceeds allowlisted "
                f"ceiling {self.recorded} (+{self.actual - self.recorded}) (HARD, allowlist-drift). "
                f"Re-baseline .complexity-allowlist.json with an updated rationale, or reduce."
            )
        severity = self.severity.value
        if self.suppressed:
            severity = "suppressed"
        if self.allowlisted:
            severity = "allowlisted"
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
    """Key for baseline entries: <rel_filepath>::<name>@<lineno>.

    filepath is normalised to a repo-relative path so the baseline is portable
    across machines and CI environments.  lineno disambiguates overloaded names
    in the same file (e.g. multiple score_cross_encoder methods across Protocol
    + concrete classes). If lineno=0, the entry is file-level (key = rel::__file__).
    """
    rel = _rel_path(filepath)
    if lineno == 0:
        return f"{rel}::{entity_name}"
    return f"{rel}::{entity_name}@{lineno}"


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


def _is_excluded_path(filepath: str) -> bool:
    """Return True for paths exempt from I13 enforcement.

    Excluded scopes — mirror the I30 production-only gate in
    check_complexity_allowlist.py:
      - yadgar/tests/   — test suite; complexity is expected and tolerated
      - scripts/        — one-off tooling scripts; not production code

    Both scopes are checked against the resolved absolute path so the
    predicate holds regardless of whether pre-commit passes a relative
    or absolute path.
    """
    resolved = Path(filepath).resolve()
    parts = resolved.parts
    # yadgar/tests/ — any depth inside the tests sub-package
    for i, part in enumerate(parts):
        if part == "yadgar" and i + 1 < len(parts) and parts[i + 1] == "tests":
            return True
    # scripts/ — the top-level scripts directory (one level below repo root)
    # Identified by: parent == repo root AND grandparent is also the repo root
    # resolved-path approach: repo root is _REPO_ROOT; check if the file is
    # directly under _REPO_ROOT / "scripts".
    try:
        rel = resolved.relative_to(_REPO_ROOT)
        if rel.parts and rel.parts[0] == "scripts":
            return True
    except ValueError:
        pass
    return False


def _check_function(  # noqa: C901 - cohesive: dispatch function; each branch = one metric check (cyclo/loc/nesting/params)
    r: FunctionResult,
    source_lines: list[str],
    baseline: dict,
    allowlist_index: dict | None = None,
    caps: object | None = None,
) -> list[Violation]:
    """Return violations for a single function result.

    Baseline keys use the same short names stored by update_baseline:
    "loc", "cyclo", "params", "nesting".

    HARD violations are checked against the allowlist: if (path, function, metric)
    is allowlisted with a non-empty rationale, the violation becomes ALLOWED.
    """
    if allowlist_index is None:
        allowlist_index = _ALLOWLIST_INDEX
    if caps is None:
        caps = _CAPS

    violations = []
    has_noqa = _has_noqa_cohesive(source_lines, r.lineno)
    key = _baseline_key(r.filepath, r.name, r.lineno)
    bl = baseline.get(key)

    # Relative path for allowlist lookup
    rel = _rel_path(r.filepath)

    def _classify(
        bl_key: str,
        cap_label: str,
        actual: int,
        hard_limit: int,
        soft_limit: int | None = None,
    ) -> Violation | None:
        """bl_key: key into baseline dict (e.g. "loc", "cyclo").
        cap_label: internal cap name for Violation.cap (also used to look up allowlist key)."""
        lower_bound = soft_limit if soft_limit is not None else hard_limit
        if actual <= lower_bound:
            return None
        is_hard = actual > hard_limit
        severity = ViolationSeverity.HARD if is_hard else ViolationSeverity.SOFT
        limit = hard_limit if is_hard else (soft_limit or hard_limit)

        # HARD → check allowlist first (bypasses baseline ratchet for HARD).
        # is_allowlisted() is itself a ratchet: present-but-drifted-past-recorded
        # fails, it does not fall through to an unconditional pass.
        if is_hard:
            allowlist_metric = _CAP_TO_ALLOWLIST_METRIC.get(cap_label, cap_label)
            if is_allowlisted(rel, r.name, allowlist_metric, actual, allowlist_index):
                return Violation(
                    filepath=r.filepath,
                    lineno=r.lineno,
                    entity=r.name,
                    cap=cap_label,
                    severity=ViolationSeverity.ALLOWED,
                    actual=actual,
                    limit=limit,
                    allowlisted=True,
                )
            # Not allowlisted, or allowlisted but drifted past its recorded
            # ceiling — either way, hard error (no baseline ratchet for HARD).
            entry = get_allowlist_entry(rel, r.name, allowlist_metric, allowlist_index)
            recorded = entry.metrics.get(allowlist_metric) if entry is not None else None
            return Violation(
                filepath=r.filepath,
                lineno=r.lineno,
                entity=r.name,
                cap=cap_label,
                severity=ViolationSeverity.HARD,
                actual=actual,
                limit=limit,
                recorded=recorded,
            )

        # SOFT → baseline ratchet
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
        if has_noqa:
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
    cyclo_soft = caps.cyclomatic_soft or 10
    cyclo_hard = caps.cyclomatic_hard
    if r.cyclo > cyclo_soft:
        v = _classify("cyclo", "cyclomatic", r.cyclo, cyclo_hard, cyclo_soft)
        if v:
            violations.append(v)

    # --- LOC (skipped for test files) ---
    if not r.is_test:
        fn_loc_soft = caps.fn_loc_soft or 80
        fn_loc_hard = caps.fn_loc_hard
        v = _classify("loc", "fn_loc", r.loc, fn_loc_hard, fn_loc_soft)
        if v:
            violations.append(v)

    # --- Nesting (always enforced) ---
    nesting_hard = caps.nesting_hard
    if r.nesting > nesting_hard:
        v = _classify("nesting", "nesting", r.nesting, nesting_hard, None)
        if v:
            violations.append(v)

    # --- Params (skipped for test files) ---
    if not r.is_test:
        params_soft = caps.params_soft or 5
        params_hard = caps.params_hard
        if r.params > params_soft:
            v = _classify("params", "params", r.params, params_hard, params_soft)
            if v:
                violations.append(v)

    return violations


def _check_class(  # noqa: C901 - cohesive: dispatch function; each branch = one class metric check (methods/attrs/depth)
    c,
    baseline: dict,
    allowlist_index: dict | None = None,
    caps: object | None = None,
) -> list[Violation]:
    """Return violations for a single class result.

    Baseline keys: "methods", "attrs", "inh_depth" (matching update_baseline).
    """
    if allowlist_index is None:
        allowlist_index = _ALLOWLIST_INDEX
    if caps is None:
        caps = _CAPS

    violations = []
    key = _baseline_key(c.filepath, c.name, c.lineno)
    bl = baseline.get(key)
    rel = _rel_path(c.filepath)

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

        if is_hard:
            allowlist_metric = _CAP_TO_ALLOWLIST_METRIC.get(cap_label, cap_label)
            if is_allowlisted(rel, c.name, allowlist_metric, actual, allowlist_index):
                return Violation(
                    filepath=c.filepath,
                    lineno=c.lineno,
                    entity=c.name,
                    cap=cap_label,
                    severity=ViolationSeverity.ALLOWED,
                    actual=actual,
                    limit=limit,
                    allowlisted=True,
                )
            entry = get_allowlist_entry(rel, c.name, allowlist_metric, allowlist_index)
            recorded = entry.metrics.get(allowlist_metric) if entry is not None else None
            return Violation(
                filepath=c.filepath,
                lineno=c.lineno,
                entity=c.name,
                cap=cap_label,
                severity=ViolationSeverity.HARD,
                actual=actual,
                limit=limit,
                recorded=recorded,
            )

        # Baseline ratchet (soft only)
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
    class_depth_hard = caps.class_depth_hard
    if c.inh_depth > class_depth_hard:
        v = _classify_cls(
            "inh_depth", "inheritance_depth", c.inh_depth, hard_limit=class_depth_hard
        )
        if v:
            violations.append(v)

    return violations


def _check_file_loc(
    file_result: FileResult,
    baseline: dict,
    allowlist_index: dict | None = None,
    caps: object | None = None,
) -> list[Violation]:
    """Return file-level LOC violations."""
    if allowlist_index is None:
        allowlist_index = _ALLOWLIST_INDEX
    if caps is None:
        caps = _CAPS

    violations = []
    file_loc_soft = caps.file_loc_soft or 500
    file_loc_hard = caps.file_loc_hard

    loc = file_result.loc
    if loc <= file_loc_soft:
        return violations

    severity = ViolationSeverity.HARD if loc > file_loc_hard else ViolationSeverity.SOFT
    limit = file_loc_hard if severity == ViolationSeverity.HARD else file_loc_soft

    if severity == ViolationSeverity.HARD:
        # HARD file LOC → check allowlist (ratchet: drifted-past-recorded fails)
        rel = _rel_path(file_result.filepath)
        if is_allowlisted(rel, "<file>", "file_loc", loc, allowlist_index):
            return [
                Violation(
                    filepath=file_result.filepath,
                    lineno=1,
                    entity="<file>",
                    cap="file_loc",
                    severity=ViolationSeverity.ALLOWED,
                    actual=loc,
                    limit=limit,
                    allowlisted=True,
                )
            ]
        # Not allowlisted, or allowlisted but drifted past its recorded ceiling
        entry = get_allowlist_entry(rel, "<file>", "file_loc", allowlist_index)
        recorded = entry.metrics.get("file_loc") if entry is not None else None
        return [
            Violation(
                filepath=file_result.filepath,
                lineno=1,
                entity="<file>",
                cap="file_loc",
                severity=ViolationSeverity.HARD,
                actual=loc,
                limit=limit,
                recorded=recorded,
            )
        ]

    # SOFT → baseline ratchet
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
    allowlist_index: dict | None = None,
    caps: object | None = None,
) -> CheckResult:
    """Analyze a single file and return a CheckResult.

    filepath is resolved to absolute so baseline key lookup works regardless of
    whether the caller passes a relative or absolute path.
    """
    if baseline is None:
        baseline = {}
    if allowlist_index is None:
        allowlist_index = _ALLOWLIST_INDEX
    if caps is None:
        caps = _CAPS
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
    all_violations.extend(_check_file_loc(file_result, baseline, allowlist_index, caps))

    # Per-function checks
    for fn_r in fn_results:
        all_violations.extend(_check_function(fn_r, source_lines, baseline, allowlist_index, caps))

    # Per-class checks
    for cls_r in cls_results:
        all_violations.extend(_check_class(cls_r, baseline, allowlist_index, caps))

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
    allowlist_index: dict | None = None,
    caps: object | None = None,
) -> CheckResult:
    """Check all given files and return aggregated CheckResult.

    Normalises filepaths to absolute so baseline keys (which are always
    absolute) match regardless of whether pre-commit passes relative paths.
    """
    baseline = load_baseline(baseline_path)
    if allowlist_index is None:
        allowlist_index = _ALLOWLIST_INDEX
    if caps is None:
        caps = _CAPS
    combined = CheckResult()

    for filepath in filepaths:
        if not filepath.endswith(".py"):
            continue
        # Normalise to absolute path so baseline key lookup always hits
        filepath = str(Path(filepath).resolve())
        # Skip test suite and one-off scripts — I13 enforces production code only.
        # Mirrors the yadgar/-only scope of check_complexity_allowlist.py (I30).
        if _is_excluded_path(filepath):
            continue
        is_test = _is_test_file(filepath)
        result = analyze_staged_file(filepath, is_test, baseline, allowlist_index, caps)
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


def _collect_live_keys(filepaths: list[str]) -> set[str]:
    """Collect all live baseline keys from the given source files.

    A live key is one that actually exists in current code:
    - ``<rel_path>::__file__`` (file-level entry)
    - ``<rel_path>::<name>@<lineno>`` (function or class entry)

    Keys for paths outside the repo root use the absolute path (same
    fallback as ``_baseline_key``).
    """
    live: set[str] = set()
    all_classes: dict = {}
    for filepath in filepaths:
        filepath = str(Path(filepath).resolve())
        is_test = _is_test_file(filepath)
        try:
            fn_results, file_result, cls_results = analyze_file(filepath, is_test, all_classes)
        except Exception:  # noqa: BLE001 — skip unreadable/unparseable files silently
            continue
        live.add(_baseline_key(filepath, "__file__"))
        for r in fn_results:
            live.add(_baseline_key(r.filepath, r.name, r.lineno))
        for c in cls_results:
            live.add(_baseline_key(c.filepath, c.name, c.lineno))
    return live


def gc_baseline(filepaths: list[str], baseline_path: str) -> int:
    """Remove stale baseline entries whose symbol no longer exists in code.

    Scans ``filepaths`` to build the set of live keys, then removes any
    baseline entry not in that set.  Returns the count of removed entries.

    Stale entries arise when a function is renamed, moved, or deleted —
    the old ``<file>::<name>@<lineno>`` key is left behind.

    STANDING DECISION (Car 7 allowlist-debt audit, 2026-08-13): ``--gc``
    stays opt-in / manually-invoked. Measured that day: 1628 of 3334
    ``.complexity-baseline.json`` entries (48.8%) are stale (no matching
    live symbol). This is NOT dangerous — a stale key just means that
    symbol is invisible to the baseline ratchet, so it falls through to
    FULL enforcement (the strictest path, never the loosest; see
    ``main()`` below). Wiring ``--gc`` into pre-commit was deliberately
    rejected when the flag was added (docs/plans/archive/PLAN_V5_49_5.md
    §8: "the flag is opt-in; pre-commit hook doesn't auto-`--gc`" —
    mitigating the risk of silently removing real entries on a bad scan).
    That decision still holds. The 48.8% stale rate is accepted cost, not
    an oversight: pay it down by running
    ``python scripts/check_complexity.py --gc --all-files`` by hand when
    someone wants a clean baseline, not automatically.
    """
    baseline = load_baseline(baseline_path)
    if not baseline:
        return 0
    live_keys = _collect_live_keys(filepaths)
    stale = [k for k in baseline if k not in live_keys]
    for k in stale:
        del baseline[k]
    if stale:
        p = Path(baseline_path)
        p.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return len(stale)


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
        "--gc",
        action="store_true",
        help=(
            "Remove stale baseline entries whose <symbol>@<line> no longer exists in current code. "
            "Combine with --update-baseline to GC then refresh in one pass."
        ),
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
        # Scan production code only: yadgar/ (excluding yadgar/tests/).
        # scripts/ is excluded — one-off tooling, not production.
        # Matches the I30 production-only scope in check_complexity_allowlist.py.
        filenames = [
            str(p)
            for p in (repo_root / "yadgar").rglob("*.py")
            if "__pycache__" not in str(p) and ".venv" not in str(p) and "/tests/" not in str(p)
        ]
    else:
        filenames = args.filenames

    if args.gc:
        removed = gc_baseline(filenames, baseline_path)
        print(f"GC: removed {removed} stale baseline entries from {baseline_path}", file=sys.stderr)
        if not args.update_baseline:
            sys.exit(0)

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
