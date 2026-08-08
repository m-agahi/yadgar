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

SANCTIONING A DELETION — ``.test-weakening-allowlist.json`` (replaced the env
override, 2026-08-08)
---------------------------------------------------------------------------
This guard used to honour a single environment variable that bypassed the whole
run.  That was a blanket, invisible escape hatch: one variable silenced EVERY
file in the diff at once, and it left no trace in the diff a reviewer actually
reads.  It was used three times on the ADR-0215 branch-removal train, and a CI
``env:`` block wired it to a PR label so it could be set from outside the repo
entirely.  It is GONE — no environment variable bypasses this guard any more,
and none may be reintroduced.  The variable's name is deliberately not repeated
anywhere in this file, so that a grep for it finds only the allowlist that
replaced it (see ``.test-weakening-allowlist.json``) and the CHANGELOG history.

A sanctioned deletion is instead recorded per file, in the repo, in the diff:

    {"yadgar/tests/e2e/test_foo_e2e.py": {"allowed_delta": -12,
                                          "rationale": "...why, citing the ADR"}}

Governance mirrors the sibling allowlists (``.health-endpoint-allowlist.json``,
``.urllib-httperror-close-allowlist.json``, ``.container-runtime-allowlist.json``):
rationale >= 40 chars, malformed entries hard-fail.  Two rules are specific to
this guard and are the whole point of the mechanism:

  * An entry grants EXACTLY its recorded delta.  A file whose measured delta is
    WORSE than the allowlisted one fails — an entry cannot absorb future
    weakening of the same file.  A grandfathering entry is the same hole in a
    nicer coat.
  * A file with no entry fails, exactly as before.

STALE ENTRIES ARE A WARNING HERE, NOT A HARD ERROR — deliberately unlike the
sibling guards.  Their inputs are the filesystem; this guard's input is a diff
against ``merge-base(origin/master, HEAD)``, which MOVES.  The moment a branch
carrying an entry merges, its file leaves the diff and the entry goes stale
through no fault of anyone's — hard-failing would turn master red for everybody.
Stale entries are printed on every run so they get cleaned up rather than
rotting silently.  Do not "fix" this to match the siblings.

Usage:
    python scripts/check_test_weakening.py                    # pre-commit mode
    python scripts/check_test_weakening.py --ci --base origin/master   # CI mode
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT = _REPO_ROOT / "docs" / "contracts" / "BEHAVIOR_CONTRACT.md"
_STATUS_HDR_RE = re.compile(r"\*\*([0-9]+)\s*✅")

_ALLOWLIST_NAME = ".test-weakening-allowlist.json"
_MIN_RATIONALE = 40

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


def resolve_merge_base(base_ref: str, run_git: GitRunner) -> str:
    """Return merge-base(base_ref, HEAD), or "" when it cannot be resolved."""
    return run_git(["merge-base", base_ref, "HEAD"]).strip()


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
    merge_base = resolve_merge_base(base_ref, run_git) or "HEAD"  # legacy fallback

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


def load_allowlist(path: Path) -> dict:
    """Load the per-file allowlist, dropping ``_``-prefixed metadata keys.

    Same shape and same tolerance as the sibling loaders: a missing file is an
    empty allowlist (the strict pre-allowlist contract), malformed JSON raises.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object of {{'path': {{allowed_delta, ...}}}}")
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _allowlist_entry_errors(path: str, meta: object) -> list[str]:
    """Validate one entry's shape. A malformed entry grants nothing and hard-fails."""
    if not isinstance(meta, dict):
        return [f"MALFORMED allowlist entry {path!r}: value must be an object"]
    errors: list[str] = []
    delta = meta.get("allowed_delta")
    if not isinstance(delta, int) or isinstance(delta, bool):
        errors.append(f"MALFORMED allowlist entry {path!r}: 'allowed_delta' must be an integer")
    elif delta >= 0:
        # An entry only ever sanctions a REMOVAL. A zero/positive allowance is
        # either a typo or an attempt to register a file that needs no entry.
        errors.append(
            f"MALFORMED allowlist entry {path!r}: 'allowed_delta' must be negative "
            f"(got {delta}) — an entry sanctions a removal, nothing else"
        )
    rationale = meta.get("rationale", "")
    if not isinstance(rationale, str) or len(rationale.strip()) < _MIN_RATIONALE:
        got = len(rationale.strip()) if isinstance(rationale, str) else 0
        errors.append(
            f"MALFORMED allowlist entry {path!r}: rationale must be >= "
            f"{_MIN_RATIONALE} chars (got {got})"
        )
    return errors


def stale_allowlist_entries(diff_text: str, allowlist: dict) -> list[str]:
    """Return warning strings for entries that no longer describe reality.

    Two shapes, both meaning "this entry over-grants and should be removed or
    tightened": the file is no longer in the diff at all (the post-merge shape),
    or its measured delta is BETTER than the recorded allowance.

    Warnings, not errors — see the module docstring: the baseline is a moving
    merge-base, so a correct entry goes stale on merge through nobody's fault.
    """
    deltas = _per_file_assert_deltas(diff_text)
    warnings: list[str] = []
    for path, meta in sorted(allowlist.items()):
        if not isinstance(meta, dict) or not isinstance(meta.get("allowed_delta"), int):
            continue  # malformed — already reported as a hard error by check_diff
        allowed = meta["allowed_delta"]
        measured = deltas.get(path)
        if measured is None:
            warnings.append(
                f"STALE allowlist entry {path!r}: the file is not in the branch diff "
                f"at all — remove it from {_ALLOWLIST_NAME}"
            )
        elif measured > allowed:
            warnings.append(
                f"STALE allowlist entry {path!r}: measured delta {measured:+d} is better "
                f"than the allowed {allowed:+d} — tighten or remove it in {_ALLOWLIST_NAME}"
            )
    return warnings


def check_diff(
    diff_text: str,
    head_green: int | None,
    staged_green: int | None,
    allowlist: dict | None = None,
) -> list[str]:
    """Pure function: return violation strings given a diff + green counts.

    *allowlist* is ``{path: {"allowed_delta": int, "rationale": str}}``. Omitting
    it (or passing ``{}``) is the strict contract: every net removal violates.
    """
    allowlist = allowlist or {}
    errors: list[str] = []

    for path, meta in sorted(allowlist.items()):
        errors.extend(_allowlist_entry_errors(path, meta))

    for path, delta in sorted(_per_file_assert_deltas(diff_text).items()):
        if delta >= 0:
            continue
        entry = allowlist.get(path)
        allowed = entry.get("allowed_delta") if isinstance(entry, dict) else None
        if isinstance(allowed, int) and not isinstance(allowed, bool) and allowed < 0:
            # Sign convention: deltas are negative, so "worse" is SMALLER.
            # -12 against an allowed -12 passes; -13 does not.
            if delta >= allowed:
                continue
            errors.append(
                f"layer 4 — NET removal of {abs(delta)} 'assert' statement(s) in {path} "
                f"({delta:+d}) EXCEEDS its allowlisted {allowed:+d}. An allowlist entry "
                f"grants exactly its recorded delta; it does not absorb further "
                f"weakening. Justify and update the entry in {_ALLOWLIST_NAME}, or "
                f"restore the assertions."
            )
            continue
        errors.append(
            f"layer 4 — NET removal of {abs(delta)} 'assert' statement(s) in {path}. "
            f"If this deletion is sanctioned, add an entry to {_ALLOWLIST_NAME} "
            f'recording the exact delta ({delta:+d}) and why — e.g. {{"{path}": '
            f'{{"allowed_delta": {delta}, "rationale": "...citing the ADR"}}}}.'
        )

    if head_green is not None and staged_green is not None:
        if staged_green < head_green:
            errors.append(
                f"layer 4 — ✅ count dropped {head_green} → {staged_green} in "
                "docs/contracts/BEHAVIOR_CONTRACT.md. Restore the rows, or land the "
                "contract change in a commit that explains which behaviours stopped "
                "being covered and why."
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    # CI mode and pre-commit mode differ ONLY in the base ref and the label.
    # Both run the same collector into the same pure check_diff — the ADR-0080
    # parity contract: same inputs → same verdict.
    label = ""
    base_ref = "origin/master"
    ci_mode = "--ci" in args
    if ci_mode:
        label = " [CI]"
        try:
            base_ref = args[args.index("--base") + 1]
        # Parenthesised tuple required — CI compiles on <py3.14 where the bare
        # `except X, Y:` form is a SyntaxError. fmt:skip keeps ruff (py314
        # target, PEP 758) from stripping the parens back to the bare form.
        except (ValueError, IndexError):  # fmt: skip
            print("test-weakening guard: ERROR: --ci requires --base <ref>", file=sys.stderr)
            return 1

        # HARD ERROR, not a fail-open. Passing --ci --base <ref> IS the caller
        # asserting that base ref is reachable. If it is not (shallow checkout,
        # unfetched ref, dubious-ownership refusal), collect_inputs would silently
        # degrade to an empty branch diff and this step would print OK / exit 0 —
        # indistinguishable in the CI log from a genuine pass, and nothing would
        # reveal that the guard never engaged. That is precisely the CI-inertness
        # this script was rewritten to eliminate; do not reintroduce it one layer
        # up. The pre-commit path keeps the fail-open, where a fresh clone with no
        # remote is a legitimate state.
        merge_base = resolve_merge_base(base_ref, _git)
        if not merge_base:
            print(
                f"test-weakening guard{label} ERROR: cannot resolve "
                f"merge-base({base_ref}, HEAD). The branch diff would be empty and this "
                "check would pass vacuously. Ensure the job checks out with "
                "fetch-depth: 0 and that the base ref is fetched.",
                file=sys.stderr,
            )
            return 1

    diff_text, base_green, after_green = collect_inputs(base_ref, _git)

    try:
        allowlist = load_allowlist(_REPO_ROOT / _ALLOWLIST_NAME)
    except ValueError as exc:
        print(f"test-weakening guard{label} ERROR: {exc}", file=sys.stderr)
        return 1

    # Printed on EVERY run, pass or fail — a dead entry that nobody is told about
    # is how an allowlist rots into a permanent grandfather clause.
    for warning in stale_allowlist_entries(diff_text, allowlist):
        print(f"test-weakening guard{label} WARNING: {warning}", file=sys.stderr)

    errors = check_diff(diff_text, base_green, after_green, allowlist)
    if errors:
        print(f"test-weakening guard{label} FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    # Echo the baseline in CI so the log PROVES what was compared, rather than
    # leaving "OK" ambiguous between "engaged and clean" and "never engaged".
    if ci_mode:
        print(f"test-weakening guard{label} OK — branch diff vs merge-base {merge_base[:12]}.")
    else:
        print("test-weakening guard OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
