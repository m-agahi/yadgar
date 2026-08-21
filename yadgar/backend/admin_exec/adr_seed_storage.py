"""ADR-seed storage helpers — the leaf calls ``seed_adr_rows`` makes.

A MOVE, not an API change (car 19, ledger task 176). These eight functions
lived at the bottom of ``adr_seed.py`` under their own
``── internal storage helpers ──`` banner; they are lifted here verbatim
because ``adr_seed.py`` sat 6 lines under the I30 ``file_loc`` hard cap of
1000 and had no room for the dry-run guard preflight task 176 asked for.
Same response the previous car made to the same cap on the same file — see
``adr_retype.py`` (``retype_page_type`` moved 2026-08-18).

``adr_seed`` re-imports every name at module level, so
``adr_seed._adr_slug_prefixes`` still resolves and the existing
``monkeypatch.setattr(adr_seed, "_adr_slug_prefixes", ...)`` seam still binds
— the call sites inside ``seed_adr_rows`` stay BARE names for exactly that
reason. Qualifying them (``adr_seed_storage._adr_slug_prefixes(...)``) would
kill that patch silently.
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


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


@observe(tier="boundary", metric="backend.admin.adr_seed._insert_adr_row")
async def _insert_adr_row(sql_storage: Any, payload: dict[str, object]) -> object:
    """Insert one ``adr`` row via ``MariaStorageEngine.create_adr_row``.

    Returns the inserted row dict (carries the AUTO_INCREMENT id per ADR-0197).
    Async because ``create_adr_row`` is async; tests inject sync stubs that
    return a dict directly — handled below.

    RAISES: the ``except Exception: return {}`` that used to sit here is what
    made the wrong-engine defect silent. The caller cannot distinguish a
    structural fault from a per-row failure if this swallows both.
    """
    result_obj = sql_storage.create_adr_row(
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
    return result_obj if isinstance(result_obj, dict) else {}


@observe(tier="boundary", metric="backend.admin.adr_seed._link_body_slug")
async def _link_body_slug(sql_storage: Any, adr_id: int, slug: str) -> None:
    """Stamp the body_slug onto an ``adr`` row (D4: wiki body stays in SurrealDB;
    the ledger row only carries the slug pointer).

    Best-effort: the row already carries its slug from the INSERT, so a failure
    here is logged at WARNING and never counted as a row failure.
    """
    try:
        result_obj = sql_storage.set_adr_body_slug(adr_id=adr_id, body_slug=slug)
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
async def _add_supersedes_link(sql_storage: Any, adr_id: int, supersedes_id: int) -> bool:
    """Insert one ``adr_supersedes`` join row (D23: supersede is the link,
    not a column mutation).

    Returns whether the link landed. It used to return ``None`` on both paths
    while swallowing the exception, so the caller's ``supersedes_links += 1``
    counted ATTEMPTS and reported failed links as successes — the same
    silent-success family as the rest of this file.
    """
    try:
        result_obj = sql_storage.add_adr_supersedes(adr_id=adr_id, supersedes_id=supersedes_id)
        if hasattr(result_obj, "__await__"):
            await result_obj
    except Exception as exc:  # noqa: BLE001 — a dangling target is expected residue
        logger.warning(
            "seed_adr_rows: supersede link failed for adr_id=%s target=%s: %s",
            adr_id,
            supersedes_id,
            exc,
        )
        return False
    return True


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
    except AttributeError:
        # A PARTIAL wiki surface only — the stub storages the unit tests inject
        # expose ``list_wiki_pages`` and nothing else. Narrowed from a blanket
        # ``except Exception`` (Car 4): a real query error must not read as
        # "the index page is gone", which is what the D35c gate would then
        # silently absorb as a legacy-trace of 0.
        return 0
    body = page.get("content") or ""
    # Count the table rows: lines starting with ``| ADR-``.
    import re as _re

    return len(_re.findall(r"^\|\s*ADR-\d{4}\s*\|", body, _re.MULTILINE))


@observe(tier="boundary", metric="backend.admin.adr_seed._count_page_type_adr_rows")
async def _count_page_type_adr_rows(sql_storage: Any, project_id: str) -> int:
    """Count rows in the ``adr`` ledger table for *project_id*.

    Reads via ``list_adr_rows`` (the same path Car F re-pointed onto); the
    result is in-memory. For a real DB the table has an index on
    ``project_id`` (migration 002, ``ix_adr_project_id``), so this is a
    cheap COUNT-style query. We list because the seed runs once; the
    ledger already enforces id allocation via AUTO_INCREMENT.

    Reaches the LEDGER handle. It used to reach the wiki one, where
    ``list_adr_rows`` does not exist — so this counter read 0 on every
    production call and the D35c gate reconciled against a fiction.
    """
    if sql_storage is None:
        return 0
    rows_obj = sql_storage.list_adr_rows(project_id=project_id)
    if hasattr(rows_obj, "__await__"):
        rows_obj = await rows_obj
    if not isinstance(rows_obj, list):
        return 0
    return sum(
        1 for r in rows_obj if isinstance(r, dict) and (r.get("page_type") or "adr") == "adr"
    )
