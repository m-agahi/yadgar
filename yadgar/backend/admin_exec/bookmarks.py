"""Backend execution bodies for the bookmark CRUD admin ops (R3 Car 3a / R5).

These are the storage-write halves of the core ``bookmark_*`` MCP tools. The
core ``@_tool`` shells keep validation + the MCP schema and forward the write
here over HTTP (POST /admin) via ``_forward_admin``. Read tools (bookmark_list)
stay core.

Each op is an undecorated ``(payload: dict) -> dict`` function; the ``@observe``
decorators satisfy the I33 tri-signal ratchet. Storage is fetched via the shared
lifecycle getter (the /admin route builds the slim engine set first).
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="backend.admin.bookmark_add")
def bookmark_add(payload: dict) -> dict:
    """Add or update a bookmark (idempotent on slug). Storage-write half.

    payload: {"slug": str, "label_override": str}
    Returns {added: bool, slug, position, label_override} or {added: False, reason}.
    Slug is already stripped + non-empty (validated core-side).
    """
    slug = payload["slug"]
    label_override = payload.get("label_override") or ""
    storage = _get_storage()

    try:
        result = storage.add_bookmark(slug, label_override=label_override)
    except ValueError as exc:
        return {"added": False, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - surface as reason, never 500 the CRUD
        logger.warning("bookmark_add error slug=%s: %s", slug, exc)
        return {"added": False, "reason": str(exc)}

    return {
        "added": True,
        "slug": result.get("slug", slug),
        "position": result.get("position", 0),
        "label_override": result.get("label_override"),
    }


@observe(tier="boundary", metric="backend.admin.bookmark_remove")
def bookmark_remove(payload: dict) -> dict:
    """Remove a bookmark (idempotent). Storage-write half.

    payload: {"slug": str}
    Returns {removed: bool, slug} or {removed: False, reason}.
    """
    slug = payload["slug"]
    storage = _get_storage()

    try:
        removed = storage.remove_bookmark(slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bookmark_remove error slug=%s: %s", slug, exc)
        return {"removed": False, "reason": str(exc)}

    return {"removed": removed, "slug": slug}


@observe(tier="boundary", metric="backend.admin.bookmark_reorder")
def bookmark_reorder(payload: dict) -> dict:
    """Move a bookmark to a new position (dense-integer shift). Storage-write half.

    payload: {"slug": str, "new_position": int}
    Returns {reordered: bool, slug, new_position} or {reordered: False, reason}.
    """
    slug = payload["slug"]
    new_position = int(payload["new_position"])
    storage = _get_storage()

    try:
        reordered = storage.reorder_bookmark(slug, new_position)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bookmark_reorder error slug=%s pos=%s: %s", slug, new_position, exc)
        return {"reordered": False, "reason": str(exc)}

    return {"reordered": reordered, "slug": slug, "new_position": new_position}
