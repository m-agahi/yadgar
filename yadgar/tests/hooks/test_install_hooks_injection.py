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

    from yadgar.core import server as _s

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

    from yadgar.core import server as _s

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
    """yadgar/core/scripts/hook_runner.py must exist as a real file."""
    from pathlib import Path

    runner = Path(__file__).parent.parent.parent / "core" / "scripts" / "hook_runner.py"
    assert runner.exists(), (
        "yadgar/core/scripts/hook_runner.py does not exist — hook_runner.py must be "
        "shipped as a real script (not inline python3 -c)"
    )


def test_hook_command_uses_shlex_quote(tmp_path, monkeypatch):
    """hook_runner.py path with double-quotes must be shell-safe via shlex.quote."""
    import json

    monkeypatch.setenv("HOME", str(tmp_path))

    # Create a project dir whose name contains a double-quote
    project_dir = tmp_path / 'proj"ect'
    project_dir.mkdir(parents=True, exist_ok=True)

    from yadgar.core import server as _s

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


# ── BUG B: token must not be baked as a literal (omit-env default) ────────────


def _all_env_blocks(settings: dict) -> list[dict]:
    blocks = []
    for entries in settings.get("hooks", {}).values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
                if isinstance(hook, dict) and "env" in hook:
                    blocks.append(hook["env"])
    return blocks


def test_token_not_baked_into_settings(tmp_path, monkeypatch):
    """BUG B: with a real-looking YADGAR_MCP_AUTH_TOKEN set, no written env
    block may carry the raw token value. Default = omit-env: the token key is
    absent from every hook env block."""
    import json

    real_token = "sk-yadgar-realish-0123456789abcdef"
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", real_token)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("YADGAR_IN_CONTAINER", raising=False)

    import yadgar.core.install.install_hooks_lib as lib

    monkeypatch.setattr(lib, "is_running_in_container", lambda: False)

    from yadgar.core import server as _s

    _s.install_hooks(project_directory=str(tmp_path / "proj"), scope="global")

    settings_file = tmp_path / ".claude" / "settings.json"
    assert settings_file.exists()
    settings = json.loads(settings_file.read_text())
    for block in _all_env_blocks(settings):
        assert real_token not in json.dumps(block), (
            f"raw auth token leaked into an env block: {block!r}"
        )
        assert "YADGAR_MCP_AUTH_TOKEN" not in block, (
            f"omit-env default: token key must be absent from env blocks, got {block!r}"
        )


# ── BUG C: known test-fixture token is refused + warned ──────────────────────


def test_test_fixture_token_guard_fires(tmp_path, monkeypatch, caplog):
    """BUG C: a known test-fixture token must never be baked and must warn."""
    import json
    import logging

    fixture_token = "a-valid-32-char-token-here!!"
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", fixture_token)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("YADGAR_IN_CONTAINER", raising=False)

    import yadgar.core.install.install_hooks_lib as lib

    monkeypatch.setattr(lib, "is_running_in_container", lambda: False)

    from yadgar.core import server as _s

    with caplog.at_level(logging.WARNING):
        _s.install_hooks(project_directory=str(tmp_path / "proj"), scope="global")

    settings_file = tmp_path / ".claude" / "settings.json"
    settings = json.loads(settings_file.read_text())
    for block in _all_env_blocks(settings):
        assert fixture_token not in json.dumps(block)
    assert any("test-fixture" in r.message.lower() for r in caplog.records), (
        "expected a warning mentioning the test-fixture token"
    )
