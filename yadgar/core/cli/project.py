"""``yadgar project ...`` subcommand — project-registry operator surface.

Car A (2026-08-14 identity train, plan §2). Closes the gap where
``backend.admin_exec.ledger_project.create_project_row`` exists and is
registered
(``backend/admin_exec/__init__.py:152``) but had no CLI / MCP path. The
seeding function is the FIRST operator step on a new deployment — every
``task`` / ``adr`` / ``agent_prompt`` ledger row FKs to the ``project``
table, so with zero rows the engine-#2 ledger refuses every write
(ADR-0078, ADR-0202/0223). This module exposes one subcommand:

  * ``yadgar project seed [--map <path>]``

Reads the TSV map (default ``.yadgar/project-id-map.tsv``, gitignored —
see Car D), calls ``create_project_row`` per row over the backend
``/admin`` route (``_forward_admin``), and reports per-row
``created`` / ``skipped`` (idempotent re-run) / ``failed``. Skips rows
whose ``project_id`` column is ``DROP`` or ``REVIEW`` — those are
operator decisions, not registry rows.

The TSV first column is ``source_directory`` — a host-side origin hint
captured at mint time, NOT a scoping key. ADR-0225 retires ``directory``
as a project-scoping concept; the column is kept here because it
documents where the row was first written from, but the registry payload
keys on ``project_id`` alone.

The registry guard is NOT relaxed by this module: unknown project_ids
are still refused on the WRITE path — by
``MariaStorageEngine.assert_project_registered`` inside
``create_task_row`` / ``create_adr_row``, and (Car 5, 2026-08-20) by
``assert_project_registered_for_create`` on the ``memorize`` /
``wiki_add`` create path. This module is the SEED that lets those
succeed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Default map path: ``<cwd>/.yadgar/project-id-map.tsv``. The repo
# gitignores ``.yadgar/`` so the file never lands in git; the VM
# (``192.168.122.101``) carries its own copy per the train plan §9.
DEFAULT_MAP_PATH = Path.cwd() / ".yadgar" / "project-id-map.tsv"

# Map rows whose column 2 is one of these are not registry rows — they
# are operator decisions (delete / human review). Listed here so the
# subcommand's output is self-documenting.
_NON_SEED_VALUES = frozenset({"DROP", "REVIEW"})

# ``project.display_name`` is VARCHAR(64) (see alembic revision
# ``003_project_registry``: ``sa.Column("display_name", sa.String(length=64),
# nullable=True)``). The TSV ``note`` column is a free-text field and can
# exceed that width; the original code used ``note[:255]`` which silently
# let >64-char notes through and tripped a MariaDB ``DataError(1406)`` at
# INSERT time. The 2026-08-20 incident showed this surfacing as
# ``failed: N`` with no per-row reason — task #241, car C8-3. We truncate
# to the schema's actual width here, BEFORE the backend call, and log the
# truncation reason so the daemon log has the operator signal the bare
# counter never carried.
DISPLAY_NAME_MAX_CHARS = 64

# Module-level logger for the seed path. ``yadgar.*`` namespace so it
# lands on the same handler as the backend's ``create_project_row``
# warning (which the operator reads for column-level reasons).
_logger = logging.getLogger("yadgar.core.cli.project")


def read_auth_token() -> str:
    """Read YADGAR_MCP_AUTH_TOKEN via the canonical resolver.

    Thin pass-through to ``yadgar.core.install.auth_token.resolve_auth_token``
    — the same chokepoint the seed subcommand routes through. The auth-token
    pattern lint hard-fails any hand-rolled ``os.environ.get("YADGAR_MCP_AUTH_TOKEN", ...)``
    so this stays a one-liner, not a copy.
    """
    from yadgar.core.install.auth_token import resolve_auth_token

    return resolve_auth_token()


def parse_map(map_path: Path) -> list[dict]:
    """Parse the project-id map TSV.

    Returns a list of dicts, one per data row, with keys:
      ``source_directory``, ``project_id``, ``memory_rows``,
      ``wiki_rows``, ``note``.

    Skips blank lines and comment lines (leading ``#``). Raises
    ``SystemExit(2)`` on a malformed row — the operator must see
    structural errors loudly, never have the CLI quietly invent a row
    that the backend would then refuse.
    """
    if not map_path.exists():
        print(f"ERROR: map file not found: {map_path}", file=sys.stderr)
        raise SystemExit(2)

    rows: list[dict] = []
    with map_path.open() as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 5:
                print(
                    f"ERROR: {map_path}:{lineno}: expected 5 tab-separated columns, "
                    f"got {len(parts)}: {line!r}",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            source_directory, project_id, mem, wiki, note = (
                parts[0].strip(),
                parts[1].strip(),
                parts[2].strip(),
                parts[3].strip(),
                parts[4].strip(),
            )
            if not source_directory or not project_id:
                print(
                    f"ERROR: {map_path}:{lineno}: empty source_directory or project_id: {line!r}",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            rows.append(
                {
                    "source_directory": source_directory,
                    "project_id": project_id,
                    "memory_rows": mem,
                    "wiki_rows": wiki,
                    "note": note,
                    "_line": lineno,
                }
            )
    return rows


def classify_row(row: dict) -> str:
    """Return ``seed`` / ``drop`` / ``review`` for a parsed row."""
    if row["project_id"] == "DROP":
        return "drop"
    if row["project_id"] == "REVIEW":
        return "review"
    return "seed"


def infer_kind(project_id: str) -> str:
    """Map-file kind column. Mirrors the mint heuristic:

      * contains a ``/`` (and not ``local/`` prefix) → ``git``
      * starts with ``local/`` → ``local``
      * prose (e.g. ``memory:486752``) → ``local`` (best-effort fallback;
        Car D §5.5 classifies these explicitly, not via this code path)

    Classifies by ``project_id`` shape alone — the original column 1
    (``source_directory``) is a host-side origin hint captured at mint
    time, NOT a scoping key. ADR-0225 retires ``directory`` as a
    project-scoping concept; this helper follows.

    The map's column 2 is the AUTHORITATIVE key, not the inferred
    one — this helper just populates the ``kind`` field the
    ``create_project_row`` payload asks for.
    """
    if "/" in project_id and not project_id.startswith("local/"):
        return "git"
    if project_id.startswith("local/"):
        return "local"
    return "local"


def is_duplicate_project_error(err: str) -> bool:
    """True when *err* is ``create_project_row`` reporting an existing key.

    The backend does NOT name the exception class: ``create_project_row``
    returns ``{"ok": False, "error": str(exc)}`` (ledger.py), and
    ``DuplicateProjectError``'s message is
    ``project already registered: '<key>'`` — no class name anywhere in it.
    Matching only on ``"DuplicateProject"``/``"duplicate"`` therefore never
    fired against a live backend, and every already-registered key came
    back as a hard failure. Observed on the sandbox VM 2026-08-15
    (core 5.183.0 / backend 5.74.0): the corpus re-key migration (since
    deleted) aborted with ``registry_seed_failed`` on the first
    same-basename collision, and — because the rows it had already created
    then collided on every retry — could not be resumed at all.

    The class-name patterns are kept for cross-version back-compat: a
    backend that does surface the class name stays supported.

    Only DUPLICATES are benign here. ``UnknownProjectError``
    (``unknown project_id: ...``) shares no fragment with these patterns
    and stays a failure, which is the behaviour the seed loop needs.
    """
    low = err.lower()
    return "duplicateproject" in low or "duplicate" in low or "already registered" in low


def seed_row(row: dict, *, auth_token: str) -> str:
    """Call ``create_project_row`` for one row over the backend /admin route.

    Returns ``"created"``, ``"skipped"``, or ``"failed"``. NEVER raises —
    the loop is fail-soft (one bad row must not abort the migration, per
    the plan's "Fail = log + continue" rule).
    """
    # Imported lazily so the import cost is paid only on use (the
    # ``--help`` path should not require httpx to be importable).
    from yadgar.core.forward import _forward_admin

    kind = infer_kind(row["project_id"])
    payload = {
        "key": row["project_id"],
        "kind": kind,
    }
    # Best-effort: surface the human-readable note in the registry row
    # so the operator can correlate later. ``create_project_row``
    # accepts ``display_name``; we use the note (truncated) as a
    # cheap-as-free annotation. Empty notes stay empty.
    #
    # ``project.display_name`` is VARCHAR(64) (alembic revision
    # ``003_project_registry``: ``sa.String(length=64)``). The original
    # code used ``note[:255]`` which silently let >64-char notes
    # through and tripped MariaDB ``DataError(1406)`` at INSERT time.
    # The 2026-08-20 incident showed this surfacing as ``failed: N``
    # with no per-row reason — task #241, car C8-3. Truncate to the
    # schema's actual width BEFORE the backend call, and log the
    # truncation reason so the daemon log carries the operator signal
    # the bare counter never did.
    note = row.get("note") or ""
    if note:
        if len(note) > 64:  # noqa: PLR2004 — schema length, not magic
            _logger.warning(
                "display_name truncated: project_id=%r note_len=%d -> 64",
                row["project_id"],
                len(note),
            )
            note = note[:64]
        payload["display_name"] = note

    try:
        result = _forward_admin(
            "create_project_row",
            payload,
            timeout_s=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"  FAIL: {row['project_id']}: backend call raised: {exc}",
            file=sys.stderr,
        )
        return "failed"

    # create_project_row returns ``{"ok": True, "row": ...}`` on
    # success and ``{"ok": False, "error": ...}`` on duplicate
    # (DuplicateProjectError caught at ledger.py:571-573) or any
    # other failure.
    if result.get("ok") is True:
        return "created"
    err = str(result.get("error", ""))
    if is_duplicate_project_error(err):
        return "skipped"
    print(f"  FAIL: {row['project_id']}: {err}", file=sys.stderr)
    return "failed"


def cmd_project_list(args: argparse.Namespace) -> int:
    """``yadgar project list [--stale]`` handler.

    With ``--stale``, calls ``list_stale_projects`` (the C11 op) and
    renders the rows the threshold flagged, ``last_validated_at`` included.
    Without ``--stale``, calls ``list_project_rows`` and renders every
    registered project — the same shape the backfill uses internally, which
    since task 384 no longer carries ``last_validated_at``.

    Failures print to stderr with a leading ``ERROR:`` marker and return
    1 — same convention as ``cmd_project_seed``. A zero-row result is NOT
    a failure; an operator running ``--stale`` and finding zero rows is
    the goal, not an error.
    """
    # Lazy import — the ``--help`` path should not require httpx.
    from yadgar.core.forward import _forward_admin

    auth_token = read_auth_token()
    if not auth_token:
        print(
            "  WARN: YADGAR_MCP_AUTH_TOKEN unset; backend may reject the request.",
            file=sys.stderr,
        )

    op = "list_stale_projects" if args.stale else "list_project_rows"
    payload: dict = {}
    try:
        result = _forward_admin(op, payload, timeout_s=30.0)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {op}: backend call raised: {exc}", file=sys.stderr)
        return 1

    # Both ops have the ``{"ok": False, "error": ...}`` failure shape.
    if result.get("ok") is False:
        print(
            f"ERROR: {op}: {result.get('error', 'unknown error')}",
            file=sys.stderr,
        )
        return 1

    if args.stale:
        rows = result.get("projects", [])
        threshold = result.get("threshold_days", "?")
        print(
            f"Stale project rows (threshold={threshold} days, count={len(rows)}):",
            file=sys.stderr,
        )
        for row in rows:
            last = row.get("last_validated_at") or "NEVER"
            print(
                f"  {row.get('key')}  kind={row.get('kind')}  last_validated_at={last}",
                file=sys.stderr,
            )
    else:
        # ``last_validated_at`` is NOT rendered here: task 384 dropped it from
        # the ``list_project_rows`` projection so the create gate that forwards
        # the same SELECT cannot be broken by an optional column. ``--stale``
        # is the surface for that value; it selects the column itself.
        rows = result.get("rows", [])
        print(f"Registered projects ({len(rows)}):", file=sys.stderr)
        for row in rows:
            print(
                f"  {row.get('key')}  kind={row.get('kind')}",
                file=sys.stderr,
            )

    # JSON to stdout for downstream consumption (jq, capture, etc.).
    print(json.dumps(result))
    return 0


def cmd_project_seed(args: argparse.Namespace) -> int:
    """``yadgar project seed`` handler.

    Walks the map file, calls ``create_project_row`` per seed-eligible
    row, and prints a per-row outcome. A backend error on one row does
    NOT abort the rest of the migration — the loop is still best-effort
    per row (a single typo'd project_id must not stop the others from
    seeding), and ``seed_row`` never raises. What DOES change is the
    final exit code: a genuine (non-duplicate) per-row failure now makes
    the run exit 1 (ledger task 13 defect 1 — this used to always return
    0 unless the map file itself was structurally malformed, so an
    operator reading the exit code alone never learned a row failed).
    Duplicates (idempotent re-run) are classified "skipped", not
    "failed", and do not trip this gate.
    """
    map_path = Path(args.map) if args.map else DEFAULT_MAP_PATH
    rows = parse_map(map_path)

    auth_token = read_auth_token()
    if not auth_token:
        # Not fatal: the backend may accept anonymous (local-socket)
        # calls. Surface a warning so the operator notices.
        print(
            "  WARN: YADGAR_MCP_AUTH_TOKEN unset; backend may reject the request.",
            file=sys.stderr,
        )

    counts = {"seed": 0, "drop": 0, "review": 0, "created": 0, "skipped": 0, "failed": 0}
    print(f"Seeding project registry from {map_path}", file=sys.stderr)
    for row in rows:
        kind = classify_row(row)
        counts[kind] += 1
        if kind != "seed":
            print(
                f"  SKIP ({kind}): {row['source_directory']} → {row['project_id']}",
                file=sys.stderr,
            )
            continue
        outcome = seed_row(row, auth_token=auth_token)
        counts[outcome] += 1
        marker = {
            "created": "OK  ",
            "skipped": "DUP ",
            "failed": "FAIL",
        }[outcome]
        print(
            f"  {marker}: {row['source_directory']} → {row['project_id']}",
            file=sys.stderr,
        )

    print(json.dumps(counts))
    if counts["failed"] > 0:
        return 1
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``project`` subcommand with the top-level CLI parser."""
    p = subparsers.add_parser(
        "project",
        help="Project-registry operator surface (Car A — 2026-08-14 train)",
    )
    project_sub = p.add_subparsers(dest="project_command", required=True)

    seed_p = project_sub.add_parser(
        "seed",
        help="Seed the engine-#2 ``project`` registry from a map TSV",
    )
    seed_p.add_argument(
        "--map",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to the project-id map TSV (default: ./.yadgar/project-id-map.tsv)",
    )
    seed_p.set_defaults(func=cmd_project_seed)

    # Car C11-#88 (task #88): staleness surface.
    # ``yadgar project list [--stale]`` — without the flag, every row;
    # with ``--stale``, rows older than ``YADGAR_PROJECT_STALENESS_DAYS``.
    list_p = project_sub.add_parser(
        "list",
        help="List registered projects (with --stale: rows older than the threshold)",
    )
    list_p.add_argument(
        "--stale",
        action="store_true",
        default=False,
        help="Show only rows whose last_validated_at is older than YADGAR_PROJECT_STALENESS_DAYS",
    )
    list_p.set_defaults(func=cmd_project_list)
