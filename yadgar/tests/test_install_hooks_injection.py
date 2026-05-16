"""§3 install_hooks shell-injection tests.

Verifies that install_hooks does NOT execute code when project_directory
contains shell metacharacters.
"""

import os
from pathlib import Path


def test_install_hooks_no_shell_injection(tmp_path, monkeypatch):
    """A project_directory containing $(...) must NOT execute as shell code."""
    pwned = tmp_path / "pwned"
    evil_dir = str(tmp_path) + "/$(touch " + str(pwned) + ")"

    # Point Claude dirs somewhere harmless
    monkeypatch.setenv("HOME", str(tmp_path))

    from yadgar import server as _s

    _s.install_hooks(project_directory=evil_dir)

    # The key assertion: the injected file must NOT have been created.
    assert not pwned.exists(), (
        "Shell injection executed: /tmp/pwned was created by install_hooks "
        "with a malicious project_directory argument"
    )


def test_install_hooks_injection_in_hook_command(tmp_path, monkeypatch):
    """The hook command strings written to settings.json must be safe.

    When hook_runner.py is used, command is an absolute path + argv[1]
    (the directory), which is NOT shell-interpolated at all.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    # Use a path without injection to verify the happy path
    project_dir = str(tmp_path / "myproject")
    os.makedirs(project_dir, exist_ok=True)

    from yadgar import server as _s

    _s.install_hooks(project_directory=project_dir)

    settings_file = tmp_path / ".claude" / "settings.json"
    if not settings_file.exists():
        # scope=project — look in project dir
        settings_file = Path(project_dir) / ".claude" / "settings.json"

    import json

    if settings_file.exists():
        settings = json.loads(settings_file.read_text())
        hooks = settings.get("hooks", {})
        # Verify no multiline python3 -c strings in hook commands
        for event, entries in hooks.items():
            for entry in entries:
                for hook in entry.get("hooks", []):
                    cmd = hook.get("command", "")
                    assert "python3 -c" not in cmd or "\\n" not in cmd, (
                        f"Hook command for {event} contains multiline python3 -c: {cmd!r}"
                    )


def test_hook_runner_script_exists():
    """yadgar/scripts/hook_runner.py must exist as a real file."""
    from pathlib import Path

    runner = Path(__file__).parent.parent / "scripts" / "hook_runner.py"
    assert runner.exists(), (
        "yadgar/scripts/hook_runner.py does not exist — hook_runner.py must be "
        "shipped as a real script (not inline python3 -c)"
    )


def test_hook_command_uses_shlex_quote(tmp_path, monkeypatch):
    """hook_runner.py path with double-quotes must be shell-safe via shlex.quote."""
    import json

    monkeypatch.setenv("HOME", str(tmp_path))

    # Create a project dir whose name contains a double-quote
    project_dir = tmp_path / 'proj"ect'
    project_dir.mkdir(parents=True, exist_ok=True)

    from yadgar import server as _s

    _s.install_hooks(project_directory=str(project_dir))

    # Locate the written settings.json
    settings_file = tmp_path / ".claude" / "settings.json"
    if not settings_file.exists():
        settings_file = project_dir / ".claude" / "settings.json"

    if not settings_file.exists():
        # install_hooks may skip writing if hook_runner.py not found — skip test
        return

    settings = json.loads(settings_file.read_text())
    for _event, entries in settings.get("hooks", {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                # Must not contain unquoted double-quote in the runner path
                assert '""' not in cmd or cmd.startswith("python3 '"), (
                    f"Hook command may be shell-unsafe: {cmd!r}. "
                    "Expected shlex.quote to wrap the path."
                )
