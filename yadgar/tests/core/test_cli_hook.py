"""Car 0 — ``yadgar hook <event>`` CLI subcommand.

The shared single code path every ported client's native hook shells out to.
Pins: argparse registration, dispatch to the right handler with stdin flowing
through, valid-event enumeration, and unknown-event rejection.
"""

from __future__ import annotations

import argparse
import io
import json
from unittest.mock import patch

import pytest

import yadgar.core.cli.hook as hook


def _parse(argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    hook.register(sub)
    return parser.parse_args(argv)


def test_register_adds_hook_subcommand():
    args = _parse(["hook", "session-start-context"])
    assert args.command == "hook"
    assert args.event == "session-start-context"
    assert args.func is hook.cmd_hook


def test_register_accepts_optional_project_directory():
    args = _parse(["hook", "prompt-recall", "/some/project"])
    assert args.event == "prompt-recall"
    assert args.project_directory == "/some/project"


def test_all_six_events_are_valid_choices():
    for event in (
        "post-tool-capture",
        "session-start-context",
        "post-compact-rehydrate",
        "pre-compact-drain",
        "prompt-recall",
        "block-reflect",
    ):
        args = _parse(["hook", event])
        assert args.event == event


def test_unknown_event_rejected_by_argparse():
    with pytest.raises(SystemExit):
        _parse(["hook", "not-a-real-event"])


def test_cmd_hook_routes_to_handler_and_reads_stdin(capsys):
    """`yadgar hook session-start-context` runs the handler against stdin."""
    args = _parse(["hook", "session-start-context"])
    payload = json.dumps({"cwd": "/proj"})
    with (
        patch.object(hook, "_http_get", return_value={"text": "CTX INJECTED"}),
        patch("sys.stdin", io.StringIO(payload)),
        pytest.raises(SystemExit) as exc,
    ):
        args.func(args)
    assert exc.value.code == 0
    assert "CTX INJECTED" in capsys.readouterr().out


def test_cmd_hook_prompt_recall_flows_stdin(capsys):
    args = _parse(["hook", "prompt-recall"])
    payload = json.dumps({"prompt": "How does recall work?", "cwd": "/proj"})
    with (
        patch.object(hook, "_http_get", return_value={"text": "42"}) as mock_get,
        patch("sys.stdin", io.StringIO(payload)),
        pytest.raises(SystemExit) as exc,
    ):
        args.func(args)
    assert exc.value.code == 0
    mock_get.assert_called_once()
    assert "/hooks/prompt-recall" in mock_get.call_args[0][0]
    assert "42" in capsys.readouterr().out


def test_dispatch_unknown_returns_1():
    assert hook.dispatch("nope") == 1


def test_dispatch_known_returns_0():
    with patch.object(hook, "_http_post"), patch("sys.stdin", io.StringIO("{}")):
        assert hook.dispatch("pre-compact-drain") == 0
