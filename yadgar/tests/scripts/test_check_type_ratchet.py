"""Tests for scripts/check_type_ratchet.py — the strict-typing ratchet.

TDD: tests written before implementation.

The ratchet exists because the tree is 306k lines with NO type checker (task
0116), so a blanket gate is impossible.  Instead: a file you TOUCH may not
carry MORE type errors than its recorded baseline.  Old code is untouched;
new code starts at zero because it has no baseline entry.

Coverage:
  (a) PARSE: mypy error lines counted per file; notes/summary ignored
  (a) PARSE: empty output -> {}
  (b) SELECT: only .py files, only ones still present, dedup across diffs
  (c) COMPARE: changed file exceeding its baseline -> violation
  (c) COMPARE: changed file at/below baseline -> clean
  (c) COMPARE: NEW changed file with any error -> violation (baseline 0)
  (c) COMPARE: unchanged file with errors -> ignored entirely
  (d) BASE: merge-base resolves; unreachable base fails OPEN (empty selection)
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_type_ratchet import (  # noqa: E402
    compare_against_baseline,
    parse_mypy_errors,
    resolve_merge_base,
    resolve_mypy_interpreter,
    select_changed_python_files,
)

# ---------------------------------------------------------------------------
# (a) PARSE
# ---------------------------------------------------------------------------

_MYPY_OUTPUT = """\
yadgar/core/thing.py:12: error: Incompatible return value type  [return-value]
yadgar/core/thing.py:19: note: Consider using a Protocol here
yadgar/core/thing.py:41: error: Argument 1 has incompatible type  [arg-type]
yadgar/backend/other.py:3: error: Missing type parameters  [type-arg]
Found 3 errors in 2 files (checked 5 source files)
"""


def test_parse_counts_errors_per_file_ignoring_notes_and_summary() -> None:
    assert parse_mypy_errors(_MYPY_OUTPUT) == {
        "yadgar/core/thing.py": 2,
        "yadgar/backend/other.py": 1,
    }


def test_parse_empty_output_is_empty_mapping() -> None:
    assert parse_mypy_errors("") == {}
    assert parse_mypy_errors("Success: no issues found in 12 source files\n") == {}


# ---------------------------------------------------------------------------
# (b) SELECT
# ---------------------------------------------------------------------------


def test_select_keeps_only_existing_python_files_and_dedups(tmp_path: Path) -> None:
    kept = tmp_path / "kept.py"
    kept.write_text("x = 1\n")
    (tmp_path / "notes.md").write_text("hi\n")

    names = ["kept.py", "kept.py", "notes.md", "deleted.py"]

    assert select_changed_python_files(names, root=tmp_path) == ["kept.py"]


# ---------------------------------------------------------------------------
# (c) COMPARE
# ---------------------------------------------------------------------------


def test_compare_flags_changed_file_that_gained_errors() -> None:
    violations = compare_against_baseline(
        current={"a.py": 4},
        baseline={"a.py": 2},
        changed=["a.py"],
    )
    assert len(violations) == 1
    assert "a.py" in violations[0]
    assert "2" in violations[0] and "4" in violations[0]


def test_compare_allows_changed_file_at_or_below_baseline() -> None:
    assert (
        compare_against_baseline(current={"a.py": 2}, baseline={"a.py": 2}, changed=["a.py"]) == []
    )
    assert (
        compare_against_baseline(current={"a.py": 1}, baseline={"a.py": 2}, changed=["a.py"]) == []
    )


def test_compare_treats_new_file_as_zero_baseline() -> None:
    violations = compare_against_baseline(current={"new.py": 1}, baseline={}, changed=["new.py"])
    assert len(violations) == 1
    assert "new.py" in violations[0]


def test_compare_ignores_files_not_in_the_change_set() -> None:
    assert (
        compare_against_baseline(
            current={"untouched.py": 99},
            baseline={},
            changed=["a.py"],
        )
        == []
    )


# ---------------------------------------------------------------------------
# (d) BASE
# ---------------------------------------------------------------------------


def test_resolve_merge_base_returns_sha() -> None:
    assert resolve_merge_base("origin/master", lambda _args: "abc123\n") == "abc123"


def test_resolve_merge_base_unreachable_returns_empty() -> None:
    def _boom(_args: list[str]) -> str:
        raise RuntimeError("no such ref")

    assert resolve_merge_base("origin/master", _boom) == ""


# ---------------------------------------------------------------------------
# (e) INTERPRETER — pre-commit runs `language: system` hooks under its own
# python, which is NOT the repo venv even when the venv is on PATH. Resolving
# the venv explicitly keeps one mypy version producing the baselines; a
# fallback to whatever `mypy` is on PATH would silently mix versions and make
# baselines irreproducible.
# ---------------------------------------------------------------------------


def test_interpreter_prefers_repo_venv_when_present(tmp_path: Path) -> None:
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    venv_python.chmod(0o755)

    assert resolve_mypy_interpreter(tmp_path) == str(venv_python)


def test_interpreter_falls_back_to_current_when_no_venv(tmp_path: Path) -> None:
    assert resolve_mypy_interpreter(tmp_path) == sys.executable
