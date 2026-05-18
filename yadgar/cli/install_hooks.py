"""install-hooks CLI subcommand.

Runs on the host machine so that hook scripts and settings.json land
in the real $HOME/.claude/, not inside a container filesystem.

Usage:
  yadgar install-hooks [--scope {project,global}] [--project-directory PATH] [--dry-run]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def cmd_install_hooks(args) -> None:
    from yadgar.install_hooks_lib import install_hooks_impl

    result = install_hooks_impl(
        home_dir=Path.home(),
        scope=args.scope,
        project_directory=args.project_directory or None,
        dry_run=args.dry_run,
    )

    status = result.get("status")
    if status == "error":
        print(f"Error: {result.get('reason')}", file=sys.stderr)
        sys.exit(1)
    elif status == "dry_run":
        # install_hooks_impl already printed the preview; nothing more to do
        pass
    else:
        print(json.dumps(result, indent=2))


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "install-hooks",
        help="Install Claude Code hooks for automatic memory capture (runs on host)",
    )
    p.add_argument(
        "--scope",
        choices=("project", "global"),
        default="global",
        help="'global' writes to ~/.claude/settings.json (default); 'project' to .claude/settings.json",
    )
    p.add_argument(
        "--project-directory",
        default="",
        metavar="PATH",
        help="Project root directory (default: $PWD)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the settings.json change without writing any files",
    )
    p.set_defaults(func=cmd_install_hooks)
