"""Backend cache namespace factories + RAM-% byte-budget machinery (I13 split).

Split from ``cache.py`` (backend 5.51.0, module-standardization train, I13 ≤500
soft cap). ``cache.py`` retains ``Cache`` + invalidation-policy dataclasses +
``_REGISTRY``; this sibling owns everything that *uses* ``Cache`` to build
named namespaces:

  * Per-namespace enabled/TTL resolver helpers
  * RAM-% budget machinery (``_read_container_memory_bytes``,
    ``_backend_cache_total_budget_bytes``, ``_namespace_budget_bytes``,
    ``_CGROUP_*``, ``_FALLBACK_CONTAINER_BYTES``, ``_NAMESPACE_WEIGHTS``)
  * Namespace factory functions (``_make_*``)
  * Registry getters (``get_ce_cache``, ``get_memory_doc_cache``,
    ``get_engram_slot_cache``, ``get_graph_cache``)

External importers continue to use ``yadgar.backend.cache.<name>`` via the
package ``__init__`` re-exports — no import path changed.

I13: nesting ≤ 4.
"""

from __future__ import annotations

from pathlib import Path

from yadgar._shared.contracts.protocols import CacheProtocol as CacheProtocol  # noqa: PLC0414
from yadgar._shared.observability.observe import observe
from yadgar.backend.cache.cache import (
    _REGISTRY,
    TTL,
    Cache,
    DataEpoch,
)

# ── RAM-% byte-budget machinery ───────────────────────────────────────────────
#
# Backend cache byte budget = YADGAR_BACKEND_CACHE_RAM_PCT × backend container
# memory limit (cgroup). Split across namespaces by fixed weights (embed vectors
# and memory_doc entries are large; a ce score is tiny) — documented, not equal.

# Cgroup v2 (memory.max) then v1 (memory.limit_in_bytes).
_CGROUP_V2 = "/sys/fs/cgroup/memory.max"
_CGROUP_V1 = "/sys/fs/cgroup/memory/memory.limit_in_bytes"

# Fallback assumed backend container size when no cgroup limit is readable
# (e.g. running the test suite on a dev host): 4 GiB matches the documented
# `--memory 4g` backend container envelope.
_FALLBACK_CONTAINER_BYTES = 4 * 1024**3

# Namespace budget weights (relative share of the total backend budget). ce
# scores are 8-byte floats (small); embed vectors are ~1.5 KB each (large). The
# data namespaces (later cars) get placeholder weights so the split is stable as
# they land. Weights are normalised to the namespaces actually requested.
_NAMESPACE_WEIGHTS = {
    "ce": 1.0,
    "embed": 4.0,
    "memory_doc": 4.0,
    "engram_slot": 1.0,
    "graph": 2.0,
}


@observe(tier="stage", metric="backend.cache.read_container_memory")
def _read_container_memory_bytes() -> int | None:
    """Return the container memory limit in bytes, or None if unbounded/unknown."""
    for path_str in (_CGROUP_V2, _CGROUP_V1):
        try:
            raw = Path(path_str).read_text().strip()
        except OSError:
            continue
        if not raw or raw == "max":
            return None
        try:
            val = int(raw)
        except ValueError:
            continue
        # cgroup "no limit" sentinels are absurdly large — treat as unbounded.
        if val <= 0 or val >= 1 << 62:
            return None
        return val
    return None


@observe(tier="stage", metric="backend.cache.total_budget")
def _backend_cache_total_budget_bytes(pct: float) -> int:
    """Total backend cache byte budget = pct%% × container memory (or fallback)."""
    limit = _read_container_memory_bytes()
    if limit is None:
        limit = _FALLBACK_CONTAINER_BYTES
    return int((pct / 100.0) * limit)


@observe(tier="stage", metric="backend.cache.namespace_budget")
def _namespace_budget_bytes(
    namespace: str, total_budget: int, *, active: tuple[str, ...] = ("ce", "embed")
) -> int:
    """This namespace's weighted share of ``total_budget``.

    Only the ``active`` namespaces (those a service actually instantiates) share
    the budget — Car 0 = ("ce", "embed"). Weighted (not equal): embed's larger
    entries earn a bigger slice.
    """
    weight_sum = sum(_NAMESPACE_WEIGHTS.get(n, 1.0) for n in active)
    if weight_sum <= 0:
        return 0
    return int(total_budget * (_NAMESPACE_WEIGHTS.get(namespace, 1.0) / weight_sum))


def _backend_cache_ram_pct_local() -> float:
    """% of backend container RAM budgeted for the unified backend cache.

    Duplicated here (not imported from ``embed_service``) so the ``memory_doc``
    cache — resolved from the storage layer — never pulls the FastAPI embed_service
    module into the retrieval hot-path import graph.
    """
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob("YADGAR_BACKEND_CACHE_RAM_PCT", "BACKEND_CACHE_RAM_PCT", float, 10.0)


# ── memory_doc namespace (Car 2) ──────────────────────────────────────────────


def _memory_doc_cache_enabled() -> bool:
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob(
        "YADGAR_MEMORY_DOC_CACHE_ENABLED",
        "MEMORY_DOC_CACHE_ENABLED",
        lambda v: v.lower() not in ("0", "false", "no"),
        True,
    )


def _memory_doc_cache_ttl_sec() -> float:
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    # 45 min default — a TTL backstop that bounds worst-case staleness from a
    # content edit / reembed that bypasses the per-id evict path. Cache lifetime
    # ≈ this interval; content is immutable except on edit/reembed.
    return resolve_knob(
        "YADGAR_MEMORY_DOC_CACHE_TTL_SEC", "MEMORY_DOC_CACHE_TTL_SEC", float, 2700.0
    )


@observe(tier="stage", metric="backend.cache.make_memory_doc_cache")
def _make_memory_doc_cache() -> Cache:
    """Build (and register) the unified ``memory_doc`` namespace (Car 2).

    Byte-budget from RAM-% (``memory_doc`` weight 4.0 — entries hold full content +
    embedding, large). TTL invalidation; ``deep_copy=True`` because the cached
    value carries a mutable ``embedding`` list/bytes and full row dicts.
    """
    if not _memory_doc_cache_enabled():
        budget = 0
    else:
        total = _backend_cache_total_budget_bytes(_backend_cache_ram_pct_local())
        budget = _namespace_budget_bytes("memory_doc", total, active=("memory_doc",))
    return Cache(
        name="memory_doc",
        max_bytes=budget,
        invalidation=TTL(_memory_doc_cache_ttl_sec()),
        deep_copy=True,
        obs_tier="cold",
    )


@observe(tier="stage", metric="backend.cache.get_memory_doc_cache")
def get_memory_doc_cache() -> CacheProtocol:
    """Return the process-global ``memory_doc`` namespace (Car 2 build_results seam).

    ``memory_doc`` caches ONLY the two immutable, KB-scale columns of a memory row
    (``content`` + ``embedding``) keyed by ``memory_id``. Everything else — heat,
    access_count, and every consolidation/decay-mutated field — is fetched fresh on
    every recall via a light ``SELECT * OMIT content, embedding`` query, so recall
    output stays byte-identical INCLUDING live heat (heat-freshness by construction).

    Invalidation is ``TTL`` (a 30-60 min backstop that bounds worst-case staleness
    from a content edit / reembed) plus an optional per-id ``invalidate(memory_id)``
    on ``memory_update(content)``. Deliberately NOT the cross-service structural
    ``data_epoch`` (deferred — the spec flags it as the highest-risk correctness
    item; TTL is the simplest-correct backstop for Car 2). DELETE needs no
    invalidation: a deleted memory is never a fusion candidate (retrieval queries
    the live DB), so ``build_results`` never re-fetches it, and memory ids are
    monotonic (``_next_id`` UPSERT counter) so a stale entry can never be served
    for a reused id.

    Single process-wide instance (constructor-DI default on ``StorageEngine``); a
    ``NullCache`` injected ⇒ always-miss ⇒ today's single-query behaviour. The
    ``YADGAR_MEMORY_DOC_CACHE_ENABLED`` kill switch (disabled ⇒ ``max_bytes=0`` ⇒
    all-miss ≡ NullCache) is honoured automatically.
    """
    existing = _REGISTRY.get("memory_doc")
    if existing is not None:
        return existing
    return _make_memory_doc_cache()


# ── engram_slot namespace (Car 3) ─────────────────────────────────────────────


def _engram_slot_cache_enabled() -> bool:
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob(
        "YADGAR_ENGRAM_SLOT_CACHE_ENABLED",
        "ENGRAM_SLOT_CACHE_ENABLED",
        lambda v: v.lower() not in ("0", "false", "no"),
        True,
    )


@observe(tier="stage", metric="backend.cache.make_engram_slot_cache")
def _make_engram_slot_cache() -> Cache:
    """Build (and register) the unified ``engram_slot`` namespace (Car 3).

    Byte-budget from RAM-% (``engram_slot`` weight 1.0 — entries are small id
    lists). Invalidation is version-in-key (``DataEpoch`` policy marker — freshness
    lives in the key via the per-slot version, no TTL / explicit bust on the read
    path). ``deep_copy=True`` because the cached value is a mutable id list.
    """
    if not _engram_slot_cache_enabled():
        budget = 0
    else:
        total = _backend_cache_total_budget_bytes(_backend_cache_ram_pct_local())
        budget = _namespace_budget_bytes("engram_slot", total, active=("engram_slot",))
    return Cache(
        name="engram_slot",
        max_bytes=budget,
        invalidation=DataEpoch(),
        deep_copy=True,
        obs_tier="cold",
    )


@observe(tier="stage", metric="backend.cache.get_engram_slot_cache")
def get_engram_slot_cache() -> CacheProtocol:
    """Return the process-global ``engram_slot`` namespace (Car 3 slot-links seam).

    ``engram_slot`` caches ONLY the STRUCTURAL slot membership: the ordered
    (``created_at``) candidate memory ids for a slot, keyed by
    ``(slot_index, slot_version)``. The volatile ``heat>0`` predicate (and the
    live ``slot_index`` match) is re-verified FRESH on every read against the
    cached candidate ids — so heat→0 decay, delete, and reslot-away are all inert
    (the fresh recheck drops them) with NO version bump. The version-in-key covers
    the ONLY vector the recheck cannot: a NEW member appearing in the slot
    (create-alloc / reslot-into), bumped at ``assign_memory_slot`` (ops.py).

    Single process-wide instance (constructor-DI default on ``StorageEngine``); a
    ``NullCache`` injected ⇒ always-miss ⇒ today's single full-slot-scan behaviour.
    The ``YADGAR_ENGRAM_SLOT_CACHE_ENABLED`` kill switch (disabled ⇒ ``max_bytes=0``
    ⇒ all-miss ≡ NullCache) is honoured automatically.
    """
    existing = _REGISTRY.get("engram_slot")
    if existing is not None:
        return existing
    return _make_engram_slot_cache()


# ── graph namespace (Car 4) ───────────────────────────────────────────────────


def _graph_cache_enabled() -> bool:
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob(
        "YADGAR_GRAPH_CACHE_ENABLED",
        "GRAPH_CACHE_ENABLED",
        lambda v: v.lower() not in ("0", "false", "no"),
        True,
    )


@observe(tier="stage", metric="backend.cache.make_graph_cache")
def _make_graph_cache() -> Cache:
    """Build (and register) the unified ``graph`` namespace (Car 4).

    Byte-budget from RAM-% (``graph`` weight 2.0 — entries are small neighbour-dict
    lists). Invalidation is version-in-key (``DataEpoch`` policy marker — freshness
    lives in the per-entity version, no TTL / explicit bust on the read path).
    ``deep_copy=True`` because the cached value is a list of mutable neighbour dicts.
    """
    if not _graph_cache_enabled():
        budget = 0
    else:
        total = _backend_cache_total_budget_bytes(_backend_cache_ram_pct_local())
        budget = _namespace_budget_bytes("graph", total, active=("graph",))
    return Cache(
        name="graph",
        max_bytes=budget,
        invalidation=DataEpoch(),
        deep_copy=True,
        obs_tier="cold",
    )


@observe(tier="stage", metric="backend.cache.get_graph_cache")
def get_graph_cache() -> CacheProtocol:
    """Return the process-global ``graph`` namespace (Car 4 adjacency seam).

    ``graph`` caches the PURE-STRUCTURAL per-entity adjacency — the neighbour
    dicts (``{entity_id, relationship_type, weight}``) for an entity — keyed by
    ``(entity_id, rel_types_key, entity_version)``. Both graph read paths (PPR
    ``_build_networkx_graph`` + spreading ``_spreading_bfs_step``) fan out through
    ``KnowledgeGraph._get_adjacent_batch``, which reads this cache per entity and
    only queries the DB (``get_relationships_for_frontier``) for the miss subset.

    Unlike Car 3's ``engram_slot`` there is NO fresh recheck: the adjacency read
    (``get_relationships_for_frontier``) filters ONLY on endpoint id — no
    ``heat`` / ``archived`` / ``valid_until`` / ``weight`` predicate — so the
    cached value is served whole. The tradeoff: DELETE is NOT inert here (Car 3's
    heat/reslot vectors were caught by its recheck). EVERY edge mutation — insert,
    weight-change (reinforce / field update), delete — bumps BOTH endpoint
    entities' versions (``scope_kind="entity"`` on the shared ``ScopeVersions``),
    or a stale adjacency would survive. ``rel_types_key`` (normalized ``rel_types``,
    ``None`` today) is in the key so a typed query never serves the ``None`` superset.

    Single process-wide instance (constructor-DI default on ``StorageEngine``); a
    ``NullCache`` injected ⇒ always-miss ⇒ today's per-depth frontier query. The
    ``YADGAR_GRAPH_CACHE_ENABLED`` kill switch (disabled ⇒ ``max_bytes=0`` ⇒
    all-miss ≡ NullCache) is honoured automatically.
    """
    existing = _REGISTRY.get("graph")
    if existing is not None:
        return existing
    return _make_graph_cache()


# ── ce namespace getter (Car 1) ───────────────────────────────────────────────


@observe(tier="stage", metric="backend.cache.get_ce_cache")
def get_ce_cache() -> CacheProtocol:
    """Return the process-global ``ce`` namespace (Car 1 recall-CE-dedup seam).

    The ``ce`` cache is a SINGLE process-wide instance (ModelCkpt-keyed), created
    and registered by ``embed_service`` at import (``_ce_cache = _make_ce_cache()``).
    Recall's reranker/fusion CE path reuses THIS instance (constructor-DI default)
    so that:
      * crossfuse's already-computed ``(query, text)`` scores are reused by
        cross_encoder / multi_passage within one request (#41 within-request dedup);
      * the process-global lifetime also gives cross-request repeat hits for free.

    Reusing the registered instance (rather than constructing a fresh
    ``Cache(name="ce")``, which would collide under the overwrite-on-dup registry
    and split hit/miss stats) keeps ONE ``ce`` object, one obs surface, one
    snapshot — and honors the ``YADGAR_CE_CACHE_ENABLED`` kill switch (disabled →
    ``max_bytes=0`` → all-miss ≡ NullCache) automatically.

    If ``embed_service`` has not been imported (e.g. a pure LocalMLClient/stdio
    process that never touched the backend HTTP module), lazily invoke its factory
    to materialise + register the namespace. The import is deferred to keep the
    retrieval hot-path import free of FastAPI.

    Import-order caveat: "one shared ``ce`` object" holds when the reranker resolves
    its cache AFTER ``embed_service`` registered ``_ce_cache`` (the normal order —
    stdio never imports embed_service, and the backend process does not build a
    retrieval ``Reranker``). If a ``Reranker`` resolved this accessor first and
    ``embed_service`` later re-ran ``_make_ce_cache()``, the overwrite-on-dup
    registry would leave the reranker holding a now-superseded instance (split
    stats). Harmless in current deployments; noted for future car wiring.
    """
    ce = _REGISTRY.get("ce")
    if ce is not None:
        return ce
    from yadgar.backend.embed_service import _make_ce_cache  # noqa: PLC0415

    return _make_ce_cache()
