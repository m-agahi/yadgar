"""D23 retype mutator — flip ``wiki_page.page_type`` on an ADR supersede.

Split out of ``adr_seed`` (2026-08-18) when that module crossed the I30
``file_loc`` HARD cap of 1000. This function was always the natural seam:
it is WIKI-ONLY (single storage handle) while the seed needs both a wiki
and a SQL handle, and it serves the supersede lifecycle rather than the
backfill. Nothing here is new — the body is unchanged.

D23: the flip is atomic with the row-side status change. It bypasses
``_WIKI_UPDATE_ALLOWED`` because it is a sanctioned server-side lifecycle
transition, not an agent/tool edit (D26: ``locked`` blocks agent edits,
NOT sanctioned transitions).
"""

from __future__ import annotations

import logging
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
