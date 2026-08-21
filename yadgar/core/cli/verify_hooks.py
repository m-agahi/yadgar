"""``yadgar verify-hooks`` — report which managed hooks are actually wired.

Ledger task 306.  Read-only counterpart to ``yadgar install --client
claude-code --hooks``: that command WRITES the managed hook entries, this one
asks whether they are still there.  Deliberately a separate subcommand rather
than a flag on ``yadgar install`` — a verify flag on a command whose default
action writes files is one typo away from an unwanted install, and this is
meant to be safe to wire into an unattended probe.

Exit codes:
  0 — every managed hook is wired (foreign-shaped and unexpected entries are
      reported but do not fail; see ``_verify`` for why).
  1 — at least one managed hook is absent from every settings file inspected,
      i.e. it never fires.

Runs on the HOST.  The daemon is containerized and cannot see the user's
``~/.claude/settings.json``, which is why this is not an MCP tool or a
``project_brief`` signal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def cmd_verify_hooks(args) -> None:
    from yadgar.core.install._verify import (  # noqa: PLC0415
        format_hook_verify_report,
        verify_managed_hooks,
    )

    home = Path(args.home) if getattr(args, "home", "") else Path.home()
    project = args.project_directory or str(Path.cwd())

    report = verify_managed_hooks(home_dir=home, project_directory=project)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        text = format_hook_verify_report(report)
        print(text) if report["ok"] else print(text, file=sys.stderr)

    sys.exit(0 if report["ok"] else 1)


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "verify-hooks",
        help=(
            "Report which yadgar-managed Claude Code hooks are actually wired "
            "into the live settings.json (read-only; exits 1 on divergence)"
        ),
    )
    p.add_argument(
        "--project-directory",
        default="",
        metavar="PATH",
        help="Project root whose .claude/settings.json is also inspected (default: cwd)",
    )
    p.add_argument(
        "--home",
        default="",
        metavar="PATH",
        help="Home directory to inspect (default: the current user's)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the raw report as JSON instead of human-readable text",
    )
    p.set_defaults(func=cmd_verify_hooks)
