"""install-subagents CLI subcommand (v5.44.0 X4).

Copies bundled agent .md templates from the yadgar package to ~/.claude/agents/.
Idempotent. Detects OS + Claude Code config path via platform_paths.

Usage:
  yadgar install-subagents [--dry-run] [--force] [--check]

Nix carve-out: skips with message when running on NixOS — the nix repo manages
~/.claude/agents/ via home-manager module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _handle_check_result(result: dict) -> None:
    """Print check result and exit with appropriate code."""
    would = result.get("would_install", [])
    if not would:
        print("All agent files already installed — no changes needed.")
        sys.exit(0)
    print(f"Would install {len(would)} agent file(s) to {result.get('agents_dir')}:")
    for name in would:
        print(f"  + {name}")
    sys.exit(1)


def cmd_install_subagents(args) -> None:
    from yadgar.core.install.install_subagents_lib import install_subagents_impl

    result = install_subagents_impl(
        home_dir=Path.home(),
        dry_run=args.dry_run,
        force=args.force,
        check=args.check,
    )

    status = result.get("status")
    if status == "error":
        print(f"Error: {result.get('reason')}", file=sys.stderr)
        sys.exit(1)
    elif status == "nix_managed":
        print(result.get("message", "NixOS detected — skipping."))
    elif status == "dry_run":
        pass  # install_subagents_impl already printed the preview
    elif status == "check":
        _handle_check_result(result)
    else:
        print(json.dumps(result, indent=2))


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "install-subagents",
        help="Install bundled agent templates to ~/.claude/agents/ (runs on host)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be installed without writing any files",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing agent files",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="List files that would be installed; exit nonzero if any changes needed",
    )
    p.set_defaults(func=cmd_install_subagents)
