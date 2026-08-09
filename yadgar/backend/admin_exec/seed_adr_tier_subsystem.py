"""Car H (0047 spine train) — one-shot seed backfill for tier + subsystem.

D35a one-shot admin op (same shape as ``seed_adr_rows`` from Car G).
Backfills ``tier`` and ``subsystem`` columns on existing ``adr`` ledger
rows that the migration 002 columns were inert for (NULL on every row).

D27 (``tier``): derived from ``status`` — superseded/rejected/deprecated
→ ``historical``; accepted/open → ``binding``. Car H uses this rule so
the seed is fully deterministic; an operator can over-stamp any row
via the ``update_adr_tier_subsystem`` helper.

D28 (``subsystem``): EXPLICIT (never inferred from the title). The seed
parses a ``## Subsystem`` markdown header from each row's wiki body
when present; otherwise leaves the row at NULL (the row's body_slug
pointers the operator to the per-ADR page for manual assignment).
Car H chose the header-parse path over bulk-infer-from-title because
D28 forbids inference — a heuristic that produces silent wrong answers
is worse than surfacing a list of rows that need operator review.

§10 Q2 vocabulary: subsystem is a free-form VARCHAR(128) + on-write
normalizer. The seed applies the same normalizer on the parsed value so
the row filter keys the canonical lowercase form.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


# ── Pure helpers (extracted for I13 fn_loc + unit testability) ────────────────


_HISTORICAL_STATUSES: frozenset[str] = frozenset({"superseded", "rejected", "deprecated"})


@observe(exempt="trivial dict-field predicate; no I/O, no error branch worth spanning")
def _is_already_stamped(row: dict[str, Any]) -> bool:
    """True when a row already has BOTH ``tier`` AND ``subsystem`` set.

    Both columns must be non-None AND non-empty for the seed to skip —
    the seed's job is to stamp the inert columns, not to overwrite
    operator-supplied values.
    """
    tier = row.get("tier")
    subsystem = row.get("subsystem")
    return bool(tier) and bool(subsystem)


@observe(exempt="trivial status→tier mapping; no I/O, no error branch worth spanning")
def _classify_tier_from_status(status: str | None) -> str:
    """Map an ``adr.status`` value to a D27 ``tier``.

    ``superseded`` | ``rejected`` | ``deprecated`` → ``historical``;
    ``accepted`` | ``open`` → ``binding``. Unknown / None → ``binding``
    (the default for an open decision).
    """
    if status and status in _HISTORICAL_STATUSES:
        return "historical"
    return "binding"


@observe(
    exempt="trivial regex parse over markdown body; no I/O, no external call, no error branch worth spanning"
)
def _extract_subsystem_from_body(body: str) -> str | None:
    """Parse a per-ADR body for a ``## Subsystem`` header value.

    The body shape (built by ``adr_render._build_adr_body``) is
    ``# ADR-NNNN: <title>\\n...## Context...## Decision...``. A
    ``## Subsystem`` header carries the value as the next non-empty line.
    Returns ``None`` when the header is absent or empty.

    Pure — no I/O, no DB access.
    """
    if not body:
        return None
    lines = body.splitlines()
    in_subsystem_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## ") and stripped.lower().startswith("## subsystem"):
            in_subsystem_section = True
            continue
        if in_subsystem_section:
            if not stripped:
                continue
            if stripped.startswith("## "):
                return None  # next section without a value → no subsystem
            return stripped
    return None


# ── Body fetch (test seam) ────────────────────────────────────────────────────


@observe(tier="stage", metric="backend.admin.seed_adr_tier_subsystem._fetch_body_via_wiki")
async def _fetch_body_via_wiki(body_reader: Callable[[str], dict[str, Any]], slug: str) -> str:
    """Fetch the body markdown via the injected reader; never raises.

    The reader is the test seam (Car G's pattern: a callable injected by
    the seed op so tests can stub without booting storage). Returns
    empty string on failure or when the page is absent.
    """
    try:
        page = body_reader(slug)
        if hasattr(page, "__await__"):
            page = await page
    except Exception as exc:  # noqa: BLE001
        logger.debug("_fetch_body_via_wiki: reader failed for slug=%s err=%s", slug, exc)
        return ""
    if not isinstance(page, dict):
        return ""
    return str(page.get("content") or "")


# ── Row update (test seam) ────────────────────────────────────────────────────


@observe(tier="stage", metric="backend.admin.seed_adr_tier_subsystem._apply_row_update")
async def _apply_row_update(
    updater: Callable[..., Any] | None,
    storage: Any | None,
    adr_id: int,
    tier: str,
    subsystem: str | None,
) -> None:
    """Stamp ``tier`` + ``subsystem`` on one ``adr`` row.

    When *updater* is supplied (test seam), it is invoked with kwargs
    ``adr_id``, ``tier``, ``subsystem``. Otherwise the function falls
    back to a live UPDATE on the SQL engine — but that path requires
    a new ``MariaStorageEngine.update_adr_tier_subsystem`` helper
    which is out of H scope (the seed's row writes today go through
    the existing ``update_task_row``-shape pattern; the H helper is a
    future addition).

    For Car H the seed uses a direct UPDATE via the storage's
    ``_engine`` connection when no test seam is supplied, so the
    seed is end-to-end runnable without a new storage method.
    """
    if updater is not None:
        await updater(adr_id=adr_id, tier=tier, subsystem=subsystem)
        return
    if storage is None:
        from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

        storage = _get_sql_storage()
    if storage is None:
        raise RuntimeError(
            "seed_adr_tier_subsystem requires storage; runtime storage not initialised"
        )
    # Direct UPDATE — the SQL engine exposes _engine from asyncmy; same
    # shape as ``set_adr_body_slug`` (storage/wiki.py:533-539).
    from sqlalchemy import text as _sa_text  # noqa: PLC0415

    async with storage._engine.begin() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            _sa_text("UPDATE adr SET tier = :tier, subsystem = :subsystem WHERE id = :id"),
            {"id": adr_id, "tier": tier, "subsystem": subsystem},
        )


# ── Per-row stamp helper (extracted for I13 fn_loc + cyclomatic caps) ────────


@observe(tier="stage", metric="backend.admin.seed_adr_tier_subsystem._stamp_one_row")
async def _stamp_one_row(
    row: dict[str, Any],
    *,
    body_reader: Callable[..., Any],
    row_updater: Callable[..., Any] | None,
    storage: Any,
) -> tuple[bool, str | None]:
    """Process one ledger row: derive tier + subsystem, apply UPDATE.

    Returns:
        ``(was_stamped, subsystem_unparsed_slug_or_none)``.
        ``was_stamped`` True when the row was updated; False when skipped
        (already stamped or missing adr_id). ``subsystem_unparsed_slug``
        is the body_slug the seed could not parse a subsystem from
        (operator reviews) or None.
    """
    if _is_already_stamped(row):
        return False, None

    adr_id = int(row.get("id") or 0)
    if not adr_id:
        return False, None

    status = row.get("status")
    tier = _classify_tier_from_status(status)

    # Parse subsystem from body when available. D28 forbids inferring
    # from the title; absent parse → NULL (operator fills later).
    body_slug = str(row.get("body_slug") or "")
    subsystem: str | None = None
    unparsed_slug: str | None = None
    if body_slug:
        body = await _fetch_body_via_wiki(body_reader, body_slug)
        raw = _extract_subsystem_from_body(body)
        if raw is not None:
            # §10 Q2 normalizer (mirror of core adr.py::_normalize_subsystem)
            normalized = raw.strip().lower()
            subsystem = normalized or None
        else:
            unparsed_slug = body_slug

    await _apply_row_update(row_updater, storage, adr_id, tier, subsystem)
    return True, unparsed_slug


# ── Admin op entry point ─────────────────────────────────────────────────────


@observe(tier="boundary", metric="backend.admin.seed_adr_tier_subsystem.seed_adr_tier_subsystem")
async def seed_adr_tier_subsystem(
    payload: dict[str, Any],
    *,
    storage: Any | None = None,
    body_reader: Callable[..., Any] | None = None,
    row_updater: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """One-shot: backfill ``tier`` + ``subsystem`` on existing ``adr`` rows.

    Args:
        payload: ``{"project_id": str}`` (required). The seed operates
            per-project.
        storage: optional storage override (test seam).
        body_reader: optional callable ``(slug) -> {"content": str, ...}``
            for the per-ADR body fetch. Defaults to ``wiki_read`` from the
            core sanctioned read path.
        row_updater: optional callable ``(adr_id, tier, subsystem) -> None``
            for the row UPDATE (test seam). Defaults to a direct SQL
            UPDATE on the storage's ``_engine`` connection.

    Returns:
        ``{"ok": True, "rows_scanned": N, "rows_updated": N, "rows_skipped": N,
        "subsystem_unparsed": [slug, ...]}`` — ``rows_skipped`` counts
        rows that were already stamped; ``subsystem_unparsed`` lists the
        page slugs the seed could NOT derive a subsystem from (the
        operator reviews these for manual assignment per D28).
    """
    project_id = payload.get("project_id")
    if not project_id:
        return {"ok": False, "error": "project_id is required"}

    if storage is None:
        from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

        storage = _get_sql_storage()
    if storage is None:
        raise RuntimeError(
            "seed_adr_tier_subsystem requires storage; runtime storage not initialised"
        )

    # Default body reader — wiki page fetch from storage directly (avoids the
    # core->backend import cycle on ``yadgar.core.server.tools.wiki``).
    if body_reader is None:

        @observe(tier="stage", metric="backend.admin.seed_adr_tier_subsystem._default_reader")
        def _default_reader(slug: str) -> dict[str, Any]:
            page = storage.get_wiki_page_by_slug(slug)
            if page is None:
                return {}
            return page

        body_reader = _default_reader

    rows_obj = storage.list_adr_rows(
        project_id=str(project_id),
        status=None,
        tier=None,
        subsystem=None,
    )
    if hasattr(rows_obj, "__await__"):
        rows = await rows_obj
    else:
        rows = rows_obj

    if not isinstance(rows, list):
        rows = []

    rows_scanned = len(rows)
    rows_updated = 0
    rows_skipped = 0
    subsystem_unparsed: list[str] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        stamped, unparsed_slug = await _stamp_one_row(
            row,
            body_reader=body_reader,
            row_updater=row_updater,
            storage=storage,
        )
        if stamped:
            rows_updated += 1
            if unparsed_slug is not None:
                subsystem_unparsed.append(unparsed_slug)
        else:
            rows_skipped += 1

    logger.info(
        "seed_adr_tier_subsystem: project_id=%s rows_scanned=%d rows_updated=%d "
        "rows_skipped=%d subsystem_unparsed=%d",
        project_id,
        rows_scanned,
        rows_updated,
        rows_skipped,
        len(subsystem_unparsed),
    )
    return {
        "ok": True,
        "project_id": str(project_id),
        "rows_scanned": rows_scanned,
        "rows_updated": rows_updated,
        "rows_skipped": rows_skipped,
        "subsystem_unparsed": subsystem_unparsed,
    }


__all__ = [
    "_apply_row_update",
    "_classify_tier_from_status",
    "_extract_subsystem_from_body",
    "_fetch_body_via_wiki",
    "_is_already_stamped",
    "_stamp_one_row",
    "seed_adr_tier_subsystem",
]
