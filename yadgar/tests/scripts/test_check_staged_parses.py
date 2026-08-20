"""Tests for ``scripts/check_staged_parses.py``.

The hook's whole value is that it FAILS on an unparseable staged file, so the
tests are written to make a vacuous pass impossible to ship:

* every negative case asserts on the SUBJECT named in the message (path, line,
  reason), never merely "returned non-zero";
* every positive case is paired with a mutation showing the check was actually
  capable of failing on that input;
* the three anti-vacuity arms documented in the script's module docstring (no
  files / unreadable file / invisible interpreter) each get a test, because
  those are the ways this hook could report OK while reading nothing.

DELIBERATELY ABSENT: a fixture asserting that PEP 758's bare ``except A, B:``
parses. It does on 3.14 and does NOT on the 3.13 that pre-commit's
``language: system`` hooks actually run, so such a test would pass under pytest
(project venv, 3.14) and fail at commit time — an environment-dependent
assertion masquerading as a language fact.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from yadgar.tests._paths import REPO_ROOT

_SCRIPT = REPO_ROOT / "scripts" / "check_staged_parses.py"

_spec = importlib.util.spec_from_file_location("check_staged_parses", _SCRIPT)
assert _spec is not None and _spec.loader is not None
C = importlib.util.module_from_spec(_spec)
sys.modules["check_staged_parses"] = C
_spec.loader.exec_module(C)


BROKEN = "def f(:\n"
VALID = "def f():\n    return 1\n"


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


class TestParsing:
    def test_a_valid_file_passes(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "ok.py", VALID)
        assert C.main([str(path)]) == 0

    def test_an_unparseable_file_fails_naming_file_and_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write(tmp_path, "bad.py", BROKEN)
        assert C.main([str(path)]) == 1
        err = capsys.readouterr().err
        assert "SYNTAX ERROR" in err
        assert str(path) in err, err
        assert ":1" in err, err

    def test_the_same_file_fixed_then_passes(self, tmp_path: Path) -> None:
        """Discrimination, in one test: identical path, opposite verdicts."""
        path = _write(tmp_path, "same.py", BROKEN)
        assert C.main([str(path)]) == 1
        path.write_text(VALID, encoding="utf-8")
        assert C.main([str(path)]) == 0

    def test_one_bad_file_among_good_ones_still_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        good_a = _write(tmp_path, "a.py", VALID)
        bad = _write(tmp_path, "b.py", BROKEN)
        good_b = _write(tmp_path, "c.py", VALID)
        assert C.main([str(good_a), str(bad), str(good_b)]) == 1
        err = capsys.readouterr().err
        assert "b.py" in err, err
        assert "a.py" not in err and "c.py" not in err, err


class TestAntiVacuity:
    """The ways this hook could report OK while checking nothing."""

    def test_no_files_is_an_error_not_a_pass(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert C.main([]) == 1
        assert "mis-wired" in capsys.readouterr().err

    def test_an_unreadable_file_is_an_error_not_a_skip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Injected, not chmod-based: CI runs as root, where a mode denial
        does not apply and the test would assert nothing."""
        path = _write(tmp_path, "unreadable.py", VALID)
        real_read_text = Path.read_text

        def boom(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == "unreadable.py":
                raise OSError(5, "Input/output error")
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", boom)
        assert C.main([str(path)]) == 1
        assert "UNREADABLE" in capsys.readouterr().err

    def test_an_undecodable_file_is_an_error_not_a_skip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "latin.py"
        path.write_bytes(b"x = '\xff\xfe not utf8'\n")
        assert C.main([str(path)]) == 1
        assert "UNDECODABLE" in capsys.readouterr().err

    def test_an_interpreter_below_the_sanity_floor_is_an_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert C.check_interpreter((2, 7)) != []
        assert C.check_interpreter((3, 8)) != []

    def test_the_real_interpreter_clears_the_sanity_floor(self) -> None:
        """Paired with the test above: the guard must not be always-red.

        This is the assertion an earlier draft got wrong — it compared against
        `requires-python` (3.14) while the hook runs under pre-commit's 3.13,
        making every commit fail.
        """
        assert C.check_interpreter() == []

    def test_the_interpreter_is_reported_on_success(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A parse verdict without its grammar is half a result."""
        path = _write(tmp_path, "ok.py", VALID)
        assert C.main([str(path)]) == 0
        out = capsys.readouterr().out
        assert f"{sys.version_info.major}.{sys.version_info.minor}" in out, out

    def test_the_sanity_floor_is_not_the_project_floor(self) -> None:
        """Pins the distinction the module docstring turns on.

        `language: system` hooks run pre-commit's interpreter, which is older
        than `requires-python`. A future edit that "corrects" the floor to match
        pyproject.toml re-breaks every commit.
        """
        assert C._SANITY_FLOOR < (3, 14)


class TestTheRealTree:
    def test_every_python_file_in_the_repo_parses(self) -> None:
        """Green on arrival — and the reason the hook can be turned on.

        Runs under the project venv (3.14), so it is NOT a substitute for the
        commit-time check under 3.13; it only proves the hook is not red on
        arrival for the tree as it stands.
        """
        # Ledger task 222: this used rglob + a hand-maintained skip set of exact
        # directory names. That set listed `.venv` but not `.venv-test`, so the
        # walk descended into a gitignored virtualenv and hit
        # joblib/test/test_func_inspect_special_encoding.py — a deliberately
        # non-UTF-8 fixture — reporting UNDECODABLE for a file that is not ours.
        #
        # Enumerating tracked files instead removes the whole CLASS: anything
        # gitignored is out of scope by construction, and the set cannot rot as
        # new build/tool directories appear. It also matches what this check is
        # FOR — the hook guards files being committed, and a file git does not
        # track is never committed.
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", "*.py"],
            capture_output=True,
            text=True,
            check=True,
        )
        files = [REPO_ROOT / rel for rel in proc.stdout.split("\0") if rel]
        # Deleted-but-still-indexed paths would break the parse call on a dirty
        # tree; keep only what is actually on disk.
        files = [p for p in files if p.is_file()]
        assert files, "found no tracked Python files — the enumeration itself is broken"
        assert C.main([str(p) for p in files]) == 0
