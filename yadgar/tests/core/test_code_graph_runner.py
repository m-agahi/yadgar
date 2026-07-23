"""TDD tests for code_graph runner (Car B, ADR-0162).

The codebase-memory-mcp binary is a 259MB external dep — fully MOCKED here.
subprocess.run and shutil.which are patched; no real binary is executed.

Coverage
--------
1. Binary resolution: shutil.which OR Car A install path; absent → typed error.
2. CLI invocation form: `codebase-memory-mcp cli <tool>`, args via stdin.
3. stderr `level=info msg=mem.init` line is stripped; JSON stdout parsed.
4. CBM_ALLOWED_ROOT + CBM_CACHE_DIR always set in the subprocess env.
5. query_graph caps returned rows + bytes (db_inspect 500-row precedent).
6. Binary-absent → CodeGraphBinaryMissing (typed), not a stacktrace.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _completed(stdout: str = "{}", stderr: str = "", returncode: int = 0):
    cp = MagicMock()
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


class TestBinaryResolution:
    def test_resolves_via_which(self):
        from yadgar.core.code_graph import runner

        with patch("shutil.which", return_value="/usr/bin/codebase-memory-mcp"):
            assert runner.resolve_binary() == "/usr/bin/codebase-memory-mcp"

    def test_falls_back_to_install_dir(self, tmp_path):
        from yadgar.core.code_graph import runner

        fake = tmp_path / "codebase-memory-mcp"
        fake.write_text("")
        with (
            patch("shutil.which", return_value=None),
            patch("yadgar.core.code_graph.runner._default_bin_dir", return_value=tmp_path),
        ):
            assert runner.resolve_binary() == str(fake)

    def test_absent_returns_none(self, tmp_path):
        from yadgar.core.code_graph import runner

        with (
            patch("shutil.which", return_value=None),
            patch("yadgar.core.code_graph.runner._default_bin_dir", return_value=tmp_path),
        ):
            assert runner.resolve_binary() is None

    def test_binary_absent_raises_typed_error(self, tmp_path):
        from yadgar.core.code_graph import runner

        with (
            patch("shutil.which", return_value=None),
            patch("yadgar.core.code_graph.runner._default_bin_dir", return_value=tmp_path),
        ):
            with pytest.raises(runner.CodeGraphBinaryMissing):
                runner.index_repository(str(tmp_path))


class TestInvocationForm:
    def test_index_calls_cli_index_repository(self, tmp_path):
        from yadgar.core.code_graph import runner

        with (
            patch("shutil.which", return_value="/bin/codebase-memory-mcp"),
            patch("subprocess.run", return_value=_completed('{"ok": true}')) as mock_run,
        ):
            out = runner.index_repository(str(tmp_path))

        assert out == {"ok": True}
        argv = mock_run.call_args.args[0]
        assert argv[0] == "/bin/codebase-memory-mcp"
        assert argv[1] == "cli"
        assert argv[2] == "index_repository"

    def test_args_passed_via_stdin(self, tmp_path):
        from yadgar.core.code_graph import runner

        with (
            patch("shutil.which", return_value="/bin/codebase-memory-mcp"),
            patch("subprocess.run", return_value=_completed("{}")) as mock_run,
        ):
            runner.index_repository(str(tmp_path))

        # JSON args go on stdin, not as a deprecated positional
        stdin_payload = mock_run.call_args.kwargs.get("input")
        assert stdin_payload is not None
        parsed = json.loads(stdin_payload)
        assert parsed["repo_path"] == str(tmp_path)


class TestStderrStrip:
    def test_mem_init_line_ignored_json_parsed(self, tmp_path):
        from yadgar.core.code_graph import runner

        stderr = "level=info msg=mem.init db=/tmp/x\nlevel=info msg=ready\n"
        with (
            patch("shutil.which", return_value="/bin/codebase-memory-mcp"),
            patch("subprocess.run", return_value=_completed('{"project": "p"}', stderr=stderr)),
        ):
            out = runner.index_repository(str(tmp_path))
        assert out == {"project": "p"}


class TestContainmentEnv:
    def test_allowed_root_set_to_indexed_path(self, tmp_path):
        from yadgar.core.code_graph import config, runner

        # Use a dedicated indexed subdir so it is disjoint from the yadgar cache
        # (the conftest pins HOME under tmp_path, so tmp_path itself is not a
        # valid "not the cache" perimeter).
        indexed = tmp_path / "repo"
        indexed.mkdir()

        with (
            patch("shutil.which", return_value="/bin/codebase-memory-mcp"),
            patch("subprocess.run", return_value=_completed("{}")) as mock_run,
        ):
            runner.index_repository(str(indexed))

        env = mock_run.call_args.kwargs["env"]
        assert env["CBM_ALLOWED_ROOT"] == str(indexed)
        assert "CBM_CACHE_DIR" in env
        # cache dir is the yadgar-owned dir, NOT under the indexed repo tree
        assert env["CBM_CACHE_DIR"] == str(config.cache_dir())
        assert not env["CBM_CACHE_DIR"].startswith(str(indexed))

    def test_allowed_root_on_query(self, tmp_path):
        from yadgar.core.code_graph import runner

        with (
            patch("shutil.which", return_value="/bin/codebase-memory-mcp"),
            patch("subprocess.run", return_value=_completed('{"rows": []}')) as mock_run,
        ):
            runner.query_graph("proj", "MATCH (n) RETURN n", allowed_root=str(tmp_path))

        env = mock_run.call_args.kwargs["env"]
        assert env["CBM_ALLOWED_ROOT"] == str(tmp_path)


class TestQueryCap:
    def test_query_caps_rows(self, tmp_path):
        from yadgar.core.code_graph import runner

        big = {"rows": [{"i": i} for i in range(5000)]}
        with (
            patch("shutil.which", return_value="/bin/codebase-memory-mcp"),
            patch("subprocess.run", return_value=_completed(json.dumps(big))),
        ):
            out = runner.query_graph("proj", "MATCH (n) RETURN n", allowed_root=str(tmp_path))

        assert len(out["rows"]) <= runner.MAX_QUERY_ROWS
        assert out.get("truncated") is True

    def test_query_caps_bytes(self, tmp_path):
        from yadgar.core.code_graph import runner

        # few rows but each enormous → byte cap fires
        big = {"rows": [{"blob": "x" * 100_000} for _ in range(50)]}
        with (
            patch("shutil.which", return_value="/bin/codebase-memory-mcp"),
            patch("subprocess.run", return_value=_completed(json.dumps(big))),
        ):
            out = runner.query_graph("proj", "MATCH (n) RETURN n", allowed_root=str(tmp_path))

        assert out.get("truncated") is True
        assert len(json.dumps(out["rows"])) <= runner.MAX_QUERY_BYTES * 2  # bounded


class TestGetArchitecture:
    def test_get_architecture_shape(self, tmp_path):
        from yadgar.core.code_graph import runner

        with (
            patch("shutil.which", return_value="/bin/codebase-memory-mcp"),
            patch(
                "subprocess.run",
                return_value=_completed('{"layers": [], "hotspots": []}'),
            ) as mock_run,
        ):
            out = runner.get_architecture("proj", allowed_root=str(tmp_path))

        assert "layers" in out
        argv = mock_run.call_args.args[0]
        assert argv[2] == "get_architecture"
        stdin_payload = json.loads(mock_run.call_args.kwargs["input"])
        assert stdin_payload["project"] == "proj"
        assert stdin_payload["aspects"] == ["all"]
