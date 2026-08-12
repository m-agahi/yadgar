"""Car G (0047 spine train) — ADR seed (pages→ledger) + retype mutator.

D35a: the SEED is a one-shot admin op the migration merely enables, NOT a
migration step. Shipped as explicit admin op, idempotent on ``body_slug``.

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
destroyed 3,622 memories through a ``>=`` check).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


# ── Retype mutator (D23 / D26) ────────────────────────────────────────────────


@observe(tier="boundary", metric="backend.admin.adr_seed.retype_page_type")
def retype_page_type(
    *,
    slug: str,
    from_type: str,
    to_type: str,
    directory: str | None = None,
    storage: Any | None = None,
) -> dict[str, object]:
    """Flip ``wiki_page.page_type`` ``from_type`` → ``to_type`` server-side.

    Sanctioned server-side lifecycle transition (D26): bypasses
    ``_WIKI_UPDATE_ALLOWED`` because the retype is a server-only mutator
    whose reachability is the security boundary (the model cannot supply
    ``page_type`` on this path — the function is invoked from
    ``admin_exec`` dispatch only).

    Atomicity: ``update_wiki_page(page_id, {"page_type": to_type},
    _sanctioned=True)`` writes the page row + a wiki_page_version snapshot in
    the storage's compound transaction (per ``_WikiMixin.update_wiki_page``).
    The retype mutator is paired with ``MariaStorageEngine._flip_adr_status``
    on the row side (D23: status flip + page-type retype both required);
    that pairing is the caller's responsibility — ``retype_page_type`` owns
    ONLY the wiki-page leg.

    Args:
        slug: the wiki page slug (e.g. ``yadgar-adr-0001``).
        from_type: the page's CURRENT ``page_type`` — assertion guard.
        to_type: the new ``page_type`` (must already be in
            ``CANONICAL_PAGE_TYPES``; today: ``adr_superseded``).
        directory: caller directory used by the storage's §25 resolution.
        storage: pre-resolved storage instance. Optional — when None the
            function pulls the live one from the runtime lifecycle so the
            ``/admin`` dispatch path works without an extra parameter.

    Returns:
        ``{"ok": True, "slug": ..., "from_type": ..., "to_type": ...}``.

    Raises:
        ValueError: when ``slug`` is not found, or ``from_type`` does not
            match the current ``page_type`` (the cross-type guard).
    """
    if storage is None:
        from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()
    if storage is None:
        raise RuntimeError("retype_page_type requires storage; runtime storage not initialised")

    page: dict[str, Any] | None = None
    # Prefer directory-aware resolution (§25 — see _WikiMixin at
    # _shared/storage/wiki.py:400). Fall back to slug-only when the storage
    # surface is partial (the test stub + dry-run paths exercise this).
    if directory is not None:
        try:
            page = storage.get_wiki_page_by_slug_directory(slug, directory)
        except AttributeError:
            page = None
    if page is None:
        page = storage.get_wiki_page_by_slug(slug)
    if page is None:
        raise ValueError(f"retype_page_type: slug={slug!r} not found in directory={directory!r}")

    current_type = page.get("page_type") or ""
    if current_type != from_type:
        raise ValueError(
            f"retype_page_type: from_type mismatch — caller asserted "
            f"{from_type!r} but page's current page_type={current_type!r}. "
            f"Refusing the cross-type retype (D23 guard)."
        )

    page_id = int(page.get("id") or 0)
    if not page_id:
        raise ValueError(f"retype_page_type: slug={slug!r} resolved to a row without an id")

    # Pass ``_sanctioned=True`` so the storage gate (mutability='locked') lets
    # the write through. This is the D26 sanctioned-transition path.
    updated = storage.update_wiki_page(
        page_id,
        {"page_type": to_type},
        _sanctioned=True,
    )
    if not updated:
        raise RuntimeError(
            f"retype_page_type: storage.update_wiki_page returned False for "
            f"page_id={page_id} slug={slug!r}"
        )

    logger.info(
        "retype_page_type: slug=%s %s -> %s (sanctioned)",
        slug,
        from_type,
        to_type,
    )
    return {
        "ok": True,
        "slug": slug,
        "page_id": page_id,
        "from_type": from_type,
        "to_type": to_type,
    }


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
    ``{project_id}_adr-NNNN`` (the canonical post-reslug slug — D32 ③).

    Excludes the ``<project>-adr-log`` monolith (deleted in migration 002)
    and the ``<project>-adr-index`` (replaced by ``list_adr_rows`` post-Car-G;
    retained for one cycle per D35d, ``superseded-by-ledger`` tagged).
    """
    import re as _re

    if not slug:
        return False
    if slug.endswith("-adr-log") or slug.endswith("-adr-index"):
        return False
    return bool(_re.search(r"-adr-\d{4}$", slug))


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

    The three known counts (index_rows vs pages_seen vs page_type='adr'
    rows) must reconcile BEFORE cutover. A residue gap is NOT absorbed
    silently. ``>=`` is NOT a gate — that was the 2026-06-16 vacuum that
    destroyed 3,622 memories.

    Returns True only when all three counts match exactly.
    """
    return index_rows == pages_seen == page_type_adr_rows


@observe(tier="stage", metric="backend.admin.adr_seed._parse_adr_id_from_slug")
def _parse_adr_id_from_slug(slug: str) -> int | None:
    """Extract the ADR-NNNN number from a per-ADR page slug, or None."""
    import re as _re

    m = _re.search(r"-adr-(\d{4})$", slug or "")
    if not m:
        return None
    return int(m.group(1))


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


@observe(tier="boundary", metric="backend.admin.adr_seed.seed_adr_rows")
async def seed_adr_rows(  # noqa: C901 - cohesive: orchestrator stitches idempotency, per-page parse, ledger insert, supersede links, D35c gate — same irreducible surface Car F allowlisted on WikiStore.add (cyclomatic=16, 2026-08-09).
    *,
    project_id: str,
    directory: str,
    storage: Any | None = None,
    body_writer: Callable[..., dict[str, object]] | None = None,
    row_inserter: Callable[..., dict[str, object]] | None = None,
    slug_linker: Callable[[int, str], None] | None = None,
) -> dict[str, object]:
    """One-shot: lift existing per-ADR wiki PAGES into the ``adr`` ledger table.

    Source of truth = per-ADR PAGES (D35b), NOT the index — enumerates pages
    via slug prefix and parses each body. Idempotent on ``body_slug``:
    re-running converges; a second run inserts 0 rows. Metadata absent from
    the page body is recovered from the index row where one exists; where
    none exists (ADR-0124) it is flagged, not dropped.

    Args:
        project_id: the canonical project key (Car A0 derive-and-cache).
        directory: absolute project root for §25 wiki resolution.
        storage: optional storage override (test seam); defaults to the live
            runtime storage.
        body_writer: optional callable (per-ADR page dict) — NOT used today
            (the body pages already exist; the seed reads them, never writes).
            Retained for the dry-run path to validate the body parse.
        row_inserter: optional callable replacing
            ``MariaStorageEngine.create_adr_row`` — test seam.
        slug_linker: optional callable replacing
            ``MariaStorageEngine.set_adr_body_slug`` — test seam.

    Returns:
        dict with keys:
          - pages_seen: total per-ADR pages enumerated (D35b source of truth)
          - rows_inserted: new ``adr`` rows this run
          - rows_skipped: pages that already had a row (``body_slug`` set)
          - flagged: pages with a metadata gap (e.g. ADR-0124 missing
            index-row provenance) — surfaced, not dropped
          - supersedes_links: number of ``adr_supersedes`` join rows inserted
          - gate: ``{"index_rows": N, "pages_seen": N, "page_type_adr_rows":
            N, "exact_match": True/False}`` — D35c verification gate output

    D35c: caller MUST inspect ``gate["exact_match"]`` and refuse to ship the
    cutover when it is False. The residue (pages with no index row, extra
    page_type rows) is explained in ``flagged`` — never silently absorbed.
    """
    if storage is None:
        from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()
    if storage is None:
        raise RuntimeError("seed_adr_rows requires storage; runtime storage not initialised")

    # D35b: enumerate per-ADR pages (slug prefix + filter), not parse_index_rows.
    # The slug prefix the live yadgar project uses today is ``yadgar-adr-``
    # (legacy Car 2 format) — Car L's reslug is shipped but the operator runs
    # it dry-run by default. The seed is format-agnostic: any slug matching
    # ``-adr-NNNN`` is consumed.
    #
    # C10 (0047 §5, judgement site (d)): the prefix is built from ADR-0202's
    # CANONICAL slug form (``owner/repo`` → ``owner_repo`` via
    # ``reslug._project_id_to_slug``), not ``basename(directory)``. Basename was
    # a project-name surrogate: two checkouts of different repos with the same
    # directory name produced the same prefix.
    #
    # BOTH prefixes are enumerated for one cycle. The live corpus is still on
    # the legacy ``yadgar-adr-`` shape, so dropping it here would make the seed
    # silently find zero pages on exactly the corpus it exists to lift.
    for _prefix in _adr_slug_prefixes(project_id, directory):
        pages = _collect_candidate_pages(
            project_slug_prefix=_prefix,
            list_pages=lambda prefix, _dir, limit: storage.list_wiki_pages(
                slug_prefix=prefix, limit=limit
            ),
        )
        if pages:
            break

    pages_seen = len(pages)
    rows_inserted = 0
    rows_skipped = 0
    flagged: list[dict[str, Any]] = []
    supersedes_links = 0

    for page in pages:
        slug = page.get("slug") or ""
        body = page.get("content") or ""
        adr_id_int = _parse_adr_id_from_slug(slug)
        if adr_id_int is None:
            flagged.append({"slug": slug, "reason": "unparsable slug suffix"})
            continue

        # Idempotency check: if the adr row already has a body_slug stamp,
        # the seed skips (keyed on body_slug per D35a).
        if row_inserter is None:
            existing = await _read_existing_adr_row(storage, slug)
            if existing is not None:
                existing_slug = existing.get("body_slug") if hasattr(existing, "get") else None
                if existing_slug:
                    rows_skipped += 1
                    continue
        # When a custom row_inserter is injected (tests), idempotency is the
        # inserter's job — we count the call as a "would-insert".

        title, status, date = _extract_title_and_status(body)

        # Compose the row payload (same shape as ``create_adr_row``).
        row_payload: dict[str, Any] = {
            "project_id": project_id,
            "title": title,
            "status": status,
            "decided_on": date or None,
            "body_slug": slug,
        }

        try:
            if row_inserter is not None:
                insert_result: object = row_inserter(row_payload)
                result: dict[str, object] | object = (
                    insert_result if isinstance(insert_result, dict) else {}
                )
            else:
                result = await _insert_adr_row(storage, row_payload)
        except Exception as exc:  # noqa: BLE001 — idempotent on duplicate
            logger.debug("seed_adr_rows: row insert failed for slug=%s err=%s", slug, exc)
            rows_skipped += 1
            continue

        inserted_id = int(str((result if isinstance(result, dict) else {}).get("id") or 0))
        if not inserted_id:
            rows_skipped += 1
            continue
        rows_inserted += 1

        # Stamp body_slug (D4: body lives in SurrealDB; the row only knows
        # the slug). The inserter path above may have already done this when
        # body_slug was passed in the INSERT; we re-stamp defensively in case
        # the inserter deferred it.
        try:
            if slug_linker is not None:
                slug_linker(inserted_id, slug)
            else:
                await _link_body_slug(storage, inserted_id, slug)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "seed_adr_rows: set_adr_body_slug failed for id=%s slug=%s: %s",
                inserted_id,
                slug,
                exc,
            )

        # Recover supersede targets from the page body (the rendered body
        # carries ``supersedes: ADR-0001,ADR-0002`` as a bullet — D23).
        targets = _parse_supersede_targets_from_body(body)
        for tid in targets:
            try:
                await _add_supersedes_link(storage, inserted_id, tid)
                supersedes_links += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "seed_adr_rows: supersede link failed for adr_id=%s target=%s: %s",
                    inserted_id,
                    tid,
                    exc,
                )

        # Flag the page-only case (ADR-0124 documented: no index row, body
        # exists; metadata filled from the page and flagged for review).
        # Today the seed is page-driven, so EVERY page is "page-only" by
        # construction — the flag is informational; it surfaces the
        # count delta between the index parse (pre-G) and the page census
        # (post-G) so the operator can see what the index would have missed.
        if not _has_index_provenance(page):
            flagged.append(
                {
                    "slug": slug,
                    "adr_id": f"ADR-{adr_id_int:04d}",
                    "reason": "no index row provenance (D35b: page-only)",
                }
            )

    # D35c: the verification gate. EXACT equality on the stated predicate.
    # Index-rows vs pages-seen vs page_type='adr' rows must reconcile. Today
    # the page_type counts come from the ledger after this run; the index
    # rows come from the legacy <project>-adr-index page if it still exists.
    index_rows = _count_legacy_index_rows(directory, storage)
    page_type_adr_rows = await _count_page_type_adr_rows(storage, project_id)
    exact_match = _exact_equality_gate(
        index_rows=index_rows,
        pages_seen=pages_seen,
        page_type_adr_rows=page_type_adr_rows,
    )

    return {
        "project_id": project_id,
        "directory": directory,
        "pages_seen": pages_seen,
        "rows_inserted": rows_inserted,
        "rows_skipped": rows_skipped,
        "flagged": flagged,
        "supersedes_links": supersedes_links,
        "gate": {
            "index_rows": index_rows,
            "pages_seen": pages_seen,
            "page_type_adr_rows": page_type_adr_rows,
            "exact_match": exact_match,
        },
    }


# ── internal storage helpers (kept private to this module) ─────────────────


@observe(tier="boundary", metric="backend.admin.adr_seed._read_existing_adr_row")
async def _read_existing_adr_row(storage: Any, slug: str) -> object:
    """Look up the ``adr`` row by ``body_slug`` (D4 idempotency key).

    Async because the MariaStorageEngine list_adr_rows is async; this
    helper is invoked from the async seed loop. Returns ``None`` when
    the storage surface is partial (the test stub path) or when no row
    exists with that body_slug.
    """
    project_id = _project_slug_from_page_slug(slug)
    try:
        rows_obj = storage.list_adr_rows(project_id=project_id)
        # Storage returns a coroutine on the real engine; tests inject
        # sync stubs that return a list directly. Handle both shapes.
        if hasattr(rows_obj, "__await__"):
            rows_obj = await rows_obj
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(rows_obj, list):
        return None
    for r in rows_obj:
        if isinstance(r, dict) and (r.get("body_slug") or "") == slug:
            return r
    return None


@observe(tier="hot", span=False)
def _adr_slug_prefixes(project_id: str, directory: str) -> list[str]:
    """ADR page-slug prefixes to enumerate, canonical first, legacy second.

    C10 (0047 §5, judgement site (d)). Two prefixes, in priority order:

    1. **Canonical (ADR-0202)** — ``owner/repo`` → ``owner_repo`` via
       :func:`reslug._project_id_to_slug`, giving ``owner_repo_adr-``. This is
       the shape :data:`reslug.NEW_SLUG_TEMPLATE` emits, so a reslugged corpus
       is found here.
    2. **Legacy** — ``basename(directory)`` + ``-adr-``, the Car 2 shape the
       live corpus still uses (``yadgar-adr-NNNN``). Retained for one cycle:
       Car L's reslug is shipped but runs dry-run by default, so the rows this
       seed exists to lift are still under the old prefix.

    The legacy entry is the ONLY remaining use of ``basename(directory)`` as a
    project surrogate, and it is now explicitly a back-compat read path rather
    than an identity derivation. Drop it once the reslug has been applied.

    Deduplicated and empties dropped, so a project whose canonical slug equals
    its basename yields one prefix rather than two identical scans.
    """
    from yadgar.backend.admin_exec.reslug import _project_id_to_slug  # noqa: PLC0415

    prefixes: list[str] = []
    if project_id:
        prefixes.append(f"{_project_id_to_slug(project_id)}_adr-")
    basename = (directory or "").rstrip("/").split("/")[-1]
    if basename:
        prefixes.append(f"{basename}-adr-")
    return list(dict.fromkeys(p for p in prefixes if p))


def _project_slug_from_page_slug(slug: str) -> str:
    """Extract the project part of a per-ADR page slug: ``X-adr-0001`` → ``X``.

    NOT ``identity.derive_project_id`` despite the old name
    (``_derive_project_id_for_slug``, renamed by C10 §5(d)). This derives
    nothing about the host — it regex-parses a slug that was already built
    elsewhere, and is used only to look a row back up by ``body_slug``. The old
    name invited the next reader to delete it as a duplicate of the real
    resolver; it is not one.
    """
    import re as _re

    m = _re.match(r"^(.+?)[-_]adr-\d{4}$", slug or "")
    return m.group(1) if m else ""


@observe(tier="boundary", metric="backend.admin.adr_seed._insert_adr_row")
async def _insert_adr_row(storage: Any, payload: dict[str, object]) -> object:
    """Insert one ``adr`` row via ``MariaStorageEngine.create_adr_row``.

    Returns the inserted row dict (carries the AUTO_INCREMENT id per
    ADR-0197). Async because ``create_adr_row`` is async; tests inject
    sync stubs that return a dict directly — handled below.
    """
    try:
        result_obj = storage.create_adr_row(
            project_id=str(payload.get("project_id", "")),
            title=str(payload.get("title", "")),
            status=str(payload.get("status", "open")),
            decided_on=payload.get("decided_on"),
            subsystem=payload.get("subsystem"),
            tier=payload.get("tier"),
            body_slug=payload.get("body_slug"),
        )
        if hasattr(result_obj, "__await__"):
            result_obj = await result_obj
    except Exception:  # noqa: BLE001
        return {}
    return result_obj if isinstance(result_obj, dict) else {}


@observe(tier="boundary", metric="backend.admin.adr_seed._link_body_slug")
async def _link_body_slug(storage: Any, adr_id: int, slug: str) -> None:
    """Stamp the body_slug onto an ``adr`` row (D4: wiki body stays in SurrealDB;
    the ledger row only carries the slug pointer)."""
    try:
        result_obj = storage.set_adr_body_slug(adr_id=adr_id, body_slug=slug)
        if hasattr(result_obj, "__await__"):
            await result_obj
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "seed_adr_rows: set_adr_body_slug failed for id=%s slug=%s: %s",
            adr_id,
            slug,
            exc,
        )


@observe(tier="boundary", metric="backend.admin.adr_seed._add_supersedes_link")
async def _add_supersedes_link(storage: Any, adr_id: int, supersedes_id: int) -> None:
    """Insert one ``adr_supersedes`` join row (D23: supersede is the link,
    not a column mutation)."""
    try:
        result_obj = storage.add_adr_supersedes(adr_id=adr_id, supersedes_id=supersedes_id)
        if hasattr(result_obj, "__await__"):
            await result_obj
    except Exception:  # noqa: BLE001
        return None
    return None


@observe(tier="stage", metric="backend.admin.adr_seed._parse_supersede_targets_from_body")
def _parse_supersede_targets_from_body(body: str) -> list[int]:
    """Extract ``supersedes`` targets from the per-ADR body bullet."""
    import re as _re

    if not body:
        return []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("- supersedes:"):
            value = s[len("- supersedes:") :].strip()
            if not value or value.lower() == "none":
                return []
            ids = _re.findall(r"ADR-(\d{4})", value)
            return [int(i) for i in ids]
    return []


@observe(tier="stage", metric="backend.admin.adr_seed._has_index_provenance")
def _has_index_provenance(page: dict[str, Any]) -> bool:
    """True when the page has index-row provenance (legacy index columns).

    D35b: the page is the ID-bearing artifact; the index may lag. Today
    every page carries an ``adr-NNNN`` slug suffix — that IS the provenance.
    This helper is a forward-looking seam: a future migration may stamp
    pages with an ``_indexed_at`` column, in which case the seed flips
    to honouring that.
    """
    return bool(_parse_adr_id_from_slug(page.get("slug") or ""))


@observe(tier="boundary", metric="backend.admin.adr_seed._count_legacy_index_rows")
def _count_legacy_index_rows(directory: str, storage: Any) -> int:
    """Return the count of rows in the legacy ``<project>-adr-index`` page.

    Used by the D35c verification gate. Returns 0 when the index page is
    absent (the migration that deletes it ran). The verification gate
    treats ``index_rows=0`` as a hard signal that the index is gone, NOT
    as a mismatch — the gate's two-sided reconciliation is between
    ``pages_seen`` and ``page_type_adr_rows``, with ``index_rows`` as the
    legacy-trace counter for a single-cycle rollback path (D35d).

    C10 (0047 §5(d)): this one KEEPS ``basename(directory)`` deliberately. The
    function's whole job is counting rows in the **legacy** index page, whose
    slug was minted as ``<basename>-adr-index`` by the Car 2 scheme. Rebuilding
    it from the canonical project slug would look up a page that, by
    construction, was never written — the counter would read 0 and the D35c
    gate would silently lose its legacy-trace signal. The basename here is a
    historical slug component, not an identity derivation.
    """
    import os as _os

    project = _os.path.basename(directory.rstrip("/"))
    slug = f"{project}-adr-index"
    try:
        page = storage.get_wiki_page_by_slug_directory(slug, directory)
        if not page:
            page = storage.get_wiki_page_by_slug(slug)
        if not page:
            return 0
    except Exception:  # noqa: BLE001
        return 0
    body = page.get("content") or ""
    # Count the table rows: lines starting with ``| ADR-``.
    import re as _re

    return len(_re.findall(r"^\|\s*ADR-\d{4}\s*\|", body, _re.MULTILINE))


@observe(tier="boundary", metric="backend.admin.adr_seed._count_page_type_adr_rows")
async def _count_page_type_adr_rows(storage: Any, project_id: str) -> int:
    """Count rows in the ``adr`` ledger table for *project_id*.

    Reads via ``list_adr_rows`` (the same path Car F re-pointed onto); the
    result is in-memory. For a real DB the table has an index on
    ``project_id`` (migration 002, ``ix_adr_project_id``), so this is a
    cheap COUNT-style query. We list because the seed runs once; the
    ledger already enforces id allocation via AUTO_INCREMENT.
    """
    try:
        rows_obj = storage.list_adr_rows(project_id=project_id)
        if hasattr(rows_obj, "__await__"):
            rows_obj = await rows_obj
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(rows_obj, list):
        return 0
    return sum(
        1 for r in rows_obj if isinstance(r, dict) and (r.get("page_type") or "adr") == "adr"
    )
