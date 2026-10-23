"""The LIVE SessionStart hook seeds the harness task list (ledger 1797).

``yadgar install --client claude-code --hooks`` registers
``hook_runner.py session-start-context``, which dispatches in-process to
``yadgar.core.cli.hook.hook_session_start_context``. Car C wired the seeder into
``core/hooks/session-start-context.py`` instead, a file nothing dispatches to
(``_hook_scripts._MANAGED_NONPREFIXED``), so on a real install the daemon never
got ``seed=1`` and no SessionStart ever seeded. Measured 2026-10-23: zero
"Task list seeded from the ledger" lines across every local transcript.

SAFETY: ``CLAUDE_CONFIG_DIR`` is pinned at ``tmp_path`` — the real
``~/.claude/tasks`` is the live store of running sessions.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

import yadgar.core.cli.hook as hk

_SID = "11111111-2222-3333-4444-555555555555"
_TASKS = [{"id": 41, "title": "Fix it", "status": "pending"}]
_NUDGE = "[yadgar] NUDGE: recreate the open tasks via TaskCreate"


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "claude-home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("YADGAR_HARNESS_SEED_DISABLED", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_TASK_LIST_ID", raising=False)
    # No mint, no session-map write from a test.
    monkeypatch.setattr(hk, "_emit_project_id", lambda cwd: None)
    return home


def _run(monkeypatch, payload: dict, response: dict | None) -> dict:
    seen: dict = {}

    def _fake_get(path, params=None, timeout=2.0):
        seen["path"] = path
        seen["params"] = dict(params or {})
        return response

    monkeypatch.setattr(hk, "_http_get", _fake_get)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    hk.hook_session_start_context()
    return seen


def test_sends_seed_flag_when_the_payload_names_a_session(cfg, monkeypatch, tmp_path):
    seen = _run(monkeypatch, {"cwd": str(tmp_path), "session_id": _SID}, {"text": ""})

    assert seen["params"].get("seed") == "1"


def test_no_seed_flag_without_a_session_id(cfg, monkeypatch, tmp_path):
    """opencode sends no session_id: keep the pre-Car-C response shape."""
    seen = _run(monkeypatch, {"cwd": str(tmp_path)}, {"text": ""})

    assert "seed" not in seen["params"]


def test_seeds_and_prints_confirmation_not_nudge(cfg, monkeypatch, tmp_path, capsys):
    resp = {"text": "BRIEF", "task_nudge": _NUDGE, "tasks": _TASKS}
    _run(monkeypatch, {"cwd": str(tmp_path), "session_id": _SID}, resp)

    out = capsys.readouterr().out
    assert (cfg / "tasks" / _SID / "41.json").is_file()
    assert "Task list seeded from the ledger" in out
    assert _NUDGE not in out
    assert "BRIEF" in out


def test_named_task_list_dir_is_seeded(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_TASK_LIST_ID", "shared-list")
    resp = {"text": "", "task_nudge": _NUDGE, "tasks": _TASKS}
    _run(monkeypatch, {"cwd": str(tmp_path), "session_id": _SID}, resp)

    assert (cfg / "tasks" / "shared-list" / "41.json").is_file()
    assert not (cfg / "tasks" / _SID).exists()


def test_failed_seed_prints_the_nudge(cfg, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CLAUDE_CODE_TASK_LIST_ID", "../escape")
    resp = {"text": "BRIEF", "task_nudge": _NUDGE, "tasks": _TASKS}
    _run(monkeypatch, {"cwd": str(tmp_path), "session_id": _SID}, resp)

    out = capsys.readouterr().out
    assert _NUDGE in out
    assert "Task list seeded" not in out
    assert not list((cfg / "tasks").rglob("*.json")) if (cfg / "tasks").exists() else True
