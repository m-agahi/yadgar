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

    Car B adds a per-kind GLOBAL counter (``kind_epoch``): bumped on every
    ``bump(kind, _)`` regardless of ``scope_id``. The /admin response piggybacks
    this so core can hold ONE number per kind and invalidate the entire kind's
    PTC entries when the counter moves — zero extra round-trips in steady
    state. Unbounded ``(kind, id) -> int`` map size never enters the response.
    """

    def __init__(self) -> None:
        self._versions: dict[tuple[str, Hashable], int] = {}
        self._kind_epoch: dict[str, int] = {}
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
            # Car B: also bump the per-kind global counter. The /admin response
            # piggybacks this so core can hold one number per kind and invalidate
            # the entire kind's PTC entries on a move.
            self._kind_epoch[scope_kind] = self._kind_epoch.get(scope_kind, 0) + 1
            return v

    @observe(tier="hot", metric="backend.cache.scope_kind_epoch_read")
    def kind_epoch(self, scope_kind: str) -> int:
        """Per-kind global epoch (bumped on every bump of any scope of this kind).

        Cheap — O(1) under the same short lock as ``version``. Used to build the
        ``scope_versions`` envelope field on /admin responses.
        """
        with self._lock:
            return self._kind_epoch.get(scope_kind, 0)

    @observe(tier="hot", metric="backend.cache.scope_kind_epoch_snapshot")
    def kind_epochs_snapshot(self, kinds: tuple[str, ...]) -> dict[str, int]:
        """Return ``{kind: epoch}`` for each kind in ``kinds`` (0 for untracked).

        A single-lock snapshot of the per-kind epoch counters the /admin
        response piggybacks. Cheap — one lock acquire per call, returns a fresh
        dict so callers can mutate it.
        """
        with self._lock:
            return {k: self._kind_epoch.get(k, 0) for k in kinds}


# Process-global ScopeVersions — the version store the backend StorageEngine reads
# on the slot-read path and bumps at the slot-write site. Single instance because
# slot writes (assign_memory_slot) and the slot read (get_memories_in_slot) share
# ONE backend process, so no header-passing / cross-service signal is needed.
_SCOPE_VERSIONS = ScopeVersions()


def get_scope_versions() -> ScopeVersions:
    """Return the process-global :class:`ScopeVersions` (version-in-key store)."""
    return _SCOPE_VERSIONS
