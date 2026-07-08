"""Wiki bookmark MCP tool registrations.

Four tools:
  bookmark_add     — add or update a bookmark (idempotent on slug)
  bookmark_remove  — remove a bookmark (idempotent)
  bookmark_list    — list bookmarks ordered by position
  bookmark_reorder — move a bookmark to a new position (dense-integer shift)

All tools use the unified @_tool() decorator pattern (same as wiki.py, memorize.py, etc.)
and delegate to StorageEngine._BookmarksMixin via yadgar.server._state._storage.
"""

from __future__ import annotations

import logging

from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar.core.server._app import _tool
from yadgar.core.server.tools._forward import _forward_admin

logger = logging.getLogger(__name__)


@_tool()
def bookmark_add(slug: str, label_override: str = "") -> dict:
    """Add or update a wiki bookmark. Idempotent: if slug already bookmarked, updates label.

    Args:
        slug: Wiki page slug to bookmark (e.g. 'yadgar-roadmap-future-improvements').
        label_override: Optional display name. Falls back to wiki title in the UI.

    Returns:
        {added: bool, slug: str, position: int} on success.
        {added: false, reason: str} on validation failure.
    """
    # R3 Car 3a: validate core-side; the storage write forwards to backend /admin.
    slug = (slug or "").strip()
    if not slug:
        return {"added": False, "reason": "slug_empty"}

    return _forward_admin("bookmark_add", {"slug": slug, "label_override": label_override or ""})


@_tool()
def bookmark_remove(slug: str) -> dict:
    """Remove a wiki bookmark. Idempotent: no error if slug not bookmarked.

    Args:
        slug: Wiki page slug to remove.

    Returns:
        {removed: bool, slug: str}
    """
    # R3 Car 3a: storage write forwards to backend /admin.
    slug = (slug or "").strip()
    return _forward_admin("bookmark_remove", {"slug": slug})


@_tool()
def bookmark_list() -> list[dict]:
    """Return all wiki bookmarks ordered by position (ascending).

    Returns:
        List of {slug, label_override, position, added_at} dicts.
    """
    storage = _get_storage()
    if storage is None:
        return []

    try:
        rows = storage.list_bookmarks()
    except Exception as exc:
        logger.warning("bookmark_list error: %s", exc)
        return []

    return [
        {
            "slug": r.get("slug", ""),
            "label_override": r.get("label_override"),
            "position": r.get("position", 0),
            "added_at": str(r.get("added_at") or ""),
        }
        for r in rows
    ]


@_tool()
def bookmark_reorder(slug: str, new_position: int) -> dict:
    """Move a bookmark to a new position; adjacent bookmarks shift to fill gaps.

    Uses dense-integer semantics: positions are always 0, 1, 2, … after reorder.

    Args:
        slug: Wiki page slug of the bookmark to move.
        new_position: Target 0-based position.

    Returns:
        {reordered: bool, slug: str, new_position: int}
    """
    # R3 Car 3a: storage write forwards to backend /admin.
    slug = (slug or "").strip()
    return _forward_admin("bookmark_reorder", {"slug": slug, "new_position": int(new_position)})
