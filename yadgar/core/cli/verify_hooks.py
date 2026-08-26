"""``yadgar verify-hooks`` — report which managed hooks are actually wired.

Ledger task 306.  Read-only counterpart to ``yadgar install --client
claude-code --hooks``: that command WRITES the managed hook entries, this one
asks whether they are still there.  Deliberately a separate subcommand rather
than a flag on ``yadgar install`` — a verify flag on a command whose default
action writes files is one typo away from an unwanted install, and this is
meant to be safe to wire into an unattended probe.

Read-only by default. ``--probe`` opts in to EXECUTING the wired hooks, which
writes to the live store — see the flag's help text and
``_verify.verify_managed_hooks``. Task 385: the probe used to run
unconditionally, so the command that advertises itself as safe for an
unattended monitor filed a real ``action_log`` row and ran a real queue drain
on every invocation.

Exit codes:
  0 — every managed hook is wired (foreign-shaped and unexpected entries are
      reported but do not fail; see ``_verify`` for why). With ``--probe``,
      every wired hook also ran.
  1 — at least one managed hook is absent from every settings file inspected,
      i.e. it never fires. With ``--probe``, also when a wired hook hung,
      crashed, or has no binary on disk.

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

    report = verify_managed_hooks(
        home_dir=home,
        project_directory=project,
        probe=bool(getattr(args, "probe", False)),
    )

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
            "into the live settings.json (read-only unless --probe; exits 1 on "
            "divergence)"
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
        "--probe",
        action="store_true",
        help=(
            "Also EXECUTE each wired hook to check it actually runs. NOT "
            "read-only: the post-tool-capture probe writes a real action_log "
            "row and the pre-compact-drain probe runs a real queue drain. Off "
            "by default so an unattended run cannot mutate the store."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the raw report as JSON instead of human-readable text",
    )
    p.set_defaults(func=cmd_verify_hooks)
