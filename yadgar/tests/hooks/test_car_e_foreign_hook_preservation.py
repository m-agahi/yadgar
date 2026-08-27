"""Task 400 — foreign-hook preservation must be UNIFORM across every event.

ADR-0161 made the five core hook events foreign-preserving by routing them
through ``_replace_managed_entries`` (strip only yadgar's own entries, keyed on
the managed script basename; append the fresh ones). Two events were left
behind: on ``scope=global`` ``install_hooks_impl`` hard-assigned
``hooks_config["Stop"]`` / ``hooks_config["SessionEnd"]``, and on
``scope=project`` ``_write_global_stop_hooks`` did the same to the GLOBAL
settings file. A foreign entry under SessionStart (e.g. nix's caveman hook)
survived an install; a foreign entry under Stop or SessionEnd was dropped.

The asymmetry was invisible because no test seeded a foreign entry under every
event — the existing coverage checks Stop/SessionEnd are *present*, never that
someone else's entry beside them *survived*. This module is that test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yadgar.core.install.install_hooks_lib import install_hooks_impl

#: Every hook event install_hooks_impl writes under ``scope=global``.
_ALL_EVENTS = [
    "PreCompact",
    "SessionStart",
    "PostToolUse",
    "UserPromptSubmit",
    "PreToolUse",
    "Stop",
    "SessionEnd",
    "InstructionsLoaded",
    "SubagentStart",
    "FileChanged",
]

#: The two events that used to be hard-assigned (the regression this pins).
_FORMERLY_HARD_ASSIGNED = ["Stop", "SessionEnd"]


def _foreign_entry(event: str) -> dict:
    """A hook entry belonging to some OTHER tool (no yadgar identity)."""
    return {
        "matcher": "",
        "hooks": [{"type": "command", "command": f"/usr/bin/env foreign-{event.lower()}.sh"}],
    }


def _foreign_command(event: str) -> str:
    return f"/usr/bin/env foreign-{event.lower()}.sh"


def _commands(settings: dict, event: str) -> list[str]:
    out: list[str] = []
    for entry in settings.get("hooks", {}).get(event, []):
        for hook in entry.get("hooks", []):
            out.append(hook.get("command", ""))
    return out


def _seed(settings_path: Path, events: list[str]) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"hooks": {event: [_foreign_entry(event)] for event in events}}, indent=2)
    )


# ── scope=global: every event, foreign entry survives ──────────────────────


def test_foreign_entry_survives_under_every_hook_event(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    _seed(settings_path, _ALL_EVENTS)

    result = install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=None)
    assert result["status"] == "installed"

    settings = json.loads(settings_path.read_text())
    dropped = [e for e in _ALL_EVENTS if _foreign_command(e) not in _commands(settings, e)]
    assert not dropped, (
        f"install dropped a foreign hook entry under {dropped} — foreign-preservation "
        "(ADR-0161) must be uniform across every event, not just the five core ones"
    )


@pytest.mark.parametrize("event", _FORMERLY_HARD_ASSIGNED)
def test_formerly_hard_assigned_events_preserve_foreign_and_install_managed(event, tmp_path):
    """Stop / SessionEnd: the foreign entry survives AND yadgar's own lands."""
    settings_path = tmp_path / ".claude" / "settings.json"
    _seed(settings_path, [event])

    install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=None)

    commands = _commands(json.loads(settings_path.read_text()), event)
    assert _foreign_command(event) in commands
    managed = [c for c in commands if "/.claude/hooks/yadgar-" in c]
    assert len(managed) == 1, f"expected exactly one yadgar {event} entry, got {managed}"


def test_reinstall_does_not_duplicate_foreign_or_managed_entries(tmp_path):
    """Idempotency: a second install must not accumulate either kind."""
    settings_path = tmp_path / ".claude" / "settings.json"
    _seed(settings_path, _ALL_EVENTS)

    install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=None)
    install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=None)

    settings = json.loads(settings_path.read_text())
    for event in _ALL_EVENTS:
        commands = _commands(settings, event)
        assert commands.count(_foreign_command(event)) == 1, (
            f"{event}: foreign entry duplicated across reinstalls — {commands}"
        )
        for command in set(commands):
            assert commands.count(command) == 1, f"{event}: duplicate managed entry {command!r}"


def test_stale_yadgar_stop_entry_is_replaced_not_accumulated(tmp_path):
    """A yadgar Stop entry from a prior install (stale interpreter path) is
    collapsed into the fresh one — foreign-preservation must not turn our own
    stale entries into permanent squatters (that would double-fire the hook)."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    stale = (
        "/some/old/venv/bin/python3 /home/somebody/.claude/hooks/yadgar-stop-memory-checkpoint.py"
    )
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"matcher": "", "hooks": [{"type": "command", "command": stale}]},
                        _foreign_entry("Stop"),
                    ]
                }
            }
        )
    )

    install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=None)

    commands = _commands(json.loads(settings_path.read_text()), "Stop")
    assert stale not in commands, "stale yadgar Stop entry must be replaced, not preserved"
    assert _foreign_command("Stop") in commands
    assert len([c for c in commands if "yadgar-stop-memory-checkpoint.py" in c]) == 1


# ── scope=project: the global Stop/SessionEnd writer ───────────────────────


def test_project_scope_preserves_foreign_global_stop_and_session_end(tmp_path):
    """``scope=project`` still writes Stop + SessionEnd into the GLOBAL
    settings.json via ``_write_global_stop_hooks`` — the second hard-assign
    site, which the task text does not name but which the same bug lives in."""
    global_settings = tmp_path / ".claude" / "settings.json"
    _seed(global_settings, ["Stop", "SessionEnd"])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    install_hooks_impl(home_dir=tmp_path, scope="project", project_directory=str(project_dir))

    settings = json.loads(global_settings.read_text())
    for event in _FORMERLY_HARD_ASSIGNED:
        commands = _commands(settings, event)
        assert _foreign_command(event) in commands, (
            f"scope=project dropped the foreign global {event} entry"
        )
        assert len([c for c in commands if "yadgar-" in c]) == 1


def test_project_scope_reinstall_is_idempotent_on_global_stop_hooks(tmp_path):
    global_settings = tmp_path / ".claude" / "settings.json"
    _seed(global_settings, ["Stop", "SessionEnd"])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    install_hooks_impl(home_dir=tmp_path, scope="project", project_directory=str(project_dir))
    install_hooks_impl(home_dir=tmp_path, scope="project", project_directory=str(project_dir))

    settings = json.loads(global_settings.read_text())
    for event in _FORMERLY_HARD_ASSIGNED:
        commands = _commands(settings, event)
        assert commands.count(_foreign_command(event)) == 1
        assert len([c for c in commands if "yadgar-" in c]) == 1


def test_non_hook_settings_keys_survive(tmp_path):
    """Regression guard for the surrounding file, not just the hooks block."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"theme": "dark", "hooks": {"Stop": [_foreign_entry("Stop")]}})
    )

    install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=None)

    settings = json.loads(settings_path.read_text())
    assert settings["theme"] == "dark"
    assert _foreign_command("Stop") in _commands(settings, "Stop")
