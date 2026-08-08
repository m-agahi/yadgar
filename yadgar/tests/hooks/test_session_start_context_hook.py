"""Tests for the session-start-context hook.

ADR-0215/0217: this file used to cover the ``?branch=`` query param the hook
appended (v5.1.9 F1), and later a trusted host-side git fact. Both are removed —
the hook now sends only ``directory``. What remains here is the G5 py3.14
HTTPError-leak guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

# Path to the hook script under test
_HOOK = Path(__file__).parent.parent.parent / "core" / "hooks" / "session-start-context.py"


def _load_hook():
    """Import the hook module from its file path, bypassing __main__ guard."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_session_start_hook", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    err = urllib.error.HTTPError("url", 401, "Unauthorized", hdrs=None, fp=None)
    closed = {"n": 0}
    _orig_close = err.close

    def _tracking_close():
        closed["n"] += 1
        _orig_close()

    err.close = _tracking_close  # type: ignore[method-assign]

    import io

    with patch("urllib.request.urlopen", side_effect=err):
        old_stdin = hook_mod.sys.stdin
        hook_mod.sys.stdin = io.StringIO(fake_stdin)
        try:
            hook_mod.main()  # must not raise
        finally:
            hook_mod.sys.stdin = old_stdin

    assert closed["n"] >= 1, "the hook must close the caught HTTPError"
