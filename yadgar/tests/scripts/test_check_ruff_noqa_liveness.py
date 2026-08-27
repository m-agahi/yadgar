"""Tests for scripts/check_ruff_noqa_liveness.py — inline-# noqa liveness guard.

WHY THIS EXISTS (car C2 / task 313, bug-bag-2 train 2026-08-23)
---------------------------------------------------------------
The sibling ``check_ruff_ignores_liveness.py`` (car 7, 2026-08-13) covers
``pyproject.toml`` ``[tool.ruff.lint.per-file-ignores]``. Inline (hash +
``noqa:`` + code) suppressions on individual lines were never gated: 309
``#`` + ``noqa: BLE001`` sites in this repo were silencing a rule that ruff
does not run (BLE001 is not in pyproject ``select``).

This test pins the gate's contracts:

  (a) INERT-RULE — ``# noqa: XXXX`` where ``XXXX`` is not in select → flagged.
  (b) BARE / EMPTY — ``# noqa`` or ``# noqa:`` (no rule code) → flagged.
  (c) HAPPY PATH — source with valid noqa (rule in select) → not flagged.
  (d) SELECT-RESOLUTION — gate reads pyproject ``select``, not a hard-coded list.
  (e) THE RATCHET (task 383) — the baseline is keyed on ``(relpath, code)``
      plus a COUNT, so a new inert marker in an already-covered file FAILS
      while a pure line shift PASSES. Under the retired line-keyed scheme
      those two were the same event, which is what made regeneration a
      laundering channel.
  (f) THE GENERATOR OWNS THE HEADER — ``--write-baseline`` re-emits the
      unreviewed-debt declaration, so it cannot be dropped by a regeneration.

Notes on ruff interaction: the test fixtures intentionally contain malformed
noqa directives (the whole point of the gate). Each is suppressed with
``# noqa: RUF100`` so ruff itself doesn't error on them — but the gate, which
runs the source-as-string through its OWN parser, sees them as malformed
and reports them via ``check()``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import check_ruff_noqa_liveness as nql  # noqa: E402


def _make_repo(
    tmp_path: Path,
    *,
    select: list[str],
    sources: dict[str, str],
    ignore: list[str] | None = None,
    pyproject_extra: str = "",
    baseline: dict[tuple[str, str], int] | None = None,
    baseline_text: str | None = None,
) -> Path:
    """Create a fake repo with a synthetic pyproject.toml + sources.

    ``sources`` maps ``yadgar/<relpath>`` to file body. Each body is written
    verbatim. Returns the repo root path.

    ``baseline`` writes ``scripts/baseline_noqa_inert.txt`` through the
    script's own renderer, so the tests exercise the real on-disk format
    rather than a hand-rolled imitation of it. ``baseline_text`` writes raw
    bytes instead — used to pin how the gate reacts to the RETIRED
    line-keyed format.
    """
    repo = tmp_path
    (repo / "yadgar").mkdir(parents=True, exist_ok=True)
    select_lit = "[" + ", ".join(f'"{s}"' for s in select) + "]"
    ignore_lit = "[" + ", ".join(f'"{s}"' for s in (ignore or [])) + "]"
    pyproject = f"[tool.ruff.lint]\nselect = {select_lit}\nignore = {ignore_lit}\n{pyproject_extra}"
    (repo / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    for relpath, body in sources.items():
        full = repo / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body, encoding="utf-8")
    if baseline is not None or baseline_text is not None:
        (repo / "scripts").mkdir(parents=True, exist_ok=True)
        text = baseline_text if baseline_text is not None else nql.render_baseline(baseline or {})
        (repo / "scripts" / "baseline_noqa_inert.txt").write_text(text, encoding="utf-8")
    return repo


# ``import os`` is inert under select=[E, F] (PLC is not selected). Built by
# concatenation so no line of THIS file ends in a noqa comment — the repo's
# own gate scans yadgar/tests/** too.
_INERT = "    import os  # noqa: " + "PLC0415\n"


def _module_with(n_sites: int, *, pad: int = 0) -> str:
    """A module body carrying *n_sites* inert PLC0415 noqa sites.

    ``pad`` prepends that many blank lines, shifting every site's line number
    while leaving the COUNT identical — the exact edit that forced a full
    baseline regeneration under the retired line-keyed scheme.
    """
    return "\n" * pad + "".join(f"def f{i}():\n{_INERT}" for i in range(n_sites))


# ---------------------------------------------------------------------------
# (a) INERT-RULE
# ---------------------------------------------------------------------------


class TestInertRule:
    """A noqa site targeting a rule NOT in pyproject ``select`` → flagged."""

    def test_ble001_against_select_EF_is_flagged(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={
                "yadgar/core/mod.py": (
                    "try:\n    1 / 0\nexcept Exception:  # noqa: BLE001\n    pass\n"
                ),
            },
        )
        errors = nql.check(repo)
        inert = [e for e in errors if "INERT-RULE" in e and "BLE001" in e]
        assert inert, (
            "BLE001 is not in select=[E, F] but the noqa suppresses a rule "
            "ruff will never run; the gate must flag it. Got errors:\n" + "\n".join(errors)
        )

    def test_arg001_against_select_EF_is_flagged(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={
                "yadgar/core/mod.py": ("def f(x):  # noqa: ARG001\n    return 1\n"),  # noqa: RUF100
            },
        )
        errors = nql.check(repo)
        inert = [e for e in errors if "INERT-RULE" in e and "ARG001" in e]
        assert inert, f"ARG001 not in select=[E, F]; gate must flag it. errors={errors}"

    def test_rule_in_select_is_not_flagged_as_inert(self, tmp_path: Path) -> None:
        """Sanity: a rule IN select must NOT be flagged as inert.

        E402 (module-level import not at top) listed as a full code in select.
        Real pyproject.toml in this repo uses full codes (``"E402"``) not
        prefixes (``"E"``).
        """
        repo = _make_repo(
            tmp_path,
            select=["E402", "F401"],
            sources={
                "yadgar/core/mod.py": ("def f():\n    import os  # noqa: E402\n"),
            },
        )
        errors = nql.check(repo)
        inert = [e for e in errors if "INERT-RULE" in e]
        assert not inert, (
            f"E402 is in select; an inert-rule flag would be a false positive: {errors}"
        )


# ---------------------------------------------------------------------------
# (b) BARE / EMPTY
# ---------------------------------------------------------------------------


class TestBareOrEmpty:
    """``# noqa`` with no rule code stated → flagged."""

    def test_bare_noqa_is_flagged(self, tmp_path: Path) -> None:
        # A bare ``noqa`` (no colon, no code) — ruff accepts this as a blanket
        # suppression, but the gate demands an explicit rule.
        mod_src = "def f():\n    x = 1  # noqa\n"  # noqa: RUF100
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={"yadgar/core/mod.py": mod_src},
        )
        errors = nql.check(repo)
        bare = [e for e in errors if "BARE" in e]
        assert bare, f"a bare # noqa must be flagged as malformed: errors={errors}"

    def test_empty_noqa_colon_is_flagged(self, tmp_path: Path) -> None:
        # A ``noqa:`` with no code after the colon — also a blanket suppression.
        mod_src = "def f():\n    x = 1  # noqa:\n"  # noqa: RUF100
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={"yadgar/core/mod.py": mod_src},
        )
        errors = nql.check(repo)
        bare = [e for e in errors if "BARE" in e]
        assert bare, f"empty ``# noqa:`` must be flagged: errors={errors}"

    def test_empty_whitespace_noqa_is_flagged(self, tmp_path: Path) -> None:
        # A ``noqa:   `` with only trailing whitespace counts as bare.
        mod_src = "def f():\n    x = 1  # noqa:   \n"  # noqa: RUF100
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={"yadgar/core/mod.py": mod_src},
        )
        errors = nql.check(repo)
        bare = [e for e in errors if "BARE" in e]
        assert bare, f"whitespace-only # noqa must be flagged: errors={errors}"


# ---------------------------------------------------------------------------
# (c) Missing pyproject / no yadgar/ tree
# ---------------------------------------------------------------------------


class TestPyprojectMissing:
    """If pyproject.toml is missing, the gate exits 0 (vacuously).

    A repo without pyproject has no ``select`` to gate against, so the
    gate passes cleanly. Same for a missing ``yadgar/`` directory.
    """

    def test_no_pyproject_is_a_clean_pass(self, tmp_path: Path) -> None:
        (tmp_path / "yadgar" / "core").mkdir(parents=True)
        (tmp_path / "yadgar" / "core" / "mod.py").write_text(
            "def f():  # noqa: BLE001\n    return 1\n",  # noqa: RUF100
            encoding="utf-8",
        )
        errors = nql.check(tmp_path)
        assert errors == [], (
            "a repo without pyproject.toml has no select to check against, "
            f"so the gate must pass cleanly; got {errors}"
        )


# ---------------------------------------------------------------------------
# (d) Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """A source file with no noqa at all — gate returns no errors."""

    def test_clean_source_passes(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={
                "yadgar/core/mod.py": "def f():\n    return 1\n",
            },
        )
        errors = nql.check(repo)
        assert errors == [], f"clean source must not produce errors; got {errors}"


# ---------------------------------------------------------------------------
# (e) Select-set resolution
# ---------------------------------------------------------------------------


class TestSelectResolution:
    """The gate reads pyproject ``[tool.ruff.lint] select``, not hardcoded."""

    def test_empty_select_means_every_code_is_inert(self, tmp_path: Path) -> None:
        """An empty ``select = []`` is degenerate but legal.

        Under it every noqa code is INERT and must be flagged — the gate is
        not silently passing when the project hasn't configured any rules.
        """
        repo = _make_repo(
            tmp_path,
            select=[],
            sources={
                "yadgar/core/mod.py": "def f():  # noqa: E402\n    import os\n",
            },
        )
        errors = nql.check(repo)
        inert = [e for e in errors if "INERT-RULE" in e and "E402" in e]
        assert inert, f"empty select: E402 is inert, gate must flag it. errors={errors}"


# ---------------------------------------------------------------------------
# (f) THE RATCHET — baseline keyed on (relpath, code) + count (task 383)
# ---------------------------------------------------------------------------


class TestCountRatchet:
    """Growth must fail; a pure line shift must not.

    Under the retired ``(relpath, lineno, code)`` key these two cases were
    INDISTINGUISHABLE: both made previously-baselined rows stop resolving, so
    both were "fixed" by regenerating the file — and a regeneration carries
    whatever new markers the same commit introduced. Measured on the
    bug-bag-2 train: the baseline went 951 -> 988 rows across nine commits
    while the tree's real site count barely moved, and 72 of the 988 rows
    pointed at lines that no longer carried the noqa they named. A row that
    resolves to nothing can never fail, so those were dead allowances that
    nobody could tell apart from live ones.
    """

    MOD = "yadgar/core/mod.py"

    def test_new_inert_marker_in_a_covered_file_fails(self, tmp_path: Path) -> None:
        """A 3rd inert site where the ratchet allows 2 → FAIL.

        This is the case the line-keyed scheme could not see.
        """
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={self.MOD: _module_with(3)},
            baseline={(self.MOD, "PLC0415"): 2},
        )
        errors = nql.check(repo)
        growth = [e for e in errors if "PLC0415" in e]
        assert growth, f"3 inert sites vs an allowance of 2 must fail; got {errors}"
        assert "allows 2" in growth[0]

    def test_line_shift_with_unchanged_count_passes(self, tmp_path: Path) -> None:
        """Same sites, moved 40 lines down, same count → PASS, no regeneration.

        This is the half that makes the ratchet usable: if an ordinary line
        shift still demanded a regeneration, regeneration stays routine and
        stays a laundering channel.
        """
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={self.MOD: _module_with(3, pad=40)},
            baseline={(self.MOD, "PLC0415"): 3},
        )
        assert nql.check(repo) == [], "a pure line shift must not need a regeneration"

    def test_count_may_shrink_silently(self, tmp_path: Path) -> None:
        """Cleaning a site up must not fail the gate — the ratchet only tightens."""
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={self.MOD: _module_with(1)},
            baseline={(self.MOD, "PLC0415"): 5},
        )
        assert nql.check(repo) == []

    def test_new_file_rule_pair_absent_from_baseline_fails(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={self.MOD: _module_with(1)},
            baseline={("yadgar/core/other.py", "PLC0415"): 9},
        )
        errors = nql.check(repo)
        assert errors, "an unbaselined (file, rule) pair must fail"
        assert "not in the baseline" in errors[0]

    def test_retired_line_keyed_rows_grant_no_allowance(self, tmp_path: Path) -> None:
        """A stale line-keyed baseline must FAIL loud, never pass silently.

        ``<relpath>:<lineno>:<CODE>`` parses as ``(path, code=lineno,
        count=CODE)`` → int() raises → the row is dropped → zero allowance.
        The direction matters: an unreadable baseline makes the gate noisy,
        not permissive.
        """
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={self.MOD: _module_with(1)},
            baseline_text=f"{self.MOD}:2:PLC0415\n",
        )
        assert nql.check(repo), "old-format rows must not silently grant allowance"


# ---------------------------------------------------------------------------
# (g) --write-baseline — the generator owns the header
# ---------------------------------------------------------------------------


class TestWriteBaseline:
    MOD = "yadgar/core/mod.py"

    def _repo(self, tmp_path: Path, n: int) -> Path:
        return _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={self.MOD: _module_with(n)},
            baseline={},
        )

    def test_regenerated_baseline_makes_the_gate_pass(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, 4)
        assert nql.check(repo), "precondition: unbaselined sites fail"
        pairs, sites = nql.write_baseline(repo)
        assert (pairs, sites) == (1, 4)
        assert nql.check(repo) == []

    def test_generator_emits_the_unreviewed_debt_header(self, tmp_path: Path) -> None:
        """The header is written by --write-baseline, not kept by hand.

        A hand-maintained header that the generator drops on the next
        regeneration would be the same laundering class one level up: the
        file would silently stop declaring that its entries are unreviewed.
        """
        repo = self._repo(tmp_path, 2)
        nql.write_baseline(repo)
        text = (repo / "scripts" / "baseline_noqa_inert.txt").read_text(encoding="utf-8")
        assert "UNREVIEWED DEBT" in text
        assert "NOT VETTED" in text
        assert "RATCHET" in text
        assert "--write-baseline" in text

    def test_rows_are_relpath_code_count(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, 3)
        nql.write_baseline(repo)
        text = (repo / "scripts" / "baseline_noqa_inert.txt").read_text(encoding="utf-8")
        rows = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        assert rows == [f"{self.MOD}:PLC0415:3"]
        assert nql.load_baseline(repo) == {(self.MOD, "PLC0415"): 3}


class TestShippedBaselineIsWellFormed:
    """The real scripts/baseline_noqa_inert.txt must parse and carry the header."""

    def test_every_row_parses_and_header_present(self) -> None:
        from yadgar.tests._paths import REPO_ROOT

        path = REPO_ROOT / "scripts" / "baseline_noqa_inert.txt"
        text = path.read_text(encoding="utf-8")
        assert "UNREVIEWED DEBT" in text, "the shipped baseline must declare its status"
        rows = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        parsed = nql.load_baseline(REPO_ROOT)
        assert len(parsed) == len(rows), (
            "every non-comment row must parse as <relpath>:<CODE>:<count> — an "
            "unparsed row grants no allowance and would fail the gate invisibly"
        )
        assert all(count > 0 for count in parsed.values())


# ---------------------------------------------------------------------------
# (h) IGNORE-OVERRIDE — `ignore` beats a less specific `select` (2026-08-26)
# ---------------------------------------------------------------------------


class TestIgnoreOverride:
    """Liveness is `select` MINUS `ignore`, resolved by selector specificity.

    The gate used to read `select` alone. This project selects the ``BLE``
    family and then ignores ``BLE001``, so ruff does not run BLE001 at all —
    yet the gate called all 155 ``# noqa: BLE001`` sites LIVE, and a human
    repeated that conclusion because the gate asserted it. A liveness guard
    blind to an ignore-override mislabels every rule in that position.

    The four expectations below were measured against ruff itself, not
    assumed — a bare ``except Exception`` probe under each config:

        select=["BLE"]     ignore=["BLE001"]  -> All checks passed  (off)
        select=["BLE001"]  ignore=["BLE"]     -> Found 1 error      (on)
        select=["BLE001"]  ignore=["BLE001"]  -> All checks passed  (off)
        select=["BLE"]     ignore=[]          -> Found 1 error      (on)
    """

    def test_family_selected_member_ignored_is_not_live(self) -> None:
        assert nql.is_live("BLE001", {"BLE"}, {"BLE001"}) is False

    def test_specific_select_beats_family_ignore(self) -> None:
        assert nql.is_live("BLE001", {"BLE001"}, {"BLE"}) is True

    def test_exact_tie_goes_to_ignore(self) -> None:
        assert nql.is_live("BLE001", {"BLE001"}, {"BLE001"}) is False

    def test_family_selected_nothing_ignored_is_live(self) -> None:
        assert nql.is_live("BLE001", {"BLE"}, set()) is True

    def test_unselected_code_is_not_live(self) -> None:
        assert nql.is_live("PLC0415", {"E", "F", "PLR0913"}, set()) is False

    def test_sibling_in_the_family_stays_live(self) -> None:
        """Ignoring BLE001 must not take the whole BLE family down."""
        assert nql.is_live("BLE002", {"BLE"}, {"BLE001"}) is True

    def test_unmaterialised_code_in_a_selected_family_is_live(self) -> None:
        """Preserved from the retired set-expansion: over-generate at the
        INERT boundary rather than flag a hypothetical future code."""
        assert nql.is_live("E0001", {"E"}, set()) is True

    def test_all_selector_is_least_specific(self) -> None:
        assert nql.is_live("BLE001", {"ALL"}, set()) is True
        assert nql.is_live("BLE001", {"ALL"}, {"BLE"}) is False

    def test_end_to_end_ignored_member_is_flagged_inert(self, tmp_path: Path) -> None:
        """The whole gate, not just the predicate: an ignore-overridden noqa
        must be reported."""
        body = (
            "def f():\n    try:\n        pass\n"
            + "    except Exception:  # noqa: "
            + "BLE001\n        pass\n"
        )
        repo = _make_repo(
            tmp_path,
            select=["BLE", "E", "F"],
            ignore=["BLE001"],
            sources={"yadgar/core/mod.py": body},
        )
        errors = nql.check(repo)
        assert [e for e in errors if "BLE001" in e], (
            f"BLE001 is ignore-overridden, so the noqa suppresses nothing "
            f"and must be flagged; got {errors}"
        )

    def test_end_to_end_specific_select_is_not_flagged(self, tmp_path: Path) -> None:
        body = (
            "def f():\n    try:\n        pass\n"
            + "    except Exception:  # noqa: "
            + "BLE001\n        pass\n"
        )
        repo = _make_repo(
            tmp_path,
            select=["BLE001", "E", "F"],
            ignore=["BLE"],
            sources={"yadgar/core/mod.py": body},
        )
        assert not [e for e in nql.check(repo) if "BLE001" in e], (
            "a more specific select beats a family ignore — flagging this would be a false positive"
        )


class TestShippedConfigLivenessPin:
    """Pin the real pyproject's resolution so this cannot silently invert.

    Recorded because the gate got this exact question wrong and reported the
    wrong answer to a human. If someone un-ignores BLE001 (ledger task #313 —
    993 bare `except Exception` handlers, only ~296 carrying a noqa), this
    test fails and names the decision, rather than the baseline quietly
    shrinking by 154 rows.
    """

    def test_ble001_is_ignore_overridden_today(self) -> None:
        from yadgar.tests._paths import REPO_ROOT

        lint = nql.load_lint_config(REPO_ROOT / "pyproject.toml")
        assert lint is not None
        select, ignore = lint
        assert "BLE" in select, "precondition: the BLE family is selected"
        assert "BLE001" in ignore, "precondition: BLE001 is ignore-overridden"
        assert nql.is_live("BLE001", select, ignore) is False, (
            "BLE001 is selected by family and then ignored, so ruff does not "
            "run it and every `# noqa: BLE001` in the tree is decorative"
        )

    def test_a_plainly_selected_rule_is_live(self) -> None:
        from yadgar.tests._paths import REPO_ROOT

        lint = nql.load_lint_config(REPO_ROOT / "pyproject.toml")
        assert lint is not None
        assert nql.is_live("F401", *lint) is True


# ---------------------------------------------------------------------------
# (i) WHAT THE SCANNER CAN SEE (task 393, 2026-08-27)
# ---------------------------------------------------------------------------


class TestTrailingReasonIsSeen:
    """A noqa with a trailing reason is a real directive and must be scanned.

    ``_NOQA_RE`` used to be anchored with ``\\s*$``, so only a noqa that ENDED
    its line matched — which excluded ``# noqa: CODE — reason``, the form this
    project's own convention mandates. Measured on the tree the day it was
    fixed: 292 of 1355 real inert sites in ``yadgar/`` were invisible, so the
    baseline frozen from that scan covered a subset of the tree while the gate
    reported the tree.

    Each form below was verified against ruff 0.15.21 (probe file,
    ``select = ["F"]``, unused ``import os``) to actually suppress — these are
    live directives, not decoration.
    """

    COMMENTS = [
        "# noqa: BLE001",  # the one form the old anchor could see
        "# noqa: BLE001 — em-dash reason (the mandated form)",
        "# noqa: BLE001 -- double-hyphen reason",
        "# noqa: BLE001 (parenthetical reason)",
        "#noqa:BLE001 no spaces anywhere",
        "# NOQA: BLE001 — ruff matches the marker case-insensitively",
        "# type: ignore  # noqa: BLE001 — second marker on the same comment",
    ]

    @pytest.mark.parametrize("comment", COMMENTS)
    def test_inert_code_is_flagged_whatever_follows_it(self, tmp_path: Path, comment: str) -> None:
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={"yadgar/core/mod.py": f"def f():\n    x = 1  {comment}\n"},
        )
        errors = nql.check(repo)
        assert [e for e in errors if "BLE001" in e], (
            f"{comment!r} is a live ruff directive naming an inert rule; the "
            f"gate must see it. Got {errors}"
        )

    def test_multiple_codes_with_a_trailing_reason_are_all_seen(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={
                "yadgar/core/mod.py": ("import os  # noqa: BLE001, PLC0415 — both inert here\n")
            },
        )
        errors = nql.check(repo)
        assert [e for e in errors if "BLE001" in e], f"first code missed: {errors}"
        assert [e for e in errors if "PLC0415" in e], f"second code missed: {errors}"

    def test_word_boundary_stops_a_false_bare(self, tmp_path: Path) -> None:
        """``# noqable`` is not a directive — ruff refuses to act on it.

        Probed: ruff 0.15.21 emits "Invalid ``# noqa`` directive" and does NOT
        suppress. It silences nothing, so flagging it as BARE would be a false
        positive on prose.
        """
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={"yadgar/core/mod.py": "def f():\n    x = 1  # noqable prose\n"},
        )
        assert nql.check(repo) == [], "a non-directive word must not be flagged"


class TestStringLiteralsAreNotDirectives:
    """``noqa`` inside a string literal is not a comment, so it is not a site.

    This is why the scanner tokenises instead of matching raw lines: dropping
    the end-of-line anchor from a LINE matcher would have started counting
    text inside docstrings and fixtures. The repo has real instances —
    ``yadgar/tests/conftest.py`` documents the ``# noqa: F401`` re-export
    idiom in a docstring, and ``yadgar/tests/_meta/test_harness_hardening.py``
    writes a conftest fixture containing one via ``textwrap.dedent``. Ruff
    acts on neither.
    """

    def test_triple_quoted_fixture_line_is_not_a_site(self, tmp_path: Path) -> None:
        # The embedded line ENDS with the noqa, so even the old end-anchored
        # regex matched it — a false positive that predates the fix.
        body = 'CONFTEST = """\\\nimport os  # noqa: BLE001\n"""\n'
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={"yadgar/core/mod.py": body},
        )
        assert nql.check(repo) == [], (
            "a noqa inside a triple-quoted string is fixture text, not a "
            f"directive ruff will ever act on; got {nql.check(repo)}"
        )

    def test_docstring_prose_is_not_a_bare_site(self, tmp_path: Path) -> None:
        body = 'def f():\n    """Use a bare # noqa here and ruff blanket-suppresses."""\n    return 1\n'
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={"yadgar/core/mod.py": body},
        )
        assert nql.check(repo) == [], (
            f"prose in a docstring must not be flagged BARE; got {nql.check(repo)}"
        )


class TestUnscannableFileIsAHardError:
    """A file the scanner cannot tokenise is reported, never skipped.

    The alternative — fall back to raw-line matching — would let the gate
    report a file clean under a scan it silently narrowed. That is the class
    this whole file exists to refuse, and the repo has precedent: a
    ruff-format rewrite of ``except (A, B):`` into Python-2 syntax silently
    blocked an AST-based scan for an entire train.
    """

    def test_tokenize_error_is_surfaced(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            # Unterminated triple-quoted string → tokenize.TokenError.
            sources={"yadgar/core/mod.py": 'x = """unterminated\n'},
        )
        errors = nql.check(repo)
        assert [e for e in errors if "UNSCANNABLE" in e], (
            f"an untokenisable file must be reported, not skipped; got {errors}"
        )

    def test_scannable_file_reports_no_tokenize_error(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            select=["E", "F"],
            sources={"yadgar/core/mod.py": "def f():\n    return 1\n"},
        )
        assert nql.check(repo) == []
