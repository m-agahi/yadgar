"""Car 7 — `yadgar install-hooks` CLI hard-removed + migration message.

The legacy `yadgar install-hooks` CLI command was hard-removed in
v5.166.0 (Car 7 of the opencode port train). The `cmd_install_hooks`
function still exists (so the old `register(subparsers)` call site in
yadgar/core/cli/__init__.py still imports cleanly), but invoking it
prints a migration message to stderr and exits 1.

These tests pin the migration contract:
  - Every legacy invocation pattern (--scope global/project,
    --project-directory, --dry-run) exits 1 with a non-empty migration
    message that includes the new canonical command.
  - No settings.json is written (the migration message is the only
    output).
  - argparse accepts the legacy flags (so old scripts don't get a
    confusing argparse error BEFORE the migration message fires).
"""

from __future__ import annotations

import os
import subprocess
import sys


def _run_cli(*args: str, home: str | None = None) -> subprocess.CompletedProcess:
    """Spawn `python -m yadgar install-hooks ...` with the given args."""
    env = os.environ.copy()
    if home is not None:
        env["HOME"] = home
    return subprocess.run(
        [sys.executable, "-m", "yadgar", "install-hooks", *args],
        capture_output=True,
        text=True,
        env=env,
    )


_EXPECTED_MIGRATION_HINTS = (
    "`yadgar install --client claude-code",  # new canonical command
    "removed",  # explains the exit code is intentional
    "v5.166.0",  # the release that removed it
)


def test_install_hooks_cli_exits_1_with_migration_message(tmp_path):
    """Default invocation (no flags) → exit 1 + migration message."""
    result = _run_cli(home=str(tmp_path))
    assert result.returncode == 1, (
        f"install-hooks CLI must exit 1; got rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "install-hooks" in result.stderr, "migration message must mention the legacy command"
    for hint in _EXPECTED_MIGRATION_HINTS:
        assert hint in result.stderr, (
            f"migration message must include {hint!r}; stderr: {result.stderr}"
        )


def test_install_hooks_cli_migration_mentions_every_scope(tmp_path):
    """Migration message must show migration for every legacy flag combination.

    Scopes: --scope global, --scope project, plus the --dry-run + project-directory combos.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    for args in (
        ("--scope", "global"),
        ("--scope", "project", "--project-directory", str(proj)),
        ("--scope", "global", "--dry-run"),
        ("--scope", "project", "--project-directory", str(proj), "--dry-run"),
    ):
        result = _run_cli(*args, home=str(tmp_path))
        assert result.returncode == 1, (
            f"install-hooks CLI must exit 1 for {args}; got rc={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        # Migration message must mention the new canonical command in every case.
        assert "`yadgar install --client claude-code" in result.stderr, (
            f"migration message missing the new command for {args}; stderr: {result.stderr}"
        )
        # Migration message must NOT write to the settings file in any case.
        # (No project_dir for global case — but project_dir case below is the load-bearing one.)
        if "--project-directory" in args:
            settings = tmp_path / ".claude" / "settings.json"
            assert not settings.exists(), (
                f"install-hooks must NOT write {settings} for {args}; file was created"
            )


def test_install_hooks_cli_does_not_write_settings_json(tmp_path):
    """Migration message only — no settings.json side effect on the host."""
    proj = tmp_path / "proj"
    proj.mkdir()
    result = _run_cli("--scope", "project", "--project-directory", str(proj), home=str(tmp_path))
    # Must not have written anything to the host's settings.json.
    assert not (tmp_path / ".claude" / "settings.json").exists(), (
        f"settings.json must NOT be written; got {result.stdout}"
    )
    # Migration message on stderr (NOT stdout; this is an error path).
    assert "install-hooks" in result.stderr


def test_install_hooks_cli_skips_container_env_check():
    """The legacy container-env check (YADGAR_IN_CONTAINER) is gone.

    The new MCP `install_hooks` tool keeps the container-refusal contract
    (test_install_hooks_host_vs_container.py covers that). The CLI stub
    must always exit 1 regardless of env, so a user in a container sees
    the migration message and isn't confused by a different error.
    """
    env = {**os.environ, "YADGAR_IN_CONTAINER": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "yadgar", "install-hooks", "--scope", "global"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1, (
        f"install-hooks CLI must always exit 1, even in container env; "
        f"got rc={result.returncode}\nstderr: {result.stderr}"
    )
    assert "install-hooks" in result.stderr, "migration message must mention the legacy command"
    assert "running_in_container" not in result.stderr, (
        "container-refusal is the MCP tool's job now, not the CLI stub's"
    )
