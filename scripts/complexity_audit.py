#!/usr/bin/env python3
"""
Complexity audit script for I13 caps.
Computes per-function: cyclomatic complexity, LOC, param count, max nesting.
Computes per-file: LOC, public symbols.
Computes per-class: method count, instance attrs, inheritance depth.

Test files (yadgar/tests/) are exempt from LOC + params caps but NOT
from cyclomatic or nesting caps.

Usage:
    python scripts/complexity_audit.py [--json] [--root ROOTDIR]
"""

import argparse
import ast
import json
import os
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# I13 caps
# ---------------------------------------------------------------------------
FN_CYCLO_HARD = 15
FN_CYCLO_SOFT = 10
FN_LOC_HARD = 150
FN_LOC_SOFT = 80
FN_PARAMS_HARD = 8
FN_PARAMS_SOFT = 5
FN_NESTING_HARD = 4

FILE_LOC_HARD = 1000
FILE_LOC_SOFT = 500
FILE_SYMBOLS_SOFT = 30

CLASS_METHODS_SOFT = 30
CLASS_ATTRS_SOFT = 15
CLASS_DEPTH_HARD = 3


# ---------------------------------------------------------------------------
# Cyclomatic complexity — count branch-introducing nodes
# ---------------------------------------------------------------------------
BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.AsyncWith,
    ast.AsyncFor,
    ast.Assert,
    ast.comprehension,
)

BOOL_OPS = (ast.And, ast.Or)


def cyclomatic(node: ast.FunctionDef) -> int:
    """McCabe cyclomatic complexity: 1 + number of branches."""
    count = 1
    for child in ast.walk(node):
        if isinstance(child, BRANCH_NODES):
            count += 1
        elif isinstance(child, ast.BoolOp):
            # each additional operand = one branch
            count += len(child.values) - 1
        elif isinstance(child, ast.IfExp):
            count += 1
        elif isinstance(child, (ast.Match,)):
            # match statement: each case is a branch
            pass  # handled via ast.match_case below
        elif hasattr(ast, "match_case") and isinstance(child, ast.match_case):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Parameter count
# ---------------------------------------------------------------------------
def param_count(node: ast.FunctionDef) -> int:
    args = node.args
    n = len(args.args) + len(args.kwonlyargs) + len(args.posonlyargs)
    if args.vararg:
        n += 1
    if args.kwarg:
        n += 1
    return n


# ---------------------------------------------------------------------------
# LOC — end_lineno - lineno + 1 (includes decorators? no — use body start)
# ---------------------------------------------------------------------------
def fn_loc(node: ast.FunctionDef) -> int:
    """Lines from def line to end_lineno inclusive."""
    return node.end_lineno - node.lineno + 1


# ---------------------------------------------------------------------------
# Nesting depth — max nesting of control-flow inside a function
# ---------------------------------------------------------------------------
NESTING_NODES = (
    ast.If,
    ast.For,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.AsyncFor,
    ast.Try,
    ast.ExceptHandler,
)

if hasattr(ast, "TryStar"):
    NESTING_NODES = NESTING_NODES + (ast.TryStar,)

if hasattr(ast, "match_case"):
    NESTING_NODES = NESTING_NODES + (ast.Match,)


def _max_nesting(node: ast.AST, current: int) -> int:
    max_depth = current
    for child in ast.iter_child_nodes(node):
        if isinstance(child, NESTING_NODES):
            depth = _max_nesting(child, current + 1)
        else:
            depth = _max_nesting(child, current)
        if depth > max_depth:
            max_depth = depth
    return max_depth


def nesting_depth(fn_node: ast.FunctionDef) -> int:
    """Max nesting depth inside function body (0 = flat)."""
    return _max_nesting(fn_node, 0)


# ---------------------------------------------------------------------------
# Class metrics
# ---------------------------------------------------------------------------
def class_metrics(cls_node: ast.ClassDef, depth: int) -> dict:
    methods = [
        n
        for n in ast.walk(cls_node)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.col_offset == cls_node.col_offset + 4
    ]
    # instance attrs: assignments to self.X in __init__
    attrs: set[str] = set()
    for node in ast.walk(cls_node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Assign):
                    for t in stmt.targets:
                        if (
                            isinstance(t, ast.Attribute)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "self"
                        ):
                            attrs.add(t.attr)
                elif isinstance(stmt, ast.AnnAssign):
                    if (
                        isinstance(stmt.target, ast.Attribute)
                        and isinstance(stmt.target.value, ast.Name)
                        and stmt.target.value.id == "self"
                    ):
                        attrs.add(stmt.target.attr)
    return {
        "methods": len(methods),
        "attrs": len(attrs),
        "depth": depth,
    }


# ---------------------------------------------------------------------------
# Inheritance depth
# ---------------------------------------------------------------------------
def inheritance_depth(
    cls_node: ast.ClassDef, all_classes: dict[str, ast.ClassDef], visited: set | None = None
) -> int:
    if visited is None:
        visited = set()
    if cls_node.name in visited:
        return 0
    visited.add(cls_node.name)
    if not cls_node.bases:
        return 0
    max_d = 0
    for base in cls_node.bases:
        base_name = None
        if isinstance(base, ast.Name):
            base_name = base.id
        elif isinstance(base, ast.Attribute):
            base_name = base.attr
        if base_name and base_name in all_classes:
            d = 1 + inheritance_depth(all_classes[base_name], all_classes, visited.copy())
            if d > max_d:
                max_d = d
    return max_d


# ---------------------------------------------------------------------------
# Public symbols
# ---------------------------------------------------------------------------
def count_public_symbols(tree: ast.Module) -> int:
    count = 0
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                count += 1
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and not t.id.startswith("_"):
                    count += 1
    return count


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------
@dataclass
class FunctionResult:
    filepath: str
    lineno: int
    name: str
    cyclo: int
    loc: int
    params: int
    nesting: int
    is_test: bool
    cyclo_hard: bool = False
    cyclo_soft: bool = False
    loc_hard: bool = False
    loc_soft: bool = False
    params_hard: bool = False
    params_soft: bool = False
    nesting_hard: bool = False

    def has_hard_violation(self) -> bool:
        return self.cyclo_hard or self.loc_hard or self.params_hard or self.nesting_hard

    def has_soft_violation(self) -> bool:
        return (
            self.cyclo_soft or self.loc_soft or self.params_soft
        ) and not self.has_hard_violation()

    def any_violation(self) -> bool:
        return (
            self.cyclo_soft
            or self.cyclo_hard
            or self.loc_soft
            or self.loc_hard
            or self.params_soft
            or self.params_hard
            or self.nesting_hard
        )


@dataclass
class FileResult:
    filepath: str
    loc: int
    public_symbols: int
    is_test: bool
    loc_hard: bool = False
    loc_soft: bool = False
    symbols_soft: bool = False


@dataclass
class ClassResult:
    filepath: str
    lineno: int
    name: str
    methods: int
    attrs: int
    inh_depth: int
    methods_soft: bool = False
    attrs_soft: bool = False
    depth_hard: bool = False


def analyze_file(
    filepath: str, is_test: bool, all_classes: dict
) -> tuple[list[FunctionResult], FileResult, list[ClassResult]]:
    with open(filepath, encoding="utf-8", errors="replace") as f:
        source = f.read()
    lines = source.splitlines()
    file_loc = len(lines)

    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        fr = FileResult(filepath=filepath, loc=file_loc, public_symbols=0, is_test=is_test)
        return [], fr, []

    pub_syms = count_public_symbols(tree)
    file_result = FileResult(
        filepath=filepath,
        loc=file_loc,
        public_symbols=pub_syms,
        is_test=is_test,
        loc_hard=not is_test and file_loc > FILE_LOC_HARD,
        loc_soft=not is_test and FILE_LOC_SOFT < file_loc <= FILE_LOC_HARD,
        symbols_soft=pub_syms > FILE_SYMBOLS_SOFT,
    )

    fn_results = []
    # Collect all function defs (top-level and nested within classes, but not doubly-nested)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        cyc = cyclomatic(node)
        loc = fn_loc(node)
        pc = param_count(node)
        nd = nesting_depth(node)

        r = FunctionResult(
            filepath=filepath,
            lineno=node.lineno,
            name=node.name,
            cyclo=cyc,
            loc=loc,
            params=pc,
            nesting=nd,
            is_test=is_test,
        )
        r.cyclo_hard = cyc > FN_CYCLO_HARD
        r.cyclo_soft = FN_CYCLO_SOFT < cyc <= FN_CYCLO_HARD
        if not is_test:
            r.loc_hard = loc > FN_LOC_HARD
            r.loc_soft = FN_LOC_SOFT < loc <= FN_LOC_HARD
            r.params_hard = pc > FN_PARAMS_HARD
            r.params_soft = FN_PARAMS_SOFT < pc <= FN_PARAMS_HARD
        r.nesting_hard = nd > FN_NESTING_HARD
        fn_results.append(r)

    # Class metrics
    cls_results = []
    # Collect local classes for depth calculation
    local_classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    merged = {**all_classes, **local_classes}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        cm = class_metrics(node, 0)
        inh_d = inheritance_depth(node, merged)
        cr = ClassResult(
            filepath=filepath,
            lineno=node.lineno,
            name=node.name,
            methods=cm["methods"],
            attrs=cm["attrs"],
            inh_depth=inh_d,
            methods_soft=cm["methods"] > CLASS_METHODS_SOFT,
            attrs_soft=cm["attrs"] > CLASS_ATTRS_SOFT,
            depth_hard=inh_d > CLASS_DEPTH_HARD,
        )
        cls_results.append(cr)

    return fn_results, file_result, cls_results


def assess_risk(r: FunctionResult) -> str:
    """Decomposition risk per I5."""
    # Functions known to cross async/thread/queue boundaries
    async_boundary_patterns = [
        "memorize",
        "recall",
        "_drain",
        "drain",
        "_apply",
        "apply",
        "_run",
        "run_",
        "handle_",
        "_handle",
        "process_",
        "_process",
        "encode",
        "_encode",
        "embed",
        "_embed",
    ]
    high_risk_files = [
        "memorize.py",
        "apply.py",
        "drainer",
        "consolidation",
        "embeddings.py",
        "retrieval/core.py",
        "conflict_resolver.py",
        "sleep_compute",
        "consolidation/orchestrator.py",
    ]
    fname = r.name.lower()
    fpath = r.filepath.lower()

    is_async_boundary = any(p in fname for p in async_boundary_patterns)
    is_high_risk_file = any(p in fpath for p in high_risk_files)

    # High: file/function in async boundary or complex shared state zone
    if is_async_boundary or is_high_risk_file:
        if r.cyclo >= FN_CYCLO_HARD or r.loc > FN_LOC_HARD:
            return "HIGH"
        return "HIGH"

    # Medium: multi-step pipelines
    if r.cyclo > 8 or r.loc > 60:
        return "MEDIUM"

    return "LOW"


def proposed_action(r: FunctionResult, risk: str) -> str:
    if not r.any_violation():
        return "-"
    # Known cohesive flows
    cohesive_patterns = [
        "_run_check_invariants",
        "check_invariants",
        "_validate",
        "validate_",
        "_handle_error",
        "format_",
        "_format",
    ]
    fname = r.name.lower()
    if any(p in fname for p in cohesive_patterns):
        return "justify-cohesion (noqa)"

    if risk == "HIGH":
        if r.has_hard_violation():
            return "decompose-with-topology-proof"
        return "defer"
    if risk == "MEDIUM":
        if r.has_hard_violation():
            return "decompose-with-topology-proof"
        return "decompose-low-risk"
    # LOW
    return "decompose-low-risk"


def sort_key(r: FunctionResult):
    hard = 1 if r.has_hard_violation() else 0
    return (-hard, -r.cyclo, -r.loc)


def run_audit(root: str) -> dict:
    root_path = Path(root)
    all_classes: dict[str, ast.ClassDef] = {}

    fn_results: list[FunctionResult] = []
    file_results: list[FileResult] = []
    cls_results: list[ClassResult] = []

    py_files = []
    for fp in root_path.rglob("*.py"):
        if "__pycache__" in str(fp) or ".venv" in str(fp) or "result/" in str(fp):
            continue
        py_files.append(fp)

    # First pass: collect all class names for depth analysis
    for fp in py_files:
        try:
            source = fp.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    all_classes[node.name] = node
        except SyntaxError:
            pass

    for fp in sorted(py_files):
        rel = str(fp.relative_to(root_path))
        is_test = rel.startswith("yadgar/tests/") or rel.startswith("tests/")
        fns, fr, cls = analyze_file(str(fp), is_test, all_classes)
        fn_results.extend(fns)
        file_results.append(fr)
        cls_results.extend(cls)

    return {
        "functions": fn_results,
        "files": file_results,
        "classes": cls_results,
    }


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------
def severity(r: FunctionResult) -> str:
    if r.has_hard_violation():
        return "HARD"
    if r.any_violation():
        return "soft"
    return "-"


def cap_detail(r: FunctionResult) -> list[str]:
    flags = []
    if r.cyclo_hard:
        flags.append(f"cyclo={r.cyclo}>15")
    elif r.cyclo_soft:
        flags.append(f"cyclo={r.cyclo}>10")
    if r.loc_hard:
        flags.append(f"LOC={r.loc}>150")
    elif r.loc_soft:
        flags.append(f"LOC={r.loc}>80")
    if r.params_hard:
        flags.append(f"params={r.params}>8")
    elif r.params_soft:
        flags.append(f"params={r.params}>5")
    if r.nesting_hard:
        flags.append(f"nesting={r.nesting}>4")
    return flags


def generate_markdown(results: dict, root: str) -> str:
    fn_results: list[FunctionResult] = results["functions"]
    file_results: list[FileResult] = results["files"]
    cls_results: list[ClassResult] = results["classes"]

    # Non-test functions for most stats
    non_test_fns = [r for r in fn_results if not r.is_test]
    test_fns = [r for r in fn_results if r.is_test]

    # Violations
    hard_violations = [r for r in fn_results if r.has_hard_violation()]
    soft_only = [r for r in fn_results if r.has_soft_violation()]
    any_viol = [r for r in fn_results if r.any_violation()]

    # Sort violations: hard first, then by cyclomatic desc
    hard_violations_sorted = sorted(hard_violations, key=sort_key)
    # All violating functions for the main table
    violating_sorted = sorted(any_viol, key=sort_key)

    total_fns = len(fn_results)
    # Soft violation % = functions with ANY soft violation / total_audited
    # "soft violation" = any cap exceeded (soft or hard threshold)
    soft_viol_pct = 100.0 * len(any_viol) / total_fns if total_fns else 0

    file_hard = [f for f in file_results if f.loc_hard]
    file_soft = [f for f in file_results if f.loc_soft]
    [f for f in file_results if f.symbols_soft]

    cls_violations = [c for c in cls_results if c.methods_soft or c.attrs_soft or c.depth_hard]

    low_risk_decomp = [r for r in any_viol if assess_risk(r) == "LOW"]
    n_bundles = (len(low_risk_decomp) + 4) // 5  # ceil div 5

    threshold_tripped = soft_viol_pct > 20.0

    lines = []
    lines.append("# Complexity Audit — yadgar v5.4.2 P12")
    lines.append("")
    lines.append(
        "**Invariants:** I13 (bounded complexity caps) + I5 (no topology-breaking decomp)."
    )
    lines.append("**Source of truth:** `docs/contracts/ARCHITECTURE_INVARIANTS.md`.")
    lines.append(
        "**Scope:** static analysis only. No runtime data. Catalog only — no decompositions performed."
    )
    lines.append("")

    # Critical flag
    if threshold_tripped:
        lines.append("---")
        lines.append("")
        lines.append(
            "## CRITICAL: >20% Soft-Cap Violation Rate — Review I13 Caps Before Shipping Enforcement"
        )
        lines.append("")
        lines.append(
            f"**{soft_viol_pct:.1f}%** of audited functions ({len(any_viol)}/{total_fns}) exceed at least one soft cap."
        )
        lines.append("")
        lines.append(
            "Per v5.4 exit criteria (docs/plans/archive/PLAN_V5_4_to_v7.md): if >20% of functions violate soft caps,"
        )
        lines.append(
            "the I13 cap numbers need review per I10 BEFORE shipping pre-commit enforcement"
        )
        lines.append("(ruff C901 + custom check-complexity hook). Specifically:")
        lines.append("- Cyclomatic soft cap (10) may be too aggressive for this codebase.")
        lines.append("- LOC soft cap (80) may be too tight for infrastructure/server code.")
        lines.append("- Run targeted analysis: separate test vs non-test violation rates.")
        lines.append("- Consider raising soft caps to cyclo≤12, LOC≤100 and re-auditing.")
        lines.append("- Do NOT ship the pre-commit hook until caps are ratified.")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"- **Total functions audited:** {total_fns} ({len(non_test_fns)} non-test, {len(test_fns)} test)"
    )
    lines.append(f"- **Hard violations:** {len(hard_violations)} functions")
    lines.append(f"- **Soft violations (any cap, no hard):** {len(soft_only)} functions")
    lines.append(
        f"- **Any cap exceeded:** {len(any_viol)} / {total_fns} = **{soft_viol_pct:.1f}%**"
    )
    lines.append(f"- **Files exceeding LOC hard cap (>1000):** {len(file_hard)}")
    lines.append(f"- **Files exceeding LOC soft cap (>500):** {len(file_soft)}")
    lines.append("")

    lines.append("### Top 10 Hard Violations (by cyclomatic)")
    lines.append("")
    lines.append("| file:line | function | cyclo | LOC | params | nesting |")
    lines.append("|---|---|---|---|---|---|")
    for r in hard_violations_sorted[:10]:
        rel = r.filepath.replace(root + "/", "").replace(root + os.sep, "")
        lines.append(
            f"| {rel}:{r.lineno} | `{r.name}` | {r.cyclo} | {r.loc} | {r.params} | {r.nesting} |"
        )
    lines.append("")

    if file_hard:
        lines.append("### Files Exceeding Hard LOC Cap (>1000 lines)")
        lines.append("")
        for f in sorted(file_hard, key=lambda x: -x.loc):
            rel = f.filepath.replace(root + "/", "").replace(root + os.sep, "")
            lines.append(f"- `{rel}` — {f.loc} lines")
        lines.append("")

    lines.append("### v5.5 Bundle Plan")
    lines.append("")
    lines.append(f"- LOW-risk decompositions identified: {len(low_risk_decomp)}")
    lines.append(f"- Recommended bundle: 5 functions per PR = **{n_bundles} PRs**")
    lines.append("- Each PR must include before/after test parity (per I13 enforcement spec).")
    lines.append("- HIGH-risk and topology-crossing functions: defer until P11 metrics available.")
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## Function Violations Table")
    lines.append("")
    lines.append("Sorted: HARD violations first, then by cyclomatic descending.")
    lines.append("Test files exempt from LOC + params caps; cyclomatic + nesting still enforced.")
    lines.append("")
    lines.append(
        "| file:line | function | cyclomatic | LOC | params | nesting | hard/soft | risk | proposed action |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")

    for r in violating_sorted:
        rel = r.filepath.replace(root + "/", "").replace(root + os.sep, "")
        flags = " ".join(cap_detail(r))
        sev = severity(r)
        risk = assess_risk(r)
        action = proposed_action(r, risk)
        test_tag = "[test] " if r.is_test else ""
        lines.append(
            f"| {rel}:{r.lineno} | `{test_tag}{r.name}` | {r.cyclo} | {r.loc} | {r.params} | {r.nesting} "
            f"| **{sev}** `{flags}` | {risk} | {action} |"
        )

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## File Metrics")
    lines.append("")
    lines.append("Non-test files only. Sorted by LOC descending.")
    lines.append("")
    lines.append("| file | LOC | public symbols | cap |")
    lines.append("|---|---|---|---|")
    file_results_sorted = sorted([f for f in file_results if not f.is_test], key=lambda x: -x.loc)
    for f in file_results_sorted:
        rel = f.filepath.replace(root + "/", "").replace(root + os.sep, "")
        cap_flags = []
        if f.loc_hard:
            cap_flags.append(f"LOC HARD>{FILE_LOC_HARD}")
        elif f.loc_soft:
            cap_flags.append(f"LOC soft>{FILE_LOC_SOFT}")
        if f.symbols_soft:
            cap_flags.append(f"symbols soft>{FILE_SYMBOLS_SOFT}")
        cap_str = ", ".join(cap_flags) if cap_flags else "-"
        lines.append(f"| `{rel}` | {f.loc} | {f.public_symbols} | {cap_str} |")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Class Violations")
    lines.append("")
    if cls_violations:
        lines.append("| file:line | class | methods | attrs | inh_depth | cap |")
        lines.append("|---|---|---|---|---|---|")
        for c in sorted(cls_violations, key=lambda x: (-x.methods, -x.inh_depth)):
            rel = c.filepath.replace(root + "/", "").replace(root + os.sep, "")
            caps = []
            if c.methods_soft:
                caps.append(f"methods={c.methods}>{CLASS_METHODS_SOFT}")
            if c.attrs_soft:
                caps.append(f"attrs={c.attrs}>{CLASS_ATTRS_SOFT}")
            if c.depth_hard:
                caps.append(f"depth={c.inh_depth}>{CLASS_DEPTH_HARD} HARD")
            lines.append(
                f"| {rel}:{c.lineno} | `{c.name}` | {c.methods} | {c.attrs} | {c.inh_depth} | {', '.join(caps)} |"
            )
    else:
        lines.append("No class cap violations found.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append(
        "- **Cyclomatic complexity:** McCabe (1 + branches). Branch nodes: `if`, `for`, `while`,"
    )
    lines.append(
        "  `ExceptHandler`, `with`, `assert`, `comprehension`, `BoolOp` operands, `IfExp`, `match_case`."
    )
    lines.append(
        "- **LOC:** `end_lineno - lineno + 1` from AST (includes docstrings, blank lines in body)."
    )
    lines.append(
        "- **Params:** `args + kwonlyargs + posonlyargs + (*args if present) + (**kwargs if present)`."
    )
    lines.append(
        "- **Nesting:** max depth of control-flow nodes (`if/for/while/with/try/ExceptHandler`) from body root."
    )
    lines.append(
        "- **Public symbols (file):** top-level defs/classes/assignments not prefixed with `_`."
    )
    lines.append("- **Instance attrs:** `self.X` assignments in `__init__` (direct body only).")
    lines.append("- **Inheritance depth:** recursive base-class walk within the same codebase.")
    lines.append(
        "- **Test exemption:** `yadgar/tests/` files exempt from LOC + params caps; cyclo + nesting enforced."
    )
    lines.append(
        "- **Risk classification:** HIGH = file/function crosses async/thread/queue boundary or"
    )
    lines.append(
        "  is in a known topology-sensitive module; MEDIUM = multi-step pipeline, single thread;"
    )
    lines.append("  LOW = mechanical independent branches.")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    results = run_audit(root)

    if args.json:
        # Serialize to JSON for inspection
        out = {
            "functions": [vars(r) for r in results["functions"]],
            "files": [vars(r) for r in results["files"]],
            "classes": [vars(r) for r in results["classes"]],
        }
        print(json.dumps(out, indent=2))
    else:
        md = generate_markdown(results, root)
        print(md)


if __name__ == "__main__":
    main()
