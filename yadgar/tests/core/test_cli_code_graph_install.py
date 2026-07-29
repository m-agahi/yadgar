"""`yadgar code-graph install` — the CLI seam the shell installers call.

Phase 2 of docs/plans/fix-shell-installer-code-graph-gap-2026-07-29.md.

Why a subcommand and not a bash reimplementation: `yadgar-setup.sh` fail-fasts
with EXIT 2 when any file in `_REQUIRED_HELPERS` is missing, so shipping a new
bash helper hard-breaks every pipx install that picks up a new script without
the matching helper. A new `yadgar <subcommand>` line adds nothing to that list
— steps 6-11 are already six such lines. It also avoids forking the pinned
SHA-256 table into a third language.

Dispatch hazard this file pins: `cmd_code_graph` resolves and validates
`args.repo` BEFORE the subcommand chain, and `install` takes no repo — so the
branch must short-circuit ahead of that or a bare `yadgar code-graph install`
dies with a raw AttributeError.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from yadgar.core.cli.code_graph import cmd_code_graph, register


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yadgar")
    register(parser.add_subparsers(dest="command"))
    return parser


class TestParserSurface:
    def test_install_subcommand_is_registered(self):
        args = _parser().parse_args(["code-graph", "install"])
        assert args.cg_command == "install"

    def test_no_code_graph_flag_parses(self):
        args = _parser().parse_args(["code-graph", "install", "--no-code-graph"])
        assert args.no_code_graph is True

    def test_no_code_graph_defaults_false(self):
        args = _parser().parse_args(["code-graph", "install"])
        assert args.no_code_graph is False

    def test_help_lists_the_opt_out(self, capsys):
        """The shell step feature-probes with `install --help`; the flag must
        be discoverable there."""
        with pytest.raises(SystemExit) as exc:
            _parser().parse_args(["code-graph", "install", "--help"])
        assert exc.value.code == 0
        assert "--no-code-graph" in capsys.readouterr().out

    def test_install_takes_no_repo_argument(self):
        """Provisioning is machine-global — a repo path would imply otherwise."""
        with pytest.raises(SystemExit):
            _parser().parse_args(["code-graph", "install", "/some/repo"])


class TestDispatch:
    def test_install_does_not_require_repo(self):
        """`cmd_code_graph` resolves `args.repo` before dispatch; `install` has
        no such attribute, so the branch must short-circuit ahead of it."""
        args = SimpleNamespace(cg_command="install", no_code_graph=False)
        with patch(
            "yadgar.core.install.code_graph_provision.provision_code_graph"
        ) as mock_provision:
            cmd_code_graph(args)  # must not raise AttributeError
        mock_provision.assert_called_once_with(opt_out=False)

    def test_opt_out_is_forwarded(self):
        args = SimpleNamespace(cg_command="install", no_code_graph=True)
        with patch(
            "yadgar.core.install.code_graph_provision.provision_code_graph"
        ) as mock_provision:
            cmd_code_graph(args)
        mock_provision.assert_called_once_with(opt_out=True)

    def test_install_never_exits_nonzero_on_failure(self):
        """The shell step must never abort an otherwise-good install, and
        `provision_code_graph` already swallows a failed download."""
        args = SimpleNamespace(cg_command="install", no_code_graph=False)
        with patch(
            "yadgar.core.install.code_graph_provision.provision_code_graph",
            return_value=False,
        ):
            assert cmd_code_graph(args) is None

    def test_install_does_not_touch_the_binary_runner(self):
        """`install` must work on a machine with NO binary — routing through the
        runner would `_die_binary_missing` (exit 2) precisely when the install
        is most needed."""
        args = SimpleNamespace(cg_command="install", no_code_graph=False)
        with (
            patch("yadgar.core.install.code_graph_provision.provision_code_graph"),
            patch("yadgar.core.code_graph.runner.resolve_binary", return_value=None),
        ):
            cmd_code_graph(args)  # no CodeGraphError, no SystemExit

    def test_unknown_subcommand_still_errors(self, capsys):
        args = SimpleNamespace(cg_command=None, repo=".", json=False)
        with pytest.raises(SystemExit) as exc:
            cmd_code_graph(args)
        assert exc.value.code == 1
        assert "install" in capsys.readouterr().err, "the hint must list the new subcommand"
