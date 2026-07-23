"""TDD tests for the `yadgar code-graph` CLI subcommands (Car B).

Coverage
--------
1. register() adds a 'code-graph' subparser with index|query|refresh.
2. cmd dispatches: index → refresh_index; query → runner.query_graph;
   refresh → refresh_index + get_architecture (parsed JSON printed).
3. refresh prints the PARSED architecture JSON (the B→C seam), no block write.
4. Live smoke guarded by shutil.which (mirrors conftest.py:491 surreal guard).
"""

from __future__ import annotations

import json
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class TestRegistration:
    def test_register_adds_code_graph(self):
        import argparse

        from yadgar.core.cli import code_graph

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        code_graph.register(sub)

        args = parser.parse_args(["code-graph", "index", "/some/repo"])
        assert args.command == "code-graph"
        assert args.cg_command == "index"
        assert args.repo == "/some/repo"

    def test_query_subcommand_parses(self):
        import argparse

        from yadgar.core.cli import code_graph

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        code_graph.register(sub)

        args = parser.parse_args(["code-graph", "query", "/repo", "MATCH (n) RETURN n LIMIT 5"])
        assert args.cg_command == "query"
        assert args.cypher == "MATCH (n) RETURN n LIMIT 5"


class TestDispatch:
    def test_index_dispatches_to_refresh_index(self, tmp_path, capsys):
        from yadgar.core.cli import code_graph

        args = SimpleNamespace(cg_command="index", repo=str(tmp_path), json=True)
        with patch(
            "yadgar.core.code_graph.default_branch.refresh_index",
            return_value={"indexed": True, "canonical_root": str(tmp_path)},
        ) as mock_ref:
            code_graph.cmd_code_graph(args)

        mock_ref.assert_called_once()

    def test_query_dispatches_to_runner(self, tmp_path, capsys):
        from yadgar.core.cli import code_graph

        args = SimpleNamespace(
            cg_command="query",
            repo=str(tmp_path),
            project="proj",
            cypher="MATCH (n) RETURN n",
            json=True,
        )
        with patch(
            "yadgar.core.code_graph.runner.query_graph",
            return_value={"rows": [{"n": 1}]},
        ) as mock_q:
            code_graph.cmd_code_graph(args)

        mock_q.assert_called_once()
        out = capsys.readouterr().out
        assert '"rows"' in out

    def test_refresh_emits_block_payload(self, tmp_path, capsys):
        """The C→D seam: refresh renders a digest and EMITS the block payload JSON.

        It does NOT write the block — Car D's hook prompt → Claude calls
        block_update with the emitted {block_name, directory, content, chars}.
        """
        from yadgar.core.cli import code_graph

        args = SimpleNamespace(cg_command="refresh", repo=str(tmp_path), project=None, json=True)
        arch = {
            "project": "proj",
            "languages": [{"language": "Java", "file_count": 10}],
            "layers": [{"name": "Ctl", "layer": "api", "reason": "http routes"}],
            "hotspots": [{"qualified_name": "Svc.run", "fan_in": 5}],
        }
        with (
            patch(
                "yadgar.core.code_graph.default_branch.refresh_index",
                return_value={
                    "indexed": True,
                    "canonical_root": str(tmp_path),
                    "subdir": "",
                    "project": "proj",
                },
            ),
            patch("yadgar.core.code_graph.runner.get_architecture", return_value=arch) as mock_arch,
            patch("yadgar.core.code_graph.runner.fetch_endpoints", return_value=[]) as mock_ep,
        ):
            code_graph.cmd_code_graph(args)

        mock_arch.assert_called_once()
        mock_ep.assert_called_once()
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["block_name"] == "code_graph"
        assert payload["directory"] == str(tmp_path)
        assert payload["skipped"] is False
        assert payload["chars"] == len(payload["content"])
        assert "── code_graph:" in payload["content"]

    def test_refresh_emits_skip_signal_when_index_skipped(self, tmp_path, capsys):
        from yadgar.core.cli import code_graph

        args = SimpleNamespace(cg_command="refresh", repo=str(tmp_path), project=None, json=True)
        with (
            patch(
                "yadgar.core.code_graph.default_branch.refresh_index",
                return_value={"skipped": True, "reason": "opted_out"},
            ),
            patch("yadgar.core.code_graph.runner.get_architecture") as mock_arch,
            patch("yadgar.core.code_graph.runner.fetch_endpoints") as mock_ep,
        ):
            code_graph.cmd_code_graph(args)

        mock_arch.assert_not_called()
        mock_ep.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["skipped"] is True
        assert payload["reason"] == "opted_out"

    def test_binary_missing_friendly_error(self, tmp_path, capsys):
        from yadgar.core.cli import code_graph
        from yadgar.core.code_graph.runner import CodeGraphBinaryMissing

        args = SimpleNamespace(
            cg_command="query",
            repo=str(tmp_path),
            project="proj",
            cypher="MATCH (n) RETURN n",
            json=False,
        )
        with patch(
            "yadgar.core.code_graph.runner.query_graph",
            side_effect=CodeGraphBinaryMissing("codebase-memory-mcp not found"),
        ):
            with pytest.raises(SystemExit):
                code_graph.cmd_code_graph(args)

        err = capsys.readouterr().err
        assert "codebase-memory-mcp" in err
        assert "Traceback" not in err


@pytest.mark.skipif(
    shutil.which("codebase-memory-mcp") is None,
    reason="codebase-memory-mcp binary not installed (live smoke; mirrors conftest.py:491 surreal guard)",
)
class TestLiveSmoke:
    def test_binary_resolves_live(self):
        from yadgar.core.code_graph import runner

        assert runner.resolve_binary() is not None
