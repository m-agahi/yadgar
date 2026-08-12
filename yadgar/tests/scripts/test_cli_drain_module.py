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


def _make_args(directory="/tmp/proj", db_path=None, transcript_path=None, project="owner/repo"):
    """Build a parsed-args double for ``cmd_drain``.

    ``project`` is explicit because ``/tmp/proj`` is not a git tree: since C5
    (ADR-0227) nothing derives an identity from a directory. ``cmd_drain``
    resolves with ``required=False``, so an absent one yields ``None`` rather
    than exiting — see ``test_forwards_none_project_when_tree_is_unresolvable``.
    """
    return SimpleNamespace(
        directory=directory,
        db_path=db_path,
        transcript_path=transcript_path,
        project=project,
    )


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

    def test_transcript_path_flag_accepted(self):
        """HOOKS Car 2: --transcript-path is a registered optional flag."""
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["drain", "/some/dir", "--transcript-path", "/t.jsonl"])
        assert args.transcript_path == "/t.jsonl"


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
        # HOOKS Car 2 forwarded (directory, transcript_path); C4 appended the
        # host-resolved project_id as the third positional.
        fwd.assert_called_once_with("/my/proj", None, "owner/repo")

    def test_forwards_transcript_path(self):
        """HOOKS Car 2: --transcript-path is threaded through to the forwarder."""
        with _patched() as fwd:
            cmd_drain(_make_args(directory="/my/proj", transcript_path="/tmp/s.jsonl"))
        fwd.assert_called_once_with("/my/proj", "/tmp/s.jsonl", "owner/repo")

    def test_forwards_the_resolved_project_id(self):
        """C4: ``--project`` reaches the backend op rather than being dropped."""
        with _patched() as fwd:
            cmd_drain(_make_args(directory="/my/proj", project="acme/widget"))
        fwd.assert_called_once_with("/my/proj", None, "acme/widget")

    def test_forwards_none_project_when_tree_is_unresolvable(self):
        """C5/ADR-0227: drain resolves with ``required=False`` — and never invents one.

        ``/tmp/proj`` has no identity, and this command runs from
        ``pre-compact-drain.sh`` where exiting would silently lose the very
        checkpoint the drain exists to save. So it degrades to ``None`` — the
        assertion is that the third positional is ``None``, NOT a substituted
        key like ``"global"`` or ``local/proj``.
        """
        with _patched() as fwd:
            cmd_drain(_make_args(directory="/tmp/proj", project=None))
        fwd.assert_called_once_with("/tmp/proj", None, None)

    def test_forward_error_propagates(self):
        """Forward-only: backend-unreachable errors surface, no local fallback."""
        with _patched(forward_side_effect=RuntimeError("YADGAR_EMBED_URL is not set")):
            with pytest.raises(RuntimeError, match="YADGAR_EMBED_URL"):
                cmd_drain(_make_args())
