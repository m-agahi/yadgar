"""Per-scope version-in-key invalidation store for the unified backend cache.

``ScopeVersions`` is the reusable version-in-key mechanism the data namespaces
(``engram_slot`` / ``graph``) use for freshness: a structural write bumps the
version of the scope it mutates and the reader embeds the current version in its
cache key, so a stale entry is simply never hit — no explicit ``invalidate``, no
cross-service round-trip.

Extracted from ``cache.py`` (task #18 C2 internal split). The process-global
singleton + accessor live here; the ``_make_*_cache`` factories and the
``StorageEngine`` slot/graph read paths reach them via the package re-export.
"""

from __future__ import annotations

import threading
from collections.abc import Hashable

from yadgar._shared.observability.observe import observe


class ScopeVersions:
    """Per-scope monotonic version map — the reusable version-in-key mechanism.

    A small in-process, thread-safe ``(scope_kind, scope_id) -> int`` counter.
    Structural writes bump the version of the scope they mutate; a reader embeds
    the current version in its cache key (e.g. ``(slot_index, slot_version)``), so
    a bump makes every prior key for that scope unreachable — a stale entry is
    simply never hit, with NO explicit ``invalidate`` call and NO cross-service
    round-trip (the version is read cheaply, in-process, on the read path).

    Car 3 uses ``scope_kind="slot"`` (slot occupancy). Car 4 will reuse the SAME
    map with ``scope_kind="entity"`` (graph neighbourhoods) — different kind,
    identical mechanism. Bumps are O(1); versions start at 0 and only increase.

    Staleness guarantee: a cached ``(scope, v)`` entry is served ONLY while the
    scope's version equals ``v``. The instant a structural mutator bumps the
    scope (create/reslot-into for slots), the reader computes ``(scope, v+1)`` →
    miss → recompute. Vectors that the fresh read-side recheck already covers
    (delete, reslot-away, heat→0 for slots) need NO bump — see the engram_slot
    cache docstring.
    """

    def __init__(self) -> None:
        self._versions: dict[tuple[str, Hashable], int] = {}
        self._lock = threading.Lock()

    @observe(tier="hot", metric="backend.cache.scope_version_read")
    def version(self, scope_kind: str, scope_id: Hashable) -> int:
        """Current version for a scope (0 if never bumped)."""
        with self._lock:
            return self._versions.get((scope_kind, scope_id), 0)

    @observe(tier="hot", metric="backend.cache.scope_version_bump")
    def bump(self, scope_kind: str, scope_id: Hashable) -> int:
        """Increment and return the scope's version. O(1), cheap enough for the
        write hot-path (a single dict update under a short lock)."""
        key = (scope_kind, scope_id)
        with self._lock:
            v = self._versions.get(key, 0) + 1
            self._versions[key] = v
            return v


# Process-global ScopeVersions — the version store the backend StorageEngine reads
# on the slot-read path and bumps at the slot-write site. Single instance because
# slot writes (assign_memory_slot) and the slot read (get_memories_in_slot) share
# ONE backend process, so no header-passing / cross-service signal is needed.
_SCOPE_VERSIONS = ScopeVersions()


def get_scope_versions() -> ScopeVersions:
    """Return the process-global :class:`ScopeVersions` (version-in-key store)."""
    return _SCOPE_VERSIONS
