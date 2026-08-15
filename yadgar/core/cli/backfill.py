"""``yadgar backfill ...`` subcommand — operator-invoked ADR backfill ops.

task-adr-backfill-prompts (2026-08-15). ``seed_adr_rows`` and ``reslug`` are
registered backend admin ops (``yadgar.backend.admin_exec._ADMIN_OPS``,
D35a / D32 ③) reachable ONLY via ``POST /admin`` on the backend — no MCP
tool and no CLI surface existed, so an operator could not run either op at
all. ``cmd_backfill`` is a thin forwarder, the same pattern ``drain`` /
``restore`` already use for other admin ops (``yadgar.core.cli._shared``'s
``forward_pre_compact_drain`` / ``forward_restore``): it reaches the backend
directly via ``yadgar.core.forward._forward_admin`` (op name + payload) —
no new core-daemon HTTP route is needed, because that forwarder already
serves ANY registered admin op by name.

Safety:
  - ``--reslug-adr-pages`` defaults to dry-run (mirrors ``reslug_adr_pages``'s
    own safe default); ``--apply`` is required to write anything. There is
    no flag combination that applies by accident.
  - ``--adr-rows`` seeds the ``adr`` ledger table from existing per-ADR wiki
    pages. The op itself never raises on a D35c gate failure or a D35b
    flagged row — this CLI is where that becomes an actionable non-zero
    exit code, since an operator reads stdout/stderr to decide whether to
    continue, and a silently-swallowed result dict would defeat that.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _print_reslug_report(result: dict, *, dry_run: bool) -> None:
    """Print a readable summary of the reslug manifest to stderr.

    ``collisions`` is the field an operator most needs to see before
    deciding whether to re-run with ``--apply`` — a colliding rewrite is
    skipped by the op itself (never overwrites the occupant), but the
    operator should know which pages were left un-rewritten.
    """
    mode = "DRY RUN" if dry_run else "APPLY"
    rewrites = result.get("rewrites", []) or []
    collisions = result.get("collisions", []) or []
    print(
        f"[{mode}] reslug: {len(rewrites)} rewrite(s), {len(collisions)} collision(s)",
        file=sys.stderr,
    )
    for c in collisions:
        print(
            f"  COLLISION: {c.get('old')} -> {c.get('new')} "
            f"(id={c.get('id')} occupant_id={c.get('occupant_id')})",
            file=sys.stderr,
        )


def _print_seed_report(result: dict) -> None:
    """Print a readable summary of the seed result to stderr.

    Surfaces exactly the fields the task calls out: pages_seen /
    rows_inserted / rows_skipped / flagged / gate — an operator reads this
    to decide whether the backfill is safe to consider done.
    """
    gate = result.get("gate", {}) or {}
    flagged = result.get("flagged", []) or []
    print(
        f"pages_seen={result.get('pages_seen')} "
        f"rows_inserted={result.get('rows_inserted')} "
        f"rows_skipped={result.get('rows_skipped')} "
        f"flagged={len(flagged)} "
        f"gate={gate}",
        file=sys.stderr,
    )
    for f in flagged:
        print(f"  FLAGGED: {f}", file=sys.stderr)


def cmd_backfill(args: argparse.Namespace) -> int:
    """``yadgar backfill`` handler.

    Resolves ``project_id`` the same way every other CLI command does
    (``resolve_cli_project`` — ``--project`` wins, else the C2 mint off
    ``directory``; unresolvable fails loud per ADR-0227, never guessed).
    """
    from yadgar.core.cli._shared import resolve_cli_project
    from yadgar.core.forward import _forward_admin

    directory = str(Path(args.directory).resolve())
    project_id = resolve_cli_project(getattr(args, "project", None), directory)

    if getattr(args, "reslug_adr_pages", False):
        dry_run = not getattr(args, "apply", False)
        result = _forward_admin("reslug", {"project_id": project_id, "dry_run": dry_run})
        _print_reslug_report(result, dry_run=dry_run)
        print(json.dumps(result))
        return 0

    if getattr(args, "adr_rows", False):
        result = _forward_admin("seed_adr_rows", {"project_id": project_id, "directory": directory})
        _print_seed_report(result)
        print(json.dumps(result))
        gate = result.get("gate", {}) or {}
        flagged = result.get("flagged", []) or []
        if not gate.get("exact_match", False) or flagged:
            return 1
        return 0

    print(
        "ERROR: specify --reslug-adr-pages or --adr-rows (see `yadgar backfill --help`)",
        file=sys.stderr,
    )
    return 2


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach ``yadgar backfill`` to the top-level parser."""
    from yadgar.core.cli._shared import add_project_argument

    p = subparsers.add_parser(
        "backfill",
        help="Operator-invoked ADR ledger/reslug backfill ops (task-adr-backfill-prompts)",
    )
    add_project_argument(p)
    p.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Project directory (default: cwd)",
    )
    p.add_argument(
        "--reslug-adr-pages",
        dest="reslug_adr_pages",
        action="store_true",
        default=False,
        help=(
            "Re-slug ADR wiki pages yadgar-adr-NNNN -> {project_id}_adr-NNNN "
            "(dry-run by default; pass --apply to write)"
        ),
    )
    p.add_argument(
        "--adr-rows",
        dest="adr_rows",
        action="store_true",
        default=False,
        help="Seed the adr ledger table from existing per-ADR wiki pages (D35a)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply --reslug-adr-pages (default: dry-run report only, no writes)",
    )
    p.set_defaults(func=cmd_backfill)
