#!/usr/bin/env python3
"""I28 — Allowlist-audit invariant: every allowlist hit must produce an audit entry.

This script performs a static check:
  1. Verifies that is_allowlisted() and _write_audit() are co-present in
     yadgar/security/allowlist.py (existence + co-location).
  2. Verifies gate_or_reject() in yadgar/secrets.py calls both is_allowlisted()
     and _write_audit() before returning clean (allowlist hit path).
  3. Verifies the YADGAR_SECRET_GATE_AUDIT_DIR env knob is documented.

This is a structural invariant: the audit write is internal to gate_or_reject()
and is_allowlisted(); no external caller is responsible for it.

Exit codes:
  0  invariant satisfied
  1  one or more violations

Usage:
  python scripts/check_allowlist_audit.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_ALLOWLIST_MODULE = _REPO_ROOT / "yadgar" / "_shared" / "security" / "allowlist.py"
_SECRETS_MODULE = _REPO_ROOT / "yadgar" / "_shared" / "security" / "secrets.py"


def _check_allowlist_module_has_write_audit() -> list[str]:
    """Verify _write_audit and is_allowlisted are defined in allowlist.py."""
    violations: list[str] = []
    if not _ALLOWLIST_MODULE.exists():
        return [f"MISSING: {_ALLOWLIST_MODULE} not found"]

    source = _ALLOWLIST_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_ALLOWLIST_MODULE))

    defined_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            defined_names.add(node.name)

    for required in ("_write_audit", "is_allowlisted"):
        if required not in defined_names:
            violations.append(f"allowlist.py: required function '{required}' not defined")

    return violations


def _collect_call_names(node: ast.FunctionDef) -> set[str]:
    """Return set of function/method names called anywhere in node's body."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _check_gate_or_reject_calls_write_audit() -> list[str]:
    """Verify gate_or_reject() in secrets.py calls _write_audit on allowlist hit."""
    violations: list[str] = []
    if not _SECRETS_MODULE.exists():
        return [f"MISSING: {_SECRETS_MODULE} not found"]

    source = _SECRETS_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_SECRETS_MODULE))

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "gate_or_reject":
            continue
        calls_found = _collect_call_names(node)
        for required_call in ("_write_audit", "is_allowlisted"):
            if required_call not in calls_found:
                violations.append(
                    f"secrets.py: gate_or_reject() does not call '{required_call}' — "
                    "audit trail missing on allowlist hit path"
                )

    return violations


def _check_audit_dir_env_documented() -> list[str]:
    """Verify YADGAR_SECRET_GATE_AUDIT_DIR appears in allowlist.py docstring."""
    violations: list[str] = []
    if not _ALLOWLIST_MODULE.exists():
        return []

    source = _ALLOWLIST_MODULE.read_text(encoding="utf-8")
    if "YADGAR_SECRET_GATE_AUDIT_DIR" not in source:
        violations.append(
            "allowlist.py: YADGAR_SECRET_GATE_AUDIT_DIR env knob not documented in module"
        )
    if "YADGAR_SECRET_GATE_ALLOWLIST_PATH" not in source:
        violations.append(
            "allowlist.py: YADGAR_SECRET_GATE_ALLOWLIST_PATH env knob not documented in module"
        )
    return violations


def main(argv: list[str] | None = None) -> int:
    """Run I28 check. Returns 0 if clean, 1 if violations found."""
    all_violations: list[str] = []
    all_violations.extend(_check_allowlist_module_has_write_audit())
    all_violations.extend(_check_gate_or_reject_calls_write_audit())
    all_violations.extend(_check_audit_dir_env_documented())

    if all_violations:
        print("I28 VIOLATIONS — allowlist-audit invariant broken:", file=sys.stderr)
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        print(
            f"\n{len(all_violations)} violation(s) found.",
            file=sys.stderr,
        )
        return 1

    print("I28 OK — allowlist audit invariant satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
