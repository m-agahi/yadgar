"""Cross-boundary seam Protocols for the modular-monolith split (folder-split #17).

This is the single, obvious home for the structural typing seams that let
``core`` and ``backend`` depend on each other's *contracts* without importing
each other's *internals*. Both sides import from here; ``_shared`` never imports
``core`` or ``backend``.

The formalized standard (plan §3): **Protocol lives in ``_shared``, the concrete
implementation lives in the owning subpackage, and the object is injected at the
one composition root (``_shared/runtime/lifecycle.init_engines``).**

Three seams are declared here:

* ``MLClientProtocol`` — the ML scoring surface shared by ``LocalMLClient`` and
  ``RemoteMLClient`` (``backend/ml_client.py``). Moved here from
  ``backend/ml_client.py``; that module re-exports it for back-compat.
* ``CacheProtocol`` — the modular-monolith cache interface. Moved here from
  ``backend/cache.py``; that module re-exports it for back-compat.
* ``StorageProtocol`` — the narrow read/write surface the retrieval + recall
  pipeline consumes on a ``StorageEngine`` (user override — no defer). Typing-only:
  ``StorageEngine`` already lives in ``_shared`` and both sides import it directly,
  so this Protocol decouples the consumer's *type* from the concrete engine; it
  introduces no DI machinery.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any, Protocol, runtime_checkable


class NullCache:
    """All-miss cache satisfying ``CacheProtocol`` — the clean ``_shared`` default.

    Lives in ``_shared`` (NOT ``backend``) so a consumer that falls back to it
    introduces no ``_shared → backend`` import edge. ``get`` always misses, ``put``
    is a no-op: behaviour-identical to the pre-cache single-query path. Injected at
    bare-construction sites and disabled-feature paths; the composition root
    (``lifecycle.init_engines``) injects the REAL backend cache singletons for the
    production hot path (byte-identical output, live caching).

    ``backend/cache.py`` keeps its own richer ``NullCache`` for backend-internal
    tests; this is the seam-level null object consumers in ``_shared`` default to.
    """

    def get(self, key: Hashable) -> Any | None:  # noqa: ARG002 — always miss
        return None

    def put(self, key: Hashable, value: Any) -> None:  # noqa: ARG002 — no-op
        return None

    def invalidate(self, scope: Hashable = None) -> None:  # noqa: ARG002 — no-op
        return None

    def stats(self) -> dict:
        return {"hits": 0, "misses": 0, "evictions": 0, "size_bytes": 0}


class NullMLClient:
    """No-op ML client satisfying ``MLClientProtocol`` — the clean ``_shared`` default.

    Lives in ``_shared`` (NOT ``backend``) so a consumer that falls back to it
    introduces no ``_shared → backend`` import edge. Every score method returns
    ``None`` — exactly the circuit-open sentinel the Protocol already declares and
    every CE/NLI call site already handles (skip that scoring pass). ``unload_if_idle``
    is a no-op.

    The composition root (``lifecycle._init_embedding_client``) injects the REAL
    ``LocalMLClient``/``RemoteMLClient`` for every production path, so this default
    is never reached in the daemon/backend — it exists only so bare-construction
    sites (tests that never score) build without pulling ``backend``.
    """

    # Mirror the real MLClient's lazy-loaded reranker attribute (inits to None on
    # the real client). Consumers reach through ``_ml._gte_reranker`` (e.g. the
    # Retriever._gte_reranker back-compat property); without this the null stub
    # raises AttributeError instead of reporting "no reranker loaded".
    _gte_reranker = None

    def score_cross_encoder(self, query: str, texts: list[str]) -> list[float] | None:  # noqa: ARG002
        return None

    def score_nli(self, query: str, texts: list[str]) -> list[float] | None:  # noqa: ARG002
        return None

    def score_pair(self, query: str, text: str) -> float | None:  # noqa: ARG002
        return None

    def unload_if_idle(self, idle_seconds: float | None = None) -> None:  # noqa: ARG002
        return None


class NullScopeVersions:
    """No-op version-in-key store — the clean ``_shared`` default for the storage
    layer's ``scope_versions`` seam (paired with ``NullCache`` for the engram_slot /
    graph caches).

    ``version`` always returns 0 and ``bump`` is a no-op. Behaviour-neutral because
    the version-in-key only matters when the engram_slot/graph CACHE is live; a bare
    consumer that defaults ``scope_versions`` to this ALSO defaults its slot/graph
    cache to ``NullCache`` (all-miss ≡ uncached full scan), so the frozen version is
    never consulted for a cache hit. The composition root injects the REAL
    ``ScopeVersions`` singleton (``backend/cache.py``) for the production path.
    """

    def version(self, scope_kind: str, scope_id: Hashable) -> int:  # noqa: ARG002
        return 0

    def bump(self, scope_kind: str, scope_id: Hashable) -> int:  # noqa: ARG002
        return 0


@runtime_checkable
class MLClientProtocol(Protocol):
    """Protocol for ML scoring clients (cross-encoder / NLI / pairwise).

    Matches ``LocalMLClient`` and ``RemoteMLClient`` in ``backend/ml_client.py``.
    The retrieval reranker (``_shared/retrieval/reranking.py``) depends on THIS,
    not the concrete client, and receives an instance injected at the composition
    root (``lifecycle.init_engines`` selects Local vs Remote).
    """

    def score_cross_encoder(self, query: str, texts: list[str]) -> list[float] | None:
        """Score query-text pairs using a cross-encoder. Returns raw scores or None on circuit-open."""
        ...

    def score_nli(self, query: str, texts: list[str]) -> list[float] | None:
        """Score query-text pairs using NLI entailment. Returns raw scores or None on circuit-open."""
        ...

    def score_pair(self, query: str, text: str) -> float | None:
        """Score a single query-text pair. Returns raw score or None on circuit-open."""
        ...

    def unload_if_idle(self, idle_seconds: float | None = None) -> None:
        """Unload models if unused for idle_seconds.

        idle_seconds: override threshold. None = use YADGAR_MODEL_IDLE_EVICTION_SECONDS
                      env (0 by default, meaning never evict). Explicit value bypasses env.
        """
        ...


@runtime_checkable
class CacheProtocol(Protocol):
    """The modular-monolith cache interface. Consumers depend on THIS, not Cache.

    Method names mirror the core ``yadgar/cache.py`` Cache + every existing backend
    consumer (``_ce_cache.get`` / ``.put``) — hence ``put`` (not ``set``).

    The reranker's ``ce`` cache and the storage layer's ``memory_doc`` /
    ``engram_slot`` / ``graph`` caches are all typed against this Protocol and are
    injected at the composition root; a ``NullCache`` (all-miss) satisfies the same
    interface for tests / disabled paths.
    """

    def get(self, key: Hashable) -> Any | None: ...

    def put(self, key: Hashable, value: Any) -> None: ...

    def invalidate(self, scope: Hashable = ...) -> None: ...

    def stats(self) -> dict: ...


@runtime_checkable
class StorageProtocol(Protocol):
    """The narrow read/write surface the retrieval + recall pipeline consumes.

    ``StorageEngine`` (``_shared/storage/__init__.py``) is the sole concrete
    implementation. This Protocol captures ONLY the methods the retrieval /
    reranking stages call on ``self._storage`` — the read side (candidate fetch,
    FTS, vector search, entity/graph lookups) that recall depends on. It exists so
    the ``Retriever`` and its stages can be typed against a contract rather than
    the concrete engine (testability + the modular-monolith seam standard, plan §3
    Q2 — user override, no defer).

    Typing-only: no DI machinery is introduced; ``StorageEngine`` continues to be
    constructed directly at the composition root.
    """

    # ── candidate + row fetch ────────────────────────────────────────────────
    def get_memory(self, memory_id: int) -> dict | None: ...

    def get_memories_by_ids(self, memory_ids: list[int]) -> list[dict]: ...

    # ── full-text search ─────────────────────────────────────────────────────
    def search_memories_fts(self, query: str, limit: int = ...) -> list[dict]: ...

    def search_memories_fts_scored(self, query: str, limit: int = ...) -> list[dict]: ...

    def search_beliefs_fts(self, query: str, limit: int = ...) -> list[dict]: ...

    def search_profiles_fts(self, query: str, limit: int = ...) -> list[dict]: ...

    def search_memories_by_content_date(self, *args: Any, **kwargs: Any) -> list[dict]: ...

    def search_memories_by_month(self, *args: Any, **kwargs: Any) -> list[dict]: ...

    # ── vector search ────────────────────────────────────────────────────────
    def search_vectors(self, *args: Any, **kwargs: Any) -> list[dict]: ...

    # ── entity / graph lookups ───────────────────────────────────────────────
    def get_all_entities(self) -> list[dict]: ...

    def get_entities_by_ids(self, entity_ids: list) -> list[dict]: ...

    def get_entity_by_id(self, entity_id: Any) -> dict | None: ...

    def get_entity_by_name(self, name: str) -> dict | None: ...

    def find_memory_ids_by_entities(self, *args: Any, **kwargs: Any) -> list[int]: ...

    def find_memory_ids_by_entity_name(self, name: str) -> list[int]: ...

    def get_memory_cofire_priors(self, *args: Any, **kwargs: Any) -> dict: ...

    def get_memory_graph_priors(self, *args: Any, **kwargs: Any) -> dict: ...
