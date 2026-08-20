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
    pages, and now defaults to dry-run for the SAME reason — the claim above
    used to be false for exactly this branch. ``--apply`` was wired only to
    the reslug half, so the irreversible half was the half with no preview:
    ``adr.id`` is AUTO_INCREMENT, IS the ADR number (ADR-0197), never moves
    backwards, and ``TRUNCATE`` is FK-blocked by ``adr_supersedes``. A wrong
    insert is permanent.
  - The op itself never raises on a D35c gate failure or a D35b flagged row —
    this CLI is where that becomes an actionable non-zero exit code, since an
    operator reads stdout/stderr to decide whether to continue, and a
    silently-swallowed result dict would defeat that. NOTE that the gate is
    computed AFTER the writes commit, so its exit code is a post-mortem: the
    pre-write check is the dry run.
  - ``reslug`` joins that gate too (ledger task 13 defect 2, 2026-08-20): it
    returns no ``ok`` key at all, and a non-empty ``collisions`` list on
    ``--apply`` now makes the CLI exit 1 — a collision means the op
    deliberately left that page un-reslugged (the occupant is never
    overwritten, so data is safe), but the operator previously got no signal
    beyond a stderr line. Dry-run collisions stay exit 0: the operator is
    reading the preview by definition, whereas ``--apply`` is the unread
    path an unattended script exercises.
  - ``--skip-adr`` states which ADR numbers to leave un-inserted (ADR-0006:
    the ids they need are already spent). Repeatable and comma-separated.
    Without it the governing ADR had no mechanism at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parse_skip_adr(raw: list[str] | None) -> list[int]:
    """Parse ``--skip-adr`` values into a sorted list of ADR numbers.

    Accepts repetition (``--skip-adr 1 --skip-adr 5``), commas
    (``--skip-adr 1,5,6``), any mix of the two, and either bare numbers or the
    ``ADR-NNNN`` form an operator reads in the procedure doc.

    Raises:
        ValueError: a token is not an ADR number. Loud, because a typo'd skip
            silently shifts every later ADR onto the wrong id.
    """
    numbers: set[int] = set()
    for entry in raw or []:
        for token in str(entry).split(","):
            token = token.strip()
            if not token:
                continue
            cleaned = token[4:] if token[:4].lower() == "adr-" else token
            try:
                numbers.add(int(cleaned))
            except ValueError as exc:
                raise ValueError(
                    f"--skip-adr: {token!r} is not an ADR number (expected e.g. "
                    "`7`, `0007` or `ADR-0007`)"
                ) from exc
    return sorted(numbers)


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


def _print_seed_report(result: dict, *, dry_run: bool) -> None:
    """Print a readable summary of the seed result to stderr.

    The four outcome counters print separately. ``rows_skipped`` used to be one
    number covering "already had a row", "the insert raised" and "the insert
    returned no id" — so the line an operator reads to decide whether the
    backfill worked could not tell "nothing to do" from "totally broken", which
    is exactly how a run that inserted zero rows read as a success.
    """
    mode = "DRY RUN" if dry_run else "APPLY"
    gate = result.get("gate", {}) or {}
    flagged = result.get("flagged", []) or []
    print(
        f"[{mode}] pages_seen={result.get('pages_seen')} "
        f"rows_inserted={result.get('rows_inserted')} "
        f"rows_already_present={result.get('rows_already_present')} "
        f"rows_failed={result.get('rows_failed')} "
        f"rows_skipped_by_request={result.get('rows_skipped_by_request')} "
        f"next_id={result.get('next_id')} ({result.get('next_id_basis')}) "
        f"flagged={len(flagged)} "
        f"gate={gate}",
        file=sys.stderr,
    )
    if result.get("ok") is False:
        print(
            f"  ABORTED: {result.get('error')} (resume after ADR-{result.get('resume_after_adr')})",
            file=sys.stderr,
        )
    for entry in result.get("plan") or []:
        print(
            f"  PLAN: {entry.get('adr')} -> id {entry.get('planned_id')} ({entry.get('slug')})",
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
        # ``reslug`` returns no ``ok`` key at all — a collision means a page
        # was deliberately left un-reslugged (the occupant is never
        # overwritten, so data is safe), but the operator previously got no
        # signal beyond a stderr line (ledger task 13 defect 2). Gated on
        # --apply only: a dry run's collisions are informational (the
        # operator is reading the report by definition — that's why they
        # ran a preview instead of --apply), whereas --apply is the unread
        # path — a script running --apply has nobody reading stderr, which
        # is exactly the harm a silent exit 0 caused.
        if not dry_run and result.get("collisions"):
            return 1
        return 0

    if getattr(args, "adr_rows", False):
        dry_run = not getattr(args, "apply", False)
        payload: dict = {"project_id": project_id, "directory": directory, "dry_run": dry_run}
        skip = _parse_skip_adr(getattr(args, "skip_adr", None))
        if skip:
            payload["skip_adr_numbers"] = skip
        result = _forward_admin("seed_adr_rows", payload)
        _print_seed_report(result, dry_run=dry_run)
        print(json.dumps(result))
        if result.get("ok") is False or (result.get("rows_failed") or 0):
            return 1
        if dry_run:
            # The D35c gate reconciles the ledger against the page census, so on
            # a dry run it necessarily disagrees — nothing was written. Gating
            # the preview on it would make every dry run exit non-zero and
            # teach the operator to ignore the exit code on the run that
            # matters.
            return 0
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
        help=(
            "Seed the adr ledger table from existing per-ADR wiki pages (D35a) "
            "(dry-run by default; pass --apply to write)"
        ),
    )
    p.add_argument(
        "--skip-adr",
        dest="skip_adr",
        action="append",
        metavar="N[,N...]",
        default=None,
        help=(
            "ADR number(s) to leave un-inserted because their ledger ids are "
            "already spent (ADR-0006). Repeatable and comma-separated; accepts "
            "`7`, `0007` or `ADR-0007`. Only meaningful with --adr-rows"
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=("Apply --reslug-adr-pages or --adr-rows (default: dry-run report only, no writes)"),
    )
    p.set_defaults(func=cmd_backfill)
