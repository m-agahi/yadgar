"""Car 0 — ``yadgar hook <event>`` CLI subcommand.

The shared single code path every ported client's native hook shells out to.
Pins: argparse registration, dispatch to the right handler with stdin flowing
through, valid-event enumeration, and unknown-event rejection.
"""

from __future__ import annotations

import argparse
import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

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


def test_cmd_hook_prompt_recall_surfaces_skip_reason_on_stderr(capsys):
    """Car 8 task 338: server returns {skipped, retry_after_seconds} when the
    throttle fires (http.py:1856) or when SessionStart ran <3 min ago (the
    session-context throttle, http.py:1841). Today the hook only reads
    ``result.get("text", "")`` and prints if truthy — a throttle hit returns
    ``{"text": "", "skipped": "rate_limited", "retry_after_seconds": 47}`` and
    the hook silently emits nothing. From the operator's seat it is
    indistinguishable from a dead daemon and from a successful "no memories"
    recall (both yield an empty stdout). stdout must STAY clean (the model
    still parses it), but stderr must carry the skip reason so a developer
    debugging "no recall" can tell throttle from empty."""
    args = _parse(["hook", "prompt-recall"])
    payload = json.dumps({"prompt": "anything", "cwd": "/proj"})
    with (
        patch.object(
            hook,
            "_http_get",
            return_value={"text": "", "skipped": "rate_limited", "retry_after_seconds": 47},
        ),
        patch("sys.stdin", io.StringIO(payload)),
        pytest.raises(SystemExit),
    ):
        args.func(args)
    out, err = capsys.readouterr()
    assert out == "", "stdout MUST stay clean — it is injected into the prompt"
    assert "rate_limited" in err, (
        "skip reason must surface on stderr so an operator can distinguish "
        "throttle from dead daemon from empty-recall"
    )
    assert "47" in err, "retry_after_seconds must surface — that is the value the operator needs"


def test_cmd_hook_prompt_recall_surfaces_session_context_skip(capsys):
    """Companion to the rate-limited case: the OTHER skip reason the server
    emits (``session_context_recent`` from the 180s session-context throttle,
    http.py:1841). It does NOT carry ``retry_after_seconds`` — the operator
    needs to see the skip label AND the absence of a retry timer. Both must
    be distinguishable from a rate-limit hit."""
    args = _parse(["hook", "prompt-recall"])
    payload = json.dumps({"prompt": "anything", "cwd": "/proj"})
    with (
        patch.object(
            hook,
            "_http_get",
            return_value={"text": "", "skipped": "session_context_recent"},
        ),
        patch("sys.stdin", io.StringIO(payload)),
        pytest.raises(SystemExit),
    ):
        args.func(args)
    out, err = capsys.readouterr()
    assert out == ""
    assert "session_context_recent" in err


def test_cmd_hook_prompt_recall_does_not_surface_skip_when_text_present(capsys):
    """A skip label can ALSO appear when the server returns BOTH text AND a
    non-blocking diagnostic (rare today, but the schema does not forbid it).
    When ``text`` is truthy we print the injection AND must NOT also print
    the skip reason on stderr — that would pollute the developer's view of
    "recall fired fine" with a false-positive "throttled" hint."""
    args = _parse(["hook", "prompt-recall"])
    payload = json.dumps({"prompt": "anything", "cwd": "/proj"})
    with (
        patch.object(
            hook,
            "_http_get",
            return_value={"text": "ok injection", "skipped": "rate_limited"},
        ),
        patch("sys.stdin", io.StringIO(payload)),
        pytest.raises(SystemExit),
    ):
        args.func(args)
    out, err = capsys.readouterr()
    assert "ok injection" in out
    assert "rate_limited" not in err, (
        "a successful injection must not also warn about a skip label — the "
        "label is descriptive metadata, not an error"
    )


def test_cmd_hook_prompt_recall_deadline_at_least_1s(capsys):
    """Car G (task #63): the prompt-recall hook deadline must clear the
    measured server latency (0.60–0.88 s) with margin. The exact value may
    be bumped in a later car (config knob TODO), so pin the LOWER bound
    rather than the literal — a future bump to 3.0 s must not break this.
    """
    args = _parse(["hook", "prompt-recall"])
    payload = json.dumps({"prompt": "anything", "cwd": "/proj"})
    with (
        patch.object(hook, "_http_get", return_value={"text": "ok"}) as mock_get,
        patch("sys.stdin", io.StringIO(payload)),
        pytest.raises(SystemExit),
    ):
        args.func(args)
    timeout = mock_get.call_args.kwargs.get("timeout")
    if timeout is None:
        timeout = mock_get.call_args.args[2] if len(mock_get.call_args.args) >= 3 else None
    assert timeout is not None, "prompt-recall call site must pass a timeout kwarg"
    assert timeout >= 1.0, (
        f"prompt-recall deadline too tight: {timeout}s — measured server "
        f"latency 0.60–0.88 s. Car G (task #63) requires >=1.0s."
    )


def test_dispatch_unknown_returns_1():
    assert hook.dispatch("nope") == 1


def test_dispatch_known_returns_0():
    with patch.object(hook, "_http_post"), patch("sys.stdin", io.StringIO("{}")):
        assert hook.dispatch("pre-compact-drain") == 0


# ── _http_get / _http_post — py3.14 ResourceWarning leak guard ──────────────
#
# HTTPError is itself a response object holding a file wrapper (a
# tempfile._TemporaryFileWrapper via addbase on py3.14). An unclosed instance
# fires a spurious ResourceWarning at GC that pytest-xdist mis-attributes to
# an unrelated test (fatal under the zero-warning gate, ADR-0087). 6 hook
# events (post-tool-capture, session-start-context, post-compact-rehydrate,
# pre-compact-drain, prompt-recall, block-reflect) all route through these two
# helpers, so this is the highest-value close-guard in the sweep.


def test_http_get_closes_response_on_success():
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"text": "ok"}).encode()
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = hook._http_get("/hooks/session-context", {"directory": "/proj"})
    assert result == {"text": "ok"}
    mock_resp.close.assert_called_once()


def test_http_get_closes_http_error_and_returns_none():
    http_err = urllib.error.HTTPError(url="", code=500, msg="Error", hdrs={}, fp=None)
    with patch("urllib.request.urlopen", side_effect=http_err):
        result = hook._http_get("/hooks/session-context")
    assert result is None
    assert http_err.fp is None or http_err.fp.closed, "the hook must close the caught HTTPError"


def test_http_post_closes_response_on_success():
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"ok": True}).encode()
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = hook._http_post("/hooks/auto-capture", {"a": 1})
    assert result == {"ok": True}
    mock_resp.close.assert_called_once()


def test_http_post_closes_http_error_and_returns_none():
    http_err = urllib.error.HTTPError(url="", code=500, msg="Error", hdrs={}, fp=None)
    with patch("urllib.request.urlopen", side_effect=http_err):
        result = hook._http_post("/hooks/auto-capture", {"a": 1})
    assert result is None
    assert http_err.fp is None or http_err.fp.closed, "the hook must close the caught HTTPError"
