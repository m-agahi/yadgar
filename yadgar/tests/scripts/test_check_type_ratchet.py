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
  (f) INCOMPLETE: an aborted mypy run is never mistaken for a clean one

WHY (f) EXISTS
--------------
The ratchet shipped PASSING VACUOUSLY.  ``yadgar/_shared/storage/client.py``
carried a prose comment opening ``# type:``; mypy parsed it as a PEP 484 type
comment, rejected it as invalid syntax, and ABORTED — reporting one error
against a path that was not in the change set.  ``compare_against_baseline``
ignores paths outside the change set by design, so it saw no violations and
the guard returned 0.  Every branch whose import graph reached that module —
almost the whole tree — passed without being checked at all.

A guard that cannot tell "clean" from "never ran" is worse than no guard, so
absence of errors is no longer accepted as evidence of success: the run must
also produce mypy's own summary line accounting for every file requested.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_type_ratchet import (  # noqa: E402
    compare_against_baseline,
    detect_incomplete_run,
    parse_checked_count,
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


# ---------------------------------------------------------------------------
# (f) INCOMPLETE — the run must be proved to have checked what it was asked to
# check.  Every payload below is real mypy output captured from this repo.
# ---------------------------------------------------------------------------

# Verbatim capture of the defect: mypy aborted on a prose `# type:` comment in
# a module that was FOLLOWED, not requested. Exit code 2.
_ABORTED_OUTPUT = """\
yadgar/_shared/storage/client.py:35: error: Invalid syntax  [syntax]
Found 1 error in 1 file (errors prevented further checking)
"""

_CLEAN_OUTPUT = "Success: no issues found in 2 source files\n"

_REAL_ERRORS_OUTPUT = """\
a.py:2: error: Incompatible return value type (got "int", expected "str")  [return-value]
Found 1 error in 1 file (checked 2 source files)
"""


def test_checked_count_parses_both_summary_forms() -> None:
    assert parse_checked_count(_CLEAN_OUTPUT) == 2
    assert parse_checked_count(_REAL_ERRORS_OUTPUT) == 2
    # Singular forms must parse too — mypy drops the plural at 1.
    assert parse_checked_count("Success: no issues found in 1 source file\n") == 1
    assert parse_checked_count("Found 1 error in 1 file (checked 1 source file)\n") == 1


def test_checked_count_is_none_when_run_aborted() -> None:
    """The abort summary names no checked count — that absence is the signal."""
    assert parse_checked_count(_ABORTED_OUTPUT) is None
    assert parse_checked_count("") is None


def test_aborted_run_is_detected_not_treated_as_clean() -> None:
    """THE REGRESSION TEST: this exact output previously scored as a pass."""
    reasons = detect_incomplete_run(
        output=_ABORTED_OUTPUT,
        changed=["a.py", "b.py"],
        returncode=2,
    )
    assert reasons, "aborted mypy run must never be reported as clean"
    joined = "\n".join(reasons)
    assert "errors prevented further checking" in joined
    # The offending path must be named, or the failure just relocates the blindness.
    assert "yadgar/_shared/storage/client.py" in joined


def test_syntax_error_outside_changed_set_is_detected() -> None:
    """follow_imports = "silent" means a followed module cannot report a normal
    error — so an error outside the requested set proves the run derailed."""
    output = (
        "some/other/module.py:35: error: Invalid syntax  [syntax]\n"
        "Found 1 error in 1 file (checked 2 source files)\n"
    )
    reasons = detect_incomplete_run(output=output, changed=["a.py", "b.py"], returncode=1)
    assert reasons
    joined = "\n".join(reasons)
    assert "some/other/module.py" in joined


def test_clean_run_over_changed_files_still_passes() -> None:
    """The guard must not be paranoid to the point of uselessness."""
    assert detect_incomplete_run(output=_CLEAN_OUTPUT, changed=["a.py", "b.py"], returncode=0) == []


def test_run_reporting_real_errors_in_changed_files_is_complete() -> None:
    """Type errors are the ratchet's business; they are not an incomplete run."""
    assert (
        detect_incomplete_run(output=_REAL_ERRORS_OUTPUT, changed=["a.py", "b.py"], returncode=1)
        == []
    )


def test_missing_summary_line_is_detected() -> None:
    """Absence of output must not be the only evidence of success."""
    assert detect_incomplete_run(output="", changed=["a.py"], returncode=0)


def test_fewer_files_checked_than_requested_is_detected() -> None:
    """Positive evidence: mypy must account for every file it was handed."""
    output = "Success: no issues found in 1 source file\n"
    assert detect_incomplete_run(output=output, changed=["a.py", "b.py"], returncode=0)


def test_fatal_returncode_is_detected_even_with_plausible_output() -> None:
    """Exit 2 is mypy's fatal-error code; never trust the stdout that came with it."""
    assert detect_incomplete_run(output=_CLEAN_OUTPUT, changed=["a.py", "b.py"], returncode=2)
