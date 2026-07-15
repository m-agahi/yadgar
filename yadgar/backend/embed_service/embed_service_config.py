"""Pure config/knob helpers for the embed_service backend.

Split out of ``embed_service.py`` (C1, module-standardization train #18). These
are stateless resolvers (env/Settings knobs, checkpoint hashes, cache-instance
factories, per-mode semaphore factory, torch thread config) with NO reassigned
module globals and NO monkeypatched-on-the-service-module readers — safe to live
in a sibling.

Re-exported from ``embed_service.py`` so ``embed_service.embed_service.<name>``
keeps resolving. The cache/semaphore INSTANCES (``_ce_cache``, ``_embed_cache``,
``_rerank_semaphores``) stay module-level in ``embed_service.py`` so
``importlib.reload(embed_service)`` re-creates them with fresh env values, which
several tests rely on; only the FACTORIES move here.

Functions carrying ``@observe`` are I33-SATISFIED (span source present) — they
are not in ``.observe-allowlist.json`` and need no key rename. The plain knob
resolvers below the caps are trivial single-return helpers (I33-auto-exempt).
"""

from __future__ import annotations

import asyncio
import logging
import os

from yadgar._shared.config import resolve_knob
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# F5-A — Per-mode concurrent-inference semaphore factory (v5.4.2)
# ---------------------------------------------------------------------------
# The INSTANCE dict lives in embed_service.py (module-level so reload() recreates
# it with fresh env values in tests); this is just the factory.


def _make_rerank_semaphores() -> dict[str, asyncio.Semaphore]:
    from yadgar._shared.config import get_settings

    _n = int(get_settings().RERANK_MAX_CONCURRENCY)
    return {mode: asyncio.Semaphore(_n) for mode in ("ce", "nli", "pair")}


def _rerank_acquire_timeout() -> float:
    from yadgar._shared.config import get_settings

    return float(get_settings().RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC)


def _dbsize_cache_ttl() -> int:
    """Return DBSIZE_CACHE_TTL_SEC from Settings (yaml/env/default 60). 0 = disabled."""
    from yadgar._shared.config import get_settings  # noqa: PLC0415

    return int(get_settings().DBSIZE_CACHE_TTL_SEC)


@observe(tier="hot")
def _shutdown_marker_path() -> str:
    """Return path for clean-shutdown marker file."""
    return os.environ.get("YADGAR_SHUTDOWN_MARKER_PATH", "/data/.shutdown_clean")


# ---------------------------------------------------------------------------
# backend v5.4.0 — LRU cache knobs + factories for CE scores + embedding vectors
# ---------------------------------------------------------------------------


def _ce_cache_enabled() -> bool:
    return resolve_knob(
        "YADGAR_CE_CACHE_ENABLED",
        "CE_CACHE_ENABLED",
        lambda v: v.lower() not in ("0", "false", "no"),
        True,
    )


def _embed_cache_enabled() -> bool:
    return resolve_knob(
        "YADGAR_EMBED_CACHE_ENABLED",
        "EMBED_CACHE_ENABLED",
        lambda v: v.lower() not in ("0", "false", "no"),
        True,
    )


def _ce_cache_max_entries() -> int:
    return resolve_knob("YADGAR_CE_CACHE_MAX_ENTRIES", "CE_CACHE_MAX_ENTRIES", int, 100000)


def _embed_cache_max_entries() -> int:
    return resolve_knob("YADGAR_EMBED_CACHE_MAX_ENTRIES", "EMBED_CACHE_MAX_ENTRIES", int, 100000)


def _backend_cache_ram_pct() -> float:
    """% of the backend container RAM budgeted for the unified backend cache.

    Byte-bounded eviction sizes each namespace from this (Car 0, backend 5.17.0).
    The legacy YADGAR_*_CACHE_MAX_ENTRIES knobs no longer cap entry count; the
    byte budget is authoritative. The *_CACHE_ENABLED kill switches still disable.
    """
    return resolve_knob("YADGAR_BACKEND_CACHE_RAM_PCT", "BACKEND_CACHE_RAM_PCT", float, 10.0)


def _cache_snapshot_dir() -> str:
    return resolve_knob("YADGAR_CACHE_SNAPSHOT_DIR", "CACHE_SNAPSHOT_DIR", str, "/data/cache")


def _cache_snapshot_interval_sec() -> int:
    return resolve_knob(
        "YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC", "CACHE_SNAPSHOT_INTERVAL_SEC", int, 600
    )


# Scoring-version salt for the CE checkpoint hash. Bump whenever CE *scoring
# semantics* change (preprocessing, truncation, score transform) — the ckpt
# mismatch at snapshot load then discards the whole persistent snapshot via the
# existing discard-on-mismatch path. Model-id changes bust the cache on their
# own; the salt covers legitimately-keyed but semantically-stale scores.
CE_SCORING_VERSION = "2"


@observe(tier="hot")
def _get_ce_checkpoint_hash() -> str:
    """Return a short hash identifying the current CE RERANKER checkpoint.

    T4 Car 0 fix: hashes ``GTE_RERANKER_MODEL`` — the model
    ``ml_client._load_gte_reranker`` actually loads — NOT the embedding model
    (the pre-fix split-brain: a reranker swap left ``_ckpt`` unchanged, so the
    disk-persistent ``ce`` snapshot served stale scores across the swap, while
    an embedding-model change wrongly busted CE scores).
    """
    import hashlib  # noqa: PLC0415

    # Read CE_SCORING_VERSION through the embed_service module object (lazy, at
    # call time — es is fully imported by now, so no circular-init issue). The
    # salt is re-exported there; tests monkeypatch it via
    # ``setattr(embed_service, "CE_SCORING_VERSION", ...)`` on the canonical
    # submodule, and this module-object read is what honours that patch (C1 split
    # would otherwise read this module's own binding and miss the rebind).
    import yadgar.backend.embed_service.embed_service as _es  # noqa: PLC0415

    model = resolve_knob(
        "YADGAR_GTE_RERANKER_MODEL",
        "GTE_RERANKER_MODEL",
        str,
        # Fallback kept in sync with the config default (T4 flip → Ettin-32m).
        # Reached only if Settings resolution fails; the reranker id must match
        # what ml_client._load_gte_reranker loads or the ckpt would key the wrong model.
        "cross-encoder/ettin-reranker-32m-v1",
    )
    return hashlib.sha256(f"{model}:{_es.CE_SCORING_VERSION}".encode()).hexdigest()[:16]


@observe(tier="hot")
def _get_embed_checkpoint_hash() -> str:
    """Return a short hash identifying the current embedding model."""
    import hashlib  # noqa: PLC0415

    model = resolve_knob("YADGAR_EMBEDDING_MODEL", "EMBEDDING_MODEL", str, "all-MiniLM-L6-v2")
    return hashlib.sha256(model.encode()).hexdigest()[:16]


@observe(tier="stage")
def _make_ce_cache():
    """Build the unified `ce` namespace (Car 0). Byte-budget from RAM-%.

    Behaviour-neutral fold-in: same keys (query_sha:text_sha:ckpt), same float
    values, same ModelCkpt-in-key invalidation, same snapshot format. Only the
    eviction discipline changed (count-cap → byte-cap). DI note: still a module
    global for now; consumer constructor-DI deferred to a later car.
    """
    from yadgar.backend.cache import (  # noqa: PLC0415
        Cache,
        ModelCkpt,
        _backend_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    if not _ce_cache_enabled():
        budget = 0
    else:
        total = _backend_cache_total_budget_bytes(_backend_cache_ram_pct())
        budget = _namespace_budget_bytes("ce", total)
    return Cache(
        name="ce",
        max_bytes=budget,
        invalidation=ModelCkpt(),
        checkpoint_hash=_get_ce_checkpoint_hash(),
        obs_tier="hot",
    )


@observe(tier="stage")
def _make_embed_cache():
    """Build the unified `embed` namespace (Car 0). See `_make_ce_cache`."""
    from yadgar.backend.cache import (  # noqa: PLC0415
        Cache,
        ModelCkpt,
        _backend_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    if not _embed_cache_enabled():
        budget = 0
    else:
        total = _backend_cache_total_budget_bytes(_backend_cache_ram_pct())
        budget = _namespace_budget_bytes("embed", total)
    return Cache(
        name="embed",
        max_bytes=budget,
        invalidation=ModelCkpt(),
        checkpoint_hash=_get_embed_checkpoint_hash(),
        obs_tier="hot",
    )


@observe(tier="stage", span=False)
def _configure_torch_threads() -> int | None:
    """Set torch intra-op threads to the CPU-aware budget (T3 Car 3).

    Process-global, set once at backend startup. N = ``torch_intraop_threads()``
    (1 at ncpu ≤ 2 = today's implicit single-thread inference, so byte-identical
    on the `--cpus 2` deployment; ncpu//2 above, reserving the other half for the
    provider gather arms — the two budgets compose within ncpu). Zero RAM cost,
    model-agnostic — the cheapest CE CPU-awareness lever.

    torch is a heavy, lazy import (never at module scope in the backend); this
    imports it locally and NO-OPS gracefully (returns None) when torch is
    unavailable, so a torch-less environment still boots. Returns the applied
    thread count, or None when torch could not be configured.
    """
    from yadgar._shared.runtime.cpu import torch_intraop_threads  # noqa: PLC0415

    n = torch_intraop_threads()
    try:
        import torch  # noqa: PLC0415

        torch.set_num_threads(n)
    except Exception as exc:  # noqa: BLE001 — torch missing/unset must not block boot
        logger.info("torch intra-op thread config skipped (%s)", exc)
        return None
    logger.info("torch intra-op threads set to %d (CPU-aware, T3 Car 3)", n)
    return n
