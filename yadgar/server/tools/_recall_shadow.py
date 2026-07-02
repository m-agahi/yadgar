"""v5.96.0 — shadow recall result-cache hit-rate counter (instrumentation only).

This module measures the hit-rate a hypothetical query→ranked-output cache (the
cache-refactor plan's *lever a*, deliberately NOT built yet) WOULD achieve, so that
building the real cache can be gated on measured evidence instead of assumption.

It stores NOTHING except (would-be-key → structural-epoch) pairs and bumps two
Prometheus counters.  It never caches a result, never changes what recall returns,
and is fully wrapped in try/except so a raise here can never break a recall or block
a write.

Model:
  * A per-directory **structural epoch** is bumped whenever a write that changes the
    candidate set / prior scalars for a directory lands (memorize / consolidation
    prior recompute).  Any coarser "bump on any memorize" is acceptable — the point
    is only to make stale keys unreachable so a would-HIT is never falsely counted
    after a structural change.
  * On each recall we compute the would-be cache key (query + scope + params) and
    look it up in a bounded dict:
      - key present AND its stored epoch == the directory's current epoch → would-HIT
      - otherwise                                                         → would-MISS
        (and we record key→current-epoch so an immediate repeat would hit)
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecallShadowParams:
    """The semantically load-bearing recall inputs that form a would-be cache key.

    Bundled into one object so observe_recall takes a single param (keeps the
    key-component list in one place and satisfies the arg-count lint gate).
    """

    query: str
    directory: str | None
    branch: str | None
    type_filter: str
    mode: str | None
    profile: str | None
    max_results: int
    min_heat: float
    tags: list[str] | None


# ── Bounded state (module-level, process-lifetime) ───────────────────────────
_LOCK = threading.Lock()

# directory -> structural epoch counter (monotonic; bumped on structural writes).
_DIR_EPOCH: dict[str, int] = {}

# global generation — bumped by cross-directory structural events (e.g. the
# consolidation prior recompute rewrites prior scalars for every directory).  It is
# folded into every key's effective epoch so a global bump invalidates ALL keys,
# not just one directory's.
_GLOBAL_GEN: list[int] = [0]

# would-be-key -> effective-epoch-at-record.  Bounded LRU (can never grow unbounded).
_MAX_SHADOW_KEYS = 4096
_SHADOW_KEYS: OrderedDict[tuple, int] = OrderedDict()


def bump_epoch(directory: str | None) -> None:
    """Advance the structural epoch for *directory* (called from write paths).

    A concrete directory bumps only that directory's epoch (memorize/forget).
    A None/empty directory bumps the GLOBAL generation, which invalidates keys for
    every directory (used for cross-directory events like consolidation's prior
    recompute).  Fully guarded: never raises, never blocks the write path.
    """
    try:
        with _LOCK:
            if directory:
                _DIR_EPOCH[directory] = _DIR_EPOCH.get(directory, 0) + 1
            else:
                _GLOBAL_GEN[0] += 1
    except Exception:  # pragma: no cover - instrumentation must never break writes
        pass


def _current_epoch(directory: str | None) -> int:
    # Effective epoch = per-directory epoch + the global generation.  Bumping either
    # advances the effective epoch, so a prior key recorded at the old value misses.
    return _DIR_EPOCH.get(directory or "global", 0) + _GLOBAL_GEN[0]


def _make_key(p: RecallShadowParams) -> tuple:
    """Build the exact would-be cache key (normalised, hashable)."""
    norm_query = " ".join((p.query or "").split()).lower()
    norm_tags = tuple(sorted(p.tags)) if p.tags else ()
    return (
        norm_query,
        p.directory or "global",
        p.branch or "",
        p.type_filter,
        p.mode or "",
        p.profile or "",
        int(p.max_results),
        round(float(p.min_heat), 2),
        norm_tags,
    )


def observe_recall(params: RecallShadowParams) -> None:
    """Record a would-HIT or would-MISS for this recall. Instrumentation only.

    Fully guarded — a raise here must never affect the recall result or latency.
    """
    try:
        from yadgar.metrics import (  # noqa: PLC0415
            yadgar_recall_shadow_cache_hits_total,
            yadgar_recall_shadow_cache_misses_total,
        )

        key = _make_key(params)
        epoch = _current_epoch(params.directory)

        with _LOCK:
            stored = _SHADOW_KEYS.get(key)
            hit = stored is not None and stored == epoch
            # (Re)record at the current epoch and mark as most-recently-used.
            _SHADOW_KEYS[key] = epoch
            _SHADOW_KEYS.move_to_end(key)
            while len(_SHADOW_KEYS) > _MAX_SHADOW_KEYS:
                _SHADOW_KEYS.popitem(last=False)

        if hit:
            yadgar_recall_shadow_cache_hits_total.inc()
        else:
            yadgar_recall_shadow_cache_misses_total.inc()
    except Exception:  # pragma: no cover - instrumentation must never break recall
        pass


def _reset_for_test() -> None:
    """Test hook: clear all shadow state."""
    with _LOCK:
        _DIR_EPOCH.clear()
        _GLOBAL_GEN[0] = 0
        _SHADOW_KEYS.clear()
