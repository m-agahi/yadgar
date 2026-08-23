"""Tests for scripts/check_ruff_noqa_liveness.py — inline-# noqa liveness guard.

WHY THIS EXISTS (car C2 / task 313, bug-bag-2 train 2026-08-23)
---------------------------------------------------------------
The sibling ``check_ruff_ignores_liveness.py`` (car 7, 2026-08-13) covers
``pyproject.toml`` ``[tool.ruff.lint.per-file-ignores]``. Inline (hash +
``noqa:`` + code) suppressions on individual lines were never gated: 309
``#`` + ``noqa: BLE001`` sites in this repo were silencing a rule that ruff
does not run (BLE001 is not in pyproject ``select``).

This test pins the new gate's three contracts:

  (a) INERT-RULE — ``# noqa: XXXX`` where ``XXXX`` is not in select → flagged.
  (b) BARE / EMPTY — ``# noqa`` or ``# noqa:`` (no rule code) → flagged.
  (c) HAPPY PATH — source with valid noqa (rule in select) → not flagged.
  (d) SELECT-RESOLUTION — gate reads pyproject ``select``, not a hard-coded list.

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
) -> Path:
    """Create a fake repo with a synthetic pyproject.toml + sources.

    ``sources`` maps ``yadgar/<relpath>`` to file body. Each body is written
    verbatim. Returns the repo root path.
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
    return repo


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
