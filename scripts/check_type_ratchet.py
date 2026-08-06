#!/usr/bin/env python3
"""Strict-typing ratchet — a file you TOUCH may not gain type errors.

WHY A RATCHET AND NOT A GATE
----------------------------
The tree is ~306k lines across ~1129 modules with NO static type checker
(task 0116).  Turning mypy on everywhere emits thousands of errors on day
one and gets switched off by the end of the week.  So the gate is
differential: mypy runs over the files this BRANCH touched, and each is
compared against a recorded per-file baseline.  Legacy files may keep their
existing errors; they may not accumulate more.  A file with no baseline
entry — i.e. every new module — is held to ZERO.

This is the enforcement half of the strict-typing discipline.  The other
half is `[tool.mypy]` in pyproject.toml, where an explicit allowlist runs
`strict = true` so new subsystems are strict from their first commit.

WHY BRANCH-DIFF AND NOT ``git diff --cached``
---------------------------------------------
Same reason as check_test_weakening.py (task #52): a CI checkout has an
empty index, so a staged-only guard silently passes on every CI run, and
locally it misses damage done by an EARLIER commit on the same branch.

  baseline = ``git merge-base origin/master HEAD``
  changed  = ``git diff --name-only <baseline> HEAD`` ∪ ``git diff --cached --name-only``

When the merge-base is unreachable (shallow clone), the selection collapses
to the staged diff and the check degrades gracefully rather than exploding.

Usage:
    python scripts/check_type_ratchet.py
    python scripts/check_type_ratchet.py --update-baseline   # re-record
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

GitRunner = Callable[[list[str]], str]

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / ".mypy-ratchet-baseline.json"
DEFAULT_BASE_REF = "origin/master"

# mypy emits ``path:line: error: message  [code]``; notes and the trailing
# summary line must not be counted as errors.
_ERROR_LINE = re.compile(r"^(?P<path>[^:]+):\d+:(?:\d+:)? error:")


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def parse_mypy_errors(output: str) -> dict[str, int]:
    """Return {path: error_count} from raw mypy stdout.

    Notes (``: note:``) and the ``Found N errors`` summary are ignored — only
    lines mypy classifies as errors count toward the ratchet.
    """
    counts: dict[str, int] = {}
    for line in output.splitlines():
        match = _ERROR_LINE.match(line)
        if match is None:
            continue
        path = match.group("path")
        counts[path] = counts.get(path, 0) + 1
    return counts


def select_changed_python_files(names: list[str], root: Path) -> list[str]:
    """Filter *names* to existing ``.py`` files, de-duplicated, order-stable.

    Deleted paths are dropped: mypy cannot check a file that is gone, and a
    deletion can never ADD a type error.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for name in names:
        if not name.endswith(".py") or name in seen:
            continue
        seen.add(name)
        if (root / name).is_file():
            kept.append(name)
    return kept


def compare_against_baseline(
    current: dict[str, int],
    baseline: dict[str, int],
    changed: list[str],
) -> list[str]:
    """Return one violation string per changed file that gained errors.

    Files absent from *changed* are ignored entirely — the ratchet only ever
    judges what this branch touched.  A file absent from *baseline* is held
    to zero, which is what makes new modules strict by default.
    """
    violations: list[str] = []
    for path in changed:
        was = baseline.get(path, 0)
        now = current.get(path, 0)
        if now > was:
            violations.append(
                f"{path}: type errors {was} -> {now} (+{now - was}). "
                f"A file you touched may not gain type errors."
            )
    return violations


def resolve_merge_base(base_ref: str, run_git: GitRunner) -> str:
    """Return merge-base(base_ref, HEAD), or "" when it cannot be resolved."""
    try:
        return run_git(["merge-base", base_ref, "HEAD"]).strip()
    except Exception:  # noqa: BLE001 — shallow clone / missing ref: degrade, don't crash
        return ""


# ---------------------------------------------------------------------------
# IO edges
# ---------------------------------------------------------------------------


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def collect_changed_names(base_ref: str, run_git: GitRunner) -> list[str]:
    """Branch diff ∪ staged diff, as raw path names."""
    names: list[str] = []
    base = resolve_merge_base(base_ref, run_git)
    if base:
        names.extend(run_git(["diff", "--name-only", base, "HEAD"]).split())
    names.extend(run_git(["diff", "--cached", "--name-only"]).split())
    return names


def resolve_mypy_interpreter(root: Path) -> str:
    """Return the interpreter that should run mypy.

    Prefer the repo's own venv. pre-commit runs ``language: system`` hooks
    under ITS python, not the project venv, even when the venv is on PATH —
    so ``sys.executable`` is not the project environment during a commit.
    Resolving the venv explicitly also pins ONE mypy version: falling back to
    whatever ``mypy`` sits on PATH would mix versions between a local run and
    a hook run, and the baseline counts would stop reproducing.
    """
    venv_python = root / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


def run_mypy(paths: list[str]) -> str:
    """Run mypy over *paths* and return its stdout (config lives in pyproject)."""
    result = subprocess.run(
        [resolve_mypy_interpreter(REPO_ROOT), "-m", "mypy", *paths],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if "No module named mypy" in result.stderr:
        raise RuntimeError(
            "mypy is not installed. Install dev extras:  pip install -e '.[dev]'\n"
            "This guard fails LOUD rather than passing silently — a type gate "
            "that no-ops when its checker is missing is worse than no gate."
        )
    return result.stdout


def load_baseline() -> dict[str, int]:
    if not BASELINE_PATH.is_file():
        return {}
    return json.loads(BASELINE_PATH.read_text())


def write_baseline(counts: dict[str, int]) -> None:
    ordered = dict(sorted(counts.items()))
    BASELINE_PATH.write_text(json.dumps(ordered, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", default=DEFAULT_BASE_REF)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Re-record the baseline for the changed files (use when a legacy "
        "file's errors legitimately move, never to silence a new error).",
    )
    args = parser.parse_args()

    changed = select_changed_python_files(collect_changed_names(args.base_ref, _git), REPO_ROOT)
    if not changed:
        return 0

    current = parse_mypy_errors(run_mypy(changed))

    if args.update_baseline:
        baseline = load_baseline()
        baseline.update({path: current.get(path, 0) for path in changed})
        write_baseline({k: v for k, v in baseline.items() if v})
        print(f"Baseline updated for {len(changed)} file(s).")
        return 0

    violations = compare_against_baseline(current, load_baseline(), changed)
    if not violations:
        return 0

    print("Strict-typing ratchet FAILED\n")
    for violation in violations:
        print(f"  {violation}")
    print(
        "\nFix the types rather than silencing them: no `# type: ignore`, no "
        "`cast()`, no bare `except`.\nIf a legacy file's baseline is genuinely "
        "stale, re-record it with:\n  python scripts/check_type_ratchet.py --update-baseline"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
