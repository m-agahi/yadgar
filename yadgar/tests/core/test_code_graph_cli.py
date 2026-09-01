"""TDD tests for the `yadgar code-graph` CLI subcommands (Car B).

Coverage
--------
1. register() adds a 'code-graph' subparser with index|query|refresh.
2. cmd dispatches: index → refresh_index; query → runner.query_graph;
   refresh → refresh_index + get_architecture (parsed JSON printed).
3. refresh prints the PARSED architecture JSON (the B→C seam), no block write.
4. Live smoke guarded by shutil.which (mirrors conftest.py:491 surreal guard).
5. task:0067 — the ``stale @ <sha>`` producer→renderer seam (AC-3/AC-4/AC-6).
   These are the CI-VISIBLE tier: they need no ``codebase-memory-mcp`` binary,
   unlike ``test_code_graph_e2e.py`` which module-skips in CI.
"""

from __future__ import annotations

import json
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest

#: A 40-hex sha — the exact shape ``git rev-parse origin/<default>`` returns and
#: the secret-gate FP's one reliable live trigger (#30); ``_stale_line`` cuts it
#: to 12 chars BEFORE ``_defang_secret_shaped_runs`` ever sees it.
_STALE_SHA = "0123456789abcdef0123456789abcdef01234567"

#: Minimal architecture fixture.  Load-bearing constraint: no ``[A-Za-z0-9/+]``
#: run of ≥40 chars, or ``_defang_secret_shaped_runs`` would split it and confuse
#: an exact-sha assertion.
_ARCH_FIXTURE = {
    "project": "proj",
    "languages": [{"language": "Java", "file_count": 10}],
    "layers": [{"name": "Ctl", "layer": "api", "reason": "http routes"}],
    "hotspots": [{"qualified_name": "Svc.run", "fan_in": 5}],
}


def _template_refresh_argv() -> list[str]:
    """The command line the stop-hook template actually tells an agent to run.

    Read out of ``code_graph_refresh_prompt.md`` rather than restated here — a
    restatement is a second copy that can agree with the CLI while the shipped
    template does not.
    """
    import shlex
    from pathlib import Path

    import yadgar.core.hooks as _hooks

    template = (
        Path(_hooks.__file__).parent / "templates" / "code_graph_refresh_prompt.md"
    ).read_text()
    for raw in template.splitlines():
        line = raw.strip()
        if line.startswith("yadgar code-graph refresh"):
            # Drop the leading `yadgar`: the parser under test is the subcommand
            # registrar, not the top-level entry point.
            return shlex.split(line)[1:]
    raise AssertionError("the template no longer contains a `yadgar code-graph refresh` line")


class TestTemplateAgreesWithTheCli:
    """The stop-hook template and the CLI must describe the same command.

    THE DEFECT: the template said the command "prints ONE JSON object to
    stdout" and told the agent to branch on the payload's own ``block_name``
    (hardcoding ``code_graph`` makes every leaf of a monorepo overwrite the same
    block). But ``_cmd_refresh`` gates that JSON on ``--json``, which the
    template's command line omitted — so the agent got the rendered digest on
    stdout, a prose instruction on stderr, and no ``block_name`` anywhere.

    Every other test in this module builds ``SimpleNamespace(..., json=True)``
    and never touches argparse, which is exactly why the divergence survived:
    the tests exercised the flagged form while the shipped template did not use
    it. This class closes that seam by parsing the template's own argv.
    """

    def test_the_templates_command_parses_with_json_enabled(self):
        import argparse

        from yadgar.core.cli import code_graph

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        code_graph.register(sub)

        args = parser.parse_args(_template_refresh_argv())

        assert args.cg_command == "refresh"
        assert args.json is True, (
            "the template's command line does not request JSON, so step 1 of the "
            "protocol cannot get the block_name it tells the agent to use"
        )

    def test_the_templates_command_puts_one_json_object_on_stdout(self, tmp_path, capsys):
        """Parse the template's argv, dispatch it, and read stdout as the agent would."""
        import argparse

        from yadgar.core.cli import code_graph

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        code_graph.register(sub)
        argv = [a.replace("{directory}", str(tmp_path)) for a in _template_refresh_argv()]
        args = parser.parse_args(argv)

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
            patch("yadgar.core.code_graph.runner.get_architecture", return_value=_ARCH_FIXTURE),
            patch("yadgar.core.code_graph.runner.fetch_endpoints", return_value=[]),
        ):
            code_graph.cmd_code_graph(args)

        payload = json.loads(capsys.readouterr().out)
        assert payload["block_name"] == "code_graph"
        assert payload["skipped"] is False
        assert payload["directory"] == str(tmp_path)


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

    def test_refresh_prints_create_or_update_hint(self, tmp_path, capsys):
        """Bug: the FIRST-EVER refresh of a repo has no existing ``code_graph``
        block, so ``block_update`` 404s not-found. The printed stderr hint
        used to say only "Claude calls block_update" — no mention of the
        ``block_create`` fallback — so an agent following just the CLI output
        (not the stop-hook template) had no guidance on the 404 path. The hint
        must name both tools plus the not-found fallback condition, mirroring
        ``code_graph_refresh_prompt.md``'s create-or-update step.
        """
        from yadgar.core.cli import code_graph

        args = SimpleNamespace(cg_command="refresh", repo=str(tmp_path), project=None, json=True)
        arch = {
            "project": "proj",
            "languages": [{"language": "Java", "file_count": 10}],
            "layers": [],
            "hotspots": [],
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
            patch("yadgar.core.code_graph.runner.get_architecture", return_value=arch),
            patch("yadgar.core.code_graph.runner.fetch_endpoints", return_value=[]),
        ):
            code_graph.cmd_code_graph(args)

        err = capsys.readouterr().err
        assert "block_update" in err
        assert "block_create" in err
        assert "not-found" in err or "not found" in err

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

    def test_refresh_fresh_digest_carries_no_stale_marker(self, tmp_path, capsys):
        """AC-3: a just-completed index is by construction NOT stale."""
        from yadgar.core.cli import code_graph

        args = SimpleNamespace(cg_command="refresh", repo=str(tmp_path), project=None, json=True)
        with (
            patch(
                "yadgar.core.code_graph.default_branch.refresh_index",
                return_value={
                    "indexed": True,
                    "canonical_root": str(tmp_path),
                    "subdir": "",
                    "project": "proj",
                    "head_sha": _STALE_SHA,
                },
            ),
            patch("yadgar.core.code_graph.runner.get_architecture", return_value=_ARCH_FIXTURE),
            patch("yadgar.core.code_graph.runner.fetch_endpoints", return_value=[]),
        ):
            code_graph.cmd_code_graph(args)

        payload = json.loads(capsys.readouterr().out)
        assert payload["skipped"] is False
        assert "stale @" not in payload["content"]

    def test_refresh_reemits_stale_marked_digest_on_fetch_failure(self, tmp_path, capsys):
        """AC-4 — THE criterion this car exists for (CI-VISIBLE, no binary needed).

        Drives the real production seam ``cmd_code_graph(refresh --json)`` with
        only the runner/git boundary patched, so the assertion covers the real
        ``_cmd_refresh`` identity construction AND the real ``render_digest`` —
        exactly where the wiring gap lived.  ``_stale_line``'s AND-guard means a
        producer that forgets either key silently renders nothing, which is how
        the marker shipped dead; this test makes that unshippable.
        """
        from yadgar.core.cli import code_graph

        args = SimpleNamespace(cg_command="refresh", repo=str(tmp_path), project=None, json=True)
        with (
            patch(
                "yadgar.core.code_graph.default_branch.refresh_index",
                return_value={
                    "skipped": True,
                    "reason": "fetch_failed",
                    "canonical_root": str(tmp_path),
                    "subdir": "",
                    "default_branch": "master",
                    "head_sha": _STALE_SHA,
                },
            ),
            patch("yadgar.core.code_graph.runner.get_architecture", return_value=_ARCH_FIXTURE),
            patch("yadgar.core.code_graph.runner.fetch_endpoints", return_value=[]),
        ):
            code_graph.cmd_code_graph(args)

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        # The cached digest IS re-emitted — a skip that writes nothing leaves the
        # previously-written block serving an aged digest with no marker at all.
        assert payload["skipped"] is False
        assert payload["block_name"] == "code_graph"
        assert payload["directory"] == str(tmp_path)
        assert payload["chars"] == len(payload["content"])
        # …and it carries the freshness marker, cut to 12 chars.
        assert f"stale @ {_STALE_SHA[:12]}" in payload["content"]
        # 13 chars absent ⇒ the short-sha cut really happened (a full 40-hex sha
        # in the digest is the secret-gate FP's one reliable live trigger, #30).
        assert _STALE_SHA[:13] not in payload["content"]
        # The reason is named on stderr — the payload shape (BC-CODEGRAPH-3) is
        # unchanged, so stderr is where a human/agent learns WHY it is stale.
        assert "fetch_failed" in captured.err

    def test_refresh_hard_skips_when_no_cached_architecture(self, tmp_path, capsys):
        """AC-6: fetch_failed + empty architecture ⇒ never emit a digest we cannot honour."""
        from yadgar.core.cli import code_graph

        args = SimpleNamespace(cg_command="refresh", repo=str(tmp_path), project=None, json=True)
        with (
            patch(
                "yadgar.core.code_graph.default_branch.refresh_index",
                return_value={
                    "skipped": True,
                    "reason": "fetch_failed",
                    "canonical_root": str(tmp_path),
                    "subdir": "",
                    "head_sha": _STALE_SHA,
                },
            ),
            patch("yadgar.core.code_graph.runner.get_architecture", return_value={}),
            patch("yadgar.core.code_graph.runner.fetch_endpoints") as mock_ep,
        ):
            code_graph.cmd_code_graph(args)

        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "block_name": "code_graph",
            "skipped": True,
            "reason": "fetch_failed",
        }
        mock_ep.assert_not_called()

    def test_refresh_hard_skips_when_no_resolvable_sha(self, tmp_path, capsys):
        """AC-6: fetch_failed without a sha ⇒ hard skip (never a bare ``stale @``)."""
        from yadgar.core.cli import code_graph

        args = SimpleNamespace(cg_command="refresh", repo=str(tmp_path), project=None, json=True)
        with (
            patch(
                "yadgar.core.code_graph.default_branch.refresh_index",
                return_value={
                    "skipped": True,
                    "reason": "fetch_failed",
                    "canonical_root": str(tmp_path),
                    "subdir": "",
                },
            ),
            patch("yadgar.core.code_graph.runner.get_architecture") as mock_arch,
            patch("yadgar.core.code_graph.runner.fetch_endpoints") as mock_ep,
        ):
            code_graph.cmd_code_graph(args)

        payload = json.loads(capsys.readouterr().out)
        assert payload["skipped"] is True
        assert payload["reason"] == "fetch_failed"
        mock_arch.assert_not_called()
        mock_ep.assert_not_called()

    def test_refresh_hard_skips_on_no_remote(self, tmp_path, capsys):
        """AC-6: ``no_remote_or_default_branch`` stays a bit-for-bit hard skip.

        Not an omission: that reason is reached precisely because no
        ``<default>`` resolved, so ``git rev-parse origin/<default>`` has nothing
        to interpolate and no sha exists by construction.
        """
        from yadgar.core.cli import code_graph

        args = SimpleNamespace(cg_command="refresh", repo=str(tmp_path), project=None, json=True)
        with (
            patch(
                "yadgar.core.code_graph.default_branch.refresh_index",
                return_value={
                    "skipped": True,
                    "reason": "no_remote_or_default_branch",
                    "canonical_root": str(tmp_path),
                    "subdir": "",
                },
            ),
            patch("yadgar.core.code_graph.runner.get_architecture") as mock_arch,
            patch("yadgar.core.code_graph.runner.fetch_endpoints") as mock_ep,
        ):
            code_graph.cmd_code_graph(args)

        payload = json.loads(capsys.readouterr().out)
        assert payload["skipped"] is True
        assert payload["reason"] == "no_remote_or_default_branch"
        mock_arch.assert_not_called()
        mock_ep.assert_not_called()

    def test_refresh_hard_skips_when_stale_render_hits_missing_binary(self, tmp_path, capsys):
        """AC-6: the stale re-render must not turn a clean skip into an exit-2.

        ``fetch_failed`` on a box with no indexer binary emits a clean skip JSON
        and exits 0 today; the stale branch adds a ``get_architecture``
        subprocess where there was none, so its typed failure has to degrade to
        that same hard skip — the hook template's step 1 expects ONE JSON object.
        """
        from yadgar.core.cli import code_graph
        from yadgar.core.code_graph.runner import CodeGraphBinaryMissing

        args = SimpleNamespace(cg_command="refresh", repo=str(tmp_path), project=None, json=True)
        with (
            patch(
                "yadgar.core.code_graph.default_branch.refresh_index",
                return_value={
                    "skipped": True,
                    "reason": "fetch_failed",
                    "canonical_root": str(tmp_path),
                    "subdir": "",
                    "head_sha": _STALE_SHA,
                },
            ),
            patch(
                "yadgar.core.code_graph.runner.get_architecture",
                side_effect=CodeGraphBinaryMissing("codebase-memory-mcp not found"),
            ),
            patch("yadgar.core.code_graph.runner.fetch_endpoints"),
        ):
            code_graph.cmd_code_graph(args)

        payload = json.loads(capsys.readouterr().out)
        assert payload["skipped"] is True
        assert payload["reason"] == "fetch_failed"

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
