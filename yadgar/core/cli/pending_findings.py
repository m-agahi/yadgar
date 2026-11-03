"""pending-findings subcommand — list pending subagent Yadgar findings (host-side).

Car A (ADR-0156): read surface for LLM-curated subagent findings.

The daemon runs in a container and cannot see the harness tmp-dir task
symlinks, so this is a HOST-SIDE CLI (disk I/O only, no MCP/DB).  The stop-hook
checkpoint cadence calls this CLI to surface pending subagent findings for LLM
curation.

Usage:
  yadgar pending-findings --transcript-path <path> [--cwd <dir>] [--json] [--advance-state]

- Default (LIST): human-readable block (agent_type + bullets + transcript_path
    per subagent).  Also snapshots exactly the listed paths + mtimes for the
    session, so a later --advance-state consumes that set and nothing else.
- --json: emit the list as JSON (also snapshots).
- --advance-state: mark the set the last LIST showed as consumed.  No re-scan:
    an agent that finished after LIST stays pending.  No prior LIST → no-op.
    Prints nothing.
- Absent/unreadable transcript → empty output, exit 0.
- ``YADGAR_TASKS_ROOT`` pins the glob to one root; otherwise every root from
    ``_tasks_roots_default`` (CLAUDE_CODE_TMPDIR, TMPDIR/TMP/TEMP, /tmp).
"""

from __future__ import annotations

import json
import sys


def cmd_pending_findings(args):
    """List (or consume the listed) pending subagent findings from .output transcripts."""
    import os

    from yadgar.core.hooks.findings_capture import (
        _default_sweep_state_path,
        advance_listed_state,
        list_pending_findings,
    )

    transcript_path = args.transcript_path or ""
    cwd = args.cwd or os.getcwd()
    state_path = _default_sweep_state_path()

    # Test / operator pin of the tasks root; unset → union of harness tmp roots.
    tasks_root = os.environ.get("YADGAR_TASKS_ROOT") or None

    # Empty/missing transcript → empty output, exit 0 (graceful no-op).
    if not transcript_path:
        if args.json:
            print("[]")
        sys.exit(0)

    if args.advance_state:
        advance_listed_state(transcript_path, state_path)
        return

    pending = list_pending_findings(transcript_path, cwd, state_path, tasks_root=tasks_root)

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
        help="Mark the set the last LIST showed as consumed (no re-scan)",
    )
    p.set_defaults(func=cmd_pending_findings)
