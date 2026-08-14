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
                                 script does not. See ``_map_target`` for the
                                 per-sentinel classification.

THE ``global`` SENTINEL IS TWO DECISIONS, NOT ONE
-------------------------------------------------
This module originally classified EVERY sentinel to ``DROP``. For ``system``
that is right (D3, user decision verbatim: "d3. delete"). For ``global`` it
silently reversed a user decision, because ``apply_map`` omits ``DROP`` rows
from the mapping and the ``global`` key therefore never reached
``project_id_backfill``:

  * D4 — DELETE only the ``_memify_derive`` sub-cohort, scoped by PRODUCER
    SIGNATURE (tags ``derived`` + ``auto-generated`` AND content matching
    "are frequently modified together"), never by ``directory_context`` alone.
  * Decision G — the REMAINDER gets ``project_id = local/aws-work`` PLUS the
    ``global`` reach tag. Owner and reach are separate axes (§1.4).

The backend already implemented both (``_is_memify_global``, and
``_plan_updates``' ``add_global_tag`` keyed on the sentinel). What the fix
adds is the CORE side handing it a mapping that contains ``global`` at all.

WHERE THE SPLIT LIVES
---------------------
The D4 discriminator is a CONTENT+TAGS predicate, so a directory-keyed map
cannot express it and the host cannot compute it. So: the PREDICATE stays
backend-side (single source of truth — ``_is_memify_global``), the COUNT
arrives from the discovery op, and the map grows a per-cohort ROW
(``global::memify`` / ``global::rest``) whose only job is to make the
destructive decision visible on its own reviewable line. The cohort suffix
is a map-file encoding: ``directory_context`` stays the literal ``"global"``
everywhere else, and ``apply_map`` splits it off before building the mapping.

KNOWN LIMITATION: a ``directory_context`` of ``""`` would write an empty
map column 1, which ``parse_map`` rejects as malformed. Measured 2026-08-14:
zero such rows in the live corpus, and the backfill refuses on them anyway
(``rows_without_a_directory_context``, no acknowledgement flag).
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
#: that IS one of these is NOT a mapping input — but the three of them get
#: three DIFFERENT answers, and collapsing them under one marker is the
#: defect this module shipped with (see ``_map_target``).
_NON_IDENTIFYING_DIRECTORY_VALUES: frozenset[str] = frozenset(
    {"", "global", "unresolved", "system"}
)

#: The sentinel that means "every project" rather than a path. Mirrors
#: ``backend/admin_exec/project_backfill.GLOBAL_SENTINEL``.
GLOBAL_SENTINEL = "global"

#: The sentinel whose WHOLE class is deleted — D3, user decision verbatim
#: ("d3. delete"). Matched backend-side by ``_is_system``.
SYSTEM_SENTINEL = "system"

#: ``global``'s two cohorts. The sentinel is NOT one decision:
#:
#:   ``memify`` — D4 (plan §5.C6 D4): DELETE the ``_memify_derive`` rows,
#:       scoped by PRODUCER SIGNATURE (tags ``derived`` AND ``auto-generated``
#:       AND content matching *"are frequently modified together"*), never by
#:       ``directory_context`` alone.
#:   ``rest``   — Decision G (plan §1.5 G): ``project_id = local/aws-work``
#:       PLUS the ``global`` reach tag. Owner and reach are SEPARATE axes
#:       (§1.4), hence BOTH, not either.
MEMIFY_COHORT = "memify"
REST_COHORT = "rest"

#: Decision G's target. ``local/`` is the correct form — verified that
#: ``/home/max/aws-work`` has no git remote. Column 2 of the map stays
#: authoritative over this: an operator retargeting the ``global::rest``
#: row is honoured, this constant only seeds the generated map.
GLOBAL_COHORT_TARGET = "local/aws-work"

#: The (directory_context, cohort) pairs whose deletion is driven by a
#: BACKEND PREDICATE (``project_backfill._is_system`` / ``_is_memify_global``)
#: rather than by the mapping. Their map rows are the operator's confirmation
#: gate, not a retarget surface — see ``apply_map``.
_DELETE_COHORT_KEYS: frozenset[tuple[str, str]] = frozenset(
    {(SYSTEM_SENTINEL, ""), (GLOBAL_SENTINEL, MEMIFY_COHORT)}
)

#: Separator between a directory_context and its cohort in map column 1.
#: The composed form (``global::memify``) exists ONLY in the file; the
#: literal ``directory_context`` is what reaches the backend mapping.
COHORT_SEP = "::"

#: A hand-edited map may annotate the reach axis inline
#: (``local/aws-work +TAG:global``). Tolerant read, strict write: the
#: generator emits the clean registry key, but the suffix is stripped rather
#: than seeding a project literally named ``local/aws-work +TAG:global``.
#: The tag itself is NOT the map's decision — ``_plan_updates``'
#: ``add_global_tag``, keyed on the sentinel, is the single source of truth.
_TAG_SUFFIX_RE = re.compile(r"(?:\s*\+TAG:[\w.-]+)+\s*$")

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
        cohort: sub-class of a sentinel that carries its own decision —
            ``"memify"`` / ``"rest"`` for ``global``. Empty for every row
            that is one whole decision. ``directory_context`` stays the
            LITERAL corpus value on every cohort row; the composed
            ``global::memify`` form exists only in map column 1.
        cohort_counts_known: False when the row is a cohort whose size the
            backend did not report. The split is still correct (the delete
            is predicate-driven backend-side), but the review numbers
            under-report — which on a DESTRUCTIVE cohort must be visible
            rather than read as "zero rows die".
    """

    directory_context: str
    memory_rows: int
    wiki_rows: int
    derived_project_id: str
    kind: str
    sentinel: str = ""
    cohort: str = ""
    cohort_counts_known: bool = True

    @property
    def map_key(self) -> str:
        """Column-1 value: ``directory_context`` or ``dc::cohort``."""
        return (
            f"{self.directory_context}{COHORT_SEP}{self.cohort}"
            if self.cohort
            else self.directory_context
        )


@dataclass
class MigrationReport:
    """The grouped, per-bucket report a dry run returns.

    Mirrors the structure of project_id_backfill's manifest so an
    operator reviewing the dry run sees the same shape both tools
    produce.
    """

    discovered: int = 0
    rows_emitted: int = 0
    cohort_counts_available: bool = True
    seed_rows: list[DirectoryRow] = field(default_factory=list)
    drop_rows: list[DirectoryRow] = field(default_factory=list)
    review_rows: list[DirectoryRow] = field(default_factory=list)
    basenames_collide: list[_CollisionEntry] = field(default_factory=list)
    applied: bool = False
    applied_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "discovered": self.discovered,
            "rows_emitted": self.rows_emitted,
            "cohort_counts_available": self.cohort_counts_available,
            "seed_rows": [asdict(r) for r in self.seed_rows],
            "drop_rows": [asdict(r) for r in self.drop_rows],
            "review_rows": [asdict(r) for r in self.review_rows],
            "basenames_collide": self.basenames_collide,
            "applied": self.applied,
            "applied_counts": self.applied_counts,
        }


# ── corpus read ─────────────────────────────────────────────────────────────


@observe(tier="stage", metric="core.migrations.rekey.discover")
def discover_directories(
    counts: dict[str, dict[str, int]] | None = None,
    cohorts: dict[str, dict[str, int]] | None = None,
) -> list[DirectoryRow]:
    """Build the per-directory bucketed list from raw counts.

    Two ways in:

    * Production: ``counts=None`` — call ``_forward_admin`` to get the
      ``rekey_discover_directories`` op result (counts AND cohorts).
    * Tests: pass fixture dicts directly; no network round-trip.

    ``cohorts`` carries the sub-counts the host cannot compute: D4's
    discriminator is a CONTENT+TAGS predicate, so a per-directory count says
    nothing about how ``global`` splits. Shape:
    ``{"memify_global": {"memory_rows": N, "wiki_rows": 0}}``.

    Pure-Python derivation over the counts follows; no further reads.
    """
    if counts is None:
        result = _forward_admin("rekey_discover_directories", {}, timeout_s=60.0)
        if not result.get("ok"):
            return []
        counts = result.get("counts", {}) or {}
        if cohorts is None:
            cohorts = result.get("cohorts", {}) or {}

    out: list[DirectoryRow] = []
    for dc, bucket in sorted(counts.items()):
        project_id, kind, sentinel = derive_project_id(dc)
        row = DirectoryRow(
            directory_context=dc,
            memory_rows=bucket.get("memory_rows", 0),
            wiki_rows=bucket.get("wiki_rows", 0),
            derived_project_id=project_id,
            kind=kind,
            sentinel=sentinel,
        )
        if sentinel == GLOBAL_SENTINEL:
            out.extend(_split_global_cohorts(row, cohorts or {}))
        else:
            out.append(row)
    return out


@observe(tier="stage", span=False, metric="core.migrations.rekey.split_global")
def _split_global_cohorts(
    row: DirectoryRow, cohorts: dict[str, dict[str, int]]
) -> list[DirectoryRow]:
    """Turn the one ``global`` row into its two decisions.

    The sentinel carries a delete cohort (D4) and a keep cohort (Decision G),
    and the map is the operator's review surface — so the destructive one gets
    its own line rather than hiding inside an aggregate. One row plus a note
    would be behaviourally identical (the backend predicate is authoritative
    either way); two rows is chosen so the delete is visible.

    ``directory_context`` stays the literal ``"global"`` on BOTH rows: it is
    the key the backend mapping is built from, and ``detect_basenames_collide``
    reads it too. Only map column 1 carries the composed form.
    """
    memify = cohorts.get("memify_global") or {}
    known = bool(memify)
    memify_memory = int(memify.get("memory_rows", 0) or 0)
    memify_wiki = int(memify.get("wiki_rows", 0) or 0)
    if not known:
        logger.warning(
            "rekey: the discovery op reported no cohort counts — the "
            "'%s%s%s' row will UNDER-REPORT the rows D4 deletes. The split "
            "itself is unaffected (the delete is predicate-driven backend-"
            "side); only the review numbers are missing.",
            GLOBAL_SENTINEL,
            COHORT_SEP,
            MEMIFY_COHORT,
        )
    return [
        DirectoryRow(
            directory_context=row.directory_context,
            memory_rows=memify_memory,
            wiki_rows=memify_wiki,
            derived_project_id="",
            kind=row.kind,
            sentinel=row.sentinel,
            cohort=MEMIFY_COHORT,
            cohort_counts_known=known,
        ),
        DirectoryRow(
            directory_context=row.directory_context,
            memory_rows=max(row.memory_rows - memify_memory, 0),
            wiki_rows=max(row.wiki_rows - memify_wiki, 0),
            derived_project_id=GLOBAL_COHORT_TARGET,
            kind=row.kind,
            sentinel=row.sentinel,
            cohort=REST_COHORT,
            cohort_counts_known=known,
        ),
    ]


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
            "# Edit column 2 to retarget a row before running --apply.\n"
            "#\n"
            "# Column 1 may carry a COHORT suffix (dir::cohort) when one\n"
            "# directory_context holds more than one decision:\n"
            f"#   {GLOBAL_SENTINEL}{COHORT_SEP}{MEMIFY_COHORT}\tD4 — DELETE the "
            "_memify_derive rows (matched backend-side\n"
            "#                  \tby producer signature: tags derived +\n"
            "#                  \tauto-generated AND content 'are frequently\n"
            "#                  \tmodified together'). CURRENTLY READABLE — a real\n"
            "#                  \tbehaviour change, not a cleanup.\n"
            f"#   {GLOBAL_SENTINEL}{COHORT_SEP}{REST_COHORT}\tDecision G — the remainder gets this "
            "project_id as its\n"
            "#                  \tOWNER and additionally keeps the 'global' reach\n"
            "#                  \ttag (added backend-side). Owner and reach are\n"
            "#                  \tseparate axes — it is BOTH, not either.\n"
            "#\n"
            "# DROP on a cohort row above is the operator's CONFIRMATION of a\n"
            "# backend-predicate delete; retargeting it makes --apply refuse\n"
            "# rather than proceed with a decision it cannot honour.\n"
            "# Free-text prose and undecided sentinels get REVIEW.\n"
        )
        for row in rows:
            target = _map_target(row)
            f.write(
                "\t".join(
                    [
                        row.map_key,
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
    """Pick the column-2 value for a DirectoryRow.

    The sentinels get THREE different answers, not one. Sending all of them
    to ``DROP`` — which this function used to do — conflated three unrelated
    facts under one marker, and on ``global`` it silently reversed a user
    decision:

      ``system``      DROP. D3, verbatim "d3. delete". A real backend delete
                      cohort (``_is_system``) matches these rows, so DROP
                      here means "excluded from the mapping, deleted by
                      predicate" — which is what the operator expects.
      ``global``      SPLITS. D4's producer cohort drops; Decision G gives the
                      REMAINDER ``local/aws-work`` plus the ``global`` reach
                      tag (added backend-side, keyed on the sentinel).
      ``unresolved``  REVIEW, not DROP. NO backend cohort matches it, so DROP
                      would merely omit it from the mapping and the backfill
                      would then refuse with ``unreviewed_directory_contexts``
                      — a confusing way to say "nobody decided". REVIEW says
                      that directly. (Measured 2026-08-14: zero live rows.)
      ``""``          REVIEW, same reasoning. See the module docstring's note
                      on the empty-column-1 limitation.
    """
    if row.kind == "sentinel":
        if row.sentinel == SYSTEM_SENTINEL:
            return DROP_MARKER
        if row.sentinel == GLOBAL_SENTINEL:
            if row.cohort == MEMIFY_COHORT:
                return DROP_MARKER
            return row.derived_project_id or REVIEW_MARKER
        return REVIEW_MARKER
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

    # Column 2 must be authoritative IN FACT, not just in the header comment.
    # The delete cohorts are matched by a BACKEND PREDICATE, so an operator who
    # retargets one would watch those rows die anyway. Refuse instead — before
    # any write of any kind, on the dry-run path too.
    retargeted = _retargeted_delete_cohorts(parsed)
    if retargeted:
        return {
            "ok": False,
            "reason": "delete_cohort_retargeted",
            "retargeted": retargeted,
            "detail": (
                "these rows name a cohort whose deletion is driven by a backend "
                "predicate, not by this mapping — retargeting column 2 cannot "
                "keep them. Restore DROP, or change the cohort backend-side."
            ),
            "counts": counts,
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
        directory_context, _cohort = _split_cohort_key(row["source_directory"])
        target = _strip_tag_suffix(row["project_id"])
        if target == DROP_MARKER:
            counts["drop"] += 1
            continue
        if target == REVIEW_MARKER:
            counts["review"] += 1
            continue

        result = _forward_admin(
            "create_project_row",
            {
                "key": target,
                "kind": ("local" if target.startswith("local/") else "git"),
                **({"display_name": row["note"][:255]} if row.get("note") else {}),
            },
            timeout_s=30.0,
        )
        outcome = _classify_registry_result(result)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome in ("created", "skipped"):
            # The LITERAL directory_context is the backend's key — the cohort
            # suffix is a map-file encoding and must never reach the mapping.
            mapping[directory_context] = target
        elif outcome == "failed":
            return {
                "ok": False,
                "reason": "registry_seed_failed",
                "failed_project_id": target,
                "counts": counts,
            }

    # Hand off to the existing C6 backfill. The dry_run flag is False
    # because the operator has confirmed at THIS level; the backfill
    # still re-checks for unknown targets and unconfirmed deletes and
    # refuses cleanly if anything is amiss.
    #
    # ``confirm_deletes`` is DERIVED from the reviewed map rather than
    # hardcoded True: a DROP-marked delete-cohort row IS the confirmation.
    # A map carrying no delete cohort at all confirms nothing.
    manifest = _forward_admin(
        "project_id_backfill",
        {
            "mapping": mapping,
            "dry_run": False,
            "quarantine_unmapped": False,
            "confirm_deletes": _confirms_deletes(parsed),
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


@observe(tier="hot", span=False, metric="core.migrations.rekey.split_cohort_key")
def _split_cohort_key(source_directory: str) -> tuple[str, str]:
    """``"global::memify"`` → ``("global", "memify")``; anything else unchanged.

    Split ONCE, at the ``parse_map`` boundary — the composed form is a map
    file encoding and nothing downstream of here should ever see it.

    ``::`` is a separator ONLY for the cohorts this module defines. ``apply_map``
    reads a possibly hand-edited file, and REVIEW rows exist precisely so an
    operator can retarget them — including the free-text-prose values the plan
    counts 18 of. Splitting on any ``::`` would silently truncate such a key
    into a mapping the corpus does not contain: fail-safe in outcome (the
    backfill reports it unmapped and refuses) but silent at this seam, which
    is the property worth keeping.
    """
    if COHORT_SEP not in source_directory:
        return (source_directory, "")
    directory_context, _sep, cohort = source_directory.partition(COHORT_SEP)
    if cohort not in (MEMIFY_COHORT, REST_COHORT):
        return (source_directory, "")
    return (directory_context, cohort)


@observe(tier="hot", span=False, metric="core.migrations.rekey.strip_tag_suffix")
def _strip_tag_suffix(target: str) -> str:
    """Drop a trailing ``+TAG:<name>`` annotation from a column-2 value.

    Tolerant read, strict write. ``write_map`` emits the clean registry key;
    this exists so a hand-edited map that spells Decision G's two axes inline
    (``local/aws-work +TAG:global``) still seeds ``local/aws-work`` rather
    than a project literally named with the annotation. The tag is applied
    backend-side from the sentinel — the map never carried that decision.
    """
    return _TAG_SUFFIX_RE.sub("", target).strip()


@observe(tier="stage", span=False, metric="core.migrations.rekey.retargeted_cohorts")
def _retargeted_delete_cohorts(parsed: Iterable[dict]) -> list[str]:
    """Return the delete-cohort map keys whose column 2 is no longer ``DROP``.

    Non-empty means the map asks for something the apply cannot deliver: the
    cohort's rows are matched by ``_is_system`` / ``_is_memify_global``
    backend-side and would be deleted regardless of what column 2 says.
    """
    out: list[str] = []
    for row in parsed:
        key = _split_cohort_key(row["source_directory"])
        if key in _DELETE_COHORT_KEYS and _strip_tag_suffix(row["project_id"]) != DROP_MARKER:
            out.append(row["source_directory"])
    return out


@observe(tier="stage", span=False, metric="core.migrations.rekey.confirms_deletes")
def _confirms_deletes(parsed: Iterable[dict]) -> bool:
    """True when the reviewed map carries a DROP-marked delete cohort.

    Callers reach this only after ``_retargeted_delete_cohorts`` came back
    empty, so any delete-cohort row present is a DROP row — the operator's
    confirmation. No delete cohort in the map means nothing to confirm, and
    the backfill's own gate stays armed.
    """
    return any(_split_cohort_key(row["source_directory"]) in _DELETE_COHORT_KEYS for row in parsed)


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
    """Bucket the discovered rows into seed/drop/review + collision report.

    Buckets are derived from ``_map_target`` rather than from ``kind``, so the
    report and the map file can never disagree about a row — the report is
    what the operator reads BEFORE the file, and two independent classifiers
    is how they drift.

    ``discovered`` counts DISTINCT ``directory_context`` values (its original
    meaning); ``rows_emitted`` counts the map lines, which is larger whenever
    a sentinel splits into cohorts.
    """
    report = MigrationReport(
        discovered=len({row.directory_context for row in rows}),
        rows_emitted=len(rows),
        cohort_counts_available=all(row.cohort_counts_known for row in rows),
    )
    for row in rows:
        target = _map_target(row)
        if target == DROP_MARKER:
            report.drop_rows.append(row)
        elif target == REVIEW_MARKER:
            report.review_rows.append(row)
        else:
            report.seed_rows.append(row)
    report.basenames_collide = detect_basenames_collide(rows)
    return report


@observe(tier="stage", metric="core.migrations.rekey.run")
def run(
    counts: dict[str, dict[str, int]] | None = None,
    *,
    cohorts: dict[str, dict[str, int]] | None = None,
    map_path: Path = DEFAULT_MAP_PATH,
    apply: bool = False,
    force: bool = False,
) -> dict:
    """One-call orchestration: discover, write the map, optionally apply.

    Returns a JSON-serialisable dict suitable for ``json.dumps`` on the
    CLI handler. With ``apply=False`` (the default) the only write is
    the map TSV itself; no storage writes, no registry writes.

    ``counts`` / ``cohorts`` accept fixtures (used by tests) so the discovery
    seam is replaceable without monkey-patching ``_forward_admin``.
    """
    rows = discover_directories(counts=counts, cohorts=cohorts)
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
        # Surface the apply's verdict at the TOP level. ``cmd_migrate_rekey``
        # returns 0/1 off ``result["ok"]``, so a refusal that lives only under
        # ``result["apply"]`` exits 0 — the gate would be undetectable to
        # anything scripting this command. Covers every refusal reason the
        # apply can produce (``delete_cohort_retargeted``,
        # ``registry_seed_failed``, and the backfill's own).
        if not apply_result.get("ok", False):
            result["ok"] = False
            result["reason"] = apply_result.get("reason", "apply_failed")
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
