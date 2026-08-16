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


# ── Car C: mechanical harness task-list seeding ──────────────────────────────
#
# SAFETY: these tests pin CLAUDE_CONFIG_DIR at a tmp_path. The real
# ~/.claude/tasks is the LIVE task store of running sessions.

_SEED_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _run_hook_with_payload(tmp_path, monkeypatch, capsys, response: dict):
    """Drive the hook against a canned daemon response; return stdout."""
    import io

    cfg = tmp_path / "claude-home"
    cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("YADGAR_HARNESS_SEED_DISABLED", raising=False)

    hook_mod = _load_hook()

    class _Resp:
        def read(self):
            return json.dumps(response).encode()

        def close(self):
            pass

    stdin = json.dumps({"cwd": str(tmp_path), "session_id": _SEED_SID})
    with patch("urllib.request.urlopen", return_value=_Resp()):
        old = hook_mod.sys.stdin
        hook_mod.sys.stdin = io.StringIO(stdin)
        try:
            hook_mod.main()
        finally:
            hook_mod.sys.stdin = old
    return capsys.readouterr().out, cfg / "tasks" / _SEED_SID


def test_hook_seeds_the_task_store_and_suppresses_the_nudge(tmp_path, monkeypatch, capsys):
    out, task_dir = _run_hook_with_payload(
        tmp_path,
        monkeypatch,
        capsys,
        {
            "text": "CATALOG",
            "task_nudge": "[yadgar] restore your task list — TaskCreate ...",
            "tasks": [
                {"id": 41, "title": "Fix the bug", "status": "pending"},
                {"id": 92, "title": "Trim the nudge", "status": "in_progress"},
            ],
        },
    )

    assert sorted(p.name for p in task_dir.glob("*.json")) == ["41.json", "92.json"]
    assert "restore your task list" not in out, (
        "a session whose list was seeded must not also be ordered to re-create it"
    )
    assert "CATALOG" in out


def test_hook_prints_the_nudge_when_the_seeder_trips(tmp_path, monkeypatch, capsys):
    """A dir in an unrecognised format is left alone — the nudge takes over."""
    cfg = tmp_path / "claude-home"
    d = cfg / "tasks" / _SEED_SID
    d.mkdir(parents=True)
    (d / ".lock").write_text("this is not the 0-byte sentinel")

    out, task_dir = _run_hook_with_payload(
        tmp_path,
        monkeypatch,
        capsys,
        {
            "text": "CATALOG",
            "task_nudge": "[yadgar] restore your task list — TaskCreate ...",
            "tasks": [{"id": 41, "title": "Fix the bug", "status": "pending"}],
        },
    )

    assert not list(task_dir.glob("*.json")), "a tripped guard must write nothing"
    assert "restore your task list" in out
    assert "CATALOG" in out


def test_hook_prints_the_nudge_unchanged_when_the_daemon_predates_car_c(
    tmp_path, monkeypatch, capsys
):
    """Old daemon: the nudge is already inside `text`, no seeding happens."""
    out, task_dir = _run_hook_with_payload(
        tmp_path, monkeypatch, capsys, {"text": "[yadgar] restore your task list\nCATALOG"}
    )

    assert not task_dir.exists()
    assert "restore your task list" in out


def test_hook_sends_the_seed_capability_flag(tmp_path, monkeypatch, capsys):
    import io
    import urllib.error

    cfg = tmp_path / "claude-home"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    hook_mod = _load_hook()
    seen = {}

    def _capture(req, timeout=None):
        seen["url"] = req.full_url
        raise urllib.error.URLError("stop here")

    with patch("urllib.request.urlopen", side_effect=_capture):
        old = hook_mod.sys.stdin
        hook_mod.sys.stdin = io.StringIO(
            json.dumps({"cwd": str(tmp_path), "session_id": _SEED_SID})
        )
        try:
            hook_mod.main()
        finally:
            hook_mod.sys.stdin = old

    assert "seed=1" in seen["url"], f"hook must declare it can seed; got {seen.get('url')!r}"


def test_hook_omits_the_seed_flag_without_a_session_id(tmp_path, monkeypatch, capsys):
    """No session_id → no store to address → do not claim the capability."""
    import io
    import urllib.error

    hook_mod = _load_hook()
    seen = {}

    def _capture(req, timeout=None):
        seen["url"] = req.full_url
        raise urllib.error.URLError("stop here")

    with patch("urllib.request.urlopen", side_effect=_capture):
        old = hook_mod.sys.stdin
        hook_mod.sys.stdin = io.StringIO(json.dumps({"cwd": str(tmp_path)}))
        try:
            hook_mod.main()
        finally:
            hook_mod.sys.stdin = old

    assert "seed=1" not in seen["url"]
