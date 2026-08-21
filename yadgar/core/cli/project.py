"""``yadgar project ...`` subcommand — project-registry operator surface.

Car A (2026-08-14 identity train, plan §2). Closes the gap where
``backend.admin_exec.ledger.create_project_row`` exists and is registered
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


def read_auth_token() -> str:
    """Read YADGAR_MCP_AUTH_TOKEN via the canonical resolver.

    Thin pass-through to ``yadgar.core.install.auth_token.resolve_auth_token``
    — the same chokepoint the seed subcommand routes through. The auth-token
    pattern lint hard-fails any hand-rolled ``os.environ.get("YADGAR_MCP_AUTH_TOKEN", ...)``
    so this stays a one-liner, not a copy.
    """
    from yadgar.core.install.auth_token import resolve_auth_token  # noqa: PLC0415

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
    (core 5.183.0 / backend 5.74.0): ``yadgar migrate rekey --apply``
    aborted with ``registry_seed_failed`` on the first same-basename
    collision, and — because the rows it had already created then
    collided on every retry — could not be resumed at all.

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
    from yadgar.core.forward import _forward_admin  # noqa: PLC0415

    kind = infer_kind(row["project_id"])
    payload = {
        "key": row["project_id"],
        "kind": kind,
    }
    # Best-effort: surface the human-readable note in the registry row
    # so the operator can correlate later. ``create_project_row``
    # accepts ``display_name``; we use the note (truncated) as a
    # cheap-as-free annotation. Empty notes stay empty.
    if row.get("note"):
        payload["display_name"] = row["note"][:255]

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
