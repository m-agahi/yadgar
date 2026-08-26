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

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import check_ruff_noqa_liveness as nql  # noqa: E402


def _make_repo(
    tmp_path: Path,
    *,
    select: list[str],
    sources: dict[str, str],
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
    pyproject = f"[tool.ruff.lint]\nselect = {select_lit}\n{pyproject_extra}"
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
        # Bare ``# noqa`` (no colon, no code) — ruff accepts this as a blanket
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
        # ``# noqa:`` with no code after colon — also a blanket suppression.
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
        # ``# noqa:   `` with only trailing whitespace counts as bare.
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
