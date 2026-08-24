"""``yadgar backfill ...`` subcommand — operator-invoked backfill ops.

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
  - ``--stamp-identity`` (Car 1, ledger task 309) forwards ``stamp_project_id``
    — the graph-table half of the ``project_id`` migration, covering the six
    tables C6's ``memory``/``wiki_page`` backfill never named. Dry-run by
    default like the two above, and unlike ``--adr-rows`` a dry run exits
    NON-ZERO when the preflight fails: that op's only failure mode IS the
    preflight (the write path's registry guard, run over every derived target
    on both paths), so a preview reporting ``ok: False`` has established that
    the apply cannot succeed. ``--mapping-file`` supplies operator overrides;
    an unreadable one exits 2 BEFORE forwarding, because a mapping that could
    not be read must never become a run "without overrides".
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

    Task 311: the gate's verdict now comes from one of TWO predicates, so the
    report NAMES the one that ran. ``exact_match: true`` alone cannot be told
    apart from a three-zero comparison, and the operator reading this line is
    the person who has to make that distinction — the dict dump the line above
    already carries reads as noise, not as an answer.
    """
    mode = "DRY RUN" if dry_run else "APPLY"
    gate = result.get("gate", {}) or {}
    flagged = result.get("flagged", []) or []
    predicate = (
        "index_absent (legacy ADR index page gone) -> pages_seen == page_type_adr_rows"
        if gate.get("index_absent")
        else "three-way -> index_rows == pages_seen == page_type_adr_rows"
    )
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
    print(
        f"  GATE predicate: {predicate} -> exact_match={gate.get('exact_match')}",
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


def _print_stamp_report(result: dict, *, dry_run: bool) -> None:
    """Print the ``--stamp-identity`` manifest summary to stderr.

    The per-table line an operator actually decides on is the BUCKET split:
    ``rows_stamped`` alone reads as success even when 80% of the table went
    undecidable, which is the report shape ADR-0222 was filed about. So every
    bucket prints, with the per-reason breakdown, and the two never-an-owner
    classes print SEPARATELY — a reach marker (``global``: no owner axis at
    all) and a conflict (two owners claim one directory) are different facts
    and take different operator actions.
    """
    mode = "DRY RUN" if dry_run else "APPLY"
    totals = result.get("totals", {}) or {}
    dangling = result.get("dangling_relationships", {}) or {}
    print(
        f"[{mode}] rows_seen={totals.get('rows_seen')} "
        f"rows_stamped={totals.get('rows_stamped')} "
        f"rows_cross_project={totals.get('rows_cross_project')} "
        f"rows_undecidable={totals.get('rows_undecidable')} "
        f"dangling_relationships={dangling.get('count')} "
        f"guards={(result.get('guards') or {}).get('checked_project_ids')}",
        file=sys.stderr,
    )
    for table, report in (result.get("tables") or {}).items():
        print(
            f"  {table}: seen={report.get('rows_seen')} "
            f"stamped={report.get('rows_stamped')} "
            f"cross_project={report.get('rows_cross_project')} "
            f"undecidable={report.get('rows_undecidable')} "
            f"{report.get('undecidable_by_reason')}",
            file=sys.stderr,
        )
    reach = result.get("reach_markers") or []
    if reach:
        print(
            f"  reach_markers (never an owner — ADR-0227): {reach}",
            file=sys.stderr,
        )
    for conflict in result.get("map_conflicts") or []:
        print(
            f"  CONFLICT: {conflict.get('directory')} claimed by "
            f"{conflict.get('project_ids')} — pass --mapping-file to settle it",
            file=sys.stderr,
        )
    if result.get("ok") is False:
        print(f"  REFUSED: {result.get('error')}", file=sys.stderr)


def _load_mapping(path: str) -> dict:
    """Read the operator's ``{directory: project_id}`` override file.

    Raises ValueError on anything that is not a JSON object of strings. Loud,
    because a silently-empty mapping is indistinguishable from "no overrides
    were needed" in the manifest, and the operator supplied the file precisely
    because the corpus join could not settle something.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"--mapping-file {path!r}: expected a JSON object, got {type(raw).__name__}"
        )
    return {str(k): str(v) for k, v in raw.items()}


def _run_stamp_identity(args: argparse.Namespace) -> int:
    """``--stamp-identity`` branch (Car 1, ledger task 309).

    Its own function rather than a fourth arm inline: ``cmd_backfill`` was
    already at the I13 cyclomatic cap, and a branch bolted on regardless is
    how a HARD gate turns into an allowlist entry nobody re-reads.

    Takes NO ``project_id``. The op is corpus-wide by construction — it
    inherits each row's owner from the rows that produced it, across every
    project in the store — so passing the invoking session's identity would
    name a scope the op does not have.
    """
    from yadgar.core.forward import _forward_admin

    dry_run = not getattr(args, "apply", False)
    payload: dict = {"dry_run": dry_run}
    mapping_file = getattr(args, "mapping_file", None)
    if mapping_file:
        try:
            payload["mapping"] = _load_mapping(mapping_file)
        except (OSError, ValueError) as exc:
            # Exit BEFORE forwarding. A mapping the operator supplied and this
            # command could not read must never become "ran without overrides"
            # — that is the run they wrote the file to prevent.
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    result = _forward_admin("stamp_project_id", payload)
    _print_stamp_report(result, dry_run=dry_run)
    print(json.dumps(result))
    # NON-ZERO ON A DRY RUN TOO, unlike --adr-rows' D35c gate. That gate
    # reconciles a POST-write census, so a preview necessarily disagrees with
    # it. This op's only failure mode is the PREFLIGHT — the write path's own
    # registry guard, run over every derived target on both paths (Car 19) —
    # so a preview reporting ok=False has established that the apply cannot
    # succeed, which is exactly what an exit code is for.
    return 1 if result.get("ok") is False else 0


def _run_reslug(args: argparse.Namespace, *, project_id: str | None) -> int:
    """``--reslug-adr-pages`` branch (own function so cmd_backfill stays at the
    I13 cyclomatic cap).
    """
    from yadgar.core.forward import _forward_admin

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


def _run_adr_rows(args: argparse.Namespace, *, project_id: str | None, directory: str) -> int:
    """``--adr-rows`` branch (own function so cmd_backfill stays at the I13 cap)."""
    from yadgar.core.forward import _forward_admin

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


def cmd_backfill(args: argparse.Namespace) -> int:
    """``yadgar backfill`` handler.

    Resolves ``project_id`` the same way every other CLI command does
    (``resolve_cli_project`` — ``--project`` wins, else the C2 mint off
    ``directory``; unresolvable fails loud per ADR-0227, never guessed).
    """
    from yadgar.core.cli._shared import resolve_cli_project

    directory = str(Path(args.directory).resolve())
    project_id = resolve_cli_project(getattr(args, "project", None), directory)

    if getattr(args, "reslug_adr_pages", False):
        return _run_reslug(args, project_id=project_id)

    if getattr(args, "adr_rows", False):
        return _run_adr_rows(args, project_id=project_id, directory=directory)

    if getattr(args, "stamp_identity", False):
        return _run_stamp_identity(args)

    print(
        "ERROR: specify --reslug-adr-pages, --adr-rows or --stamp-identity "
        "(see `yadgar backfill --help`)",
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
        "--stamp-identity",
        dest="stamp_identity",
        action="store_true",
        default=False,
        help=(
            "Stamp project_id on entity / relationship / memory_cluster / checkpoint / "
            "memory_block / episode by inheriting it from already-stamped rows "
            "(dry-run by default; pass --apply to write)"
        ),
    )
    p.add_argument(
        "--mapping-file",
        dest="mapping_file",
        metavar="PATH",
        default=None,
        help=(
            "JSON {directory: project_id} overrides for --stamp-identity. Wins over "
            "the corpus-derived map — use it to settle a directory two projects "
            "claim. Sentinel targets ('global', 'system', 'unresolved') are refused"
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Apply --reslug-adr-pages, --adr-rows or --stamp-identity "
            "(default: dry-run report only, no writes)"
        ),
    )
    p.set_defaults(func=cmd_backfill)
