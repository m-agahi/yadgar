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
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _resolve_python_shebang() -> str:
    """Return the shebang line to pin yadgar-bundled hooks at install time.

    Hooks that `import yadgar.paths` (stop-memory-checkpoint, session-end-capture,
    post-tool-capture, prompt-recall) need a Python that has yadgar on its path.
    `#!/usr/bin/env python3` resolves to whichever python3 is first on PATH,
    which on many systems (notably NixOS with a pipx-installed yadgar) is a
    system python that does NOT have yadgar importable.

    Strategy: at copy time, pin the shebang to `sys.executable` — the same
    interpreter running install_hooks. Since users invoke `yadgar install_hooks`
    from the venv that has yadgar, this is the interpreter that can resolve
    `import yadgar.paths`.

    Returns the literal shebang line including the leading `#!` and trailing
    newline.
    """
    return f"#!{sys.executable}\n"


# ── Container detection ────────────────────────────────────────────────────


def is_running_in_container() -> bool:
    """True iff YADGAR_IN_CONTAINER=1 is set.

    Explicit opt-in avoids false positives in CI/runner environments that
    happen to have /.dockerenv present (e.g. Forgejo Actions container
    images).  Only the yadgar core service sets this env var at launch.
    """
    return os.environ.get("YADGAR_IN_CONTAINER", "") == "1"


# ── Internal helpers ───────────────────────────────────────────────────────


def _copy_hook(src: Path, dst: Path, dry_run: bool) -> None:
    """Copy a hook script, rewrite its shebang to the installer's python,
    and mark it executable. No-op on dry_run.

    Shebang rewrite: any `#!/usr/bin/env python3` (or `#!/usr/bin/env python`)
    first line is replaced with `#!<sys.executable>` so yadgar-bundled hooks
    that `import yadgar.paths` find a python that has yadgar on its path.
    Other shebang forms are preserved.
    """
    if dry_run:
        return
    if not src.exists():
        return
    text = src.read_text()
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("#!") and "python" in lines[0]:
        first = lines[0].strip()
        if first in ("#!/usr/bin/env python3", "#!/usr/bin/env python"):
            lines[0] = _resolve_python_shebang()
            text = "".join(lines)
    dst.write_text(text)
    dst.chmod(0o755)


def _make_hook_entry(cmd: str, matcher: str, env_block: dict) -> dict:
    """Build a single hook entry dict."""
    entry: dict = {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": cmd}],
    }
    if env_block:
        entry["hooks"][0]["env"] = env_block
    return entry


def _append_if_absent(
    hooks_config: dict,
    event: str,
    cmd: str,
    env_block: dict,
    matcher: str = "",
) -> None:
    """Register a hook entry under *event* only if no entry with the same command exists."""
    existing = hooks_config.get(event, [])
    already = any(
        entry.get("hooks", [{}])[0].get("command", "") == cmd
        for entry in existing
        if isinstance(entry, dict) and entry.get("hooks")
    )
    if not already:
        existing.append(_make_hook_entry(cmd, matcher, env_block))
    hooks_config[event] = existing


def _install_global_scripts(
    package_hooks: Path,
    global_hooks_dir: Path,
    dry_run: bool,
) -> tuple[Path, Path, Path]:
    """Copy always-global hook scripts; return (stop_dst, session_end_dst, db_lockdown_dst)."""
    stop_dst = global_hooks_dir / "yadgar-stop-memory-checkpoint.py"
    _copy_hook(package_hooks / "stop-memory-checkpoint.py", stop_dst, dry_run)

    session_end_dst = global_hooks_dir / "yadgar-session-end-capture.py"
    _copy_hook(package_hooks / "session-end-capture.py", session_end_dst, dry_run)

    # v5.20.0: standalone DB lockdown — not routed through hook_runner dispatcher
    db_lockdown_dst = global_hooks_dir / "yadgar-db-lockdown-check.py"
    _copy_hook(package_hooks / "db-lockdown-check.py", db_lockdown_dst, dry_run)

    return stop_dst, session_end_dst, db_lockdown_dst


def _build_core_hooks(
    hooks_config: dict,
    runner: str,
    env_block: dict,
    db_lockdown_dst: Path,
) -> None:
    """Populate the four core (replace-always) hook event entries."""

    def _runner_entry(hook_type: str, matcher: str = "") -> dict:
        cmd = f"python3 {shlex.quote(runner)} {hook_type}"
        return _make_hook_entry(cmd, matcher, env_block)

    hooks_config["PreCompact"] = [_runner_entry("pre-compact-drain")]
    hooks_config["SessionStart"] = [
        _runner_entry("session-start-context"),
        _runner_entry("post-compact-rehydrate", matcher="compact"),
    ]
    # PostToolUse: two entries — (1) generic capture, (2) block-reflect on block_* writes.
    # block-reflect matcher: any of the five block write tools (v5.35.1).
    _block_reflect_matcher = "mcp__yadgar__block_(create|update|delete|replace|append)"
    hooks_config["PostToolUse"] = [
        _runner_entry("post-tool-capture"),
        _runner_entry("block-reflect", matcher=_block_reflect_matcher),
    ]
    hooks_config["UserPromptSubmit"] = [_runner_entry("prompt-recall")]

    # v5.20.0: direct-command entry so hookEventName is always emitted
    db_cmd = f'python3 "{db_lockdown_dst}"'
    hooks_config["PreToolUse"] = [_make_hook_entry(db_cmd, "Bash", env_block)]


def _install_append_hooks(
    package_hooks: Path,
    hooks_dir: Path,
    hooks_config: dict,
    env_block: dict,
    dry_run: bool,
) -> None:
    """Install and register the append-if-absent hook scripts."""
    _append_specs = [
        ("subagent-stop.py", "yadgar-subagent-stop.py", "SubagentStop", ""),
        ("instructions-loaded.py", "yadgar-instructions-loaded.py", "InstructionsLoaded", ""),
        ("subagent-start.py", "yadgar-subagent-start.py", "SubagentStart", ""),
        ("file-changed.py", "yadgar-file-changed.py", "FileChanged", ""),
    ]
    for src_name, dst_name, event, matcher in _append_specs:
        dst = hooks_dir / dst_name
        _copy_hook(package_hooks / src_name, dst, dry_run)
        _append_if_absent(hooks_config, event, f'python3 "{dst}"', env_block, matcher)


def _write_global_stop_hooks(
    global_claude_dir: Path,
    stop_entry: list,
    session_end_entry: list,
) -> None:
    """Merge Stop + SessionEnd into the global settings.json (scope=project path)."""
    global_settings_path = global_claude_dir / "settings.json"
    global_settings: dict = {}
    if global_settings_path.exists():
        try:
            global_settings = json.loads(global_settings_path.read_text())
        except Exception:
            global_settings = {}
    global_hooks = global_settings.get("hooks", {})
    global_hooks["Stop"] = stop_entry
    global_hooks["SessionEnd"] = session_end_entry
    global_settings["hooks"] = global_hooks
    _atomic_write(global_claude_dir, global_settings_path, global_settings)


def _resolve_scope_paths(
    home_dir: Path,
    scope: str,
    project_dir: Path,
) -> tuple[Path, Path, Path, Path]:
    """Return (global_claude_dir, global_hooks_dir, hooks_dir, settings_target_dir)."""
    global_claude_dir = home_dir / ".claude"
    global_hooks_dir = global_claude_dir / "hooks"
    if scope == "global":
        return global_claude_dir, global_hooks_dir, global_hooks_dir, global_claude_dir
    claude_dir = project_dir / ".claude"
    return global_claude_dir, global_hooks_dir, claude_dir / "hooks", claude_dir


def _copy_scope_scripts(
    package_hooks: Path,
    hooks_dir: Path,
    dry_run: bool,
) -> None:
    """Bulk-copy dispatcher-pattern hook scripts into hooks_dir."""
    _files = {
        "pre-compact-drain.sh": 0o755,
        "post-compact-rehydrate.sh": 0o755,
        "post-tool-capture.py": 0o755,
        "session-start-context.py": 0o755,
        "prompt-recall.py": 0o755,
        "subagent-stop.py": 0o755,
        "instructions-loaded.py": 0o755,
        "subagent-start.py": 0o755,
        "file-changed.py": 0o755,
    }
    if dry_run:
        return
    for filename, mode in _files.items():
        src = package_hooks / filename
        dst = hooks_dir / filename
        if src.exists():
            shutil.copy2(src, dst)
            dst.chmod(mode)


def _load_settings(settings_path: Path) -> dict:
    """Read existing settings.json; return empty dict on missing or parse error."""
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text())
    except Exception:
        return {}


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
    global_claude_dir, global_hooks_dir, hooks_dir, settings_target_dir = _resolve_scope_paths(
        home_dir, scope, project_dir
    )

    if not dry_run:
        global_hooks_dir.mkdir(parents=True, exist_ok=True)
        if scope != "global":
            hooks_dir.mkdir(parents=True, exist_ok=True)

    package_hooks = Path(__file__).parent / "hooks"
    _copy_scope_scripts(package_hooks, hooks_dir, dry_run)

    # Always-global scripts
    stop_dst, session_end_dst, db_lockdown_dst = _install_global_scripts(
        package_hooks, global_hooks_dir, dry_run
    )

    # hook_runner.py (dispatcher for core hooks)
    hook_runner_dst = hooks_dir / "hook_runner.py"
    _copy_hook(Path(__file__).parent / "scripts" / "hook_runner.py", hook_runner_dst, dry_run)
    _runner = str(hook_runner_dst)

    # Auth env block
    _auth_token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    _env_block: dict = {"YADGAR_MCP_AUTH_TOKEN": _auth_token} if _auth_token else {}

    settings_path = settings_target_dir / "settings.json"
    settings_data = _load_settings(settings_path)
    hooks_config = settings_data.get("hooks", {})

    # Core hooks (always replaced)
    _build_core_hooks(hooks_config, _runner, _env_block, db_lockdown_dst)

    # Append-if-absent hooks
    _install_append_hooks(package_hooks, hooks_dir, hooks_config, _env_block, dry_run)

    settings_data["hooks"] = hooks_config

    _stop_entry = [
        {"matcher": "", "hooks": [{"type": "command", "command": f'python3 "{stop_dst}"'}]}
    ]
    _session_end_entry = [
        {"matcher": "", "hooks": [{"type": "command", "command": f'python3 "{session_end_dst}"'}]}
    ]

    if scope == "global":
        hooks_config["Stop"] = _stop_entry
        hooks_config["SessionEnd"] = _session_end_entry
        settings_data["hooks"] = hooks_config

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

    _atomic_write(settings_target_dir, settings_path, settings_data)

    if scope == "project":
        _write_global_stop_hooks(global_claude_dir, _stop_entry, _session_end_entry)

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
            "SessionEnd (sentinel capture — global)",
            "SubagentStop (findings capture — append-if-absent)",
            "InstructionsLoaded (recall on CLAUDE.md load — append-if-absent)",
            "SubagentStart (context injection at dispatch — append-if-absent)",
            "FileChanged (team_inbox + PLAN_*.md — append-if-absent)",
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
