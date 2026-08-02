#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""AST guard: every DB write to ledger tables goes through _LedgerMixin.

Spine Car A (task-table-refactor-2026-07-29, D20). The mixin is the
single chokepoint for task/adr/agent_prompt/runtime_config access.
Direct SQL or raw ORM calls from outside the mixin are forbidden.

Allowlist covers pre-existing violations that are out of scope for this
train (they get migrated in their own car).
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

LEDGER_TABLES = frozenset({"task", "adr", "agent_prompt", "runtime_config"})
LEDGER_MODEL_NAMES = frozenset(
    {"Task", "ADR", "ADRModel", "AgentPrompt", "AgentPromptModel", "RuntimeConfig"}
)
LEDGER_METHOD_NAMES = frozenset(
    {
        "_next_number",
        "_ledger_table",
        "_ledger_healthcheck",
        "_init_ledger",
        "_ledger_insert",
        "_ledger_select",
        "_ledger_update",
        "_ledger_delete",
    }
)
MIXIN_PATH = Path("yadgar/_shared/storage/ledger.py")

# Pre-existing violations — allowed for this train, must be migrated later.
ALLOWLIST: dict[str, str] = {
    "yadgar/core/cli/stats.py:719": "stats command — pre-existing own connection",
    "yadgar/core/hooks/prompt-recall.py:83": "hook — pre-existing own connection",
    "yadgar/core/hooks/prompt-recall.py:98": "hook — pre-existing own connection",
    "yadgar/core/server/tools/project.py:1381": "pre-existing direct DB call",
    "yadgar/core/server/tools/audit.py": "audit — pre-existing direct DB calls (10+ sites)",
}


def _file_key(path: Path) -> str:
    return str(path).removeprefix("./")


def _is_mixin_file(path: Path) -> bool:
    try:
        return path.resolve() == MIXIN_PATH.resolve()
    except OSError:
        return False


def _uses_ledger_table(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (line, fragment) for any SQL string that touches a ledger table.

    Only flags `.execute()` / `.executemany()` calls with string SQL that
    references a ledger table — not bare string literals (those are usually
    enum names or kwargs, not SQL).
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"execute", "executemany", "exec_driver_sql"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    sql = first.value
                    for table in LEDGER_TABLES:
                        if table in sql:
                            hits.append((first.lineno, sql[:80]))
                            break
    return hits


def _uses_ledger_orm(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (line, fragment) for ORM queries on ledger models.

    Flags `session.query(Task)` / `session.query(ADRModel)` / etc. calls
    that reference a ledger model class. These must go through _LedgerMixin.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "query" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Name) and first.id in LEDGER_MODEL_NAMES:
                    hits.append((first.lineno, f"session.query({first.id})"))
    return hits


def _calls_mixin_method(tree: ast.AST) -> bool:
    """Return True if the file calls any _LedgerMixin method directly."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in LEDGER_METHOD_NAMES:
                return True
    return False


# Files that legitimately reference ledger table names without going through
# the mixin — the SurrealDB implementation of runtime_config (being replaced),
# the ORM models file itself, and the Alembic migration files.
EXEMPT_FILES: frozenset[str] = frozenset(
    {
        "yadgar/_shared/storage/runtime_config.py",  # old SurrealDB runtime_config — replaced by Car G
        "yadgar/_shared/storage/alembic_models.py",  # ORM model definitions
        "yadgar/_shared/storage/alembic/versions/001_runtime_config.py",
        "yadgar/_shared/storage/alembic/versions/002_ledger_tables.py",
        "yadgar/_shared/storage/alembic/env.py",
    }
)


def check_file(path: Path) -> list[str]:
    """Return a list of violation strings for `path`."""
    if _is_mixin_file(path):
        return []
    key = _file_key(path)
    if key in EXEMPT_FILES:
        return []
    try:
        source = path.read_text()
    except OSError, UnicodeDecodeError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    violations: list[str] = []
    uses_ledger = _uses_ledger_table(tree)
    for line, fragment in uses_ledger:
        site = f"{key}:{line}"
        if site in ALLOWLIST:
            continue
        violations.append(
            f"{site}: direct SQL on ledger table "
            f"({fragment!r}) — must go through _LedgerMixin ({MIXIN_PATH})"
        )
    uses_orm = _uses_ledger_orm(tree)
    for line, fragment in uses_orm:
        site = f"{key}:{line}"
        if site in ALLOWLIST:
            continue
        violations.append(
            f"{site}: direct ORM query on ledger model "
            f"({fragment}) — must go through _LedgerMixin ({MIXIN_PATH})"
        )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        default=["yadgar"],
        help="paths to scan (default: yadgar/)",
    )
    args = parser.parse_args()
    violations: list[str] = []
    for root in args.paths:
        root_path = Path(root)
        if root_path.is_file():
            violations.extend(check_file(root_path))
            continue
        for path in root_path.rglob("*.py"):
            violations.extend(check_file(path))
    if violations:
        print(f"FAIL: {len(violations)} ledger chokepoint violation(s)")
        for v in violations:
            print(f"  {v}")
        return 1
    print("OK: all ledger-table access goes through _LedgerMixin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
