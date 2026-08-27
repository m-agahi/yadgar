#!/usr/bin/env python3
"""Engine-#2 ledger chokepoint guard (D20) — Car A of 0047 spine train.

WHAT THIS GUARD DOES
--------------------
AST-scans ``yadgar/**/*.py`` AND ``scripts/**/*.py`` (tests excluded, mirrors
check_dynamic_span_names) and FAILS on any code that accesses one of the seven
ledger tables via raw SQL OUTSIDE ``MariaStorageEngine`` and OUTSIDE the
explicit allowlist. The scanned set is ``SCAN_DIRS``.

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
  ``.ledger-chokepoint-allowlist.txt`` is EMPTY as shipped and the scan exits
  0 with it and without it — the two entries it once held were both dead by
  ledger task 388, and one of them asserted a ``UPDATE adr SET`` that task 202
  had already routed through the engine. The mechanism is kept for a future
  documented violation, not because one is outstanding. Prefer a POSITIONAL
  MATCHER FIX over an entry: a false positive means the matcher is imprecise,
  and fixing it protects every future call site rather than one line number.
  An entry must state plainly whether it is a checker-precision artifact or
  genuine tracked debt; it is never a general escape hatch.

DETECTION SCOPE
---------------
- String arguments to ``text(...)`` / ``execute(...)`` / ``exec(...)`` calls
  that start with an SQL statement keyword (``SELECT`` / ``INSERT`` /
  ``UPDATE`` / ``DELETE`` / ``MERGE`` / ``REPLACE`` / ``TRUNCATE``) AND name
  a ledger table in a TABLE POSITION — after ``FROM`` / ``JOIN`` / ``INTO`` /
  ``UPDATE`` / ``TABLE`` / ``TRUNCATE``, optionally schema-qualified. Prose
  mentions of a table name in a docstring are NOT flagged, and neither is a
  table name appearing only as a string VALUE (``WHERE TABLE_NAME = 'adr'``)
  or as a column name. The rule is "SQL statement accesses a ledger table",
  not "table name appears anywhere".
- A table position qualified by ``information_schema.`` is a catalog
  metadata read, not a ledger row access, and is skipped — PER POSITION.
  Other table positions in the same statement are still checked, so a
  literal that mixes ``FROM information_schema.TABLES`` with
  ``UPDATE adr SET ...`` is still a violation on ``adr``.
- Tests are excluded from the scan (mirrors check_dynamic_span_names).
- The scan covers ``yadgar/`` and ``scripts/`` (``SCAN_DIRS``). ``scripts/``
  was added by ledger task 394 — an operator script is exactly where an
  un-chokepointed ledger write is most tempting and was, until then, invisible.
- Free-standing SQL strings (no ``text(...)`` wrapper) are out of scope.
  The guard is not a SQL parser — it is an AST walker over call shapes,
  by design. A reviewer who writes `conn.execute(raw_string)` must wrap
  the string in ``text(...)`` for the chokepoint to apply; that wrapping
  is also what makes the engine's emitted SQL dialect-correct.

Usage:
  python scripts/check_ledger_chokepoint.py                 # check yadgar/ + scripts/, exit 0/1
  python scripts/check_ledger_chokepoint.py --root <dir>    # scan ONLY that one dir instead
  python scripts/check_ledger_chokepoint.py --allowlist <p> # documented-violations file
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

# Repo-root-relative directories the guard scans when no ``--root`` is given.
#
# ``scripts/`` joined the set in ledger task 394. The scan was ``yadgar/``-only
# for its whole life, so an operator script under ``scripts/`` could carry raw
# ledger SQL and never be seen — exactly the D20 bypass this gate exists to
# prevent, in the one directory whose whole purpose is one-off writes against
# the live store. Measured when the widening landed: ZERO violations under
# ``scripts/``, and that is structurally expected rather than suspicious —
# ``backfill_agent_pattern_from_wiki.py`` is the only script that reaches the
# ledger at all and it forwards REGISTERED admin ops via ``_forward_admin``,
# which is the chokepoint. The widening is a tripwire for the NEXT such
# script, not a bug-finder for the current ones.
SCAN_DIRS: tuple[str, ...] = ("yadgar", "scripts")

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

# Keywords after which SQL names a TABLE rather than a column or a value.
# ``DELETE FROM t`` / ``INSERT INTO t`` / ``REPLACE INTO t`` / ``MERGE INTO t``
# / ``UPDATE t SET`` / ``TRUNCATE t`` / ``TRUNCATE TABLE t`` / ``... JOIN t``
# are all covered by this set; a bare column or a quoted string VALUE never
# follows one of them.
_TABLE_INTRODUCERS = ("FROM", "JOIN", "INTO", "UPDATE", "TABLE", "TRUNCATE")

# A SQL identifier, optionally back-quoted. Used for the SCHEMA half of a
# qualified reference (``db.task`` / ``information_schema.TABLES``).
_SQL_IDENT = r"`?[A-Za-z_][A-Za-z0-9_$]*`?"

# The one schema whose objects are catalog METADATA, never ledger rows.
# ``SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_NAME='adr'``
# reads the catalog; the ``'adr'`` in the WHERE clause is a string VALUE.
_METADATA_SCHEMA = "information_schema"


def _table_position_re(table: str) -> re.Pattern[str]:
    """Regex matching ``table`` where SQL puts a TABLE NAME, not a value.

    Requires one of ``_TABLE_INTRODUCERS`` immediately before the name,
    with an optional schema qualifier in between (``FROM db.task``,
    ``JOIN `db`.`task```). The schema is captured so the caller can reject
    ``information_schema.``-qualified hits without discarding the rest of
    the statement.
    """
    return re.compile(
        r"\b(?:" + "|".join(_TABLE_INTRODUCERS) + r")\s+"
        r"(?:(?P<schema>" + _SQL_IDENT + r")\s*\.\s*)?"
        r"`?" + re.escape(table) + r"`?"
        r"(?![A-Za-z0-9_$])",
        re.IGNORECASE,
    )


_TABLE_POSITION_RES: dict[str, re.Pattern[str]] = {t: _table_position_re(t) for t in LEDGER_TABLES}

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


def _referenced_in_table_position(literal: str, table: str) -> bool:
    """True when ``literal`` names ``table`` where SQL expects a TABLE.

    Every occurrence is examined, not just the first: a statement may read
    the catalog AND touch a ledger row, and the catalog read must not grant
    the row access clemency. A hit qualified by ``information_schema.`` is
    catalog metadata (``information_schema.adr`` is not the ledger's ``adr``)
    and does not count; any other hit does.
    """
    for match in _TABLE_POSITION_RES[table].finditer(literal):
        schema = (match.group("schema") or "").strip("`").lower()
        if schema == _METADATA_SCHEMA:
            continue
        return True
    return False


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


def _build_alias_map(tree: ast.Module) -> dict[str, str]:
    """Map each ``from ... import X as Y`` local binding to its origin symbol.

    ``from sqlalchemy import text as sql`` binds the local name ``sql`` to
    the origin symbol ``text``. Later, a bare ``sql(...)`` call is an
    ``ast.Name(id="sql")`` — without this map, matching on the surface name
    alone would never see ``text`` and the chokepoint check would silently
    not apply.

    Only ``ast.ImportFrom`` is walked. Plain ``ast.Import`` (``import
    sqlalchemy as sa``) binds the local name to a *module*, not a callable —
    calls through it are ``ast.Attribute`` (``sa.text(...)``), whose
    ``.attr`` is already the real symbol name (``"text"``) independent of
    the module alias, so no resolution is needed for that shape.

    Walks the WHOLE module tree (not just top-level statements), so imports
    local to a function body are covered too. A name imported more than once
    (rebound) takes the last binding seen — mirrors ordinary Python name
    shadowing; the guard does not attempt control-flow-sensitive resolution.
    """
    alias_map: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound_name = alias.asname or alias.name
                alias_map[bound_name] = alias.name
    return alias_map


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
    alias_map: dict[str, str],
) -> list[Violation]:
    """Detect chokepoint violations inside a Call node's string args.

    SQL-execution-shaped calls are:

    - ``text(...)`` / ``sa.text(...)`` / ``sqlalchemy.text(...)`` — wraps a
      SQL statement string for execution.
    - ``conn.execute(...)`` / ``session.execute(...)`` /
      ``engine.execute(...)`` — actually runs the SQL.

    A string argument to one of these that mentions a ledger table is a
    chokepoint violation, UNLESS the call sits inside the engine class.

    A bare ``Name`` call (``sql(...)``) is resolved through ``alias_map``
    before the ``_SQL_EXEC_FUNCS`` check — an aliased import
    (``from sqlalchemy import text as sql``) binds the SQL-exec function to
    a different surface name, and matching on the surface name alone lets it
    slip past the guard entirely. ``Attribute`` calls (``sa.text(...)``) are
    unaffected: the attribute name is already the real symbol name regardless
    of what the owning module was imported as.
    """
    if in_engine:
        return []

    func = call.func
    func_name: str | None = None
    if isinstance(func, ast.Name):
        func_name = alias_map.get(func.id, func.id)
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

    MATCHING IS POSITIONAL, NOT BARE-TOKEN. A ledger table counts only where
    SQL puts a TABLE NAME — after ``FROM`` / ``JOIN`` / ``INTO`` / ``UPDATE`` /
    ``TABLE`` / ``TRUNCATE``, with an optional schema qualifier. A hit whose
    qualifier is ``information_schema.`` is catalog metadata and is skipped;
    every OTHER table position in the same statement is still checked.

    Car B (task #201) found the false positive: ``adr_seed._read_next_adr_id``
    runs ``SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE
    TABLE_NAME = 'adr'`` and the bare-substring matcher flagged the ``'adr'``
    token inside the WHERE clause's string VALUE. Car B's remedy was to
    ``return []`` for the WHOLE literal on any ``FROM information_schema.``
    anywhere in it — far wider than the false positive, because it made any
    literal MIXING a metadata read with a real ledger access invisible::

        text("SELECT AUTO_INCREMENT FROM information_schema.TABLES "
             "WHERE TABLE_NAME='adr'; UPDATE adr SET tier='binding'")

    That string scored ZERO violations. Positional matching removes the
    whole-string escape hatch: the metadata ``FROM`` is skipped on its
    qualifier, the ``UPDATE adr`` is still flagged.

    KNOWN GAPS (deliberate — a positional matcher under-detects where a
    bare-token one over-detected, and each of these would cost more false
    positives than it buys):

    - Comma-separated table lists (``FROM a, task`` / ``UPDATE a, task SET``):
      only the first name after the introducer is read. Adding ``,`` as an
      introducer would flag a COLUMN named ``task`` in
      ``INSERT INTO foo (id, task)``.
    - A literal that does not START with an SQL statement keyword is not
      scanned at all — ``_is_sql_literal`` gates on ``_SQL_STARTERS``, which
      omits ``WITH``, so a CTE-first statement is out of scope. Pre-existing
      and unrelated to the exemption; no such literal exists in this repo
      (measured 2026-08-26).
    """
    if not _is_sql_literal(literal):
        return []
    out: list[Violation] = []
    for table in LEDGER_TABLES:
        if not _referenced_in_table_position(literal, table):
            continue
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


def _allowlist_key(src_file: Path) -> str:
    """Normalize a scanned path for allowlist matching.

    Prefers repo-root-relative (portable across machines/CI/worktrees, and
    the form every OTHER committed allowlist in this repo uses — e.g.
    ``.auth-token-pattern-allowlist.txt``'s
    ``yadgar/backend/embed_service/embed_service.py:790``) and falls back to
    the absolute path when ``src_file`` is not under ``_REPO_ROOT`` — e.g. a
    test scanning a ``tmp_path`` fixture root, which is exactly what this
    file's own ``test_allowlisted_file_passes`` does. Mirrors
    ``check_auth_token_pattern.py``'s ``_allowlist_key`` (same repo, same
    pattern, added there specifically because this script's original
    absolute-path-only matching does not survive a checkout at a different
    path — including a git worktree of the same repo, whose absolute prefix
    differs from the main checkout's).
    """
    resolved = src_file.resolve()
    try:
        return str(resolved.relative_to(_REPO_ROOT))
    except ValueError:
        return str(resolved)


def _scan_function_body(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    src_file: Path,
    in_engine: bool,
    allowed: set[tuple[str, int]],
    alias_map: dict[str, str],
) -> list[Violation]:
    """Inspect one function's Call nodes for SQL-execution-shaped chokepoint uses.

    Only calls to ``text(...)`` / ``execute(...)`` / ``exec(...)`` (resolved
    through ``alias_map`` for aliased imports) whose string arguments mention
    a ledger table are reported. Free-standing string literals are ignored —
    that is the difference between an SQL statement and a docstring / log
    message.
    """
    violations: list[Violation] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        for v in _violations_in_call(node, src_file, in_engine, alias_map):
            key = (_allowlist_key(v.source_file), v.lineno)
            if key in allowed:
                continue
            violations.append(v)
    return violations


def _scan_class(
    class_node: ast.ClassDef,
    src_file: Path,
    allowed: set[tuple[str, int]],
    alias_map: dict[str, str],
) -> list[Violation]:
    """Scan every method in the class. Methods inside the engine are exempt."""
    violations: list[Violation] = []
    in_engine = _is_engine_class(class_node)
    for sub in ast.walk(class_node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.extend(_scan_function_body(sub, src_file, in_engine, allowed, alias_map))
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

    alias_map = _build_alias_map(tree)
    violations: list[Violation] = []

    # 1. Module-level (free) functions: NOT in any class → always in scope.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.extend(
                _scan_function_body(
                    node, src_file, in_engine=False, allowed=allowed, alias_map=alias_map
                )
            )

    # 2. Class method bodies: in-engine methods are exempt, others are in scope.
    for sub in ast.walk(tree):
        if isinstance(sub, ast.ClassDef):
            violations.extend(_scan_class(sub, src_file, allowed, alias_map))
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


def default_roots() -> list[Path]:
    """The directories scanned when no explicit ``--root`` is given.

    Resolved against ``_REPO_ROOT`` at CALL time (not import time) so a test
    can monkeypatch ``_REPO_ROOT`` onto a fixture tree and exercise the DEFAULT
    root set — the only way to prove the default covers ``scripts/`` rather
    than proving the scanner accepts an arbitrary ``--root``, which it always
    did. A root that does not exist is skipped, so a partial checkout does not
    turn the gate into a crash.
    """
    return [p for p in (_REPO_ROOT / d for d in SCAN_DIRS) if p.is_dir()]


def scan(
    root: Path | None = None,
    allowed: set[tuple[str, int]] | None = None,
) -> list[Violation]:
    """Scan every in-scope file under ``root`` and return every violation.

    ``root=None`` scans the DEFAULT root set (``SCAN_DIRS`` — ``yadgar/`` and
    ``scripts/``). An explicit ``root`` scans THAT ONE directory and nothing
    else: the override is a replacement, never an addition, so an existing
    caller pointing at a fixture tree keeps getting exactly that tree.
    """
    roots = [root] if root is not None else default_roots()
    if allowed is None:
        allowed = set()
    violations: list[Violation] = []
    for r in roots:
        for f in _iter_py_files(r):
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
        default=None,
        help=(
            "Scan THIS ONE directory instead of the default set "
            f"({'/, '.join(SCAN_DIRS)}/). Replaces the default set, never adds to it."
        ),
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
    root = Path(args.root) if args.root else None
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
