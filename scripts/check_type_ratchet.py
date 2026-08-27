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

WHY AN INCOMPLETE RUN IS A FAILURE, NOT A PASS
----------------------------------------------
This guard shipped PASSING VACUOUSLY.  ``yadgar/_shared/storage/client.py``
carried a prose comment opening ``# type:``; mypy read it as a PEP 484 type
comment, rejected it as invalid syntax, and ABORTED — attributing one error to
a module that was FOLLOWED rather than requested.  ``compare_against_baseline``
ignores paths outside the change set by design, so it found no violations and
the guard returned 0.  Every branch whose import graph reached that module —
effectively the whole tree — passed without being type-checked at all.

So absence of errors is no longer accepted as evidence of success.  Before any
comparison happens, ``detect_incomplete_run`` demands POSITIVE proof that mypy
analysed what it was handed, and fails LOUD when it did not.  The same gate
guards ``--update-baseline``: a baseline recorded from an aborted run would
bake the blindness into the data instead of the code.

Usage:
    python scripts/check_type_ratchet.py
    python scripts/check_type_ratchet.py --update-baseline               # re-record touched files
    python scripts/check_type_ratchet.py --all-files --update-baseline   # regenerate whole baseline
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

GitRunner = Callable[[list[str]], str]

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / ".mypy-ratchet-baseline.json"
DEFAULT_BASE_REF = "origin/master"

# mypy emits ``path:line: error: message  [code]``; notes and the trailing
# summary line must not be counted as errors.
_ERROR_LINE = re.compile(r"^(?P<path>[^:]+):\d+:(?:\d+:)? error:")

# Colour codes defeat every ``^``-anchored regex below. mypy colourises when it
# believes it is talking to a terminal, and the Claude Code harness exports
# ``FORCE_COLOR=3`` into the environment its Bash tool runs in, so a hook that
# captures mypy's stdout got ``\x1b[1mSuccess: ...`` and matched NOTHING against
# ``^Success:``. The guard then reported "COULD NOT RUN / mypy printed no summary
# line" on a run that had in fact succeeded -- a well-formed failure over a real
# pass (ADR-0420's class, pointed at the type gate itself). The workaround people
# reached for was chanting ``unset FORCE_COLOR`` before every commit; that is a
# ritual, not a fix, and it silently stops working the moment someone forgets.
# Two belts: ask mypy not to colour (below, in run_mypy), and strip anything that
# still arrives so no future colour source can blind the parser again.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Remove ANSI SGR/CSI sequences so ``^``-anchored parsing works on captured output."""
    return _ANSI_RE.sub("", text)


# mypy prints exactly one summary line, in one of two shapes, and drops the
# plural at 1.  Its ``checked N source files`` count is the only POSITIVE
# evidence the run produced that the requested files were really analysed.
_SUMMARY_CLEAN = re.compile(r"^Success: no issues found in (?P<checked>\d+) source files?\b", re.M)
_SUMMARY_ERRORS = re.compile(
    r"^Found \d+ errors? in \d+ files? \(checked (?P<checked>\d+) source files?\)", re.M
)

# What mypy prints INSTEAD of a checked-count when a blocking error stopped it.
_ABORT_MARKER = "errors prevented further checking"

# A file mypy could not even parse. Both spellings appear: the human message and
# the error code.
_SYNTAX_ERROR_LINE = re.compile(
    r"^(?P<path>[^:]+):\d+:(?:\d+:)? error: .*?(?:Invalid syntax|\[syntax\])"
)

# mypy's exit codes: 0 = no issues, 1 = type errors found. Anything else is a
# fatal/usage error and the stdout that came with it cannot be trusted.
_USABLE_RETURNCODES = frozenset({0, 1})


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


def parse_checked_count(output: str) -> int | None:
    """Return the ``checked N source files`` count, or None if mypy printed none.

    None means mypy never got as far as reporting a total — the run cannot be
    read as clean no matter how empty the rest of the output looks.
    """
    for pattern in (_SUMMARY_CLEAN, _SUMMARY_ERRORS):
        match = pattern.search(output)
        if match is not None:
            return int(match.group("checked"))
    return None


def detect_incomplete_run(output: str, changed: list[str], returncode: int) -> list[str]:
    """Return the reasons mypy did NOT check what it was asked to check.

    An empty list means the run is usable — it may still report type errors,
    which is the ratchet's business, not this function's.  A non-empty list
    means the result says nothing about *changed* and must never score as a
    pass.  Every reason names the offending path where one exists: a loud
    failure that does not say WHICH file broke only relocates the blindness.
    """
    reasons: list[str] = []

    if returncode not in _USABLE_RETURNCODES:
        reasons.append(
            f"mypy exited {returncode}; only 0 (clean) and 1 (type errors found) mean it ran."
        )

    if _ABORT_MARKER in output:
        reasons.append(f'mypy reported "{_ABORT_MARKER}" — it stopped early.')

    unparseable = sorted(
        {
            match.group("path")
            for match in map(_SYNTAX_ERROR_LINE.match, output.splitlines())
            if match is not None
        }
    )
    if unparseable:
        reasons.append("mypy could not parse: " + ", ".join(unparseable))

    # An error against a path OUTSIDE the requested set means the run derailed
    # into a module it was only following. This is sound ONLY because
    # pyproject sets `follow_imports = "silent"`, under which a merely-followed
    # module reports no ordinary errors — so anything it does report is a
    # blocking error. If that setting is ever changed, this check starts firing
    # on every run and THIS COMMENT is the reason why.
    wanted = {Path(path).as_posix() for path in changed}
    stray = sorted(
        path for path in parse_mypy_errors(output) if Path(path).as_posix() not in wanted
    )
    if stray:
        reasons.append(
            "mypy reported errors in files it was not asked about, so it derailed "
            "into a followed module: " + ", ".join(stray)
        )

    checked = parse_checked_count(output)
    if checked is None:
        reasons.append("mypy printed no summary line, so there is no evidence it checked anything.")
    elif checked < len(changed):
        reasons.append(
            f"mypy accounted for only {checked} source file(s) but was handed {len(changed)}."
        )

    return reasons


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


def collect_all_python_names(run_git: GitRunner) -> list[str]:
    """Every TRACKED .py path — the baseline's universe.

    Deliberately the same universe ``select_changed_python_files`` draws from,
    which is any tracked ``.py`` file and not just ``yadgar/``. A baseline
    narrower than that would hold an untouched legacy ``scripts/*.py`` to zero
    the first time somebody edits it, and block the commit.
    """
    return run_git(["ls-files", "*.py"]).splitlines()


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


class MypyRun(NamedTuple):
    """mypy's stdout together with the exit code that qualifies it.

    The exit code is carried deliberately: reading stdout alone is precisely
    how this guard went blind, because an aborted run's stdout looks empty of
    anything the ratchet was watching for.
    """

    output: str
    returncode: int


def run_mypy(paths: list[str]) -> MypyRun:
    """Run mypy over *paths* and return stdout + exit code (config in pyproject)."""
    # Strip the harness's FORCE_COLOR and pin mypy's own colour switch off, so
    # the captured stdout is plain text no matter who set what. See _ANSI_RE.
    env = {k: v for k, v in os.environ.items() if k != "FORCE_COLOR"}
    env["MYPY_FORCE_COLOR"] = "0"
    env["NO_COLOR"] = "1"
    result = subprocess.run(
        [resolve_mypy_interpreter(REPO_ROOT), "-m", "mypy", *paths],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
        env=env,
    )
    if "No module named mypy" in result.stderr:
        raise RuntimeError(
            "mypy is not installed. Install dev extras:  pip install -e '.[dev]'\n"
            "This guard fails LOUD rather than passing silently — a type gate "
            "that no-ops when its checker is missing is worse than no gate."
        )
    return MypyRun(output=strip_ansi(result.stdout), returncode=result.returncode)


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
        help="Re-record the baseline for the selected files (use when a legacy "
        "file's errors legitimately move, never to silence a new error).",
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="Select every tracked .py file instead of the branch diff. Combine "
        "with --update-baseline to regenerate the whole baseline from scratch.",
    )
    args = parser.parse_args()

    names = (
        collect_all_python_names(_git)
        if args.all_files
        else collect_changed_names(args.base_ref, _git)
    )
    changed = select_changed_python_files(names, REPO_ROOT)
    if not changed:
        return 0

    run = run_mypy(changed)

    # Gate BEFORE any comparison, and on the --update-baseline path too: a
    # baseline recorded from a run that never happened is the same blindness,
    # merely persisted to disk where it is harder to see.
    incomplete = detect_incomplete_run(run.output, changed, run.returncode)
    if incomplete:
        print("Strict-typing ratchet COULD NOT RUN\n")
        print(f"mypy did not check the {len(changed)} file(s) it was given:\n")
        for reason in incomplete:
            print(f"  {reason}")
        print(
            "\nThis is NOT a pass. The result says nothing about the files you "
            "touched.\nA common cause is a prose comment that opens `# type:` — "
            "mypy reads it as a\nPEP 484 type comment, rejects it as invalid "
            "syntax and abandons the whole run.\nFix the blocking error above, "
            "then re-run."
        )
        return 1

    current = parse_mypy_errors(run.output)

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
