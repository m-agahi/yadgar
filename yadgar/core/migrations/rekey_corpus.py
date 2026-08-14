"""Car D (2026-08-14 identity train, §3) — corpus re-key migration.

Algorithm (accepted 2026-08-14, ``decision-corpus-rekey-migration-algorithm``):

  1. list all rows  ->  collect DISTINCT directory_context values
  2. per directory:  git?  ->  owner/repo        (from ``git remote get-url origin``)
                     else  ->  local/<basename>
  3. ensure the ``project`` registry has a row for each derived key
  4. THEN migrate the rows

This is a CORE-side operator script. The dry-run discovers
``directory_context`` values via the new ``rekey_discover_directories``
admin op (the layer-boundary contract forbids core from importing a
storage handle directly), derives ``project_id`` for each path locally
using the existing ``yadgar.core.identity`` helpers, and writes the
operator-reviewable map TSV. The apply step uses ``_forward_admin``
to call ``create_project_row`` per seed row and ``project_id_backfill``
once for the row-level UPDATE.

Public entry points:

  ``discover_directories()``     thin wrapper over the admin op — used
                                 by tests to inject fixtures.
  ``derive_project_id(directory)`` host-side derivation: ``.yadgar/project-id``
                                 upward walk first (ADR-0199 escape hatch),
                                 then ``git remote get-url origin`` (host
                                 excluded, trailing ``.git`` stripped, lower
                                 cased), then ``local/<basename>``.
  ``write_map(rows, path)``      write a TSV. Column 2 is the AUTHORITATIVE
                                 target — the operator edits the file, the
                                 script does not. Sentinels ('global',
                                 'system', 'unresolved') are pre-classified
                                 to ``DROP``; free-text prose to ``REVIEW``.
  ``apply_map(map_path)``        dry-run by default; with ``--apply`` calls
                                 ``create_project_row`` for each seed row
                                 and ``project_id_backfill`` once for the
                                 surviving mapping.
  ``run(...)``                   one-call orchestration: discover, write
                                 the map, optionally apply.

CLI surface lives in ``yadgar/core/cli/migrate.py`` (registered as
``yadgar migrate rekey [--map PATH] [--apply]``).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

#: Sentinels that name NO project. Carried verbatim from
#: ``_project_id_writer._NON_IDENTIFYING_PROJECT_IDS``. A directory_context
#: that IS one of these is NOT a mapping input — it is a delete / quarantine
#: decision (D3 'system' deletes, D4 'global'+_memify_derive deletes by
#: producer signature). Pre-classifying them in the map means the operator's
#: review is over the rows that have a real mapping story.
_NON_IDENTIFYING_DIRECTORY_VALUES: frozenset[str] = frozenset(
    {"", "global", "unresolved", "system"}
)

#: Default map path. The repo gitignores ``.yadgar/`` so the file never
#: lands in git (Car A module docstring).
DEFAULT_MAP_PATH = Path.cwd() / ".yadgar" / "project-id-map.tsv"

#: ``DROP`` is the column-2 value that tells ``apply_map`` the row is a
#: delete-cohort decision, not a seed. Mirrors project.py:24.
DROP_MARKER = "DROP"

#: Sentry for a row that was free-text prose in the corpus (its
#: directory_context was used as a description, not a path). Prose rows
#: are NEVER seed candidates — there is no project they could belong to —
#: so the auto-derivation writes ``REVIEW`` and the operator decides.
REVIEW_MARKER = "REVIEW"

#: Regex to decide if a directory string LOOKS like a path. The corpus
#: mixes real paths (``/home/max/git/yadgar``) with free-text prose
#: (``db_inspect``, 2026-08-10). The prose rows get REVIEW, not a
#: fabricated local/<basename>.
_PATH_LIKE_RE = re.compile(r"^[/~.]")


class _CollisionEntry(TypedDict):
    """Per-basename collision record — TypedDict so mypy 2.3 accepts
    the literal cleanly (the bare ``dict[str, list[str]]`` literal in
    a list comprehension triggers a known ``dict-item`` false positive
    when the values come from a ``defaultdict[str, list[str]]``).
    Runtime shape is unchanged: ``{"basename": str, "paths": list[str]}``."""

    basename: str
    paths: list[str]


@dataclass(frozen=True)
class DirectoryRow:
    """One row in the discovered directory_context set.

    Attributes:
        directory_context: the literal value found on at least one corpus row.
        memory_rows: count of ``memory`` rows carrying this value.
        wiki_rows: count of ``wiki_page`` rows carrying this value.
        derived_project_id: result of ``derive_project_id`` (empty when
            the row is a sentinel or prose — see ``kind``).
        kind: ``"git"``, ``"local"``, ``"sentinel"``, or ``"prose"``.
        sentinel: the value when ``kind == "sentinel"`` (``"global"`` /
            ``"system"`` / ``"unresolved"``); empty otherwise.
    """

    directory_context: str
    memory_rows: int
    wiki_rows: int
    derived_project_id: str
    kind: str
    sentinel: str = ""


@dataclass
class MigrationReport:
    """The grouped, per-bucket report a dry run returns.

    Mirrors the structure of project_id_backfill's manifest so an
    operator reviewing the dry run sees the same shape both tools
    produce.
    """

    discovered: int = 0
    seed_rows: list[DirectoryRow] = field(default_factory=list)
    drop_rows: list[DirectoryRow] = field(default_factory=list)
    review_rows: list[DirectoryRow] = field(default_factory=list)
    basenames_collide: list[_CollisionEntry] = field(default_factory=list)
    applied: bool = False
    applied_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "discovered": self.discovered,
            "seed_rows": [asdict(r) for r in self.seed_rows],
            "drop_rows": [asdict(r) for r in self.drop_rows],
            "review_rows": [asdict(r) for r in self.review_rows],
            "basenames_collide": self.basenames_collide,
            "applied": self.applied,
            "applied_counts": self.applied_counts,
        }


# ── corpus read ─────────────────────────────────────────────────────────────


@observe(tier="stage", metric="core.migrations.rekey.discover")
def discover_directories(counts: dict[str, dict[str, int]] | None = None) -> list[DirectoryRow]:
    """Build the per-directory bucketed list from raw counts.

    Two ways in:

    * Production: ``counts=None`` — call ``_forward_admin`` to get the
      ``rekey_discover_directories`` op result.
    * Tests: pass a fixture ``{directory_context: {memory_rows, wiki_rows}}``
      directly; no network round-trip.

    Pure-Python derivation over the counts follows; no further reads.
    """
    if counts is None:
        result = _forward_admin("rekey_discover_directories", {}, timeout_s=60.0)
        if not result.get("ok"):
            return []
        counts = result.get("counts", {}) or {}

    out: list[DirectoryRow] = []
    for dc, bucket in sorted(counts.items()):
        project_id, kind, sentinel = derive_project_id(dc)
        out.append(
            DirectoryRow(
                directory_context=dc,
                memory_rows=bucket.get("memory_rows", 0),
                wiki_rows=bucket.get("wiki_rows", 0),
                derived_project_id=project_id,
                kind=kind,
                sentinel=sentinel,
            )
        )
    return out


# ── per-directory derivation ────────────────────────────────────────────────


@observe(tier="stage", span=False, metric="core.migrations.rekey.derive")
def derive_project_id(directory: str) -> tuple[str, str, str]:
    """Map a directory string to a (project_id, kind, sentinel) triple.

    Mirrors the decision-page algorithm. Empty string + prose paths go
    to ``(""``, ``"prose"``, ``"")`` — the caller's job to surface them
    in the REVIEW bucket. Sentinels go to ``(""``, ``"sentinel"``,
    ``<sentinel-name>``) so the map writer knows to emit ``DROP`` and
    the operator's review is not over noise.

    Order of resolution (mirrors ``mint_project_id`` minus the raise):
      1. ``.yadgar/project-id`` upward walk (operator-set override)
      2. ``git remote get-url origin`` → ``owner/repo`` (host excluded)
      3. ``local/<basename>``
    """
    from yadgar.core.identity import (  # noqa: PLC0415
        _normalise_remote,
        _origin_remote,
        _walk_project_id_file,
    )

    if directory in _NON_IDENTIFYING_DIRECTORY_VALUES:
        return ("", "sentinel", directory)

    if not _PATH_LIKE_RE.match(directory):
        # Free-text prose used as a description; never fabricate a
        # local/<basename> key out of a phrase.
        return ("", "prose", "")

    # 1. .yadgar/project-id upward walk — operator override.
    project_id_file = _walk_project_id_file(directory)
    if project_id_file:
        kind = "local" if project_id_file.startswith("local/") else "git"
        return (project_id_file, kind, "")

    # 2. git remote get-url origin → owner/repo.
    remote = _origin_remote(directory)
    if remote:
        return (_normalise_remote(remote), "git", "")

    # 3. local/<basename>.
    basename = Path(directory).name or "root"
    return (f"local/{basename}", "local", "")


# ── basename collision detector ─────────────────────────────────────────────


@observe(tier="stage", span=False, metric="core.migrations.rekey.collisions")
def detect_basenames_collide(rows: Iterable[DirectoryRow]) -> list[_CollisionEntry]:
    """Return a list of basenames that map to two or more paths.

    Standing risk the user accepted (memory 534404): two same-basename
    directories in different paths collide on one ``local/<basename>``
    key. The migration must REPORT any basename mapping to two paths
    so the blast radius is visible before it is chosen.

    Empty list = no collisions. The dry-run report includes this list
    so the operator can see it before any write.
    """
    by_basename: defaultdict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row.kind != "local" or not row.derived_project_id.startswith("local/"):
            continue
        if not _PATH_LIKE_RE.match(row.directory_context):
            continue
        base = Path(row.directory_context).name
        by_basename[base].append(row.directory_context)
    out: list[_CollisionEntry] = []
    for base, paths in sorted(by_basename.items()):
        if len(paths) <= 1:
            continue
        sorted_paths: list[str] = list(paths)
        sorted_paths.sort()
        out.append(_CollisionEntry(basename=base, paths=sorted_paths))
    return out


# ── map I/O ─────────────────────────────────────────────────────────────────


@observe(tier="stage", metric="core.migrations.rekey.write_map")
def write_map(rows: Iterable[DirectoryRow], path: Path, *, force: bool = False) -> None:
    """Write the project-id map TSV.

    Column layout (tab-separated, comment lines start with ``#``):
      1. ``source_directory``        — the literal directory_context value
      2. ``project_id``              — the AUTHORITATIVE target (``DROP``,
                                       ``REVIEW``, or the derived key)
      3. ``memory_rows``             — count of memory rows
      4. ``wiki_rows``               — count of wiki_page rows
      5. ``note``                    — operator annotation, empty by default

    Sentinels (kind=``sentinel``) get ``DROP`` in column 2 unconditionally.
    Free-text prose (kind=``prose``) gets ``REVIEW`` — there is no key it
    can map to, and the operator decides. Real paths get the derived
    ``owner/repo`` or ``local/<basename>`` key.

    Refuses to overwrite an existing map unless ``force=True`` — column 2
    is authoritative over derivation, so silently clobbering a hand-edited
    map is a destructive action and the script asks before doing it.
    """
    if path.exists() and not force:
        print(
            f"ERROR: map file exists: {path} (use --force to overwrite)",
            file=sys.stderr,
        )
        raise SystemExit(2)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write(
            "# project-id map — Car D, 2026-08-14 train\n"
            "# Column 2 is AUTHORITATIVE over any derivation.\n"
            "# Sentinels are pre-classified to DROP; free-text prose to REVIEW.\n"
            "# Edit column 2 to retarget a row before running --apply.\n"
        )
        for row in rows:
            target = _map_target(row)
            f.write(
                "\t".join(
                    [
                        row.directory_context,
                        target,
                        str(row.memory_rows),
                        str(row.wiki_rows),
                        "",  # note — operator fills in by hand
                    ]
                )
                + "\n"
            )


@observe(tier="hot", span=False, metric="core.migrations.rekey.map_target")
def _map_target(row: DirectoryRow) -> str:
    """Pick the column-2 value for a DirectoryRow."""
    if row.kind == "sentinel":
        return DROP_MARKER
    if row.kind == "prose":
        return REVIEW_MARKER
    return row.derived_project_id or REVIEW_MARKER


# ── apply ───────────────────────────────────────────────────────────────────


@observe(tier="boundary", metric="core.migrations.rekey.apply")
def apply_map(map_path: Path, *, confirm: bool = False) -> dict:
    """Read the map, ensure registry rows, then run the backfill.

    Dry-run by default: returns the plan and writes nothing. With
    ``confirm=True`` the function:
      1. calls ``create_project_row`` (via ``_forward_admin``) for every
         seed row (idempotent; duplicates are reported as ``skipped``
         and not an error).
      2. builds the ``{directory_context: project_id}`` mapping the
         C6 backfill expects, dropping DROP/REVIEW rows.
      3. delegates to ``project_id_backfill(dry_run=False, …)`` for the
         row-level UPDATE.
    """
    from yadgar.core.cli.project import parse_map

    parsed = parse_map(map_path)
    counts = {
        "seed_attempted": 0,
        "created": 0,
        "skipped": 0,
        "failed": 0,
        "drop": 0,
        "review": 0,
        "rows_updated": 0,
        "rows_quarantined": 0,
    }

    if not confirm:
        # Dry-run path: parse, count buckets, return.
        for row in parsed:
            if row["project_id"] == DROP_MARKER:
                counts["drop"] += 1
            elif row["project_id"] == REVIEW_MARKER:
                counts["review"] += 1
            else:
                counts["seed_attempted"] += 1
        return {"ok": True, "applied": False, "dry_run": True, "counts": counts}

    # Live path — seed the registry first, then migrate the rows.
    # All backend ops go through ``_forward_admin``; auth token is read
    # by the forwarder itself, not here.
    mapping: dict[str, str] = {}
    for row in parsed:
        if row["project_id"] == DROP_MARKER:
            counts["drop"] += 1
            continue
        if row["project_id"] == REVIEW_MARKER:
            counts["review"] += 1
            continue

        result = _forward_admin(
            "create_project_row",
            {
                "key": row["project_id"],
                "kind": ("local" if row["project_id"].startswith("local/") else "git"),
                **({"display_name": row["note"][:255]} if row.get("note") else {}),
            },
            timeout_s=30.0,
        )
        outcome = _classify_registry_result(result)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome in ("created", "skipped"):
            mapping[row["source_directory"]] = row["project_id"]
        elif outcome == "failed":
            return {
                "ok": False,
                "reason": "registry_seed_failed",
                "failed_project_id": row["project_id"],
                "counts": counts,
            }

    # Hand off to the existing C6 backfill. The dry_run flag is False
    # because the operator has confirmed at THIS level; the backfill
    # still re-checks for unknown targets and unconfirmed deletes and
    # refuses cleanly if anything is amiss.
    manifest = _forward_admin(
        "project_id_backfill",
        {
            "mapping": mapping,
            "dry_run": False,
            "quarantine_unmapped": False,
            "confirm_deletes": True,
        },
        timeout_s=120.0,
    )

    totals = manifest.get("totals", {}) if isinstance(manifest, dict) else {}
    counts["rows_updated"] = totals.get("rows_updated", 0)
    counts["rows_quarantined"] = totals.get("rows_quarantined", 0)

    return {
        "ok": manifest.get("ok", False),
        "applied": manifest.get("applied", False),
        "manifest": manifest,
        "counts": counts,
    }


@observe(tier="stage", span=False, metric="core.migrations.rekey.classify_registry_result")
def _classify_registry_result(result: dict) -> str:
    """Reduce ``create_project_row``'s envelope to ``created/skipped/failed``."""
    if result.get("ok") is True:
        return "created"
    err = str(result.get("error", ""))
    if "DuplicateProject" in err or "duplicate" in err.lower():
        return "skipped"
    return "failed"


# ── forwarder (mirrors project.py's seam) ───────────────────────────────────


@observe(tier="boundary", metric="core.migrations.rekey.forward_admin")
def _forward_admin(op: str, payload: dict, *, timeout_s: float = 30.0) -> dict:
    """Thin pass-through to ``yadgar.core.forward._forward_admin``.

    Indirection exists so tests can patch the migration's HTTP seam
    without reaching across modules. The actual failure-mode contract
    (RuntimeError when the daemon is unreachable) lives in
    ``yadgar.core.forward``.
    """
    from yadgar.core.forward import _forward_admin as _real  # noqa: PLC0415

    return _real(op, payload, timeout_s=timeout_s)


# ── top-level orchestration ─────────────────────────────────────────────────


@observe(tier="stage", metric="core.migrations.rekey.generate_report")
def generate_report(rows: list[DirectoryRow]) -> MigrationReport:
    """Bucket the discovered rows into seed/drop/review + collision report."""
    report = MigrationReport(discovered=len(rows))
    for row in rows:
        if row.kind == "sentinel":
            report.drop_rows.append(row)
        elif row.kind == "prose":
            report.review_rows.append(row)
        else:
            report.seed_rows.append(row)
    report.basenames_collide = detect_basenames_collide(rows)
    return report


@observe(tier="stage", metric="core.migrations.rekey.run")
def run(
    counts: dict[str, dict[str, int]] | None = None,
    *,
    map_path: Path = DEFAULT_MAP_PATH,
    apply: bool = False,
    force: bool = False,
) -> dict:
    """One-call orchestration: discover, write the map, optionally apply.

    Returns a JSON-serialisable dict suitable for ``json.dumps`` on the
    CLI handler. With ``apply=False`` (the default) the only write is
    the map TSV itself; no storage writes, no registry writes.

    ``counts`` accepts a fixture (used by tests) so the discovery seam
    is replaceable without monkey-patching ``_forward_admin``.
    """
    rows = discover_directories(counts=counts)
    if not rows:
        return {"ok": False, "reason": "no_directories"}

    report = generate_report(rows)

    if not map_path.exists() or force:
        write_map(rows, map_path, force=force)

    result: dict[str, Any] = {
        "ok": True,
        "map": str(map_path),
        "report": report.to_dict(),
    }
    if apply:
        apply_result = apply_map(map_path, confirm=True)
        result["apply"] = apply_result
        report.applied = bool(apply_result.get("applied"))
        report.applied_counts = apply_result.get("counts", {})
    result["report"] = report.to_dict()
    return result


# ── CLI (script-level invocation) ───────────────────────────────────────────


@observe(tier="hot", span=False, metric="core.migrations.rekey.build_parser")
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yadgar-core-migrations-rekey-corpus",
        description="Car D — corpus re-key migration. Dry-run by default.",
    )
    p.add_argument(
        "--map",
        type=Path,
        default=DEFAULT_MAP_PATH,
        help="Path to the project-id map TSV (default: ./.yadgar/project-id-map.tsv)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration (requires --map to exist; fails loud on registry errors)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing map file. --force never overrides the apply guard.",
    )
    return p


@observe(tier="boundary", metric="core.migrations.rekey.main")
def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.apply and not args.map.exists():
        print(
            f"ERROR: --apply requires an existing map file; generate one first at {args.map}",
            file=sys.stderr,
        )
        return 2
    result = run(map_path=args.map, apply=args.apply, force=args.force)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
