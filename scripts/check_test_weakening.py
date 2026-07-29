#!/usr/bin/env python3
"""Layer 4 tamper-protection: branch-diff guard (task #52).

Fails if the branch introduces a NET removal of ``assert`` statements in ANY
SINGLE file of the e2e scan set (``_E2E_PATH_RE``) OR a decrease in the ✅ count
in ``docs/contracts/BEHAVIOR_CONTRACT.md``.

The per-file netting is load-bearing, not a detail — see
``_per_file_assert_deltas``.  Summing globally lets an addition in one e2e module
mask a removal in another, which over a branch-sized window is the common case
rather than the exception.

WHY BRANCH-DIFF AND NOT ``git diff --cached`` (fixed 2026-07-29)
---------------------------------------------------------------
This guard used to source its ENTIRE input from ``git diff --cached``.  A CI
checkout has an empty index — nothing is staged — so ``diff_text`` was always
``""`` there: the hook executed, printed ``test-weakening guard OK.`` and exited
0 *regardless of what the PR contained*.  It ran in CI (via ``validate``'s
``pre-commit run --all-files``) and could not fail there, ever.  Correct trigger,
correct scope, correct wiring, and an assertion structurally incapable of firing
in the environment where it mattered most.  Tamper protection existed only for
contributors with hooks installed — precisely the population least likely to be
tampering.

The fix ports ``check_backend_bump``'s ADR-0080 contract:

  baseline = ``git merge-base origin/master HEAD``
  diff     = ``git diff <baseline> HEAD``  ∪  ``git diff --cached``
  ✅ counts = <baseline> vs (index if the contract is staged, else HEAD)

One pure ``check_diff()`` is fed from the same inputs in both modes, so local and
CI return the same verdict for the same repo state.  When ``origin/master`` is
unreachable (fresh clone, no remote, unborn HEAD) the baseline degrades to HEAD:
the branch diff collapses to empty and the check reduces to the legacy
staged-only behaviour — identical fail-open to ``check_backend_bump``.

CI NOTE: the branch mode needs real history.  ``validate``'s shallow checkout
cannot reach the merge-base, so it still fail-opens; the job that can actually go
red is ``invariant-checks``, which carries ``fetch-depth: 0`` and runs this
script with ``--ci --base origin/master`` (same shape as ``check_backend_bump``).

Override: set ``ALLOW_TEST_WEAKEN=1`` in the environment to bypass.  This is
intentionally a one-time env override, not a permanent flag, so weakening a
test always requires an explicit acknowledgement.

Usage:
    python scripts/check_test_weakening.py                    # pre-commit mode
    python scripts/check_test_weakening.py --ci --base origin/master   # CI mode
    ALLOW_TEST_WEAKEN=1 python scripts/check_test_weakening.py         # bypass
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT = _REPO_ROOT / "docs" / "contracts" / "BEHAVIOR_CONTRACT.md"
_STATUS_HDR_RE = re.compile(r"\*\*([0-9]+)\s*✅")

_ALLOW_ENV = "ALLOW_TEST_WEAKEN"

# Scan scope — MUST stay in lockstep with check_e2e_assertions.scan_paths()
# (yadgar/tests/e2e/**/*.py ∪ yadgar/tests/**/*e2e*.py).  Declared here as a
# regex because this guard reads `diff --git` header lines, not the filesystem;
# that independence is exactly why the two drifted apart before 2026-07-29.
# `yadgar/tests/core/test_tamper_guards.py::TestLayer3Layer4ScopeLockstep`
# asserts the agreement mechanically.
_E2E_PATH_RE = re.compile(r"yadgar/tests/(?:e2e/[^\s]*\.py|[^\s]*e2e[^\s]*\.py)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


GitRunner = Callable[[list[str]], str]

_CONTRACT_REL = "docs/contracts/BEHAVIOR_CONTRACT.md"


def _git(args: list[str]) -> str:
    """Run a git command and return stdout (empty string on non-zero exit)."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    return result.stdout if result.returncode == 0 else ""


def _per_file_assert_deltas(diff_text: str) -> dict[str, int]:
    """Return {e2e_path: lines_added - lines_removed} for 'assert' in *diff_text*.

    PER FILE, not one global sum.  A removal in test A is NOT compensated by an
    addition in test B — they are different tests.  This matters far more under
    branch-diff mode than it did under the old staged-only mode: a commit window
    is narrow so offsetting was rare, but over a whole branch it is the norm.
    Measured while running this car's own mutation test: a `-1` in
    ``test_consolidation_embedded_e2e.py`` was masked by a `+5` in
    ``test_code_graph_e2e.py`` earlier on the branch, global net `+4`, guard
    green.  Global-net over a branch window degrades the guard to "the branch's
    total e2e assert count went down", which is weaker than what it replaced.

    Only counts files in the e2e scan set (see ``_E2E_PATH_RE``).
    """
    deltas: dict[str, int] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            # Is this file in the e2e scan set? (widened 2026-07-29 — the old
            # `yadgar/tests/e2e/` pin missed six *e2e* modules living elsewhere)
            m = _E2E_PATH_RE.search(line)
            current = m.group(0) if m else None
            if current is not None:
                deltas.setdefault(current, 0)
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if re.search(r"\bassert\b", line):
                deltas[current] += 1
        elif line.startswith("-") and not line.startswith("---"):
            if re.search(r"\bassert\b", line):
                deltas[current] -= 1
    return deltas


def _green_count_from_text(text: str) -> int | None:
    """Extract the ✅ count from a BEHAVIOR_CONTRACT.md body, or None if not found."""
    m = _STATUS_HDR_RE.search(text)
    return int(m.group(1)) if m else None


def collect_inputs(base_ref: str, run_git: GitRunner) -> tuple[str, int | None, int | None]:
    """Gather ``check_diff`` inputs from the branch, not just the index.

    Baseline is ``merge-base(base_ref, HEAD)`` so the verdict is about the whole
    branch — which is the question CI asks — rather than about one commit.  The
    diff is the branch diff UNIONED with the staged diff, so pre-commit also sees
    the about-to-exist commit.  In CI the staged half is empty and the union
    collapses to the branch diff; same function, same verdict.

    Fallback: when the merge-base is unreachable, baseline becomes HEAD.  The
    branch diff is then empty by construction and the check degrades to the
    legacy staged-vs-HEAD comparison instead of raising or diffing a bogus ref.

    Returns:
        (diff_text, base_green, after_green)
    """
    merge_base = run_git(["merge-base", base_ref, "HEAD"]).strip()
    if not merge_base:
        merge_base = "HEAD"  # legacy per-commit fallback

    branch_diff = run_git(["diff", merge_base, "HEAD"])
    staged_diff = run_git(["diff", "--cached"])
    diff_text = branch_diff + staged_diff

    base_green = _green_count_from_text(run_git(["show", f"{merge_base}:{_CONTRACT_REL}"]))
    # "After" = the about-to-exist state: the index when the contract is staged,
    # else HEAD, else the working tree (contract not yet tracked).
    after_text = run_git(["show", f":{_CONTRACT_REL}"]) or run_git(
        ["show", f"HEAD:{_CONTRACT_REL}"]
    )
    if not after_text and _CONTRACT.is_file():
        after_text = _CONTRACT.read_text(encoding="utf-8")
    after_green = _green_count_from_text(after_text)

    return diff_text, base_green, after_green


def check_diff(diff_text: str, head_green: int | None, staged_green: int | None) -> list[str]:
    """Pure function: return violation strings given a diff + green counts."""
    errors: list[str] = []

    for path, delta in sorted(_per_file_assert_deltas(diff_text).items()):
        if delta < 0:
            errors.append(
                f"layer 4 — NET removal of {abs(delta)} 'assert' statement(s) in "
                f"{path}. If this is intentional, set ALLOW_TEST_WEAKEN=1 when "
                "committing."
            )

    if head_green is not None and staged_green is not None:
        if staged_green < head_green:
            errors.append(
                f"layer 4 — ✅ count dropped {head_green} → {staged_green} in "
                "docs/contracts/BEHAVIOR_CONTRACT.md. If this is intentional, set "
                "ALLOW_TEST_WEAKEN=1 when committing."
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if os.environ.get(_ALLOW_ENV, "").strip() == "1":
        print(f"check_test_weakening: bypassed ({_ALLOW_ENV}=1)")
        return 0

    # CI mode and pre-commit mode differ ONLY in the base ref and the label.
    # Both run the same collector into the same pure check_diff — the ADR-0080
    # parity contract: same inputs → same verdict.
    label = ""
    base_ref = "origin/master"
    if "--ci" in args:
        label = " [CI]"
        try:
            base_ref = args[args.index("--base") + 1]
        # Parenthesised tuple required — CI compiles on <py3.14 where the bare
        # `except X, Y:` form is a SyntaxError. fmt:skip keeps ruff (py314
        # target, PEP 758) from stripping the parens back to the bare form.
        except (ValueError, IndexError):  # fmt: skip
            print("test-weakening guard: ERROR: --ci requires --base <ref>", file=sys.stderr)
            return 1

    diff_text, base_green, after_green = collect_inputs(base_ref, _git)

    errors = check_diff(diff_text, base_green, after_green)
    if errors:
        print(f"test-weakening guard{label} FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"test-weakening guard{label} OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
