#!/usr/bin/env python3
"""Guard: inline `# noqa: XXXX` comments must target rules ruff actually runs
under the resolved pyproject `[tool.ruff.lint]` config, and must state a rule.

WHY THIS EXISTS (Car C2 / task 313, bug-bag-2 train 2026-08-23)
--------------------------------------------------------------
The sibling ``check_ruff_ignores_liveness.py`` (car 7, 2026-08-13) only
covers ``pyproject.toml`` ``[tool.ruff.lint.per-file-ignores]`` — the file-
level table. Inline ``# noqa: XXXX`` suppressions on individual lines were
never gated, so hundreds of sites in this repo were silently suppressing a
rule that ruff does not run. Every one of those suppressions hides the NEXT
violation of the named rule in that file with no signal — the same
silent-suppression class the per-file-ignores gate was built to catch, one
rung deeper.

Two failure classes caught here:

  (a) INERT-RULE — ``# noqa: XXXX`` where ruff does not run ``XXXX``: it is
      absent from ``select``, OR it is selected by a family prefix and then
      ignore-overridden by a more specific ``ignore`` entry (see ``is_live``
      — this project selects ``BLE`` and ignores ``BLE001``, so BLE001 is
      OFF and all 155 of its noqa sites are decorative). The named rule is
      dead, so the suppression silences nothing — but a developer who later
      makes the rule live would suddenly find it suppressed with no audit
      trail.

  (b) BARE / EMPTY — ``# noqa`` or ``# noqa:`` with no rule code stated.
      A blanket suppression is too coarse to audit; the gate requires a
      named rule. Prose after a bare marker does NOT make it prose: ruff
      0.15.21 treats ``# noqa cannot suppress hard violations`` as a live
      blanket suppression of that line (verified by probe), so the gate
      flags it too.

  (c) UNSCANNABLE — a file :mod:`tokenize` cannot read. The gate reports it
      rather than falling back to raw-line matching, because a guard that
      silently narrows its own coverage reports a cleanliness it did not
      check. See ``iter_comments``.

WHAT THE GATE LOOKS AT (task 393, 2026-08-27)
---------------------------------------------
COMMENT TOKENS, not raw lines, and the noqa need not end its line. The
original ``_NOQA_RE`` was anchored with ``\\s*$``, requiring the noqa comment
to be the last thing on the line — which excluded ``# noqa: CODE — reason``,
the exact form this project's own convention mandates. 292 of 1355 real inert
sites in ``yadgar/`` were therefore invisible, and the baseline frozen from
that scan was structurally incomplete: the gate was enforcing a subset of the
tree while claiming the tree. Tokenising is what lets the regex drop the
anchor safely — a looser regex over raw lines would have started counting
``# noqa`` text that lives inside string literals and docstrings, of which
this repo has several.

A further class (VACUOUS — noqa: XXXX where XXXX IS in select but the line
below doesn't trigger XXXX) is left to ``check_ruff_ignores_liveness``'s
spirit and is intentionally NOT checked here: it would require running
ruff per-site, which is O(lines) subprocesses for marginal signal — the
named-rule resolution against ``select``/``ignore`` already catches the
silent-suppression class the audit found.

THE BASELINE IS KEYED ON (relpath, code) + A COUNT — NOT ON LINE NUMBERS
------------------------------------------------------------------------
(task 383, 2026-08-26.) It used to be keyed on ``(relpath, lineno, code)``.
Line numbers are volatile: ANY commit that shifts lines in a covered file
forced a full regeneration of the baseline — and a regeneration is
indistinguishable from quietly absorbing that same commit's NEW inert
markers. Measured on the bug-bag-2 train: the file grew 951 -> 988 rows with
no commit message mentioning it, hiding +25 new inert sites in
``core/server/http.py``, +7 in ``_shared/storage/sql/mariadb.py`` and +1 each
in four more files. Nothing was lying; the key simply could not tell growth
apart from a shift.

Keying on ``(relpath, code)`` with a stored COUNT fixes both halves:

  * a pure line shift changes nothing, so no regeneration is needed, so
    regeneration stops being a routine event that can carry a payload; and
  * the count IS the ratchet — one more inert ``# noqa: PLC0415`` in a file
    that already has 38 of them takes the count to 39 and FAILS, where the
    old scheme saw an ordinary line-number churn.

The ratchet only loosens by a deliberate, stated edit: counts may FALL
freely (clean a site up and the gate stays green), never rise silently.

Exit codes:
  0  no unbaselined inert-rule / bare noqa sites in yadgar/
  1  one or more sites flagged

Regenerate (rare — see above; state the before/after counts in the commit):
  python scripts/check_ruff_noqa_liveness.py --write-baseline
"""

from __future__ import annotations

import io
import re
import sys
import tokenize
import tomllib
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# Recognise both single-code (hash + ``noqa:`` + code, e.g. E402) and
# comma-separated codes (e.g. E402 + F401), AND bare (hash + ``noqa``
# with no colon) / empty (hash + ``noqa:`` with nothing or whitespace
# after) — the BARE class.
#
# APPLIED TO A COMMENT TOKEN, NOT A RAW LINE, AND NOT ANCHORED AT EOL
# (task 393). It used to be ``^[^#\n]*#\s*noqa(?::\s*([A-Z0-9,\s]*?))?\s*$``:
# the trailing ``\s*$`` required the noqa comment to be the LAST thing on its
# line, which excluded the very form ``pyproject.toml`` mandates for this
# repo — hash + ``noqa: CODE — reason``. Measured on the tree the day this
# was fixed: 292 of 1355 real inert sites in ``yadgar/`` were invisible to the
# gate, i.e. the baseline it enforced was structurally incomplete and it
# reported a cleanliness it had not checked.
#
# Behaviour verified against ruff 0.15.21 rather than assumed (probe files,
# ``select = ["F"]``, unused ``import os``). Written hash-first so this
# comment does not itself become a directive:
#
#   hash + ``noqa: F401 — reason``     -> suppressed (mandated form; MUST match)
#   hash + ``noqa:F401,E402 trailing`` -> suppressed (no space, trailing prose)
#   hash + ``noqa cannot suppress X``  -> suppressed (BLANKET — prose after a
#                                         bare marker is still a directive)
#   hash + ``NOQA``                    -> suppressed (ruff is case-insensitive)
#   hash + ``blah noqa blah``          -> NOT suppressed (must follow the hash)
#   hash + ``noqable prose``           -> NOT suppressed (whole word only)
#
# ``finditer`` (not ``search``): one comment may carry two markers, e.g. a
# ``type: ignore`` followed by a hash + ``noqa: F401``, and ruff acts on the
# second.
#
# Capture groups:
#   codes: raw code-string (possibly absent) — checked for INERT-RULE / BARE.
#          Absent or empty → BARE, matching ruff, which treats a marker with
#          no parsable code as a blanket suppression (or rejects it outright
#          with an "Invalid directive" warning). Either way it names no rule,
#          so it cannot be audited — which is what the gate demands.
_NOQA_RE = re.compile(
    r"#[ \t]*(?i:noqa)"  # the marker, after a `#`; ruff is case-insensitive
    r"(?![A-Za-z0-9_])"  # ...as a whole word: `noqable` prose is not one
    r"(?P<colon>:)?[ \t]*"
    r"(?P<codes>[A-Z]+[0-9]+(?:[ \t]*,[ \t]*[A-Z]+[0-9]+)*)?"
)

# A valid ruff code shape: 1-3 uppercase letters followed by 3-4 digits.
# Matches: A002, B017 (flake8-pytest-style), E402 (pycodestyle),
# PLC0414 (pylint 4-digit), BLE001 (flake8-blind-except).
_CODE_RE = re.compile(r"^[A-Z]{1,3}\d{3,4}$")

# Baseline allowlist — pre-C2 inert noqa sites the gate accepts during
# the cleanup window. Format: ``<relpath>:<CODE>:<count>`` per line.
_BASELINE_FILENAME = "baseline_noqa_inert.txt"

# The header the generator ALWAYS emits. Modelled on
# ``.swallow-baseline.json``'s ``_header`` (ADR-0420): a frozen baseline with
# no stated status gets read as a vetted allowlist, which is how unreviewed
# debt turns into "somebody approved this". This file carried no header at
# all until task 383. It is written by ``--write-baseline`` rather than kept
# by hand precisely so a regeneration cannot drop it — a hand-added header
# that the generator silently discards would be the laundering class one
# level up.
_BASELINE_HEADER = """\
# AUTO-GENERATED BASELINE — inline `# noqa` liveness gate (task 313 / 383).
#
# THESE ENTRIES ARE UNREVIEWED DEBT FROZEN IN PLACE. THEY ARE NOT VETTED
# EXEMPTIONS. Nobody read these sites and decided each suppression was
# correct; they are the state of the tree on the day the gate landed,
# recorded so the gate could be enforced at zero NEW violations. Do not cite
# an entry's presence here as evidence that its noqa was reviewed and
# approved.
#
# Every row names a rule ruff does NOT run for this project — either absent
# from `[tool.ruff.lint] select`, or selected by family prefix and then
# ignore-overridden (this project selects `BLE` and ignores `BLE001`). The
# suppression silences nothing today: it is dead code that would spring to
# life, silently, the moment someone makes that rule live. That is the
# cleanup backlog this file IS.
#
# THIS IS A RATCHET. Counts may be REMOVED or LOWERED freely — fix a site,
# drop the row. A count may NEVER RISE, and a new (file, code) pair may never
# appear, without a stated reason in the commit message: that is a new inert
# suppression, which is exactly what the gate exists to refuse.
#
# Keyed on (relpath, code) + count, NOT line numbers — line-keyed rows forced
# a regeneration on every line shift, and a regeneration is indistinguishable
# from absorbing that commit's own new markers (measured: 951 -> 988 rows on
# one train, unmentioned).
#
# TASK 393 (2026-08-27) — THIS FILE GREW AND NOTHING NEW WAS SUPPRESSED.
# 300 rows / 1063 sites -> 448 rows / 1355 sites. Every one of those +292
# sites already existed in the tree; the scanner could not see them. The old
# `_NOQA_RE` required the noqa comment to END its line, so `# noqa: CODE —
# reason` — the form this project mandates — matched nothing. Zero source
# lines were added or edited in that commit, and the pre-fix scan is a strict
# subset of the post-fix scan (no row lost, no count lowered), which is what
# makes "visibility, not new debt" a measurement rather than a claim.
# Two rows were DROPPED at the same time, and they are the other direction:
# `backend/admin_exec/project_registry.py:BLE001:1` (file deleted since) and
# `backend/admin_exec/seed_adr_tier_subsystem.py:PLC0415:2` (site cleaned up)
# were stale over-allowances the ratchet had been carrying.
#
# Format: <relpath>:<CODE>:<count>
# Regenerate: python scripts/check_ruff_noqa_liveness.py --write-baseline
"""

BaselineCounts = dict[tuple[str, str], int]


def load_baseline(repo_root: Path) -> BaselineCounts:
    """Load the inert-noqa baseline as ``{(relpath, code): allowed_count}``.

    Format per line: ``<relpath>:<CODE>:<count>``. Comment (``#``) and blank
    lines are skipped. Lines that don't parse are ignored — but note the
    direction that failure takes under count keying: an unparseable row
    contributes NO allowance, so a corrupted or stale-format baseline makes
    the gate FAIL loudly rather than pass silently.

    A missing baseline file is NOT an error: the gate operates in
    "report-only" mode, surfacing every inert site without an allowlist.
    This is the desired behaviour for a fresh checkout that hasn't
    initialised the baseline yet.
    """
    baseline_path = repo_root / "scripts" / _BASELINE_FILENAME
    if not baseline_path.is_file():
        return {}
    out: BaselineCounts = {}
    for raw in baseline_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            path, code, count_s = line.rsplit(":", 2)
            count = int(count_s)
        except ValueError:
            continue
        out[(path.strip(), code.strip())] = count
    return out


def _selector_set(lint: dict, *keys: str) -> set[str]:
    """Union the named selector lists from a ``[tool.ruff.lint]`` table."""
    raw: set[str] = set()
    for key in keys:
        raw.update(lint.get(key, []) or [])
    return {code.strip() for code in raw if code and isinstance(code, str)}


def load_lint_config(pyproject_path: Path) -> tuple[set[str], set[str]] | None:
    """Return ``(select, ignore)``, or None if pyproject is missing.

    Reads ``select`` + ``extend-select`` and ``ignore`` + ``extend-ignore``.

    ``ignore`` USED TO BE UNREAD, and that was a real defect in this gate
    (2026-08-26). This project's pyproject selects the ``BLE`` family and
    then ignores ``BLE001``; ruff therefore does not run BLE001 at all, so
    every ``# noqa: BLE001`` in the tree suppresses nothing. Reading only
    ``select``, the gate called all 155 of them LIVE — and then reported that
    conclusion to a human, who repeated it. A liveness guard that cannot see
    an ignore-override mislabels every rule in that position, which is the
    same failure family as the gates this car exists to fix: a guard
    reporting a state it did not actually check.

    None is the missing-config sentinel: callers must pass it through (a
    repo with no pyproject has nothing to gate against, so this gate is
    a no-op there).
    """
    if not pyproject_path.is_file():
        return None
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    lint = data.get("tool", {}).get("ruff", {}).get("lint", {})
    return (
        _selector_set(lint, "select", "extend-select"),
        _selector_set(lint, "ignore", "extend-ignore"),
    )


def _match_specificity(code: str, selectors: set[str]) -> int:
    """Length of the longest selector in *selectors* that prefixes *code*.

    ``-1`` when nothing matches. ``ALL`` matches every code at specificity 0
    (it is ruff's least-specific selector).
    """
    best = -1
    for sel in selectors:
        if sel == "ALL":
            best = max(best, 0)
        elif code.startswith(sel):
            best = max(best, len(sel))
    return best


def is_live(code: str, select: set[str], ignore: set[str]) -> bool:
    """True when ruff would actually RUN *code* under this select/ignore pair.

    Ruff resolves the two lists by SELECTOR SPECIFICITY — the longest matching
    prefix wins — and on an exact tie ``ignore`` wins. Measured against ruff
    0.16 rather than assumed (a bare ``except Exception`` probe file):

        select=["BLE"]     ignore=["BLE001"]  -> All checks passed  (off)
        select=["BLE001"]  ignore=["BLE"]     -> Found 1 error      (on)
        select=["BLE001"]  ignore=["BLE001"]  -> All checks passed  (off)
        select=["BLE"]     ignore=[]          -> Found 1 error      (on)

    Prefix matching also subsumes what the retired ``_select_expands`` did by
    materialising 11 000 codes per family: ``is_live("E0001", {"E"}, set())``
    is True because ``"E0001".startswith("E")``, so a hypothetical future code
    in a selected family is still treated as live — the deliberate
    over-generation is preserved, without the set.
    """
    selected = _match_specificity(code, select)
    if selected < 0:
        return False
    return selected > _match_specificity(code, ignore)


def iter_comments(path: Path) -> tuple[list[tuple[int, str]], str | None]:
    """Return ``([(lineno, comment_text), ...], tokenize_error)`` for one file.

    Comments come from :mod:`tokenize`, NOT from matching raw lines, and that
    is the point (task 393). A raw-line scan cannot tell a real ``# noqa``
    comment from the same characters sitting inside a string literal, and this
    repo has both: ``yadgar/tests/conftest.py`` documents the
    ``# noqa: F401`` re-export idiom inside a docstring, and
    ``yadgar/tests/_meta/test_harness_hardening.py`` writes a conftest fixture
    containing one through ``textwrap.dedent``. Ruff acts on neither — they
    are not comments — but a line scanner counts both, so loosening the regex
    to see trailing reasons WITHOUT tokenising would have traded one blind
    spot for a crop of phantom sites.

    ``tokenize_error`` is non-None when the file could not be tokenised, and
    the caller turns it into a HARD ERROR rather than falling back to line
    matching. Deliberate: a gate that quietly downgrades its own coverage on
    the files it cannot parse is reporting a cleanliness it did not check,
    which is the exact class this file exists to refuse. (Precedent in this
    repo: a ruff-format rewrite of ``except (A, B):`` to Python-2 syntax
    silently blocked an AST-based scan for an entire train.) Zero files in
    ``yadgar/`` + ``scripts/`` fail to tokenise today.
    """
    try:
        body = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):  # fmt: skip
        return [], None  # not a text file we can read — skip silently
    comments: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(body).readline):
            if tok.type == tokenize.COMMENT:
                comments.append((tok.start[0], tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError) as exc:  # fmt: skip
        return [], f"{type(exc).__name__}: {exc}"
    return comments, None


def scan_file(
    path: Path,
    select: set[str],
    ignore: set[str],
    repo_root: Path,
) -> tuple[list[str], dict[str, list[int]]]:
    """Scan one Python file for BARE noqa sites and inert-rule sites.

    Returns ``(hard_errors, inert_sites)`` where:

      * ``hard_errors`` — BARE/EMPTY noqa, malformed rule codes, and files
        that could not be tokenised. These are NEVER baselined: a blanket or
        misspelled suppression is always a defect, at any count, and an
        unscannable file is always a hole in the gate's own coverage.
      * ``inert_sites`` — ``{code: [lineno, ...]}`` for well-formed codes that
        ruff does not run. The CALLER compares those counts against the
        baseline allowance; this function does not, because the allowance is
        per (file, code) and only the caller knows the file's rel-path key.
    """
    hard_errors: list[str] = []
    inert_sites: dict[str, list[int]] = defaultdict(list)
    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:  # pragma: no cover - defensive
        rel = str(path)

    comments, tok_error = iter_comments(path)
    if tok_error is not None:
        hard_errors.append(
            f"UNSCANNABLE: {rel}: could not be tokenised ({tok_error}). The "
            "gate cannot see this file's `# noqa` comments, so it must not "
            "report the file clean. Fix the file — the gate does not fall "
            "back to line matching, because a silent coverage downgrade is "
            "the failure it exists to catch."
        )
        return hard_errors, {}

    for lineno, comment in comments:
        for m in _NOQA_RE.finditer(comment):
            codes_str = (m.group("codes") or "").strip()
            if not codes_str:
                # BARE / EMPTY class — must be flagged. Never allowlisted: a
                # blanket suppression with no rule is ALWAYS a defect.
                hard_errors.append(
                    f"BARE: {rel}:{lineno}: bare `# noqa` (no rule code) — "
                    "specify the rule you are suppressing, or ruff treats it "
                    "as a blanket suppression that no lint pass can audit."
                )
                continue
            # Split on comma, validate each code.
            for code in (c.strip() for c in codes_str.split(",")):
                if not code:
                    continue
                if not _CODE_RE.match(code):
                    # Not a ruff-shaped code (e.g. typo). Flag as inert too —
                    # the gate's whole point is "names a rule ruff will run."
                    # Not allowlisted: a misspelled rule is ALWAYS a defect.
                    hard_errors.append(
                        f"INERT-RULE: {rel}:{lineno}: `# noqa: {code}` names a "
                        "rule that is not a valid ruff code (expected one or two "
                        "uppercase letters + 3-4 digits). Either it is misspelled "
                        "or ruff silently ignores it."
                    )
                    continue
                if not is_live(code, select, ignore):
                    inert_sites[code].append(lineno)
    return hard_errors, dict(inert_sites)


def collect_inert(
    repo_root: Path, select: set[str], ignore: set[str]
) -> tuple[list[str], BaselineCounts]:
    """Walk ``yadgar/`` and return ``(hard_errors, {(relpath, code): count})``.

    The counts are the OBSERVED state of the tree — the same shape the
    baseline stores, which is what lets ``--write-baseline`` and the gate
    share one scan.
    """
    hard_errors: list[str] = []
    counts: BaselineCounts = {}
    yadgar = repo_root / "yadgar"
    if not yadgar.is_dir():
        return hard_errors, counts
    for path in sorted(yadgar.rglob("*.py")):
        errs, inert = scan_file(path, select, ignore, repo_root)
        hard_errors.extend(errs)
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:  # pragma: no cover - defensive
            rel = str(path)
        for code, linenos in inert.items():
            counts[(rel, code)] = len(linenos)
    return hard_errors, counts


def _format_growth(rel: str, code: str, observed: int, allowed: int) -> str:
    if allowed == 0:
        return (
            f"INERT-RULE: {rel}: {observed} `# noqa: {code}` site(s) suppress a "
            f"rule ruff does not run for this project — it is either absent from "
            f"pyproject `[tool.ruff.lint] select` or selected and then "
            f"ignore-overridden — and this (file, rule) pair is not in the "
            f"baseline. The suppression is a silent no-op today — drop the noqa, "
            f"or make the rule live (`select` it, and drop any more-specific "
            f"`ignore` entry) with a stated reason."
        )
    return (
        f"INERT-RULE: {rel}: {observed} inert `# noqa: {code}` site(s), but the "
        f"ratchet allows {allowed}. {observed - allowed} NEW inert suppression(s) "
        f"of a rule ruff does not run. Drop the new noqa(s). The baseline is a "
        f"ratchet: it may shrink, never grow. If the growth is genuinely "
        f"unavoidable, regenerate with "
        f"`python scripts/{Path(__file__).name} --write-baseline` AND say why in "
        f"the commit message."
    )


def check(repo_root: Path | None = None) -> list[str]:
    """Walk yadgar/ for BARE noqa sites and inert-rule GROWTH.

    Returns a list of formatted error strings. Empty list means clean.
    Missing pyproject → empty list (no config to drift against, nothing to gate).
    Missing baseline → every inert site reported (no allowlist).
    """
    repo_root = repo_root or _REPO_ROOT
    pyproject = repo_root / "pyproject.toml"
    lint = load_lint_config(pyproject)
    if lint is None:
        # No pyproject → no resolved select → nothing to gate. This is the
        # sentinel the tests pin to ensure the gate doesn't false-positive
        # on a sub-project with its own ruff config (none today, but the
        # contract is future-proof).
        return []
    select, ignore = lint
    # Liveness follows ruff's own select/ignore precedence (see `is_live`):
    # family prefixes count, and an ignore-override beats a less specific
    # select. If select is empty (`select = []`), all named rules are inert.
    # The script still scans; every named code becomes an INERT-RULE report.
    baseline = load_baseline(repo_root)
    errors, observed = collect_inert(repo_root, select, ignore)
    for (rel, code), count in sorted(observed.items()):
        allowed = baseline.get((rel, code), 0)
        if count > allowed:
            errors.append(_format_growth(rel, code, count, allowed))
    return errors


def render_baseline(counts: BaselineCounts) -> str:
    """Render the baseline file text (header + sorted rows) for *counts*."""
    rows = [f"{rel}:{code}:{count}" for (rel, code), count in sorted(counts.items())]
    return _BASELINE_HEADER + "\n".join(rows) + ("\n" if rows else "")


def write_baseline(repo_root: Path | None = None) -> tuple[int, int]:
    """Regenerate the baseline from the current tree.

    Returns ``(pair_count, site_count)`` — the number of ``(file, rule)`` rows
    written and the total number of inert sites they account for.
    """
    repo_root = repo_root or _REPO_ROOT
    lint = load_lint_config(repo_root / "pyproject.toml")
    if lint is None:
        return 0, 0
    _errors, counts = collect_inert(repo_root, *lint)
    path = repo_root / "scripts" / _BASELINE_FILENAME
    path.write_text(render_baseline(counts), encoding="utf-8")
    return len(counts), sum(counts.values())


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--write-baseline" in args:
        pairs, sites = write_baseline()
        print(f"wrote scripts/{_BASELINE_FILENAME}: {pairs} (file, rule) row(s), {sites} site(s).")
        return 0
    errors = check()
    if errors:
        print("ruff noqa liveness check FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        # Total count summary at the end so a long failure list is readable.
        print(f"\n  total: {len(errors)} violation(s)", file=sys.stderr)
        return 1
    print("ruff noqa liveness check OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
