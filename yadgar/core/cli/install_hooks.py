"""install-hooks CLI subcommand — REMOVED 2026-07-26 (Car 7 of the opencode port train).

The `yadgar install-hooks` command has been removed. It was a parallel-path
Claude-Code-only sibling of `yadgar install --client <name> --hooks`,
which is now the single canonical entrypoint for the hooks surface across
all supported clients (claude-code, cursor, opencode).

Migration:
  OLD: yadgar install-hooks [--scope {project,global}] [--project-directory PATH] [--dry-run]
  NEW: yadgar install --client claude-code [--hooks | --no-hooks] [--scope ...] [--project-directory ...] [--print]

The new command wires MCP + rules + hooks (default-on hooks for clients
with a hooks_kind) in one shot; --print emits a JSON fragment preview
without writing any files; --no-hooks opts out.

Background:
- docs/plans/port-opencode-re-audit-2026-07-26.md (opencode port re-audit)
- ADR-0143 (multi-client porting; verification gate #59 satisfied)
- ADR-0154 (Path A core-only, no backend bump)
- ADR-0161 (global-authoritative hook install)

Hard-removal per AGENTS.md 'forward-only: refactor trains rip-and-replace —
no backward-compat knobs/flags/dual-paths/re-export shims'. Intermediate
train states need only be CI-green, not runnable.

This stub remains so the old `register(subparsers)` call site in
yadgar/core/cli/__init__.py still imports cleanly; it prints a hard-remove
message and exits 1 when invoked, so any user (or setup script) that still
calls `yadgar install-hooks` gets an actionable error instead of a silent
no-op.
"""

from __future__ import annotations

import argparse
import sys

_REMOVED_MESSAGE = (
    "`yadgar install-hooks` has been removed (Car 7 of the opencode port train, "
    "v5.166.0, 2026-07-26).\n"
    "Use `yadgar install --client claude-code [--hooks | --no-hooks] "
    "[--scope ...]` instead.\n"
    "See docs/plans/port-opencode-re-audit-2026-07-26.md §3.1 for the "
    "single-source-of-truth rationale and the `yadgar install` orchestrator "
    "(yadgar/core/install/clients/install.py::install_client) that replaces this.\n"
    "Migration examples:\n"
    "  OLD: yadgar install-hooks --scope global\n"
    "  NEW: yadgar install --client claude-code --hooks --scope global\n"
    "  OLD: yadgar install-hooks --scope project --project-directory /path/to/repo\n"
    "  NEW: yadgar install --client claude-code --hooks --scope project --project-directory /path/to/repo\n"
    "  OLD: yadgar install-hooks --dry-run\n"
    "  NEW: yadgar install --client claude-code --hooks --print\n"
)


def cmd_install_hooks(args) -> None:  # noqa: ARG001 — args is the legacy argparse namespace
    """Hard-removed entrypoint — prints a migration message and exits 1."""
    print(_REMOVED_MESSAGE, file=sys.stderr)
    sys.exit(1)


def register(subparsers) -> None:
    """Register `yadgar install-hooks` for backward-compatible arg parsing.

    The command exists ONLY so old invocations get an actionable error.
    Calling it exits 1 with the migration message above.
    """
    p = subparsers.add_parser(
        "install-hooks",
        help=(
            "[REMOVED] Use `yadgar install --client claude-code --hooks` instead — "
            "this command exits 1 with a migration message"
        ),
    )
    # Keep the legacy args so old scripts don't get a confusing argparse error
    # before the migration message can be printed.
    p.add_argument(
        "--scope",
        choices=("project", "global"),
        default="global",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--project-directory",
        default="",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.set_defaults(func=cmd_install_hooks)
