#!/usr/bin/env python3
"""Coverage lint for docs/BEHAVIOR_CONTRACT.md (the behavior-contract self-check).

Three original rules, all derived from the contract's own header:

  1. A ✅ row MUST cite a resolvable ``path::node`` test reference. A ✅ without a
     resolvable reference is a rejected claim (belief-without-a-test).
  2. ANY row carrying a ``path::node`` reference (✅ OR a ⏳ with a [r]/[u] tag that
     names a test) MUST resolve to a real test. The tag itself is NOT mandatory —
     this is *validate-if-present*: a dangling reference is a failure, an
     un-referenced [r]/[u] row is fine (it just means "coverage exists, not yet
     wired to a contract e2e"). This extends rule 1 beyond ✅.
  3. The header counts (✅/⏳/❌ and, over the ⏳ rows, [r]/[u]/none) MUST equal the
     actual tally over all BC-* rows. Header drift is a failure — this is the
     finding (header math drifted from content) that v5.71 fixed, made permanent.

Resolution: a reference is ``relative/path.py::Node[::SubNode]``. The path is
resolved relative to the repo root, with a ``yadgar/`` prefix tried as a fallback
(the contract historically cites ``tests/e2e/...`` while the tree is
``yadgar/tests/e2e/...``). A reference resolves if the file exists AND every
``::``-segment after the path appears as a ``class``/``def`` name in that file.

Tamper-protection extensions (task #52):

  4. LAYER 1 — ✅-count floor: the tally of ✅ rows MUST NOT drop below
     ``_GREEN_FLOOR``.  Raising the floor is an explicit committed edit.  This
     catches silent un-verification (✅→⏳/❌) without a floor bump.

  5. LAYER 2 — ✅ ↔ decorator integrity: every node cited by a ✅ row (and its
     enclosing class if the node is a method) MUST NOT carry a skip/skipif/xfail
     decorator.  Catches the pattern "flip ✅ but the mapped test is xfail/skipped."

Run as a plain (non-e2e) pytest: ``yadgar/tests/test_contract_coverage.py``.
Or standalone: ``python scripts/check_contract_coverage.py`` (exit 0 = clean).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT = _REPO_ROOT / "docs" / "BEHAVIOR_CONTRACT.md"

_STATUSES = ("✅", "❌", "⏳")
# A `path.py::Node[::Sub]` reference inside backticks or bare.
_REF_RE = re.compile(r"([A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_]+)+)")
# Header count lines we assert against (rule 3).
_STATUS_HDR_RE = re.compile(r"\*\*([0-9]+)\s*✅\s*·\s*([0-9]+)\s*⏳\s*·\s*([0-9]+)\s*❌\.\*\*")
_TAG_HDR_RE = re.compile(r"\*\*([0-9]+)\s*`\[r\]`.*?·\s*([0-9]+)\s*`\[u\]`.*?·\s*([0-9]+)\s*none")

# ---------------------------------------------------------------------------
# Layer 1 — ✅-count floor (tamper-protection #52).
# Raise this constant in an explicit commit whenever green count legitimately grows.
# ---------------------------------------------------------------------------
_GREEN_FLOOR = 13

# Decorator names that indicate a test is deliberately disabled.
_SKIP_DECORATORS = frozenset({"skip", "skipif", "xfail"})


# ---------------------------------------------------------------------------
# Parse rows
# ---------------------------------------------------------------------------
def _row_status(line: str) -> str | None:
    """First status emoji on the line, or None."""
    for emoji in _STATUSES:
        if emoji in line:
            return emoji
    return None


def parse_rows(text: str) -> list[tuple[str, str, str]]:
    """Return (bc_id, status, line) for every BC-* row (list-item OR table)."""
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        m = re.match(r"^- (BC-[A-Za-z0-9]+)\b", line)
        if not m:
            m = re.match(r"^\|\s*(BC-T\d+)\s*\|", line)
        if not m:
            continue
        status = _row_status(line)
        if status is None:
            continue
        rows.append((m.group(1), status, line))
    return rows


def _row_tag(line: str) -> str:
    """Classify a row's coverage tag: 'r', 'u', or 'none'."""
    if "[r]" in line or "[ci+r]" in line:
        return "r"
    if "[u]" in line:
        return "u"
    return "none"


# ---------------------------------------------------------------------------
# Resolve a `path::node` reference
# ---------------------------------------------------------------------------
def _candidate_paths(rel: str) -> list[Path]:
    return [_REPO_ROOT / rel, _REPO_ROOT / "yadgar" / rel]


def resolve_ref(ref: str) -> str | None:
    """Return None if the reference resolves, else an error string."""
    path_part, *nodes = ref.split("::")
    src = next((p for p in _candidate_paths(path_part) if p.is_file()), None)
    if src is None:
        return f"file not found: {path_part}"
    body = src.read_text(encoding="utf-8")
    for node in nodes:
        if not re.search(rf"\b(?:class|def)\s+{re.escape(node)}\b", body):
            return f"node {node!r} not found in {path_part}"
    return None


# ---------------------------------------------------------------------------
# Layer 2 helpers — decorator integrity
# ---------------------------------------------------------------------------
def _decorator_name(node: ast.expr) -> str | None:
    """Extract the bare attribute/name from a decorator expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


def _node_has_skip_decorator(node: ast.AST) -> bool:
    """Return True if a class/function node carries a skip/skipif/xfail decorator."""
    decorators = getattr(node, "decorator_list", [])
    return any(_decorator_name(d) in _SKIP_DECORATORS for d in decorators)


def _build_ast_index(src: str) -> dict[str, ast.AST]:
    """Return name→AST-node for every top-level class and function in *src*."""
    tree = ast.parse(src)
    index: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            index[node.name] = node
    return index


def check_green_floor(text: str, floor: int = _GREEN_FLOOR) -> list[str]:
    """Layer 1: actual ✅ count must be >= *floor*. Returns violation strings."""
    rows = parse_rows(text)
    n_green = sum(1 for _, s, _ in rows if s == "✅")
    if n_green < floor:
        return [
            f"layer 1 — ✅-count floor violated: {n_green} ✅ rows found, "
            f"floor is {floor}. "
            "Raise _GREEN_FLOOR in scripts/check_contract_coverage.py only when "
            "new e2e-green tests justify it; never lower it silently."
        ]
    return []


def check_green_integrity(text: str, repo_root: Path = _REPO_ROOT) -> list[str]:
    """Layer 2: every node cited by a ✅ row must not carry skip/skipif/xfail.

    Checks the specific node named in the reference AND its enclosing class (if
    the reference is ``File.py::Class::method``).  A skip/xfail on *any* level
    means the test is not actually running.
    """
    errors: list[str] = []
    rows = parse_rows(text)

    # Cache parsed AST indexes per file path to avoid re-parsing.
    _ast_cache: dict[Path, dict[str, ast.AST]] = {}

    def _get_index(path: Path) -> dict[str, ast.AST]:
        if path not in _ast_cache:
            try:
                src = path.read_text(encoding="utf-8")
                _ast_cache[path] = _build_ast_index(src)
            except SyntaxError:
                _ast_cache[path] = {}
        return _ast_cache[path]

    def _candidate_paths_root(rel: str) -> list[Path]:
        return [repo_root / rel, repo_root / "yadgar" / rel]

    for bc_id, status, line in rows:
        if status != "✅":
            continue
        refs = _REF_RE.findall(line)
        for ref in refs:
            parts = ref.split("::")
            path_part = parts[0]
            nodes = parts[1:]
            src_path = next((p for p in _candidate_paths_root(path_part) if p.is_file()), None)
            if src_path is None:
                # File resolution failure already reported by rule 2; skip here.
                continue
            index = _get_index(src_path)
            # Walk nodes left-to-right, checking each (class, then method).
            for node_name in nodes:
                ast_node = index.get(node_name)
                if ast_node is None:
                    # Node not found is a rule-2 issue; skip here.
                    continue
                if _node_has_skip_decorator(ast_node):
                    errors.append(
                        f"{bc_id}: ✅ cites {ref!r} but node {node_name!r} "
                        f"carries a skip/skipif/xfail decorator — "
                        "test is not actually running (layer 2)"
                    )
    return errors


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check(text: str) -> list[str]:
    """Return a list of violation strings (empty = clean)."""
    errors: list[str] = []
    rows = parse_rows(text)

    # Rules 1 + 2 — reference resolution.
    for bc_id, status, line in rows:
        refs = _REF_RE.findall(line)
        if status == "✅" and not refs:
            errors.append(f"{bc_id}: ✅ without a `path::node` test reference (rule 1)")
        for ref in refs:
            err = resolve_ref(ref)
            if err:
                errors.append(f"{bc_id}: dangling reference {ref!r} — {err} (rule 2)")

    # Rule 3 — header counts == actual tally.
    n_green = sum(1 for _, s, _ in rows if s == "✅")
    n_pending = sum(1 for _, s, _ in rows if s == "⏳")
    n_broken = sum(1 for _, s, _ in rows if s == "❌")
    tag = {"r": 0, "u": 0, "none": 0}
    for _, s, line in rows:
        if s == "⏳":
            tag[_row_tag(line)] += 1

    sm = _STATUS_HDR_RE.search(text)
    if not sm:
        errors.append("header: status count line (`N ✅ · N ⏳ · N ❌`) not found (rule 3)")
    else:
        hg, hp, hb = (int(x) for x in sm.groups())
        if (hg, hp, hb) != (n_green, n_pending, n_broken):
            errors.append(
                f"header status drift (rule 3): header says {hg} ✅ · {hp} ⏳ · {hb} ❌; "
                f"actual {n_green} ✅ · {n_pending} ⏳ · {n_broken} ❌"
            )
    tm = _TAG_HDR_RE.search(text)
    if not tm:
        errors.append("header: tag count line (`N [r] · N [u] · N none`) not found (rule 3)")
    else:
        hr, hu, hn = (int(x) for x in tm.groups())
        if (hr, hu, hn) != (tag["r"], tag["u"], tag["none"]):
            errors.append(
                f"header tag drift (rule 3): header says {hr} [r] · {hu} [u] · {hn} none; "
                f"actual {tag['r']} [r] · {tag['u']} [u] · {tag['none']} none"
            )

    # Layer 2 (tamper-protection). Layer 1 (the ✅-count floor) applies to the
    # REAL contract only — enforced in main()/CLI/pre-commit, NOT on arbitrary
    # check(text) inputs (e.g. minimal test fixtures with fewer than floor ✅ rows).
    errors.extend(check_green_integrity(text))

    return errors


def main() -> int:
    if not _CONTRACT.is_file():
        print(f"ERROR: contract not found at {_CONTRACT}", file=sys.stderr)
        return 1
    text = _CONTRACT.read_text(encoding="utf-8")
    errors = check(text)
    errors.extend(check_green_floor(text))  # Layer 1: floor on the REAL contract
    if errors:
        print("BEHAVIOR_CONTRACT coverage lint FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("BEHAVIOR_CONTRACT coverage lint OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
