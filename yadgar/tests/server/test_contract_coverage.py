"""Tests for scripts/check_contract_coverage.py — the BEHAVIOR_CONTRACT lint.

Non-e2e (runs under CI `-m 'not e2e'`). Validates the three lint rules:
  1. ✅ rows must cite a resolvable `path::node` reference.
  2. any row with a `path::node` reference must resolve (validate-if-present).
  3. header counts must equal the actual ✅/⏳/❌ + [r]/[u]/none tally.

Test plan:
  1. Real contract passes (the shipped docs/BEHAVIOR_CONTRACT.md is clean).
  2. ✅ without a reference → violation naming rule 1.
  3. dangling reference (file/node absent) → violation naming rule 2.
  4. header status drift → violation naming rule 3.
  5. header tag drift → violation naming rule 3.
  6. a valid reference resolves (resolve_ref returns None).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check_contract_coverage.py"

_spec = importlib.util.spec_from_file_location("check_contract_coverage", _SCRIPT)
assert _spec and _spec.loader
ccc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccc)


# A minimal valid contract body the lint accepts (header tally matches rows).
_VALID = (
    "**1 ✅ · 0 ⏳ · 0 ❌.**\n"
    "**0 `[r]` · 0 `[u]` · 0 none.**\n"
    "- BC-E1 row. ✅ `tests/e2e/test_vacuum_backup_safety.py::TestBCE1_RowCountsPreserved`\n"
)


def test_real_contract_passes() -> None:
    """The shipped contract is clean."""
    text = (_REPO_ROOT / "docs" / "BEHAVIOR_CONTRACT.md").read_text(encoding="utf-8")
    assert ccc.check(text) == []


def test_green_without_reference_fails() -> None:
    text = "**1 ✅ · 0 ⏳ · 0 ❌.**\n**0 `[r]` · 0 `[u]` · 0 none.**\n- BC-X1 no ref here. ✅\n"
    errs = ccc.check(text)
    assert any("rule 1" in e and "BC-X1" in e for e in errs), errs


def test_dangling_reference_fails() -> None:
    text = (
        "**1 ✅ · 0 ⏳ · 0 ❌.**\n**0 `[r]` · 0 `[u]` · 0 none.**\n"
        "- BC-X2 row. ✅ `tests/e2e/does_not_exist.py::Nope`\n"
    )
    errs = ccc.check(text)
    assert any("rule 2" in e and "BC-X2" in e for e in errs), errs


def test_dangling_reference_on_pending_row_fails() -> None:
    """Rule 2 extends beyond ✅ — a ⏳ row with a bad ref also fails."""
    text = (
        "**0 ✅ · 1 ⏳ · 0 ❌.**\n**0 `[r]` · 1 `[u]` · 0 none.**\n"
        "- BC-X3 row. ⏳[u] `tests/missing.py::Gone`\n"
    )
    errs = ccc.check(text)
    assert any("rule 2" in e and "BC-X3" in e for e in errs), errs


def test_header_status_drift_fails() -> None:
    text = (
        "**99 ✅ · 0 ⏳ · 0 ❌.**\n**0 `[r]` · 0 `[u]` · 0 none.**\n"
        "- BC-E1 row. ✅ `tests/e2e/test_vacuum_backup_safety.py::TestBCE1_RowCountsPreserved`\n"
    )
    errs = ccc.check(text)
    assert any("header status drift" in e for e in errs), errs


def test_header_tag_drift_fails() -> None:
    text = "**0 ✅ · 1 ⏳ · 0 ❌.**\n**9 `[r]` · 0 `[u]` · 0 none.**\n- BC-X4 unit row. ⏳[u] P2\n"
    errs = ccc.check(text)
    assert any("header tag drift" in e for e in errs), errs


def test_valid_reference_resolves() -> None:
    assert (
        ccc.resolve_ref("tests/e2e/test_vacuum_backup_safety.py::TestBCE1_RowCountsPreserved")
        is None
    )


def test_clean_minimal_contract_passes() -> None:
    assert ccc.check(_VALID) == []
