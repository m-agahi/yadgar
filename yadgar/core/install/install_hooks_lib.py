"""Shared install_hooks implementation — canonical module.

Both the MCP tool (yadgar/server/tools/misc.py) and the CLI subcommand
(yadgar/cli/install_hooks.py) call install_hooks_impl() here.

Car C5 (ADR-0066 module-standardization split) broke the former 833-LOC file
into cohesive siblings; this module keeps the ``install_hooks_impl`` orchestrator
and re-exports the full public + private surface so external importers and tests
(which import ``from yadgar.core.install.install_hooks_lib import …`` and the
PEP-562 shim ``yadgar.core.install_hooks_lib``) are byte-unaffected:

  ``_interpreter``   — durable-interpreter resolution (_stable_python et al.)
  ``_hook_scripts``  — hook-script copy + stale-orphan sweep
  ``_settings``      — settings.json hook-entry assembly + container detection

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
import shlex
import sys  # noqa: F401 — re-exported: tests patch ihl.sys.executable (shared sys singleton)
from pathlib import Path

from yadgar._shared.observability.observe import observe

# Re-exported surface — external code + tests import these from this module path
# (and via the PEP-562 shim yadgar.core.install_hooks_lib). Keep the imports here.
from ._hook_scripts import (
    _MANAGED_NONPREFIXED,
    _copy_hook,
    _is_nix_symlink,
    _sha256_file,
    _sweep_stale_hook_scripts,
)
from ._interpreter import (
    _WORKTREE_MARKER,
    _canonical_repo_python,
    _entry_interpreter,
    _existing_registration_ok,
    _is_durable_interpreter,
    _is_git_worktree_path,
    _main_repo_root,
    _pipx_python,
    _registered_python,
    _resolve_python_shebang,
    _stable_python,
)
from ._settings import (
    _TEST_FIXTURE_TOKENS,
    _append_if_absent,
    _atomic_write,
    _build_core_hooks,
    _entry_command,
    _install_append_hooks,
    _install_global_scripts,
    _load_settings,
    _make_hook_entry,
    _resolve_env_block,
    _resolve_scope_paths,
    _write_global_stop_hooks,
    is_running_in_container,
)

logger = logging.getLogger(__name__)

__all__ = [
    "_MANAGED_NONPREFIXED",
    "_TEST_FIXTURE_TOKENS",
    "_WORKTREE_MARKER",
    "_append_if_absent",
    "_atomic_write",
    "_build_core_hooks",
    "_canonical_repo_python",
    "_copy_hook",
    "_entry_command",
    "_entry_interpreter",
    "_existing_registration_ok",
    "_install_append_hooks",
    "_install_global_scripts",
    "_is_durable_interpreter",
    "_is_git_worktree_path",
    "_is_nix_symlink",
    "_load_settings",
    "_main_repo_root",
    "_make_hook_entry",
    "_pipx_python",
    "_registered_python",
    "_resolve_env_block",
    "_resolve_python_shebang",
    "_resolve_scope_paths",
    "_sha256_file",
    "_stable_python",
    "_sweep_stale_hook_scripts",
    "_write_global_stop_hooks",
    "install_hooks_impl",
    "is_running_in_container",
]


# ── Shared install logic ───────────────────────────────────────────────────


@observe(tier="boundary")
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

    # Resolve the durable interpreter ONCE, before any copy, so hook commands
    # and script shebangs all agree. The existing registration (target
    # settings first, then global) is the preferred substitute when the
    # running interpreter is non-durable (agent worktree / tmp venv).
    settings_path = settings_target_dir / "settings.json"
    settings_data = _load_settings(settings_path)
    _existing_python = _registered_python(settings_data)
    if _existing_python is None and scope == "project":
        _existing_python = _registered_python(_load_settings(global_claude_dir / "settings.json"))
    _python_path = _stable_python(existing=_existing_python, home_dir=home_dir)

    if not dry_run:
        global_hooks_dir.mkdir(parents=True, exist_ok=True)
        if scope != "global":
            hooks_dir.mkdir(parents=True, exist_ok=True)

    package_hooks = Path(__file__).parents[1] / "hooks"

    # Always-global scripts
    stop_dst, session_end_dst, router_dst = _install_global_scripts(
        package_hooks, global_hooks_dir, dry_run, _python_path
    )

    # #64: sweep yadgar-installed orphan hook scripts from the GLOBAL hooks dir —
    # the non-prefixed vestige copies prior installs emitted (content-hash-gated,
    # nix-symlink-skipped) + the superseded db-lockdown orphan. Global-dir only:
    # the orphans only ever landed in ~/.claude/hooks (scope=global writes there).
    _sweep_stale_hook_scripts(package_hooks, global_hooks_dir, dry_run)

    # hook_runner.py (dispatcher for core hooks)
    hook_runner_dst = hooks_dir / "hook_runner.py"
    _copy_hook(
        Path(__file__).parents[1] / "scripts" / "hook_runner.py",
        hook_runner_dst,
        dry_run,
        _python_path,
    )
    _runner = str(hook_runner_dst)

    # Auth env block (BUG B/C fix) — see _resolve_env_block.
    _env_block: dict = _resolve_env_block()

    hooks_config = settings_data.get("hooks", {})

    # Core hooks (always replaced)
    _build_core_hooks(hooks_config, _runner, _env_block, router_dst, _python_path)

    # Append-if-absent hooks
    _install_append_hooks(package_hooks, hooks_dir, hooks_config, _env_block, dry_run, _python_path)

    settings_data["hooks"] = hooks_config

    _python = shlex.quote(_python_path)
    _stop_entry = [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": f"{_python} {shlex.quote(str(stop_dst))}"}],
        }
    ]
    _session_end_entry = [
        {
            "matcher": "",
            "hooks": [
                {"type": "command", "command": f"{_python} {shlex.quote(str(session_end_dst))}"}
            ],
        }
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
            "PreToolUse (router-guard)",
            "Stop (memory checkpoint — global)",
            "SessionEnd (sentinel capture — global)",
            "InstructionsLoaded (recall on CLAUDE.md load — append-if-absent)",
            "SubagentStart (context injection at dispatch — append-if-absent)",
            "FileChanged (team_inbox + PLAN_*.md — append-if-absent)",
        ],
        "settings_file": str(settings_path),
        "global_settings_file": global_settings_file,
    }
