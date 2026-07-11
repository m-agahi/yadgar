"""Wiki bookmark CRUD — stored in `wiki_bookmark` SurrealDB table.

_BookmarksMixin provides:
  - add_bookmark(slug, label_override="") -> dict
  - remove_bookmark(slug) -> bool
  - get_bookmark(slug) -> dict | None
  - list_bookmarks() -> list[dict]
  - reorder_bookmark(slug, new_position) -> bool

Schema (per plan section "Storage — new SurrealDB table"):
  slug          string UNIQUE   — wiki page slug
  label_override option<string> — user display name; None → frontend uses wiki title
  position      int             — ordering (0-based, dense integers)
  added_at      datetime        — creation timestamp

Slug is normalised (stripped whitespace) before storage.
Empty slugs raise ValueError.
add_bookmark is idempotent: calling on an existing slug updates label_override + position.
reorder_bookmark uses dense-integer shift: all bookmarks at or after new_position
shift up by one to make room.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span

_log = logging.getLogger(__name__)


class _BookmarksMixin:
    """Wiki bookmark CRUD — mixed into StorageEngine."""

    # ------------------------------------------------------------------ helpers

    @staticmethod
    @observe(tier="hot")
    def _normalise_slug(slug: str) -> str:
        """Strip whitespace; raise ValueError if empty after stripping."""
        cleaned = slug.strip()
        if not cleaned:
            raise ValueError(f"bookmark slug must not be empty: {slug!r}")
        return cleaned

    # ------------------------------------------------------------------ CRUD

    @trace_span()
    def add_bookmark(self, slug: str, label_override: str = "") -> dict:
        """Insert or update a bookmark. Idempotent on slug.

        If the slug already exists, updates label_override.
        Returns the stored bookmark dict.
        """
        slug = self._normalise_slug(slug)
        now = self._now_iso()

        existing = self.get_bookmark(slug)
        if existing is not None:
            # Update label_override only
            self._q(
                "UPDATE wiki_bookmark SET label_override = $label WHERE slug = $slug",
                {"slug": slug, "label": label_override or None},
            )
            existing["label_override"] = label_override or None
            return existing

        # New row: position = current count (0-based)
        count_rows = self._q("SELECT count() AS cnt FROM wiki_bookmark GROUP ALL")
        count = int(count_rows[0]["cnt"]) if count_rows else 0

        bid = self._next_id("wiki_bookmark")
        self._q(
            "CREATE type::record('wiki_bookmark', $id) SET "
            "slug = $slug, "
            "label_override = $label, "
            "position = $pos, "
            "added_at = $ts",
            {
                "id": bid,
                "slug": slug,
                "label": label_override or None,
                "pos": count,
                "ts": now,
            },
        )
        return self.get_bookmark(slug) or {
            "slug": slug,
            "label_override": label_override or None,
            "position": count,
            "added_at": now,
        }

    @trace_span()
    def remove_bookmark(self, slug: str) -> bool:
        """Delete bookmark by slug. Returns True if deleted, False if not found."""
        slug = slug.strip()
        existing = self.get_bookmark(slug)
        if existing is None:
            return False
        old_pos = int(existing.get("position", 0))
        self._q("DELETE wiki_bookmark WHERE slug = $slug", {"slug": slug})
        # Compact positions: shift down rows above old_pos
        self._q(
            "UPDATE wiki_bookmark SET position = position - 1 WHERE position > $pos",
            {"pos": old_pos},
        )
        return True

    @trace_span()
    def get_bookmark(self, slug: str) -> dict | None:
        """Fetch a single bookmark by slug. Returns None if not found."""
        slug = slug.strip()
        rows = self._q(
            "SELECT * FROM wiki_bookmark WHERE slug = $slug LIMIT 1",
            {"slug": slug},
        )
        return self._row_to_dict(rows[0]) if rows else None

    @trace_span()
    def list_bookmarks(self) -> list[dict]:
        """Return all bookmarks ordered by position ascending."""
        rows = self._q("SELECT * FROM wiki_bookmark ORDER BY position ASC")
        return self._rows_to_dicts(rows)

    @trace_span()
    def reorder_bookmark(self, slug: str, new_position: int) -> bool:
        """Move *slug* to *new_position*; shift other bookmarks to maintain density.

        Returns True if the bookmark was found and moved, False if not found.
        Dense-integer semantics: positions are always 0, 1, 2, … after reorder.
        """
        slug = slug.strip()
        existing = self.get_bookmark(slug)
        if existing is None:
            return False

        old_pos = int(existing.get("position", 0))

        # Fetch total count to clamp new_position
        rows = self._q("SELECT count() AS cnt FROM wiki_bookmark GROUP ALL")
        total = int(rows[0]["cnt"]) if rows else 1
        new_position = max(0, min(new_position, total - 1))

        if old_pos == new_position:
            return True

        if new_position < old_pos:
            # Moving up: shift rows in [new_position, old_pos-1] down by 1
            self._q(
                "UPDATE wiki_bookmark SET position = position + 1 "
                "WHERE position >= $lo AND position < $hi AND slug != $slug",
                {"lo": new_position, "hi": old_pos, "slug": slug},
            )
        else:
            # Moving down: shift rows in [old_pos+1, new_position] up by 1
            self._q(
                "UPDATE wiki_bookmark SET position = position - 1 "
                "WHERE position > $lo AND position <= $hi AND slug != $slug",
                {"lo": old_pos, "hi": new_position, "slug": slug},
            )

        self._q(
            "UPDATE wiki_bookmark SET position = $pos WHERE slug = $slug",
            {"pos": new_position, "slug": slug},
        )
        return True
