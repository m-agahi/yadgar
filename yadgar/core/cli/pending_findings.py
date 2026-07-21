"""pending-findings subcommand — list pending subagent Yadgar findings (host-side).

Car A (ADR-0156): read surface for LLM-curated subagent findings.

The daemon runs in a container and cannot see /tmp task symlinks, so this is a
HOST-SIDE CLI (disk I/O only, no MCP/DB).  The stop-hook checkpoint cadence
calls this CLI to surface pending subagent findings for LLM curation.

Usage:
  yadgar pending-findings --transcript-path <path> [--cwd <dir>] [--json] [--advance-state]

- Default: human-readable block (agent_type + bullets + transcript_path per subagent).
- --json: emit the list as JSON.
- --advance-state: batch-advance the dedup state for all currently-pending paths
    (marks them consumed); prints nothing (or a count).
- Absent/unreadable transcript → empty output, exit 0.
"""

from __future__ import annotations

import json
import sys


def cmd_pending_findings(args):
    """List (or consume) pending subagent findings from on-disk .output transcripts."""
    import os

    from yadgar.core.hooks.findings_capture import (
        _default_sweep_state_path,
        _tasks_root_default,
        advance_pending_state,
        collect_pending_findings,
    )

    transcript_path = args.transcript_path or ""
    cwd = args.cwd or os.getcwd()
    state_path = _default_sweep_state_path()

    # Allow test injection of the tasks root via env (avoids globbing /tmp in tests).
    tasks_root = os.environ.get("YADGAR_TASKS_ROOT") or _tasks_root_default()

    # Empty/missing transcript → empty output, exit 0 (graceful no-op).
    if not transcript_path:
        if args.json:
            print("[]")
        sys.exit(0)

    pending = collect_pending_findings(transcript_path, cwd, state_path, tasks_root=tasks_root)

    if args.advance_state:
        advance_pending_state(pending, state_path)
        # Silent on success (only print if something was advanced).
        return

    if not pending:
        if args.json:
            print("[]")
        # Human-readable: empty output (caller checks for empty).
        return

    if args.json:
        print(json.dumps(pending, ensure_ascii=False))
        return

    # Human-readable block: one section per subagent.
    lines = []
    for entry in pending:
        agent_type = entry.get("agent_type", "unknown")
        findings = entry.get("findings", [])
        tp = entry.get("transcript_path", "")
        lines.append(f"agent_type: {agent_type}")
        for bullet in findings:
            lines.append(f"  - {bullet}")
        lines.append(f"  transcript_path: {tp}")
        lines.append("")
    print("\n".join(lines).rstrip())


def register(subparsers):
    p = subparsers.add_parser(
        "pending-findings",
        help="List pending subagent Yadgar findings (host-side, no DB)",
    )
    p.add_argument(
        "--transcript-path",
        type=str,
        default="",
        help="Path to the current session transcript (stem = session-uuid)",
    )
    p.add_argument(
        "--cwd",
        type=str,
        default="",
        help="Project working directory (default: cwd)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit findings list as JSON instead of human-readable block",
    )
    p.add_argument(
        "--advance-state",
        action="store_true",
        default=False,
        dest="advance_state",
        help="Mark all pending transcript paths as consumed (batch dedup advance)",
    )
    p.set_defaults(func=cmd_pending_findings)
