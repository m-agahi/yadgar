"""Tests for yadgar/core/cli/drain.py — pre-compaction context drain subcommand.

T2 Car B: cmd_drain is a thin forwarder — the drain writes run backend-side via
the POST /admin op ``pre_compact_drain`` (yadgar.core.cli._shared.
forward_pre_compact_drain). Strategy: patch the forward helper at its source
module (cmd_drain lazy-imports it by name) and pin the JSON-stdout contract plus
the register() wiring. silence_logging is patched too — the real one calls
logging.disable(CRITICAL) process-wide, which would leak into sibling tests.
"""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from yadgar.core.cli.drain import cmd_drain, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(directory="/tmp/proj", db_path=None):
    return SimpleNamespace(directory=directory, db_path=db_path)


@contextmanager
def _patched(forward_return=None, forward_side_effect=None):
    if forward_return is None and forward_side_effect is None:
        forward_return = {"status": "drained", "epoch": 1, "auto_checkpoint_created": True}
    with (
        patch("yadgar.core.cli._shared.silence_logging"),
        patch(
            "yadgar.core.cli._shared.forward_pre_compact_drain",
            return_value=forward_return,
            side_effect=forward_side_effect,
        ) as fwd,
    ):
        yield fwd


# ---------------------------------------------------------------------------
# register() — parser wiring
# ---------------------------------------------------------------------------


class TestRegister:
    def test_creates_drain_subparser(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["drain", "/some/dir"])
        assert args.directory == "/some/dir"
        assert hasattr(args, "func")

    def test_db_path_still_accepted_for_compat(self):
        """--db-path is kept for CLI compatibility (ignored since T2 Car B)."""
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["drain", "/some/dir", "--db-path", "/x/y.db"])
        assert args.db_path == "/x/y.db"

    def test_func_is_cmd_drain(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["drain", "/some/dir"])
        assert args.func is cmd_drain


# ---------------------------------------------------------------------------
# cmd_drain — forward + JSON stdout contract
# ---------------------------------------------------------------------------


class TestCmdDrainForward:
    def test_prints_result_as_json(self, capsys):
        result = {"status": "drained", "epoch": 2, "auto_checkpoint_created": False}
        with _patched(forward_return=result):
            cmd_drain(_make_args())
        out = capsys.readouterr().out
        assert json.loads(out) == result

    def test_forwards_directory(self):
        with _patched() as fwd:
            cmd_drain(_make_args(directory="/my/proj"))
        fwd.assert_called_once_with("/my/proj")

    def test_forward_error_propagates(self):
        """Forward-only: backend-unreachable errors surface, no local fallback."""
        with _patched(forward_side_effect=RuntimeError("YADGAR_EMBED_URL is not set")):
            with pytest.raises(RuntimeError, match="YADGAR_EMBED_URL"):
                cmd_drain(_make_args())
