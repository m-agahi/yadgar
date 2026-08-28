#!/usr/bin/env python3
"""Layer 4 tamper-protection: branch-diff guard (task #52, widened task 379).

Fails if the branch WEAKENS any test file under ``yadgar/tests/`` (see
``_TEST_PATH_RE``) OR decreases the ✅ count in
``docs/contracts/BEHAVIOR_CONTRACT.md``.

SCOPE — WHY THIS IS NOT E2E-ONLY ANY MORE (task 379, 2026-08-26)
----------------------------------------------------------------
This guard used to scan ``_E2E_PATH_RE`` alone: ``yadgar/tests/e2e/**`` plus
modules with ``e2e`` in the filename.  ADR-0430 names this script as THE
mechanism enforcing "no test may be weakened to reach green", and the scan
could only ever fail on an e2e file.  Measured on PR #68: 29 test files
changed, 4,224 lines, ZERO of them e2e.  The guard passed unconditionally while
three assertions were relaxed, and ``.test-weakening-allowlist.json`` was never
consulted — there was nothing in scope to consult it for.  Correct trigger,
correct wiring, scope structurally incapable of covering the files that
actually change: the same defect class as the ``git diff --cached`` blindness
described below, one layer up.

``_E2E_PATH_RE`` survives, but ONLY as layer 3's lockstep declaration — layer 3
(``check_e2e_assertions``) still scans e2e alone, and
``test_tamper_guards.py::TestLayer3Layer4ScopeLockstep`` still asserts the two
agree.  Layer 4's own scan set is ``_TEST_PATH_RE``.

THE THREE WEAKENING SHAPES
---------------------------
A net-``assert``-count rule is structurally blind to the relaxation that
motivated this widening.  ``assert schema == ""`` rewritten to
``assert not schema`` removes one ``assert`` line and adds one: net ZERO.  So
three metrics are netted per file, not one:

  ``asserts``  net change in ``assert`` / ``assertX(`` lines.  Negative = an
               assertion was deleted from a module that kept its name.
  ``strict``   net change in EXACT-VALUE assertions — those carrying ``==``,
               ``!=``, ``is None`` / ``is not None`` / ``is True`` / ``is
               False``, or ``assertEqual`` / ``assertNotEqual`` / ``assertIs*``.
               Negative = an exact-value check was relaxed to a looser one
               (``== ""`` → ``not x``, ``== N`` → ``>= N``, ``assertEqual`` →
               ``assertIn``).
  ``skips``    net change in ``pytest.skip(`` / ``pytest.xfail(`` /
               ``pytest.importorskip(`` / ``mark.skip`` / ``mark.skipif`` /
               ``mark.xfail``.  POSITIVE = a test that used to run was silenced.

``strict`` deliberately treats ``is None`` as exact, so swapping ``== ""`` for
``is None`` — a CONTRACT change, not a weakening — does not fire.  That
discriminator is what keeps the rule usable: measured over the whole
``train/bug-bag-2-2026-08-23`` diff (39 test files, 5,644 inserted lines) these
three rules produce exactly ONE violation, the real relaxation in
``yadgar/tests/backend/test_cls_store.py``, and zero false positives on the
other 38 files.

A file added by the branch (``new file mode``) is exempt from the ``skips``
rule and a rename is exempt from all three: neither weakens anything that
previously ran.

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
                                           ∪  ``git diff``   (task 410)
  ✅ counts = <baseline> vs (index if the contract is staged, else HEAD)

THE UNSTAGED SEGMENT (task 410, 2026-08-28)
--------------------------------------------
The union above ended at ``--cached`` for a year.  Both remaining segments read
COMMITTED or INDEXED state, so a working-tree edit was invisible and a run
before ``git add`` printed ``test-weakening guard OK.`` over a diff it had
never seen — the same reports-success-while-seeing-nothing shape as the two
defects above it, at the one moment an author is most likely to consult the
guard.  ``git diff`` (index → worktree) is now the third segment.

The three segments telescope (``merge_base..HEAD`` + ``HEAD..index`` +
``index..worktree``), so nothing is counted twice.  This does NOT convert the
guard into "is this commit a weakening" — it never was: the branch segment
already fails today's commit over a weakening committed last week.  Reading
the working tree extends the existing contract by one segment.  It goes LAST
in the concatenation so that a ``new file mode`` header staged in the index is
read before any worktree body line for the same file.

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

An entry carries at least one allowance key, one per shape — ``allowed_delta``
(negative, ``asserts``), ``allowed_strict_delta`` (negative, ``strict``),
``allowed_new_skips`` (positive, ``skips``).  They are INDEPENDENT: sanctioning
a relaxed comparison is not a licence to delete an assertion from the same
file.  An entry granting nothing at all is a typo and hard-fails.

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

# LAYER 4's OWN scan set (task 379) — every test module, not just the e2e ones.
# `_E2E_PATH_RE` above is now purely layer 3's lockstep declaration; scoping
# layer 4 to it made the guard incapable of failing on the 29 non-e2e test files
# PR #68 changed. See the module docstring.
_TEST_PATH_RE = re.compile(r"yadgar/tests/[^\s]*\.py")

# An `assert` statement, or a unittest `assertX(...)` call.  `\bassert\b` alone
# cannot match `assertEqual` (no word boundary before a word character), so the
# unittest family needs its own alternative.
_ASSERT_RE = re.compile(r"\bassert\b|\bassert[A-Z]\w*\s*\(")

# An EXACT-VALUE assertion.  `is None` / `is True` / `is False` count as exact
# on purpose: rewriting `== ""` to `is None` is a contract change, not a
# weakening, and flagging it would make this rule unusable (car K's sibling edit
# in test_patterns_unit.py is precisely that swap).
_STRICT_ASSERT_RE = re.compile(
    r"==|!="
    r"|\bis\s+(?:not\s+)?(?:None|True|False)\b"
    r"|\bassert(?:Equal|NotEqual|Is|IsNot|IsNone|IsNotNone)\b"
)

# Anything that stops a test from running.
#
# ANCHORED to statement/decorator position (`^\s*`), unlike the two assert
# patterns.  A skip guard is always a bare statement or a decorator, never a
# substring of prose — whereas a meta-test that BUILDS a diff fixture writes
# lines like `"+        pytest.skip(\"flaky\")\n"` inside a string literal, and
# an unanchored pattern counts those as real added skips.  Found by running the
# widened guard over this car's own commit: the five diff fixtures in
# test_tamper_guards.py below were reported as five new skip guards.  Anchoring
# is the precise fix (the fixture line starts with whitespace then a quote),
# and it costs no real detection: `        pytest.skip(...)` still matches.
_SKIP_RE = re.compile(
    r"^\s*(?:"
    r"pytest\.(?:skip|xfail|importorskip)\s*\("
    r"|@\s*pytest\.mark\.(?:skip|skipif|xfail)\b"
    r"|pytestmark\s*=.*\bmark\.(?:skip|skipif|xfail)\b"
    r")"
)

# Allowance keys, one per shape.  (json key, metric attribute, sign, label)
# `sign` is the direction that means WORSE: -1 for the count metrics (fewer
# assertions is worse), +1 for skips (more silencing is worse).
_SHAPES = (
    ("allowed_delta", "asserts", -1),
    ("allowed_strict_delta", "strict", -1),
    ("allowed_new_skips", "skips", +1),
)


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


class FileMetrics:
    """The three netted weakening metrics for ONE file in the diff.

    ``asserts`` and ``strict`` are added-minus-removed (negative = worse);
    ``skips`` is added-minus-removed too, but POSITIVE is worse — a skip guard
    that was not there before silences a test that used to run.

    Deliberately a plain class, NOT a ``@dataclass``: the meta-tests load this
    script with ``importlib.util.module_from_spec`` without registering it in
    ``sys.modules``, and ``dataclasses`` resolves annotations through
    ``sys.modules[cls.__module__]`` — which is ``None`` under that loader, so a
    dataclass here fails at import with an AttributeError.
    """

    __slots__ = ("asserts", "strict", "skips", "is_new", "is_rename")

    def __init__(self) -> None:
        self.asserts = 0
        self.strict = 0
        self.skips = 0
        self.is_new = False
        self.is_rename = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"FileMetrics(asserts={self.asserts:+d}, strict={self.strict:+d}, "
            f"skips={self.skips:+d}, is_new={self.is_new}, is_rename={self.is_rename})"
        )


def _per_file_metrics(diff_text: str) -> dict[str, FileMetrics]:
    """Return ``{test_path: FileMetrics}`` for every ``yadgar/tests/`` file in *diff_text*.

    PER FILE, not one global sum.  A removal in test A is NOT compensated by an
    addition in test B — they are different tests.  This matters far more under
    branch-diff mode than it did under the old staged-only mode: a commit window
    is narrow so offsetting was rare, but over a whole branch it is the norm.
    Measured while running this guard's own mutation test: a `-1` in
    ``test_consolidation_embedded_e2e.py`` was masked by a `+5` in
    ``test_code_graph_e2e.py`` earlier on the branch, global net `+4`, guard
    green.  Global-net over a branch window degrades the guard to "the branch's
    total assert count went down", which is weaker than what it replaced.

    Scope is ``_TEST_PATH_RE`` — every test module (task 379), not the e2e
    subset the guard used to be pinned to.
    """
    metrics: dict[str, FileMetrics] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            m = _TEST_PATH_RE.search(line)
            current = m.group(0) if m else None
            if current is not None:
                metrics.setdefault(current, FileMetrics())
            continue
        if current is None:
            continue
        # File-level headers, read before the +/- body lines they describe.
        if line.startswith("new file mode"):
            metrics[current].is_new = True
            continue
        if line.startswith(("rename from", "rename to")):
            metrics[current].is_rename = True
            continue
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            sign = 1
        elif line.startswith("-"):
            sign = -1
        else:
            continue
        body = line[1:]
        entry = metrics[current]
        if _ASSERT_RE.search(body):
            entry.asserts += sign
            if _STRICT_ASSERT_RE.search(body):
                entry.strict += sign
        if _SKIP_RE.search(body):
            entry.skips += sign
    return metrics


def _measured(entry: FileMetrics, attr: str) -> int:
    return int(getattr(entry, attr))


def _per_file_assert_deltas(diff_text: str) -> dict[str, int]:
    """Back-compat view: ``{test_path: net assert delta}``.

    Kept because it is the shape the guard's own error text and several
    meta-tests reason about; ``_per_file_metrics`` is the full record.
    """
    return {path: m.asserts for path, m in _per_file_metrics(diff_text).items()}


def _green_count_from_text(text: str) -> int | None:
    """Extract the ✅ count from a BEHAVIOR_CONTRACT.md body, or None if not found."""
    m = _STATUS_HDR_RE.search(text)
    return int(m.group(1)) if m else None


def resolve_merge_base(base_ref: str, run_git: GitRunner) -> str:
    """Return merge-base(base_ref, HEAD), or "" when it cannot be resolved."""
    return run_git(["merge-base", base_ref, "HEAD"]).strip()


def collect_inputs(base_ref: str, run_git: GitRunner) -> tuple[str, int | None, int | None]:
    """Gather ``check_diff`` inputs from the whole tree, not just the index.

    Baseline is ``merge-base(base_ref, HEAD)`` so the verdict is about the whole
    branch — which is the question CI asks — rather than about one commit.  The
    diff is the union of THREE telescoping segments:

        merge_base..HEAD    the branch diff       ``git diff <base> HEAD``
        HEAD..index         the about-to-commit   ``git diff --cached``
        index..worktree     the unsaved edit      ``git diff``

    They abut rather than overlap, so nothing is double-counted and the sum is
    exactly ``merge_base..worktree``.  In CI the last two are empty and the
    union collapses to the branch diff; same function, same verdict.

    THE THIRD SEGMENT (task 410).  It used to be absent, which made a
    standalone run dishonest: ``python scripts/check_test_weakening.py`` before
    ``git add`` printed ``test-weakening guard OK.`` over a working tree it had
    never looked at.  Both of the other segments read committed or indexed
    state, so an edit that existed only on disk could not be seen — the same
    shape as the ``--cached``-only blindness described in the module docstring,
    one step further out.  Note this guard was never scoped to "is THIS COMMIT
    a weakening": the branch segment already fails today's commit for a
    weakening committed last week.  Reading the working tree extends that
    contract by one segment; it does not introduce a new kind of friction.

    Order matters and the worktree segment goes LAST.  ``_per_file_metrics``
    streams the diff and applies a ``new file mode`` header to the body lines
    that follow it for that file, so a file created in the index and edited
    further on disk must have its header read before those later body lines —
    otherwise the "a new file silences nothing" exemption stops applying to
    them.

    Fallback: when the merge-base is unreachable, baseline becomes HEAD.  The
    branch diff is then empty by construction and the check degrades to the
    legacy staged-vs-HEAD comparison instead of raising or diffing a bogus ref.

    Returns:
        (diff_text, base_green, after_green)
    """
    merge_base = resolve_merge_base(base_ref, run_git) or "HEAD"  # legacy fallback

    branch_diff = run_git(["diff", merge_base, "HEAD"])
    staged_diff = run_git(["diff", "--cached"])
    unstaged_diff = run_git(["diff"])
    diff_text = branch_diff + staged_diff + unstaged_diff

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
    """Validate one entry's shape. A malformed entry grants nothing and hard-fails.

    An entry carries at least one allowance key (``_SHAPES``), each validated
    independently.  The signs are NOT interchangeable: the count allowances must
    be negative (they sanction a REMOVAL), the skip allowance positive (it
    sanctions an ADDITION).  A zero/wrong-signed value is either a typo or an
    attempt to register a file that needs no entry.
    """
    if not isinstance(meta, dict):
        return [f"MALFORMED allowlist entry {path!r}: value must be an object"]
    errors: list[str] = []

    present = [key for key, _attr, _sign in _SHAPES if key in meta]
    if not present:
        errors.append(
            f"MALFORMED allowlist entry {path!r}: grants nothing — name at least one of "
            + ", ".join(key for key, _a, _s in _SHAPES)
        )
    for key, _attr, sign in _SHAPES:
        if key not in meta:
            continue
        value = meta.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"MALFORMED allowlist entry {path!r}: {key!r} must be an integer")
            continue
        if sign < 0 and value >= 0:
            errors.append(
                f"MALFORMED allowlist entry {path!r}: {key!r} must be negative "
                f"(got {value}) — an entry sanctions a removal, nothing else"
            )
        elif sign > 0 and value <= 0:
            errors.append(
                f"MALFORMED allowlist entry {path!r}: {key!r} must be positive "
                f"(got {value}) — an entry sanctions an added skip, nothing else"
            )

    rationale = meta.get("rationale", "")
    if not isinstance(rationale, str) or len(rationale.strip()) < _MIN_RATIONALE:
        got = len(rationale.strip()) if isinstance(rationale, str) else 0
        errors.append(
            f"MALFORMED allowlist entry {path!r}: rationale must be >= "
            f"{_MIN_RATIONALE} chars (got {got})"
        )
    return errors


def _granted(meta: object, key: str, sign: int) -> int | None:
    """Return the well-formed allowance *meta* records under *key*, else None."""
    if not isinstance(meta, dict):
        return None
    value = meta.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if sign < 0 and value >= 0:
        return None
    if sign > 0 and value <= 0:
        return None
    return value


def stale_allowlist_entries(diff_text: str, allowlist: dict) -> list[str]:
    """Return warning strings for entries that no longer describe reality.

    Two shapes, both meaning "this entry over-grants and should be removed or
    tightened": the file is no longer in the diff at all (the post-merge shape),
    or its measured delta is BETTER than the recorded allowance — checked per
    SHAPE, so an entry that is still accurate about one metric and stale about
    another says so.

    Warnings, not errors — see the module docstring: the baseline is a moving
    merge-base, so a correct entry goes stale on merge through nobody's fault.
    """
    metrics = _per_file_metrics(diff_text)
    warnings: list[str] = []
    for path, meta in sorted(allowlist.items()):
        entry = metrics.get(path)
        if entry is None:
            if any(_granted(meta, key, sign) is not None for key, _a, sign in _SHAPES):
                warnings.append(
                    f"STALE allowlist entry {path!r}: the file is not in the branch diff "
                    f"at all — remove it from {_ALLOWLIST_NAME}"
                )
            continue
        for key, attr, sign in _SHAPES:
            allowed = _granted(meta, key, sign)
            if allowed is None:
                continue  # absent or malformed — check_diff reports malformed
            measured = _measured(entry, attr)
            better = measured > allowed if sign < 0 else measured < allowed
            if better:
                warnings.append(
                    f"STALE allowlist entry {path!r}: measured {attr} {measured:+d} is "
                    f"better than the allowed {allowed:+d} — tighten or remove "
                    f"{key!r} in {_ALLOWLIST_NAME}"
                )
    return warnings


_SHAPE_MESSAGES = {
    "asserts": (
        "NET removal of {n} 'assert' statement(s) in {path} ({delta:+d}) — an "
        "assertion was deleted from a module that kept its name"
    ),
    "strict": (
        "NET removal of {n} exact-value assertion(s) in {path} ({delta:+d}) — a "
        "strict check (== / != / is None / assertEqual) was relaxed to a looser "
        "one. Net 'assert' count does NOT move when `assert x == \"\"` becomes "
        "`assert not x`, which is why this is counted separately"
    ),
    "skips": (
        "{n} new skip guard(s) in {path} ({delta:+d}) — pytest.skip / xfail / "
        "importorskip silences a test that used to run"
    ),
}


def check_diff(
    diff_text: str,
    head_green: int | None,
    staged_green: int | None,
    allowlist: dict | None = None,
) -> list[str]:
    """Pure function: return violation strings given a diff + green counts.

    *allowlist* is ``{path: {<allowance key>: int, "rationale": str}}``. Omitting
    it (or passing ``{}``) is the strict contract: every weakening violates.
    """
    allowlist = allowlist or {}
    errors: list[str] = []

    for path, meta in sorted(allowlist.items()):
        errors.extend(_allowlist_entry_errors(path, meta))

    for path, entry in sorted(_per_file_metrics(diff_text).items()):
        if entry.is_rename:
            # A move is not a weakening; blaming the destination for lines the
            # rename carried across would make every reorganisation red.
            continue
        meta = allowlist.get(path)
        for key, attr, sign in _SHAPES:
            measured = _measured(entry, attr)
            worse_than_zero = measured < 0 if sign < 0 else measured > 0
            if not worse_than_zero:
                continue
            if attr == "skips" and entry.is_new:
                # Nothing was silenced — the file did not exist before.
                continue
            allowed = _granted(meta, key, sign)
            if allowed is not None:
                # Sign convention: an entry grants EXACTLY its recorded delta.
                # -12 against an allowed -12 passes; -13 does not.
                within = measured >= allowed if sign < 0 else measured <= allowed
                if within:
                    continue
                errors.append(
                    "layer 4 — "
                    + _SHAPE_MESSAGES[attr].format(n=abs(measured), path=path, delta=measured)
                    + f". This EXCEEDS its allowlisted {key}={allowed:+d}. An allowlist "
                    f"entry grants exactly its recorded delta; it does not absorb "
                    f"further weakening. Justify and update the entry in "
                    f"{_ALLOWLIST_NAME}, or restore the assertions."
                )
                continue
            errors.append(
                "layer 4 — "
                + _SHAPE_MESSAGES[attr].format(n=abs(measured), path=path, delta=measured)
                + f". If this change is sanctioned, add an entry to {_ALLOWLIST_NAME} "
                f'recording the exact delta and why — e.g. {{"{path}": '
                f'{{"{key}": {measured}, "rationale": "...citing the ADR"}}}}.'
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
