"""Shared install_hooks implementation.

Both the MCP tool (yadgar/server/tools/misc.py) and the CLI subcommand
(yadgar/cli/install_hooks.py) call install_hooks_impl() here.

Container detection:
  is_running_in_container() returns True only when YADGAR_IN_CONTAINER=1
  is set in the environment.  Explicit opt-in avoids false positives in CI
  runners (e.g. Forgejo Actions) that have /.dockerenv present but are NOT
  the yadgar core service container.

  To enable container-mode refusal, callers must set:
    YADGAR_IN_CONTAINER=1
  in the environment before launching the process.  The nix module and
  docker-compose config set this on the yadgar core service ExecStart.
  CI pipelines must NOT set it.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Container detection ────────────────────────────────────────────────────


def is_running_in_container() -> bool:
    """True iff YADGAR_IN_CONTAINER=1 is set.

    Explicit opt-in avoids false positives in CI/runner environments that
    happen to have /.dockerenv present (e.g. Forgejo Actions container
    images).  Only the yadgar core service sets this env var at launch.
    """
    return os.environ.get("YADGAR_IN_CONTAINER", "") == "1"


# ── Shared install logic ───────────────────────────────────────────────────


def install_hooks_impl(
    home_dir: Path,
    scope: str,
    project_directory: str | None,
    dry_run: bool = False,
) -> dict:
    """Write Claude Code hook config to the appropriate settings.json.

    Parameters
    ----------
    home_dir:
        The user's home directory.  Callers pass ``Path.home()`` for real
        usage or a temp dir in tests.
    scope:
        ``"project"`` — write to <project_directory>/.claude/settings.json
        ``"global"``  — write to ~/.claude/settings.json
    project_directory:
        Project root string.  ``None`` / empty → ``Path.cwd()``.
    dry_run:
        When True, compute but do NOT write changes; return the would-be
        settings dict under ``"preview"`` and print a compact preview to
        stdout.
    """
    if scope not in ("project", "global"):
        return {
            "status": "error",
            "reason": f"Invalid scope '{scope}': must be 'project' or 'global'",
        }

    project_dir = Path(project_directory) if project_directory else Path.cwd()

    # Global paths (Stop hook always here; all hooks go here when scope=global)
    global_claude_dir = home_dir / ".claude"
    global_hooks_dir = global_claude_dir / "hooks"

    # Determine where hook scripts and settings are written based on scope
    if scope == "global":
        hooks_dir = global_hooks_dir
        settings_target_dir = global_claude_dir
    else:
        claude_dir = project_dir / ".claude"
        hooks_dir = claude_dir / "hooks"
        settings_target_dir = claude_dir

    if not dry_run:
        global_hooks_dir.mkdir(parents=True, exist_ok=True)
        if scope != "global":
            hooks_dir.mkdir(parents=True, exist_ok=True)

    # Copy hook scripts from package
    package_hooks = Path(__file__).parent / "hooks"

    hook_files = {
        "pre-compact-drain.sh": 0o755,
        "post-compact-rehydrate.sh": 0o755,
        "post-tool-capture.py": 0o755,
        "session-start-context.py": 0o755,
        "prompt-recall.py": 0o755,
        "subagent-stop.py": 0o755,
        "instructions-loaded.py": 0o755,
        "subagent-start.py": 0o755,
    }

    if not dry_run:
        for filename, mode in hook_files.items():
            src = package_hooks / filename
            dst = hooks_dir / filename
            if src.exists():
                shutil.copy2(src, dst)
                dst.chmod(mode)

    # Stop hook — always installed globally
    stop_hook_src = package_hooks / "stop-memory-checkpoint.py"
    stop_hook_dst = global_hooks_dir / "yadgar-stop-memory-checkpoint.py"
    if not dry_run and stop_hook_src.exists():
        shutil.copy2(stop_hook_src, stop_hook_dst)
        stop_hook_dst.chmod(0o755)

    # hook_runner.py
    hook_runner_src = Path(__file__).parent / "scripts" / "hook_runner.py"
    hook_runner_dst = hooks_dir / "hook_runner.py"
    if not dry_run and hook_runner_src.exists():
        shutil.copy2(hook_runner_src, hook_runner_dst)
        hook_runner_dst.chmod(0o755)

    _runner = str(hook_runner_dst)

    # Auth env block
    _auth_token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    _env_block: dict = {}
    if _auth_token:
        _env_block = {"YADGAR_MCP_AUTH_TOKEN": _auth_token}

    def _hook_entry(hook_type: str, matcher: str = "") -> dict:
        entry: dict = {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": f"python3 {shlex.quote(_runner)} {hook_type}",
                }
            ],
        }
        if _env_block:
            entry["hooks"][0]["env"] = _env_block
        return entry

    # Build hooks config
    settings_path = settings_target_dir / "settings.json"
    settings_data: dict = {}
    if settings_path.exists():
        try:
            settings_data = json.loads(settings_path.read_text())
        except Exception:
            settings_data = {}

    hooks_config = settings_data.get("hooks", {})
    hooks_config["PreCompact"] = [_hook_entry("pre-compact-drain")]
    hooks_config["SessionStart"] = [
        _hook_entry("session-start-context"),
        _hook_entry("post-compact-rehydrate", matcher="compact"),
    ]
    hooks_config["PostToolUse"] = [_hook_entry("post-tool-capture")]
    hooks_config["UserPromptSubmit"] = [_hook_entry("prompt-recall")]
    hooks_config["PreToolUse"] = [_hook_entry("db-lockdown-check", matcher="Bash")]

    # SubagentStop — append-if-absent semantics.
    # We only add the yadgar entry if no entry with our command substring exists.
    # This preserves user-defined SubagentStop hooks and avoids duplicates on re-runs.
    _subagent_stop_src = package_hooks / "subagent-stop.py"
    _subagent_stop_dst = hooks_dir / "yadgar-subagent-stop.py"
    if not dry_run and _subagent_stop_src.exists():
        shutil.copy2(_subagent_stop_src, _subagent_stop_dst)
        _subagent_stop_dst.chmod(0o755)

    _subagent_stop_cmd = f'python3 "{_subagent_stop_dst}"'
    _existing_subagent_stop = hooks_config.get("SubagentStop", [])
    _already_registered = any(
        entry.get("hooks", [{}])[0].get("command", "") == _subagent_stop_cmd
        for entry in _existing_subagent_stop
        if isinstance(entry, dict) and entry.get("hooks")
    )
    if not _already_registered:
        _subagent_stop_hook_entry: dict = {
            "matcher": "",
            "hooks": [{"type": "command", "command": _subagent_stop_cmd}],
        }
        if _env_block:
            _subagent_stop_hook_entry["hooks"][0]["env"] = _env_block
        _existing_subagent_stop.append(_subagent_stop_hook_entry)
    hooks_config["SubagentStop"] = _existing_subagent_stop

    # InstructionsLoaded — append-if-absent semantics.
    # Fires recall on CLAUDE.md load (session_start / compact only — throttled in script).
    _il_src = package_hooks / "instructions-loaded.py"
    _il_dst = hooks_dir / "yadgar-instructions-loaded.py"
    if not dry_run and _il_src.exists():
        shutil.copy2(_il_src, _il_dst)
        _il_dst.chmod(0o755)

    _il_cmd = f'python3 "{_il_dst}"'
    _existing_il = hooks_config.get("InstructionsLoaded", [])
    _il_already_registered = any(
        entry.get("hooks", [{}])[0].get("command", "") == _il_cmd
        for entry in _existing_il
        if isinstance(entry, dict) and entry.get("hooks")
    )
    if not _il_already_registered:
        _il_hook_entry: dict = {
            "matcher": "",
            "hooks": [{"type": "command", "command": _il_cmd}],
        }
        if _env_block:
            _il_hook_entry["hooks"][0]["env"] = _env_block
        _existing_il.append(_il_hook_entry)
    hooks_config["InstructionsLoaded"] = _existing_il

    # SubagentStart — append-if-absent semantics.
    # No matcher needed — fires on all subagent dispatches.
    _ss_src = package_hooks / "subagent-start.py"
    _ss_dst = hooks_dir / "yadgar-subagent-start.py"
    if not dry_run and _ss_src.exists():
        shutil.copy2(_ss_src, _ss_dst)
        _ss_dst.chmod(0o755)

    _ss_cmd = f'python3 "{_ss_dst}"'
    _existing_ss = hooks_config.get("SubagentStart", [])
    _ss_already_registered = any(
        entry.get("hooks", [{}])[0].get("command", "") == _ss_cmd
        for entry in _existing_ss
        if isinstance(entry, dict) and entry.get("hooks")
    )
    if not _ss_already_registered:
        _ss_hook_entry: dict = {
            "matcher": "",
            "hooks": [{"type": "command", "command": _ss_cmd}],
        }
        if _env_block:
            _ss_hook_entry["hooks"][0]["env"] = _env_block
        _existing_ss.append(_ss_hook_entry)
    hooks_config["SubagentStart"] = _existing_ss

    settings_data["hooks"] = hooks_config

    _stop_entry = [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python3 "{stop_hook_dst}"',
                }
            ],
        }
    ]

    # Stop hook placement depends on scope:
    # - scope=global: same file as the rest of the hooks — merge directly.
    # - scope=project: separate global file (so Stop fires in every session).
    if scope == "global":
        # Everything goes into one file — add Stop alongside the rest.
        hooks_config["Stop"] = _stop_entry
        settings_data["hooks"] = hooks_config
    # else: settings_data already has hooks_config; global Stop handled below

    if dry_run:
        preview = json.dumps(settings_data, indent=2)
        print(f"[dry-run] Would write to: {settings_path}")
        print(preview)
        return {
            "status": "dry_run",
            "scope": scope,
            "project_directory": str(project_dir),
            "settings_file": str(settings_path),
            "preview": settings_data,
        }

    # Atomic write: primary settings file
    _atomic_write(settings_target_dir, settings_path, settings_data)

    # For scope=project, also register Stop in the global settings file
    # (always global so it fires in every session regardless of project)
    if scope == "project":
        global_settings_path = global_claude_dir / "settings.json"
        global_settings: dict = {}
        if global_settings_path.exists():
            try:
                global_settings = json.loads(global_settings_path.read_text())
            except Exception:
                global_settings = {}
        global_hooks = global_settings.get("hooks", {})
        global_hooks["Stop"] = _stop_entry
        global_settings["hooks"] = global_hooks
        _atomic_write(global_claude_dir, global_settings_path, global_settings)

    global_settings_file = (
        str(settings_path) if scope == "global" else str(global_claude_dir / "settings.json")
    )
    return {
        "status": "installed",
        "scope": scope,
        "project_directory": str(project_dir),
        "hooks_directory": str(hooks_dir),
        "hooks_installed": [
            "PreCompact (drain)",
            "SessionStart (context)",
            "SessionStart (compact restore)",
            "PostToolUse (auto-capture)",
            "UserPromptSubmit (auto-recall)",
            "PreToolUse (DB lockdown)",
            "Stop (memory checkpoint — global)",
            "SubagentStop (findings capture — append-if-absent)",
            "InstructionsLoaded (recall on CLAUDE.md load — append-if-absent)",
            "SubagentStart (context injection at dispatch — append-if-absent)",
        ],
        "settings_file": str(settings_path),
        "global_settings_file": global_settings_file,
    }


def _atomic_write(directory: Path, target: Path, data: dict) -> None:
    """Write *data* as JSON to *target* atomically via a temp file."""
    directory.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path_str = tempfile.mkstemp(dir=directory, prefix=".settings_tmp_", suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp_path_str, target)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except Exception:
            pass
        raise
