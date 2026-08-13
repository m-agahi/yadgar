"""Tests for scripts/check_auth_token_pattern.py (bug train car 9).

The guard AST-scans ``yadgar/**/*.py`` (tests excluded) and FAILS if any code
outside the explicit allowlist hand-rolls
``os.environ.get("YADGAR_MCP_AUTH_TOKEN", ...)`` (or the bracket-subscript
equivalent) instead of routing through the ONE sanctioned resolver,
``yadgar.core.install.auth_token.resolve_auth_token()``.

Run:
  uv run pytest yadgar/tests/scripts/test_check_auth_token_pattern.py
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).parent.parent.parent.parent / "scripts" / "check_auth_token_pattern.py"


def run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _make_root(tmp_path: Path, src: str, name: str = "mod.py") -> Path:
    """Write ``src`` into a scannable root dir and return that root."""
    root = tmp_path / "pkg"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(textwrap.dedent(src))
    return root


# ---------------------------------------------------------------------------
# Violations → exit 1
# ---------------------------------------------------------------------------


def test_environ_get_call_fails(tmp_path):
    """A bare ``os.environ.get("YADGAR_MCP_AUTH_TOKEN", ...)`` → exit 1."""
    root = _make_root(
        tmp_path,
        """\
        import os

        def read_token():
            return os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
        """,
    )
    res = run_script("--root", str(root), "--allowlist", str(tmp_path / "no-such-allowlist.txt"))
    assert res.returncode == 1, res.stdout
    assert "YADGAR_MCP_AUTH_TOKEN" in res.stdout
    assert "mod.py:4" in res.stdout


def test_environ_get_call_no_default_arg_fails(tmp_path):
    """``os.environ.get("YADGAR_MCP_AUTH_TOKEN")`` (no default) still matches."""
    root = _make_root(
        tmp_path,
        """\
        import os

        def read_token():
            return os.environ.get("YADGAR_MCP_AUTH_TOKEN")
        """,
    )
    res = run_script("--root", str(root), "--allowlist", str(tmp_path / "no-such-allowlist.txt"))
    assert res.returncode == 1, res.stdout


def test_environ_subscript_fails(tmp_path):
    """``os.environ["YADGAR_MCP_AUTH_TOKEN"]`` (bracket form) is caught too."""
    root = _make_root(
        tmp_path,
        """\
        import os

        def read_token():
            return os.environ["YADGAR_MCP_AUTH_TOKEN"]
        """,
    )
    res = run_script("--root", str(root), "--allowlist", str(tmp_path / "no-such-allowlist.txt"))
    assert res.returncode == 1, res.stdout


def test_aliased_os_import_still_caught(tmp_path):
    """Matching is on the ``.environ.get`` attribute shape, not the module name."""
    root = _make_root(
        tmp_path,
        """\
        import os as _os

        def read_token():
            return _os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
        """,
    )
    res = run_script("--root", str(root), "--allowlist", str(tmp_path / "no-such-allowlist.txt"))
    assert res.returncode == 1, res.stdout


# ---------------------------------------------------------------------------
# Non-violations → exit 0
# ---------------------------------------------------------------------------


def test_resolve_auth_token_call_passes(tmp_path):
    """Routing through the sanctioned resolver is not flagged."""
    root = _make_root(
        tmp_path,
        """\
        from yadgar.core.install.auth_token import resolve_auth_token

        def read_token():
            return resolve_auth_token()
        """,
    )
    res = run_script("--root", str(root), "--allowlist", str(tmp_path / "no-such-allowlist.txt"))
    assert res.returncode == 0, res.stdout


def test_unrelated_environ_get_passes(tmp_path):
    """A different env var read is not flagged — the guard is name-specific."""
    root = _make_root(
        tmp_path,
        """\
        import os

        def read_port():
            return os.environ.get("YADGAR_PORT", "8765")
        """,
    )
    res = run_script("--root", str(root), "--allowlist", str(tmp_path / "no-such-allowlist.txt"))
    assert res.returncode == 0, res.stdout


def test_docstring_mention_not_flagged(tmp_path):
    """A prose mention of the env var name in a docstring/comment is not code."""
    root = _make_root(
        tmp_path,
        """\
        def f():
            \"\"\"Reads YADGAR_MCP_AUTH_TOKEN from somewhere.\"\"\"
            # os.environ.get("YADGAR_MCP_AUTH_TOKEN") is mentioned here as prose only
            return None
        """,
    )
    res = run_script("--root", str(root), "--allowlist", str(tmp_path / "no-such-allowlist.txt"))
    assert res.returncode == 0, res.stdout


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def test_allowlisted_line_passes(tmp_path):
    """An entry on the allowlist suppresses the violation for that exact line."""
    root = _make_root(
        tmp_path,
        """\
        import os

        def legitimate_violation():
            # pre-existing — covered by allowlist
            return os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
        """,
    )
    allowlist = tmp_path / "auth_token.allowlist"
    allowlist.write_text(f"{root.resolve()}/mod.py:5: documented exception\n")
    res = run_script("--root", str(root), "--allowlist", str(allowlist))
    assert res.returncode == 0, res.stdout


def test_allowlist_is_line_specific(tmp_path):
    """An allowlist entry for one line does not suppress a violation on another."""
    root = _make_root(
        tmp_path,
        """\
        import os

        def a():
            return os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")

        def b():
            return os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
        """,
    )
    allowlist = tmp_path / "auth_token.allowlist"
    allowlist.write_text(f"{root.resolve()}/mod.py:4: documented exception for a() only\n")
    res = run_script("--root", str(root), "--allowlist", str(allowlist))
    assert res.returncode == 1, res.stdout
    assert "mod.py:7" in res.stdout
    assert "mod.py:4" not in res.stdout


def test_missing_allowlist_file_behaves_as_empty(tmp_path):
    """A --allowlist path that does not exist degrades to no allowlist (never crashes)."""
    root = _make_root(
        tmp_path,
        """\
        import os

        def f():
            return os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
        """,
    )
    res = run_script("--root", str(root), "--allowlist", str(tmp_path / "does-not-exist.txt"))
    assert res.returncode == 1, res.stdout


# ---------------------------------------------------------------------------
# Scanning scope
# ---------------------------------------------------------------------------


def test_tests_dir_excluded(tmp_path):
    """A violating file under a tests/ dir is excluded from the scan."""
    tests_dir = tmp_path / "pkg" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_x.py").write_text(
        textwrap.dedent(
            """\
            import os

            def fixture():
                return os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
            """
        )
    )
    res = run_script(
        "--root", str(tmp_path / "pkg"), "--allowlist", str(tmp_path / "no-such-allowlist.txt")
    )
    assert res.returncode == 0, res.stdout


def test_list_all_flag_outputs_all_violations(tmp_path):
    """``--list-all`` is the same output as default failure output."""
    root = _make_root(
        tmp_path,
        """\
        import os

        def a():
            return os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")

        def b():
            return os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
        """,
    )
    res = run_script(
        "--root", str(root), "--allowlist", str(tmp_path / "no-such-allowlist.txt"), "--list-all"
    )
    assert res.returncode == 1
    assert "mod.py:4" in res.stdout
    assert "mod.py:7" in res.stdout


# ---------------------------------------------------------------------------
# The real repo (regression guard) — matches check_backend_bump / most
# check_*.py scripts' implicit contract: clean against the actual tree given
# the real committed allowlist.
# ---------------------------------------------------------------------------


def test_real_repo_is_clean():
    """Every YADGAR_MCP_AUTH_TOKEN read in yadgar/ is sanctioned or allowlisted."""
    res = run_script()
    assert res.returncode == 0, res.stdout
