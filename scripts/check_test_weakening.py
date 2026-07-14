#!/usr/bin/env python3
"""Layer 4 tamper-protection: pre-commit diff guard (task #52).

Compares staged changes against HEAD and fails if the diff introduces a NET
removal of ``assert`` statements in ``yadgar/tests/e2e/`` OR a decrease in the
✅ count in ``docs/contracts/BEHAVIOR_CONTRACT.md``.

Override: set ``ALLOW_TEST_WEAKEN=1`` in the environment to bypass.  This is
intentionally a one-time env override, not a permanent flag, so weakening a
test always requires an explicit acknowledgement.

Usage:
    python scripts/check_test_weakening.py   # operates on staged diff (git diff --cached)
    ALLOW_TEST_WEAKEN=1 python scripts/check_test_weakening.py   # bypass

Pre-commit wiring (add to .pre-commit-config.yaml under the existing local hooks):

    - id: check-test-weakening
      name: Block silent test weakening (e2e assert removal / ✅ regression)
      language: system
      entry: python scripts/check_test_weakening.py
      pass_filenames: false
      files: ^(yadgar/tests/e2e/.*\\.py|docs/BEHAVIOR_CONTRACT\\.md)$

The hook only fires when staged changes touch e2e tests or the contract —
zero overhead on unrelated commits.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT = _REPO_ROOT / "docs" / "contracts" / "BEHAVIOR_CONTRACT.md"
_STATUS_HDR_RE = re.compile(r"\*\*([0-9]+)\s*✅")

_ALLOW_ENV = "ALLOW_TEST_WEAKEN"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(*args: str) -> str:
    """Run a git command and return stdout (empty string on non-zero exit)."""
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    return result.stdout if result.returncode == 0 else ""


def _diff_assert_delta(diff_text: str) -> int:
    """Return (lines_added - lines_removed) for 'assert ' in *diff_text*.

    A negative value means NET removal of assert statements.
    Only counts lines in e2e test files.
    """
    in_e2e_file = False
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            # Check if this file is under yadgar/tests/e2e/
            in_e2e_file = bool(re.search(r"yadgar/tests/e2e/.*\.py", line))
        if not in_e2e_file:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if re.search(r"\bassert\b", line):
                added += 1
        elif line.startswith("-") and not line.startswith("---"):
            if re.search(r"\bassert\b", line):
                removed += 1
    return added - removed


def _green_count_from_text(text: str) -> int | None:
    """Extract the ✅ count from a BEHAVIOR_CONTRACT.md body, or None if not found."""
    m = _STATUS_HDR_RE.search(text)
    return int(m.group(1)) if m else None


def _green_count_head() -> int | None:
    """Return the ✅ count from HEAD's BEHAVIOR_CONTRACT.md."""
    head_text = _run("git", "show", "HEAD:docs/contracts/BEHAVIOR_CONTRACT.md")
    if not head_text:
        return None
    return _green_count_from_text(head_text)


def _green_count_staged() -> int | None:
    """Return the ✅ count from the staged (index) BEHAVIOR_CONTRACT.md."""
    staged_text = _run("git", "show", ":docs/contracts/BEHAVIOR_CONTRACT.md")
    if not staged_text:
        # Not staged; read from working tree.
        if _CONTRACT.is_file():
            return _green_count_from_text(_CONTRACT.read_text(encoding="utf-8"))
        return None
    return _green_count_from_text(staged_text)


def check_diff(diff_text: str, head_green: int | None, staged_green: int | None) -> list[str]:
    """Pure function: return violation strings given a diff + green counts."""
    errors: list[str] = []

    delta = _diff_assert_delta(diff_text)
    if delta < 0:
        errors.append(
            f"layer 4 — NET removal of {abs(delta)} 'assert' statement(s) in "
            "yadgar/tests/e2e/. If this is intentional, set ALLOW_TEST_WEAKEN=1 "
            "when committing."
        )

    if head_green is not None and staged_green is not None:
        if staged_green < head_green:
            errors.append(
                f"layer 4 — ✅ count dropped {head_green} → {staged_green} in "
                "docs/contracts/BEHAVIOR_CONTRACT.md. If this is intentional, set "
                "ALLOW_TEST_WEAKEN=1 when committing."
            )

    return errors


def main() -> int:
    if os.environ.get(_ALLOW_ENV, "").strip() == "1":
        print(f"check_test_weakening: bypassed ({_ALLOW_ENV}=1)")
        return 0

    diff_text = _run("git", "diff", "--cached")
    head_green = _green_count_head()
    staged_green = _green_count_staged()

    errors = check_diff(diff_text, head_green, staged_green)
    if errors:
        print("test-weakening guard FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("test-weakening guard OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
