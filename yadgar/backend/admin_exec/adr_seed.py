"""Car G (0047 spine train) — ADR seed (pages→ledger) + retype mutator.

D35a: the SEED is a one-shot admin op the migration merely enables, NOT a
migration step. Shipped as explicit admin op, idempotent on the ADR NUMBER
parsed out of ``body_slug`` — task 168 corrected this from string equality on
``body_slug`` itself, which never matched once a row carried a canonical slug
and its page still carried the legacy one.

TWO STORAGE HANDLES. No single storage object has both ``list_wiki_pages`` and
``create_adr_row``, so ``seed_adr_rows`` takes ``storage`` (wiki) and
``sql_storage`` (ledger) separately. ``retype_page_type`` is wiki-only and keeps
its single handle. See ``_StructuralSeedError`` for what one handle cost.

D35b: source of truth = per-ADR wiki PAGES (slug prefix enumeration + body
parse), NOT parse_index_rows — the index may lag (and historically drops
pages; ADR-0124 is the documented example, §1.5 of the master plan).

D23: the retype mutator flips ``wiki_page.page_type`` ``adr`` →
``adr_superseded`` atomic with the row-side status flip. Bypasses
``_WIKI_UPDATE_ALLOWED`` because it is a sanctioned server-side lifecycle
transition, not an agent/tool edit (D26: ``locked`` blocks agent edits,
NOT sanctioned transitions).

D35c verification gate: EXACT equality on a stated predicate. Three known
counts (index_rows vs pages_seen vs page_type='adr' rows) reconciled BEFORE
cutover, never absorbed silently. ``>=`` is NOT a gate (2026-06-16 vacuum
destroyed 3,622 memories through a ``>=`` check). Task 311: when ``index_rows``
is 0 the legacy index page is GONE (ADR-0253 deleted it corpus-wide), so the
reconciliation is two-sided — ``pages_seen == page_type_adr_rows``.
``gate["index_absent"]`` reports which of the two predicates ran.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar.backend.admin_exec._preview_guards import preview_guard_kwargs

# Re-export: retype_page_type moved to adr_retype (2026-08-18, I30 file_loc
# cap). Imported here so `adr_seed.retype_page_type` keeps resolving for the
# dispatch table and existing tests — a move, not an API change.
from yadgar.backend.admin_exec.adr_retype import retype_page_type as retype_page_type

# Re-export: the ``── internal storage helpers ──`` block moved to
# adr_seed_storage (car 19, same I30 file_loc cap, same response). Imported at
# MODULE level and called by BARE name below so
# `monkeypatch.setattr(adr_seed, "_adr_slug_prefixes", ...)` still binds — a
# move, not an API change.
from yadgar.backend.admin_exec.adr_seed_storage import _add_supersedes_link as _add_supersedes_link
from yadgar.backend.admin_exec.adr_seed_storage import _adr_slug_prefixes as _adr_slug_prefixes
from yadgar.backend.admin_exec.adr_seed_storage import (
    _count_legacy_index_rows as _count_legacy_index_rows,
)
from yadgar.backend.admin_exec.adr_seed_storage import (
    _count_page_type_adr_rows as _count_page_type_adr_rows,
)
from yadgar.backend.admin_exec.adr_seed_storage import _insert_adr_row as _insert_adr_row
from yadgar.backend.admin_exec.adr_seed_storage import _link_body_slug as _link_body_slug
from yadgar.backend.admin_exec.adr_seed_storage import (
    _parse_supersede_targets_from_body as _parse_supersede_targets_from_body,
)

logger = logging.getLogger(__name__)

#: Guard methods ``MariaStorageEngine.create_adr_row`` runs BEFORE its INSERT.
#:
#: Ledger task 176: the dry run stops at ``_seed_one_page``'s ``if run.dry_run:``
#: branch, so it never reaches ``create_adr_row`` and never reaches the guards
#: inside it. A preview that cannot fail the way the real run fails is a green
#: light with no braking distance — the 2026-08-18 VM rehearsal (task 175) is
#: the recorded instance: a clean preview, then ``UnknownProjectError`` on all
#: 230 pages because the ``project`` registry had never been seeded on that VM.
#:
#: ``_preflight_write_guards`` ITERATES this tuple on both paths, so adding a
#: name here is the whole wiring. ``test_adr_seed_dry_run_guard_parity`` ASTs
#: ``create_adr_row`` and fails if the two ever disagree — a guard added to the
#: write path cannot silently skip the dry run.
_WRITE_PATH_GUARDS: tuple[str, ...] = ("assert_project_registered",)


# ── ADR seed (D35a / D35b) ─────────────────────────────────────────────────────


# Per-ADR page slug pattern: <project>-adr-NNNN (legacy) OR
# {project_id}_adr-NNNN (Car L reslug). The seed enumerates BOTH prefixes.
# The legacy prefix matches yadgar-adr-NNNN for the yadgar project today
# (Car L's reslug is shipped but the operator runs it dry-run by default);
# the canonical prefix is the post-reslug form.


@observe(tier="stage", metric="backend.admin.adr_seed._is_per_adr_page_slug")
def _is_per_adr_page_slug(slug: str) -> bool:
    """True when *slug* is a per-ADR page slug (any of the supported shapes).

    The seed enumerates ``yadgar-adr-NNNN`` (legacy Car 2 slug, still
    authoritative for the 194 pages Car L's reslug hasn't yet rewritten) AND
    ``{project_id}_adr-NNNN`` (the canonical post-reslug slug — D32 ③, ``/``
    in ``project_id`` -> ``_``). The separator immediately before ``adr-``
    is therefore EITHER ``-`` (legacy) OR ``_`` (canonical) — task-adr-
    backfill-prompts fix 1: a hyphen-only regex matched zero canonical pages,
    so a corpus that had already been re-slugged would seed nothing while
    still returning a normal-looking success dict.

    Excludes the ``<project>-adr-log`` / ``{project_id}_adr-log`` monolith
    (deleted in migration 002) and the ``<project>-adr-index`` /
    ``{project_id}_adr-index`` (replaced by ``list_adr_rows`` post-Car-G;
    retained for one cycle per D35d, ``superseded-by-ledger`` tagged).
    """
    import re as _re

    if not slug:
        return False
    if slug.endswith("-adr-log") or slug.endswith("_adr-log"):
        return False
    if slug.endswith("-adr-index") or slug.endswith("_adr-index"):
        return False
    return bool(_re.search(r"[-_]adr-\d{4}$", slug))


@observe(tier="stage", metric="backend.admin.adr_seed._collect_candidate_pages")
def _collect_candidate_pages(
    *,
    project_slug_prefix: str,
    list_pages: Callable[..., list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return every per-ADR page under *project_slug_prefix*.

    Pure function — the ``list_pages`` callback is injected so the seed can
    run against a stub storage in tests without booting the real engine.
    Source of truth for D35b: per-ADR PAGES, NOT parse_index_rows.
    """
    pages = list_pages(project_slug_prefix, None, 10000)
    return [p for p in pages if _is_per_adr_page_slug(p.get("slug") or "")]


@observe(tier="stage", metric="backend.admin.adr_seed._exact_equality_gate")
def _exact_equality_gate(
    *,
    index_rows: int,
    pages_seen: int,
    page_type_adr_rows: int,
) -> bool:
    """D35c verification gate — EXACT equality on a stated predicate.

    The known counts (index_rows vs pages_seen vs page_type='adr' rows) must
    reconcile BEFORE cutover. A residue gap is NOT absorbed silently. ``>=`` is
    NOT a gate — that was the 2026-06-16 vacuum that destroyed 3,622 memories.

    TWO predicates, chosen by whether the legacy index still exists:

    * ``index_rows > 0`` — the three-way equality. Unchanged.
    * ``index_rows == 0`` — the legacy ``<project>-adr-index`` page is GONE, so
      the reconciliation is two-sided: ``pages_seen == page_type_adr_rows``.

    Ledger task 311. ``_count_legacy_index_rows``'s docstring has asserted this
    reading since Car G — *"treats index_rows=0 as a hard signal that the index
    is gone, NOT as a mismatch — the gate's two-sided reconciliation is between
    pages_seen and page_type_adr_rows, with index_rows as the legacy-trace
    counter"* — and nothing implemented it. ADR-0253 deleted the index page
    corpus-wide, so the counter returns 0 forever, ``exact_match`` was
    unreachable, and ``cli/backfill.py``'s ``if not gate["exact_match"]:
    return 1`` turned a clean 0-insert ``--apply`` into a reported failure. The
    op that lies about its own success is worse than the op with no gate.

    This is NOT the ``>=`` weakening above. The zero case still demands EXACT
    equality; it demands it of the two counts that still have a source. The
    third side is not relaxed, it is absent — and a source that no longer
    exists cannot be evidence either way.

    CONSEQUENCE, stated so the next reader does not file it as vacuous: an
    empty project (no ADR pages, no ledger rows) now gates green on ``0 == 0``.
    That is correct per ADR-0429 — the op has no remaining work on any corpus
    and is retained only for future projects onboarding legacy ADRs — and it is
    what makes "there was nothing to do" exit 0 instead of 1.

    Returns True only when the counts the corpus can still produce match
    exactly.
    """
    if index_rows == 0:
        return pages_seen == page_type_adr_rows
    return index_rows == pages_seen == page_type_adr_rows


@observe(tier="stage", metric="backend.admin.adr_seed._verification_gate")
def _verification_gate(
    *,
    index_rows: int,
    pages_seen: int,
    page_type_adr_rows: int,
) -> dict[str, object]:
    """Build the D35c ``gate`` block: the counts, the verdict, and its provenance.

    ``index_absent`` is the discriminator task 311 added. Two different
    predicates answer to ``exact_match`` now — see ``_exact_equality_gate``,
    which owns the branch — and ``exact_match: true`` on a 0/0/0 gate is
    otherwise indistinguishable from a three-way match. The operator reading
    the CLI report is the one who has to tell them apart, so the fact travels
    in the payload rather than being re-derived downstream.

    A named function rather than a dict literal inline in ``seed_adr_rows``:
    the block stopped being a transcription of three counts the moment its
    verdict acquired a branch, and ``seed_adr_rows`` sits under an I13
    allowlisted ``fn_loc`` ceiling that a growing literal eats into.
    """
    return {
        "index_rows": index_rows,
        "pages_seen": pages_seen,
        "page_type_adr_rows": page_type_adr_rows,
        # Paired with ``_exact_equality_gate``'s own ``index_rows == 0`` branch:
        # this key REPORTS which predicate ran, that one CHOOSES it.
        "index_absent": index_rows == 0,
        "exact_match": _exact_equality_gate(
            index_rows=index_rows,
            pages_seen=pages_seen,
            page_type_adr_rows=page_type_adr_rows,
        ),
    }


@observe(tier="stage", metric="backend.admin.adr_seed._parse_adr_id_from_slug")
def _parse_adr_id_from_slug(slug: str) -> int | None:
    """Extract the ADR-NNNN number from a per-ADR page slug, or None.

    Matches BOTH the legacy hyphen separator (``yadgar-adr-0042``) and the
    canonical underscore separator (``m-agahi_yadgar_adr-0042`` — D32 ③).
    Same fix as ``_is_per_adr_page_slug`` — see its docstring.
    """
    import re as _re

    m = _re.search(r"[-_]adr-(\d{4})$", slug or "")
    if not m:
        return None
    return int(m.group(1))


@observe(tier="stage", metric="backend.admin.adr_seed._has_index_provenance")
def _has_index_provenance(page: dict[str, Any]) -> bool:
    """True when the page has index-row provenance (legacy index columns).

    D35b: the page is the ID-bearing artifact; the index may lag. Today
    every page carries an ``adr-NNNN`` slug suffix — that IS the provenance.
    This helper is a forward-looking seam: a future migration may stamp
    pages with an ``_indexed_at`` column, in which case the seed flips
    to honouring that.

    Stayed in this module when the rest of the ``── internal storage helpers ──``
    block moved to ``adr_seed_storage`` (car 19): it reads ``_parse_adr_id_from_slug``,
    which lives here, and it is a pure slug predicate rather than a storage call.
    """
    return bool(_parse_adr_id_from_slug(page.get("slug") or ""))


@observe(tier="stage", metric="backend.admin.adr_seed._adr_page_sort_key")
def _adr_page_sort_key(page: dict[str, Any]) -> tuple[int, int]:
    """Ascending-ADR-number sort key for the candidate page list.

    Unparsable slugs sort LAST (first tuple element flips 0 -> 1) rather
    than raising — a bare ``_parse_adr_id_from_slug`` result of ``None``
    would blow up a numeric comparison against an ``int`` if used directly
    as the sort key.
    """
    adr_id = _parse_adr_id_from_slug(page.get("slug") or "")
    if adr_id is None:
        return (1, 0)
    return (0, adr_id)


@observe(tier="stage", metric="backend.admin.adr_seed._extract_title_and_status")
def _extract_title_and_status(body: str) -> tuple[str, str, str]:
    """Parse a per-ADR page body for (title, status, date).

    The page body shape (built by ``adr_render._build_adr_body``) is
    ``# ADR-NNNN: <title>\\n...## Context...## Decision...``. Returns
    ``(title, status, date)`` from the bullets (``- status: ...``,
    ``- date: ...``). When the body shape is unknown, returns
    ``("(unknown)", "open", "")`` — the seed flags such rows in its result.

    Pure helper: no I/O, no DB access. Caller-side state is restored at the
    end of ``seed_adr_rows``.
    """
    title = "(unknown)"
    status = "open"
    date = ""
    if not body:
        return title, status, date
    # H1 carries the title: "# ADR-NNNN: <title>"
    import re as _re

    m = _re.match(r"^#\s*ADR-\d{4}:\s*(.+?)\s*$", body, _re.MULTILINE)
    if m:
        title = m.group(1).strip()
    # Bullets carry the status + date.
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("- status:"):
            status = s[len("- status:") :].strip() or "open"
        elif s.startswith("- date:") or s.startswith("- decided_on:"):
            key_len = len("- date:") if s.startswith("- date:") else len("- decided_on:")
            date = s[key_len:].strip()
    return title, status, date


@dataclass
class _SeedRun:
    """One seed pass: its run-scoped inputs, plus the tally it accumulates.

    Grouped into an object because the I13 caps are hard (params <= 8) and the
    per-page helper needs all of it — the alternative was a ten-parameter
    function the hook rejected outright. Every test seam stays a named field
    constructed in ``seed_adr_rows``; only the argument list is hidden.

    ``rows_skipped`` used to be ONE counter absorbing "already had a row", "the
    insert raised" and "the insert returned no id", so the report could not tell
    "nothing to do" from "totally broken" — the wrong-engine bug's observable
    symptom was ``rows_inserted=0, rows_skipped=236``. Three fields now, three
    result keys.

    ``present`` holds the ADR NUMBERS already in the ledger: read once before the
    loop, updated in place on each insert.
    """

    project_id: str
    sql_storage: Any
    skip: set[int]
    dry_run: bool
    next_id: int
    row_inserter: Callable[..., dict[str, object]] | None = None
    slug_linker: Callable[[int, str], None] | None = None
    present: set[int] = field(default_factory=set)
    rows_inserted: int = 0
    rows_already_present: int = 0
    rows_failed: int = 0
    rows_skipped_by_request: int = 0
    supersedes_links: int = 0
    supersedes_failed: int = 0
    flagged: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    last_inserted_number: int | None = None
    #: ADR number -> the ledger row id it actually landed on. The ids are
    #: NOT the numbers (one global AUTO_INCREMENT, many projects — task 177),
    #: so a `supersedes: ADR-0042` line in prose cannot be used as an FK.
    #: This map is the resolution ADR-0197's own consequences demanded:
    #: "derived from slugs rather than from numbers written in old prose".
    number_to_id: dict[int, int] = field(default_factory=dict)
    supersedes_unresolved: int = 0


class _StructuralSeedError(RuntimeError):
    """The ledger surface itself is wrong — a missing method or a bad signature.

    Car 4 / defect 1: the ledger calls used to run against the SurrealDB handle,
    which has no ADR methods at all, and the resulting ``AttributeError`` was
    caught by a blanket ``except Exception`` and counted as a skip. The op then
    reported ``rows_inserted=0, rows_skipped=236`` — indistinguishable from
    "already backfilled". A structural fault is NOT a per-row failure: it will
    hit every remaining page identically, so the loop stops and says so
    (same lesson as PR #48's ``with_edges`` regression).

    TWO triggers, and the second exists because the first was not enough:

    1. TYPE-based — ``AttributeError`` / ``TypeError`` from the insert call. A
       missing method or a changed signature.
    2. POSITION-based — the FIRST insert of a run fails, whatever the exception
       type. Task 175: the VM rehearsal (2026-08-18) raised ``UnknownProjectError``
       from the ADR-0078 registry guard on all 230 pages. It is neither of the two
       types above, so it fell through to the per-row branch and counted 230
       identical faults — precisely the outcome this class exists to prevent. An
       exception-type allowlist cannot enumerate every structural fault; "nothing
       has succeeded yet and this one failed" can, because nothing about the next
       page differs from this one.
    """


@observe(tier="hot", span=False)
def _normalise_skip_numbers(raw: Iterable[int] | None) -> set[int]:
    """Coerce the ``skip_adr_numbers`` payload value to a ``set[int]``.

    Annotated ``Iterable[int]`` rather than ``set[int]`` because this op is
    registered under ``_kwargs_op`` (``fn(**payload)``) and the payload arrives
    from JSON — a set cannot survive that transport, so the honest declared
    type is the iterable the CLI actually forwards (a list).
    """
    if raw is None:
        return set()
    return {int(n) for n in raw}


@observe(tier="boundary", metric="backend.admin.adr_seed._present_adr_numbers")
async def _present_adr_numbers(sql_storage: Any, project_id: str) -> set[int]:
    """ADR NUMBERS already carrying a ledger row, read once for the whole run.

    Car 3 fixes two halves of one defect. The lookup was keyed on a project id
    regex-parsed out of the page slug (``yadgar-adr-0001`` → ``yadgar``) while
    rows live under the real ``owner/repo`` key, so ``list_adr_rows`` returned
    ``[]`` for every slug shape; and the comparison was exact string equality
    against the page's CURRENT slug, so a row stamped with a canonical
    ``body_slug`` never matched its legacy-slugged page — the shape all six
    existing rows have. Reducing both sides to the ADR NUMBER fixes both, and is
    symmetric by construction: nothing is rewritten, so no direction can be got
    wrong.

    Read ONCE before the loop, not per page: 230 pages × a full-table list is
    230 round trips, normalising once cannot drift from normalising 230 times,
    and it closes the inverse hole — a number owning both slug shapes was
    inserted twice by the per-page check.

    Empty set when the ledger handle is absent; the caller decides whether that
    is fatal (it is, for a real insert).
    """
    if sql_storage is None:
        return set()
    rows_obj = sql_storage.list_adr_rows(project_id=project_id)
    if hasattr(rows_obj, "__await__"):
        rows_obj = await rows_obj
    if not isinstance(rows_obj, list):
        return set()
    present: set[int] = set()
    for r in rows_obj:
        if not isinstance(r, dict):
            continue
        number = _parse_adr_id_from_slug(r.get("body_slug") or "")
        if number is not None:
            present.add(number)
    return present


@observe(tier="boundary", metric="backend.admin.adr_seed._present_adr_number_to_id")
async def _present_adr_number_to_id(sql_storage: Any, project_id: str) -> dict[int, int]:
    """ADR number -> row id, for rows THIS project already has.

    Seeds ``run.number_to_id`` so a resumed run can resolve a ``supersedes:``
    target that landed in an earlier run. Keyed on the number parsed out of
    ``body_slug`` — the slug is the durable link between the page's number and
    the row, which is exactly what ADR-0197 said supersede targets must be
    derived from.
    """
    if sql_storage is None:
        return {}
    rows_obj = sql_storage.list_adr_rows(project_id=project_id)
    if hasattr(rows_obj, "__await__"):
        rows_obj = await rows_obj
    if not isinstance(rows_obj, list):
        return {}
    mapping: dict[int, int] = {}
    for r in rows_obj:
        if not isinstance(r, dict):
            continue
        number = _parse_adr_id_from_slug(r.get("body_slug") or "")
        row_id = r.get("id")
        if number is not None and row_id is not None:
            mapping[number] = int(row_id)
    return mapping


@observe(tier="boundary", metric="backend.admin.adr_seed._read_next_adr_id")
async def _read_next_adr_id(sql_storage: Any, project_id: str) -> tuple[int, str]:
    """The id the NEXT ``adr`` INSERT will take, plus which source said so.

    ``information_schema.TABLES.AUTO_INCREMENT`` first, ``max(id) + 1`` as the
    fallback. The order is not stylistic: ADR-0006 MEASURED on
    ``mariadb:11.4.12`` that deleting rows leaves the counter untouched (the next
    insert took id 2, not 1), so ``max(id) + 1`` under-predicts in exactly the
    state this table is in — and it is project-scoped while the counter is
    table-global, so it also under-predicts when another project holds a higher
    id. The dry run is the human gate in front of an unrepairable write; it must
    not be able to lie about the numbering.

    Returns ``(next_id, basis)`` — basis ``"information_schema"`` | ``"max_id"``
    | ``"unavailable"``. A ``"max_id"`` basis is a signal to stop, not a value to
    trust; that is why it is returned alongside the number.
    """
    engine = getattr(sql_storage, "engine", None) or getattr(sql_storage, "_engine", None)
    if engine is not None:
        from sqlalchemy import text as _sa_text  # noqa: PLC0415

        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    _sa_text(
                        "SELECT AUTO_INCREMENT FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'adr'"
                    )
                )
                row = result.first()
            if row is not None and row[0] is not None:
                return int(row[0]), "information_schema"
        except Exception as exc:  # noqa: BLE001 — fall through to the max(id) read
            logger.warning(
                "seed_adr_rows: information_schema AUTO_INCREMENT read failed (%s); "
                "falling back to max(id)+1, which under-predicts after a DELETE",
                exc,
            )
    present = await _present_adr_row_ids(sql_storage, project_id)
    if not present:
        return 1, "max_id" if sql_storage is not None else "unavailable"
    return max(present) + 1, "max_id"


@observe(tier="boundary", metric="backend.admin.adr_seed._present_adr_row_ids")
async def _present_adr_row_ids(sql_storage: Any, project_id: str) -> set[int]:
    """Ledger ``adr.id`` values for *project_id* — the ``max(id)+1`` fallback input."""
    if sql_storage is None:
        return set()
    rows_obj = sql_storage.list_adr_rows(project_id=project_id)
    if hasattr(rows_obj, "__await__"):
        rows_obj = await rows_obj
    if not isinstance(rows_obj, list):
        return set()
    return {int(r["id"]) for r in rows_obj if isinstance(r, dict) and r.get("id")}


@observe(tier="stage", metric="backend.admin.adr_seed._seed_one_page")
async def _seed_one_page(page: dict[str, Any], run: _SeedRun) -> None:
    """Decide ONE page's outcome, mutating *run*. Raises on a structural fault.

    Four outcomes and nothing else: unparsable (flagged), skipped by request,
    already present, or handed to ``_insert_one_row``.
    """
    slug = page.get("slug") or ""
    body = page.get("content") or ""
    number = _parse_adr_id_from_slug(slug)
    if number is None:
        run.flagged.append({"slug": slug, "reason": "unparsable slug suffix"})
        return

    # Car 2 / ADR-0006: the operator-stated skip. Keyed on the NUMBER, not the
    # slug, so both slug shapes for the same ADR collapse to one decision — a
    # slug-keyed set would silently miss whichever shape was not typed.
    if number in run.skip:
        run.rows_skipped_by_request += 1
        return

    if number in run.present:
        run.rows_already_present += 1
        return

    title, status, date = _extract_title_and_status(body)
    row_payload: dict[str, Any] = {
        "project_id": run.project_id,
        "title": title,
        "status": status,
        "decided_on": date or None,
        "body_slug": slug,
    }

    if run.dry_run:
        # planned_id counts from the REAL next AUTO_INCREMENT value, and the
        # offset is the plan's own length — ids are handed out in insertion
        # order, so the Nth planned insert takes the Nth free id.
        run.plan.append(
            {
                "adr": f"ADR-{number:04d}",
                "slug": slug,
                "planned_id": run.next_id + len(run.plan),
                "title": title,
                "status": status,
            }
        )
        run.present.add(number)
        return

    await _insert_one_row(run, number=number, slug=slug, body=body, payload=row_payload)


@observe(tier="stage", metric="backend.admin.adr_seed._insert_one_row")
async def _insert_one_row(
    run: _SeedRun,
    *,
    number: int,
    slug: str,
    body: str,
    payload: dict[str, Any],
) -> None:
    """Write one ``adr`` row and its follow-on links, mutating *run*.

    Split from ``_seed_one_page`` because the two do different jobs (classify vs
    write) and the combined function exceeded the I13 hard caps.

    Raises:
        _StructuralSeedError: the ledger SURFACE is wrong (missing method,
            signature mismatch). Every remaining page would fail identically, so
            the caller stops rather than counting 230 identical faults.
    """
    try:
        if run.row_inserter is not None:
            insert_result: object = run.row_inserter(payload)
            result: dict[str, object] | object = (
                insert_result if isinstance(insert_result, dict) else {}
            )
        else:
            result = await _insert_adr_row(run.sql_storage, payload)
    except (AttributeError, TypeError) as exc:
        # Swallowing this is what made the wrong-engine defect invisible for the
        # op's whole life: an AttributeError became an empty dict, then a missing
        # id, then a skip, then a success-shaped report.
        raise _StructuralSeedError(
            f"the ledger surface cannot take an ADR insert ({type(exc).__name__}: {exc}); "
            f"failed at slug={slug!r}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — this ROW failed; the next may not
        # Task 175: an exception-TYPE allowlist cannot recognise a structural
        # fault. UnknownProjectError (the ADR-0078 registry guard) is neither
        # AttributeError nor TypeError, so it fell through here and counted 230
        # identical faults — exactly what _StructuralSeedError exists to stop.
        # The reliable signal is positional, not type-based: if the very FIRST
        # insert of the run fails, nothing about the next page differs, so every
        # remaining page fails identically. Stop and say so.
        if run.rows_inserted == 0 and run.rows_failed == 0:
            raise _StructuralSeedError(
                f"the first ADR insert failed, so every remaining page will fail "
                f"identically ({type(exc).__name__}: {exc}); failed at slug={slug!r}"
            ) from exc
        logger.warning("seed_adr_rows: row insert failed for slug=%s err=%s", slug, exc)
        run.rows_failed += 1
        # Task 174: the WARNING above reaches the backend CONTAINER log, which an
        # operator on the live box cannot read by any sanctioned route. Without a
        # reason here the result dict says `rows_failed=230, flagged=[]` and gives
        # nowhere to look. The reason travels with the count.
        run.flagged.append(
            {
                "slug": slug,
                "adr_id": f"ADR-{number:04d}",
                "reason": f"insert failed ({type(exc).__name__}: {exc})",
            }
        )
        return

    inserted_id = int(str((result if isinstance(result, dict) else {}).get("id") or 0))
    if not inserted_id:
        logger.warning("seed_adr_rows: insert returned no id for slug=%s", slug)
        run.rows_failed += 1
        run.flagged.append(
            {
                "slug": slug,
                "adr_id": f"ADR-{number:04d}",
                "reason": "insert returned no id",
            }
        )
        return
    run.rows_inserted += 1
    run.last_inserted_number = number
    run.present.add(number)
    run.number_to_id[number] = inserted_id

    await _link_and_supersede(run, adr_id=inserted_id, slug=slug, body=body)

    # Flag the page-only case (ADR-0124 documented: no index row, body exists).
    if not _has_index_provenance({"slug": slug}):
        run.flagged.append(
            {
                "slug": slug,
                "adr_id": f"ADR-{number:04d}",
                "reason": "no index row provenance (D35b: page-only)",
            }
        )


@observe(tier="stage", metric="backend.admin.adr_seed._link_and_supersede")
async def _link_and_supersede(
    run: _SeedRun,
    *,
    adr_id: int,
    slug: str,
    body: str,
) -> None:
    """Re-stamp ``body_slug`` and insert the page's ``supersedes`` links.

    Both best-effort AND counted: ``create_adr_row`` already wrote ``body_slug``
    in the INSERT, and an unresolvable supersede target is expected residue.
    Counted because ``supersedes_links += 1`` used to run unconditionally
    against a helper that swallowed its own failure — reporting failed links as
    successes.
    """
    if run.slug_linker is not None:
        try:
            run.slug_linker(adr_id, slug)
        except Exception as exc:  # noqa: BLE001 — same best-effort contract as the real path
            logger.warning(
                "seed_adr_rows: injected slug_linker failed for id=%s slug=%s: %s",
                adr_id,
                slug,
                exc,
            )
    else:
        await _link_body_slug(run.sql_storage, adr_id, slug)

    # ``supersedes:`` (D23) parses to ADR NUMBERS, not row ids: one global
    # AUTO_INCREMENT, many projects (task 177). Resolve through the slug-derived
    # map, which is what ADR-0197's own consequences required.
    for target_number in _parse_supersede_targets_from_body(body):
        target_id = run.number_to_id.get(target_number)
        if target_id is None:
            # Forward ref or skipped. A wrong FK is unrepairable; a gap is not.
            run.supersedes_unresolved += 1
            run.flagged.append(
                {
                    "slug": slug,
                    "adr_id": f"ADR-{target_number:04d}",
                    "reason": (
                        f"supersedes target ADR-{target_number:04d} has no row in this "
                        f"project yet — link not written"
                    ),
                }
            )
            continue
        if await _add_supersedes_link(run.sql_storage, adr_id, target_id):
            run.supersedes_links += 1
        else:
            run.supersedes_failed += 1


@observe(tier="boundary", metric="backend.admin.adr_seed._preflight_write_guards")
async def _preflight_write_guards(
    sql_storage: Any,
    project_id: str,
    *,
    row_inserter: Callable[..., dict[str, object]] | None,
) -> str | None:
    """Run every :data:`_WRITE_PATH_GUARDS` guard, dry run or not. Error text or None.

    This is what makes a clean dry run evidence: the same validation
    ``create_adr_row`` performs before its INSERT runs on the preview too, so an
    input the apply would reject is rejected by the preview at the same point.

    Returns a string rather than raising, because ``admin_exec`` pins the
    never-raise error model — the caller folds it into ``result["error"]``
    alongside the counts, which is the shape the CLI already keys on
    (``cmd_backfill`` tests ``ok is False`` BEFORE its dry-run ``return 0``, so
    a preflight failure already exits non-zero on a dry run).

    Three rejections, all of them "the apply cannot succeed from here":

    1. **The guard says no.** ``UnknownProjectError`` and anything else the
       guard raises. This is the case task 176 was filed for.
    2. **No ledger handle at all** (engine #2 not composed). Every read helper
       defaults to empty on ``None``, so without this branch the preview reads
       clean and the apply dies on the first insert. Same defect, second
       instance — and ``project_registry``'s own docstring already made the
       argument: a silent pass "is a guard that protects nothing on exactly the
       deployments that need it most".
    3. **The handle cannot run the guard** (method absent). That is the
       wrong-engine shape from task 168, where every ``AttributeError`` off a
       handle with zero ADR methods was absorbed into a success-shaped report.
       "Could not check" is not "checked and passed".

    Skipped entirely when ``row_inserter`` is injected: that seam replaces
    ``create_adr_row``, so the guards inside it are not the ones that will run.
    Mirrors how ``present`` / ``number_to_id`` already treat the same seam.

    Guards are invoked with ``refresh=False`` when they accept it (see
    :data:`_GUARD_NO_WRITE_KW`) — a preview must not stamp
    ``project.last_validated_at``. The APPLY still stamps it, via
    ``create_adr_row``'s own unqualified call to the same guard, so the clock
    task 384 repaired keeps moving on real writes.
    """
    if row_inserter is not None:
        return None
    if sql_storage is None:
        return (
            "the ledger handle is absent (engine #2 not composed), so the write-path "
            f"guards {list(_WRITE_PATH_GUARDS)} could not run — a dry run cannot "
            "predict an apply it was unable to validate"
        )
    for guard_name in _WRITE_PATH_GUARDS:
        guard = getattr(sql_storage, guard_name, None)
        if guard is None:
            return (
                f"the ledger surface has no {guard_name!r}, so the write-path guard "
                "could not run; a handle that cannot be checked is not a handle that passed"
            )
        try:
            result_obj = guard(project_id, **preview_guard_kwargs(guard))
            if hasattr(result_obj, "__await__"):
                await result_obj
        except Exception as exc:  # noqa: BLE001 — every guard failure is fatal here
            return (
                f"write-path guard {guard_name!r} rejected project_id {project_id!r} "
                f"({type(exc).__name__}: {exc}); the apply would fail identically on "
                "every page"
            )
    return None


@observe(tier="boundary", metric="backend.admin.adr_seed.seed_adr_rows")
async def seed_adr_rows(
    *,
    project_id: str,
    directory: str,
    storage: Any | None = None,
    sql_storage: Any | None = None,
    skip_adr_numbers: Iterable[int] | None = None,
    dry_run: bool = False,
    row_inserter: Callable[..., dict[str, object]] | None = None,
    slug_linker: Callable[[int, str], None] | None = None,
) -> dict[str, object]:
    """One-shot: lift existing per-ADR wiki PAGES into the ``adr`` ledger table.

    Source of truth = per-ADR PAGES (D35b), NOT the index — enumerates pages
    via slug prefix and parses each body. Idempotent on the ADR NUMBER behind
    ``body_slug``: re-running converges; a second run inserts 0 rows.

    TWO STORAGE HANDLES, not one. This op used to resolve ``_get_storage()``
    alone and call the ADR methods on it; because it is registered under
    ``_kwargs_op`` (``fn(**payload)``, which never injects ``storage=``),
    ``storage`` was ALWAYS ``None`` in production and always resolved to the
    engine with zero ADR methods. Split now, resolved the way the sibling
    one-shot seed (``seed_adr_tier_subsystem``) already resolved them.

    Args:
        project_id: the canonical ``owner/repo`` project key.
        directory: absolute project root for §25 wiki resolution.
        storage: WIKI handle (SurrealDB) — ``list_wiki_pages`` /
            ``get_wiki_page_by_slug*``. Optional test seam; defaults to
            ``_get_storage()``.
        sql_storage: LEDGER handle (MariaDB) — ``list_adr_rows`` /
            ``create_adr_row`` / ``set_adr_body_slug`` / ``add_adr_supersedes``.
            Optional test seam; defaults to ``_get_sql_storage()``, which is
            legitimately ``None`` when engine #2 did not come up. A real insert
            against a ``None`` handle returns ``{"ok": False}``, not a skip.
        skip_adr_numbers: ADR numbers to leave un-inserted (ADR-0006: the ids
            they need are already spent). Keyed on the NUMBER so both slug
            shapes for one ADR collapse to a single decision.
        dry_run: compute and return the planned (ADR number → ledger id)
            mapping without writing anything.
        row_inserter: optional callable replacing ``create_adr_row`` — test
            seam. When supplied it owns idempotency, so the pre-read
            present-set is skipped.
        slug_linker: optional callable replacing ``set_adr_body_slug`` — test
            seam.

    Returns:
        dict with keys:
          - pages_seen: per-ADR pages enumerated (D35b source of truth). Counts
            SLUGS, so an ADR owning both slug shapes is counted twice — 236 for a
            230-ADR corpus with six re-slugged namesakes.
          - rows_inserted / rows_already_present / rows_failed /
            rows_skipped_by_request: the four outcomes ``rows_skipped`` conflated.
          - next_id / next_id_basis: the id the first insert took (or would), and
            which source said so.
          - plan: dry-run only — one entry per planned insert.
          - flagged, supersedes_links, supersedes_failed.
          - gate: D35c output ``{"index_rows", "pages_seen",
            "page_type_adr_rows", "index_absent", "exact_match"}``.
            ``index_absent`` (task 311) names which predicate produced
            ``exact_match`` — the two-sided one when the legacy index page is
            gone, the three-way one when it still exists.
        On a structural fault: ``{"ok": False, "error", "resume_after_adr"}``
        alongside the partial counts. Never raises — ``admin_exec`` pins the
        never-raise error model and both tool shells key on ``ok is False``.

    D35c: caller MUST inspect ``gate["exact_match"]``. NOTE that the gate is
    computed AFTER the writes commit, so a False there is a post-mortem, not a
    guard — use ``dry_run`` for the pre-write check.

    Task 176: ``dry_run`` can now BEAR that weight. Every guard
    ``create_adr_row`` runs before its INSERT (:data:`_WRITE_PATH_GUARDS`) is
    run by ``_preflight_write_guards`` on both paths, so a clean preview is
    evidence the apply will get past the same checks rather than a green light
    with no braking distance. Before this the dry run returned at
    ``_seed_one_page``'s ``if run.dry_run:`` branch and reached no guarded call
    at all — the 2026-08-18 VM rehearsal (task 175) is what that cost.
    """
    if storage is None:
        from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()
    if storage is None:
        raise RuntimeError("seed_adr_rows requires storage; runtime storage not initialised")
    if sql_storage is None:
        from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

        sql_storage = _get_sql_storage()

    skip = _normalise_skip_numbers(skip_adr_numbers)

    # D35b: enumerate per-ADR pages (slug prefix + filter), not parse_index_rows.
    # BOTH prefixes are scanned EVERY run and the results UNIONED (de-duplicated
    # by slug) — see ``_adr_slug_prefixes`` for which two and why. A
    # canonical-only scan, or a break-on-first-nonempty loop, finds the single
    # already-reslugged page and never touches the 200+ legacy-format pages this
    # seed exists to lift.
    seen_slugs: set[str] = set()
    pages: list[dict[str, Any]] = []
    for _prefix in _adr_slug_prefixes(project_id, directory):
        prefix_pages = _collect_candidate_pages(
            project_slug_prefix=_prefix,
            list_pages=lambda prefix, _dir, limit: storage.list_wiki_pages(
                slug_prefix=prefix, limit=limit
            ),
        )
        for _page in prefix_pages:
            _slug = _page.get("slug") or ""
            if _slug in seen_slugs:
                continue
            seen_slugs.add(_slug)
            pages.append(_page)

    # Ascending ADR-number insertion order. Insertion order is the ONLY lever on
    # which AUTO_INCREMENT id a page lands on, so the non-skipped pages must go
    # out in numeric order for "skip the spent numbers, the rest land on their
    # own" to hold. Unparsable slugs sort LAST: they are flagged and never
    # inserted, so their placement cannot move an id — sorting them out of the
    # way just keeps the enumeration deterministic.
    pages = sorted(pages, key=_adr_page_sort_key)

    pages_seen = len(pages)

    # Car 3: ONE pre-loop read of what the ledger already holds, reduced to ADR
    # NUMBERS. An injected ``row_inserter`` owns its own idempotency (that is
    # what the seam is for), so the set stays empty on that path.
    next_id, next_id_basis = await _read_next_adr_id(sql_storage, project_id)
    run = _SeedRun(
        project_id=project_id,
        sql_storage=sql_storage,
        skip=skip,
        dry_run=dry_run,
        next_id=next_id,
        row_inserter=row_inserter,
        slug_linker=slug_linker,
        present=(
            set()
            if row_inserter is not None
            else await _present_adr_numbers(sql_storage, project_id)
        ),
        # Seeded from existing rows so a RESUMED run resolves earlier landings.
        number_to_id=(
            {}
            if row_inserter is not None
            else await _present_adr_number_to_id(sql_storage, project_id)
        ),
    )

    # Task 176: the write path's guards, run on BOTH paths. Without this the dry
    # run reaches no guarded call at all — every helper above is an unguarded
    # read — so a clean preview predicted nothing about the apply.
    structural_error: str | None = await _preflight_write_guards(
        sql_storage, project_id, row_inserter=row_inserter
    )
    if structural_error is None:
        for page in pages:
            try:
                await _seed_one_page(page, run)
            except _StructuralSeedError as exc:
                structural_error = str(exc)
                break

    # D35c: the verification gate. EXACT equality on the stated predicate.
    # Index-rows vs pages-seen vs page_type='adr' rows must reconcile. The
    # index rows come from the legacy <project>-adr-index WIKI page; the row
    # count comes from the LEDGER — the two handles, not one.
    index_rows = _count_legacy_index_rows(directory, storage)
    page_type_adr_rows = await _count_page_type_adr_rows(sql_storage, project_id)
    result_dict: dict[str, object] = {
        "project_id": project_id,
        "directory": directory,
        "dry_run": dry_run,
        "pages_seen": pages_seen,
        "rows_inserted": run.rows_inserted,
        "rows_already_present": run.rows_already_present,
        "rows_failed": run.rows_failed,
        "rows_skipped_by_request": run.rows_skipped_by_request,
        "skip_adr_numbers": sorted(skip),
        "next_id": next_id,
        "next_id_basis": next_id_basis,
        "flagged": run.flagged,
        "supersedes_links": run.supersedes_links,
        "supersedes_failed": run.supersedes_failed,
        "supersedes_unresolved": run.supersedes_unresolved,
        # Where each ADR actually landed. Ids are not numbers.
        "number_to_id": dict(sorted(run.number_to_id.items())),
        "gate": _verification_gate(
            index_rows=index_rows,
            pages_seen=pages_seen,
            page_type_adr_rows=page_type_adr_rows,
        ),
    }
    if dry_run:
        result_dict["plan"] = run.plan
    if structural_error is not None:
        # ERROR MODEL (admin_exec): never raise. The partial counts and the
        # resume point ride along — an operator whose 230-row insert aborted
        # mid-way needs to know where it stopped, and ``adr.id`` has no undo.
        result_dict["ok"] = False
        result_dict["error"] = structural_error
        result_dict["resume_after_adr"] = run.last_inserted_number
    return result_dict
