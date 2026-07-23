"""Tests for v5.1.9 F1 — session-start-context hook passes branch query param.

TDD — written BEFORE the v5.1.9 F1 implementation (red-green).
Covers:
- Hook captures branch via subprocess on host
- ?branch=<value> added to URL when git succeeds
- No ?branch= appended when git returns empty or fails
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

# Path to the hook script under test
_HOOK = Path(__file__).parent.parent.parent / "core" / "hooks" / "session-start-context.py"


def _load_hook():
    """Import the hook module from its file path, bypassing __main__ guard."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_session_start_hook", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── F1: branch subprocess capture ────────────────────────────────────────────


def test_branch_captured_from_git_and_appended_to_url(tmp_path, capsys):
    """Hook adds ?branch=<branch> when git succeeds."""
    hook_mod = _load_hook()

    fake_stdin = json.dumps({"cwd": str(tmp_path)})
    captured_url = {}

    def _fake_run(cmd, **kw):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "feat/my-feature\n"
        return result

    def _fake_urlopen(req, timeout=None):
        captured_url["url"] = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
        resp = MagicMock()
        resp.read.return_value = json.dumps({"text": ""}).encode()
        return resp

    with patch.object(hook_mod.subprocess, "run", side_effect=_fake_run):
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            import io

            old_stdin = hook_mod.sys.stdin
            hook_mod.sys.stdin = io.StringIO(fake_stdin)
            try:
                hook_mod.main()
            finally:
                hook_mod.sys.stdin = old_stdin

    url = captured_url.get("url", "")
    assert "branch=" in url, f"Expected branch= in URL, got: {url}"
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert params.get("branch", [None])[0] == "feat/my-feature"


def test_no_branch_param_when_git_fails(tmp_path, capsys):
    """Hook omits ?branch= when git returns non-zero exit code."""
    hook_mod = _load_hook()

    fake_stdin = json.dumps({"cwd": str(tmp_path)})
    captured_url = {}

    def _fake_run_fail(cmd, **kw):
        result = MagicMock()
        result.returncode = 128
        result.stdout = ""
        return result

    def _fake_urlopen(req, timeout=None):
        captured_url["url"] = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
        resp = MagicMock()
        resp.read.return_value = json.dumps({"text": ""}).encode()
        return resp

    with patch.object(hook_mod.subprocess, "run", side_effect=_fake_run_fail):
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            import io

            old_stdin = hook_mod.sys.stdin
            hook_mod.sys.stdin = io.StringIO(fake_stdin)
            try:
                hook_mod.main()
            finally:
                hook_mod.sys.stdin = old_stdin

    url = captured_url.get("url", "")
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert "branch" not in params, f"Expected no branch= in URL, got: {url}"


def test_no_branch_param_when_git_raises(tmp_path, capsys):
    """Hook omits ?branch= when subprocess.run raises (e.g. git not found)."""
    hook_mod = _load_hook()

    fake_stdin = json.dumps({"cwd": str(tmp_path)})
    captured_url = {}

    def _fake_run_exc(cmd, **kw):
        raise FileNotFoundError("git not found")

    def _fake_urlopen(req, timeout=None):
        captured_url["url"] = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
        resp = MagicMock()
        resp.read.return_value = json.dumps({"text": ""}).encode()
        return resp

    with patch.object(hook_mod.subprocess, "run", side_effect=_fake_run_exc):
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            import io

            old_stdin = hook_mod.sys.stdin
            hook_mod.sys.stdin = io.StringIO(fake_stdin)
            try:
                hook_mod.main()
            finally:
                hook_mod.sys.stdin = old_stdin

    url = captured_url.get("url", "")
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert "branch" not in params, f"Expected no branch= in URL, got: {url}"


# ── G5 hardening: HTTPError from a non-200 must be closed (py3.14 leak guard) ──


def test_http_error_from_daemon_is_closed(tmp_path):
    """A non-200 daemon response (urllib HTTPError) is closed, not leaked, and swallowed.

    HTTPError subclasses tempfile._TemporaryFileWrapper (via addbase) on py3.14; an
    unclosed instance fires a spurious ResourceWarning at GC that pytest-xdist
    mis-attributes to an unrelated test. The hook must close it deterministically
    and still degrade silently (daemon issue → skip, never crash the session).
    """
    import urllib.error

    hook_mod = _load_hook()
    fake_stdin = json.dumps({"cwd": str(tmp_path)})

    def _fake_run_fail(cmd, **kw):
        result = MagicMock()
        result.returncode = 128
        result.stdout = ""
        return result

    err = urllib.error.HTTPError("url", 401, "Unauthorized", hdrs=None, fp=None)
    closed = {"n": 0}
    _orig_close = err.close

    def _tracking_close():
        closed["n"] += 1
        _orig_close()

    err.close = _tracking_close  # type: ignore[method-assign]

    import io

    with patch.object(hook_mod.subprocess, "run", side_effect=_fake_run_fail):
        with patch("urllib.request.urlopen", side_effect=err):
            old_stdin = hook_mod.sys.stdin
            hook_mod.sys.stdin = io.StringIO(fake_stdin)
            try:
                hook_mod.main()  # must not raise
            finally:
                hook_mod.sys.stdin = old_stdin

    assert closed["n"] >= 1, "the hook must close the caught HTTPError"
