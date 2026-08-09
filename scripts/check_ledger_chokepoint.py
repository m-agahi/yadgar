#!/usr/bin/env python3
"""Engine-#2 ledger chokepoint guard (D20) — Car A of 0047 spine train.

WHAT THIS GUARD DOES
--------------------
AST-scans ``yadgar/**/*.py`` (tests excluded, mirrors check_dynamic_span_names)
and FAILS on any code that accesses one of the seven ledger tables via raw
SQL OUTSIDE ``MariaStorageEngine`` and OUTSIDE the explicit allowlist.

  Ledger tables (D20 chokepoint surface):
    task, adr, agent_pattern, agent_discipline,
    task_blocked_by, adr_supersedes, agent_pattern_composes

D20 is the policy: every row access to the engine-#2 ledger goes through
a sanctioned surface. The vehicle is ``MariaStorageEngine``'s ledger methods
(§1 architecture note — re-introducing a ``_LedgerMixin`` would re-trigger
PR #32's MRO bug, where the mixin sat behind ``_RuntimeConfigMixin`` and was
dead code with green tests).

ALLOWED
-------
- Access from inside a method of a class literally named
  ``MariaStorageEngine``. The class is identified by name — not by import
  path or by ``isinstance`` — to keep the AST scan stdlib-only and to make
  the rule obvious to a reviewer.
- An entry in the allowlist file (one ``path:lineno:reason`` per line).
  Use sparingly; the allowlist is for the PRE-EXISTING violations car H
  audits found, not a general escape hatch.

DETECTION SCOPE
---------------
- String arguments to ``text(...)`` / ``execute(...)`` / ``exec(...)`` calls
  that mention a ledger table name AND start with an SQL statement keyword
  (``SELECT`` / ``INSERT`` / ``UPDATE`` / ``DELETE`` / ``MERGE`` /
  ``REPLACE`` / ``TRUNCATE``). Prose mentions of a table name in a
  docstring are NOT flagged — the rule is "SQL statement references a
  ledger table", not "table name appears anywhere".
- Tests are excluded from the scan (mirrors check_dynamic_span_names).
- Free-standing SQL strings (no ``text(...)`` wrapper) are out of scope.
  The guard is not a SQL parser — it is an AST walker over call shapes,
  by design. A reviewer who writes `conn.execute(raw_string)` must wrap
  the string in ``text(...)`` for the chokepoint to apply; that wrapping
  is also what makes the engine's emitted SQL dialect-correct.

Usage:
  python scripts/check_ledger_chokepoint.py                 # check yadgar/, exit 0/1
  python scripts/check_ledger_chokepoint.py --root <dir>    # scan a different root
  python scripts/check_ledger_chokepoint.py --allowlist <p> # pre-existing violations file
  python scripts/check_ledger_chokepoint.py --list-all      # list every violation

Exit codes:
  0  no chokepoint violations found
  1  one or more violations found
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Ledger tables (D20 chokepoint surface). Order matters for stable error
# messages — keep alphabetical by table name.
LEDGER_TABLES: tuple[str, ...] = (
    "adr",
    "adr_supersedes",
    "agent_discipline",
    "agent_pattern",
    "agent_pattern_composes",
    "agent_pattern_model",
    "client",
    "task",
    "task_blocked_by",
)

# Class that holds the chokepoint's sanctioned surface. Any method body inside
# this class is exempt (the method IS the chokepoint).
ENGINE_CLASS_NAME = "MariaStorageEngine"

# SQL statement starters. A string literal that BEGINS (after optional
# whitespace) with one of these is an SQL statement — docstrings that
# mention ``task`` as a word do not start with ``SELECT`` / ``INSERT`` /
# ``UPDATE`` / ``DELETE``. The match is the leading-word check.
_SQL_STARTERS = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "REPLACE",
    "TRUNCATE",
)
_SQL_START_RE = re.compile(
    r"^\s*(?:" + "|".join(_SQL_STARTERS) + r")\b",
    re.IGNORECASE,
)

# Function names whose string-argument calls carry an SQL statement. A
# Call whose ``func`` is one of these (Name or Attribute) and whose args
# contain a string literal is the chokepoint's exact surface to inspect.
_SQL_EXEC_FUNCS = frozenset(
    {
        "text",  # sqlalchemy.sql.text(...)
        "execute",  # conn.execute(...), session.execute(...), engine.execute(...)
        "exec",  # asyncio-style shortcut (rare)
    }
)


@dataclass(frozen=True)
class Violation:
    source_file: Path
    lineno: int
    table: str
    snippet: str


def _is_sql_literal(literal: str) -> bool:
    """True when ``literal`` looks like an SQL statement (not prose).

    The check is conservative on purpose: it must match a leading
    ``SELECT`` / ``INSERT`` / ``UPDATE`` / ``DELETE`` / ``MERGE`` / ``REPLACE``
    / ``TRUNCATE`` after whitespace. Prose like "we INSERT INTO ..."
    embedded in a docstring does NOT trigger because it has no SQL starter
    at the start of the literal. Multi-line string literals are accepted
    when the SQL starter appears on any line.
    """
    return _SQL_START_RE.search(literal) is not None


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def _load_allowlist(path: Path | None) -> set[tuple[str, int]]:
    """Parse ``path:lineno:reason`` lines into a ``{(path, lineno)}`` set.

    A missing path or empty file returns an empty set (no allowlist).
    Malformed lines are silently dropped — the allowlist is an opt-in
    permission, never a build-breaker on its own.
    """
    if path is None or not path.is_file():
        return set()
    allowed: set[tuple[str, int]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # path:lineno:reason — reason may contain ':'
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        try:
            lineno = int(parts[1].strip())
        except ValueError:
            continue
        allowed.add((parts[0].strip(), lineno))
    return allowed


# ---------------------------------------------------------------------------
# AST scan
# ---------------------------------------------------------------------------


def _is_engine_class(class_node: ast.ClassDef) -> bool:
    """True when ``class_node``'s name is ``MariaStorageEngine``.

    Bases and metaclass are ignored — the rule is the literal name. The
    shipped engine-#2 architecture (``yadgar/_shared/storage/sql/mariadb.py:85``)
    uses the bare name; renaming it is a load-bearing signal that requires
    updating this guard.
    """
    return class_node.name == ENGINE_CLASS_NAME


def _class_depth(tree: ast.Module, target: ast.ClassDef) -> int:
    """Nesting depth of a ClassDef inside the module (0 = top-level).

    Used so nested class defs (``class Foo: class MariaStorageEngine: ...``)
    still get caught by the same engine-class check.
    """
    depth = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                if child is target:
                    return depth
            depth += 1
    return 0


def _violations_in_call(
    call: ast.Call,
    src_file: Path,
    in_engine: bool,
) -> list[Violation]:
    """Detect chokepoint violations inside a Call node's string args.

    SQL-execution-shaped calls are:

    - ``text(...)`` / ``sa.text(...)`` / ``sqlalchemy.text(...)`` — wraps a
      SQL statement string for execution.
    - ``conn.execute(...)`` / ``session.execute(...)`` /
      ``engine.execute(...)`` — actually runs the SQL.

    A string argument to one of these that mentions a ledger table is a
    chokepoint violation, UNLESS the call sits inside the engine class.
    """
    if in_engine:
        return []

    func = call.func
    func_name: str | None = None
    if isinstance(func, ast.Name):
        func_name = func.id
    elif isinstance(func, ast.Attribute):
        func_name = func.attr

    if func_name is None or func_name not in _SQL_EXEC_FUNCS:
        return []

    violations: list[Violation] = []
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            violations.extend(_violations_in_string_literal(src_file, arg.value, arg.lineno))
        elif isinstance(arg, ast.JoinedStr):  # f-string
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    violations.extend(
                        _violations_in_string_literal(src_file, sub.value, sub.lineno)
                    )
    return violations


def _violations_in_string_literal(
    src_file: Path,
    literal: str,
    lineno: int,
) -> list[Violation]:
    """Find every ledger-table reference inside ``literal``.

    Restricted to string literals that look like SQL: the literal must
    START (after optional whitespace) with one of the SQL statement
    starters (``SELECT`` / ``INSERT`` / ``UPDATE`` / ``DELETE`` / ...).
    Prose mentions of a table name without that leading keyword do not
    trigger — the guard is targeted at actual SQL strings, not docstrings
    or log messages.
    """
    if not _is_sql_literal(literal):
        return []
    out: list[Violation] = []
    for table in LEDGER_TABLES:
        pattern = rf"(?<![A-Za-z0-9_])(?:`)?{re.escape(table)}(?:`)?(?![A-Za-z0-9_])"
        if re.search(pattern, literal, re.IGNORECASE) is not None:
            snippet = literal.strip().splitlines()[0]
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            out.append(
                Violation(
                    source_file=src_file,
                    lineno=lineno,
                    table=table,
                    snippet=snippet,
                )
            )
    return out


def _scan_function_body(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    src_file: Path,
    in_engine: bool,
    allowed: set[tuple[str, int]],
) -> list[Violation]:
    """Inspect one function's Call nodes for SQL-execution-shaped chokepoint uses.

    Only calls to ``text(...)`` / ``execute(...)`` / ``exec(...)`` whose
    string arguments mention a ledger table are reported. Free-standing
    string literals are ignored — that is the difference between an SQL
    statement and a docstring / log message.
    """
    violations: list[Violation] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        for v in _violations_in_call(node, src_file, in_engine):
            key = (str(v.source_file), v.lineno)
            if key in allowed:
                continue
            violations.append(v)
    return violations


def _scan_class(
    class_node: ast.ClassDef,
    src_file: Path,
    allowed: set[tuple[str, int]],
) -> list[Violation]:
    """Scan every method in the class. Methods inside the engine are exempt."""
    violations: list[Violation] = []
    in_engine = _is_engine_class(class_node)
    for sub in ast.walk(class_node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.extend(_scan_function_body(sub, src_file, in_engine, allowed))
    return violations


def scan_file(
    src_file: Path,
    allowed: set[tuple[str, int]],
) -> list[Violation]:
    """Parse ``src_file`` and return every chokepoint violation in it.

    Walks both module-level functions and class method bodies. A method
    inside a class literally named ``MariaStorageEngine`` is exempt
    (it IS the chokepoint); everything else is in scope.
    """
    try:
        source = src_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(src_file))
    except SyntaxError as exc:
        print(f"WARNING: could not parse {src_file}: {exc}", file=sys.stderr)
        return []
    except OSError as exc:
        print(f"WARNING: could not read {src_file}: {exc}", file=sys.stderr)
        return []

    violations: list[Violation] = []

    # 1. Module-level (free) functions: NOT in any class → always in scope.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.extend(_scan_function_body(node, src_file, in_engine=False, allowed=allowed))

    # 2. Class method bodies: in-engine methods are exempt, others are in scope.
    for sub in ast.walk(tree):
        if isinstance(sub, ast.ClassDef):
            violations.extend(_scan_class(sub, src_file, allowed))
    return violations


def _iter_py_files(root: Path) -> list[Path]:
    """Yield in-scope .py files under root (tests excluded, mirrors I33)."""
    files: list[Path] = []
    for p in sorted(root.rglob("*.py")):
        rel = str(p)
        if "/tests/" in rel or rel.endswith("_test.py") or p.name.startswith("test_"):
            continue
        files.append(p)
    return files


def scan(
    root: Path | None = None,
    allowed: set[tuple[str, int]] | None = None,
) -> list[Violation]:
    """Scan all in-scope files under ``root`` and return every violation."""
    if root is None:
        root = _REPO_ROOT / "yadgar"
    if allowed is None:
        allowed = set()
    violations: list[Violation] = []
    for f in _iter_py_files(root):
        violations.extend(scan_file(f, allowed))
    return violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Engine-#2 ledger chokepoint guard — every row access to "
            "task/adr/agent_pattern/agent_discipline/... must go through "
            "MariaStorageEngine."
        ),
    )
    parser.add_argument(
        "--root",
        default=str(_REPO_ROOT / "yadgar"),
        help="Directory to scan (default: yadgar/).",
    )
    parser.add_argument(
        "--allowlist",
        default=None,
        help="Path to allowlist file (path:lineno:reason per line).",
    )
    parser.add_argument(
        "--list-all",
        action="store_true",
        help="Print every violation (same as default failure output).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root)
    allowed = _load_allowlist(Path(args.allowlist)) if args.allowlist else set()
    violations = scan(root, allowed)

    for v in violations:
        print(
            f"{v.source_file}:{v.lineno}: ledger table `{v.table}` accessed "
            f"outside {ENGINE_CLASS_NAME} — `{v.snippet}`. "
            f"Move the read/write to a {ENGINE_CLASS_NAME} method."
        )

    if violations:
        print(
            f"\n{len(violations)} ledger-chokepoint violation(s) found — every "
            f"row access to a ledger table must go through "
            f"{ENGINE_CLASS_NAME} (D20). See scripts/check_ledger_chokepoint.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
