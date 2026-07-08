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

v5.100.0 — source label ("hook" | "tool"):
  The ``source`` field on RecallShadowParams distinguishes explicit MCP-tool recalls
  ("tool") from the three hook auto-recall endpoints ("hook").  Hook auto-recalls
  fire 50-200 times/hour per session on repeated prompt text and would inflate the
  blended would-be hit-rate without this split.  The #88 output-cache gating
  decision should be evaluated on "tool" traffic only.

  ``source`` is kept as a **required** field with no default so every call site
  must be explicit.  Silently defaulting to one value would contaminate exactly
  the signal this label was added to preserve.

  ``source`` is also included in the shadow cache key so that a hook call and a
  tool call for the identical query occupy independent keyspaces (a hook hit for
  query Q must not register as a tool hit for query Q).

R3 Car 2 — cross-process shared epoch store:
  The structural epoch is the cache-invalidation signal that lets core read-tool
  caches (project_brief / wiki_read / wiki_query) know a backend write invalidated
  their cached result.  Post-Car-1 the WRITE that bumps the epoch runs in the
  BACKEND process while the READ that keys on it runs in the CORE process.  A
  process-local counter (module dict) is therefore split-brained: core never sees
  a backend bump and serves stale reads.

  The epoch is now backed by small counter FILES under the SHARED QUEUE VOLUME
  (the same volume both processes already mount for the file queue — see
  MIGRATION_NOTES Car 0).  Layout under ``<base>/cache_epoch/``:
    * per-directory counter → ``<safe(dir)>``  (sanitized + hashed dir name)
    * global generation      → ``_global``
  ``_current_epoch(dir)`` = per-dir counter + global counter, so a bump of either
  advances the effective epoch (a global bump invalidates every dir).  Writes are
  flock-serialised read→+1→atomic-replace (cross-process monotonic, bounded size —
  a single int per file, never an append log).  Reads are lock-free (atomic
  os.replace makes a partial read impossible) and default to 0 on a missing file
  or any error, so the safe failure direction is a MISS (recompute), never a false
  HIT (stale serve).

  The ``_SHADOW_KEYS`` LRU + would-be-hit/miss instrumentation below stays
  process-local — it is per-process measurement, not shared state.  Only the epoch
  source moved to the shared files.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from yadgar._shared.observability.observe import observe


@dataclass(frozen=True, slots=True)
class RecallShadowParams:
    """The semantically load-bearing recall inputs that form a would-be cache key.

    Bundled into one object so observe_recall takes a single param (keeps the
    key-component list in one place and satisfies the arg-count lint gate).

    v5.100.0: ``source`` is a **required** field ("hook" | "tool").  It is kept
    required (no default) so callers must be explicit — silently defaulting to
    one value would contaminate the signal the label was added to isolate.
    ``source`` is also part of the cache key so hook and tool calls for the
    same query occupy independent keyspaces.
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
    source: str  # "hook" | "tool" — required; no default (see docstring)


# ── Bounded state (module-level, process-lifetime) ───────────────────────────
_LOCK = threading.Lock()

# would-be-key -> effective-epoch-at-record.  Bounded LRU (can never grow unbounded).
# NOTE: this LRU is per-process instrumentation only (see module docstring); it is
# intentionally NOT shared across processes — unlike the epoch counters below.
_MAX_SHADOW_KEYS = 4096
_SHADOW_KEYS: OrderedDict[tuple, int] = OrderedDict()

# ── Shared cross-process epoch store (R3 Car 2) ──────────────────────────────
# Sentinel filename for the global generation counter (bumped by cross-directory
# structural events, folded into every dir's effective epoch).
_GLOBAL_FILE = "_global"
_EPOCH_SUBDIR = "cache_epoch"


@observe(tier="hot", metric="tools.recall_shadow.epoch_base_dir")
def _epoch_base_dir() -> Path:
    """Resolve the shared epoch directory under the shared queue volume.

    Mirrors how the file queue resolves its base (core: YADGAR_DATA_DIR; backend:
    YADGAR_QUEUE_BASE — both point at the SAME mounted volume, see MIGRATION_NOTES
    Car 0).  Read at call time (never cached at import) so pytest's env monkeypatch
    and the two deployed processes both resolve correctly.
    """
    base = os.environ.get("YADGAR_QUEUE_BASE") or os.environ.get("YADGAR_DATA_DIR")
    if not base:
        from yadgar._shared import paths as _paths  # noqa: PLC0415

        base = str(_paths.DATA_DIR)
    return Path(base) / _EPOCH_SUBDIR


@observe(tier="hot", metric="tools.recall_shadow.epoch_counter_path")
def _counter_path(directory: str | None) -> Path:
    """Return the counter file path for *directory* (or the global sentinel).

    A concrete directory is sanitized + hashed into a single stable filename so
    arbitrary paths (slashes, length) never break the file name and two distinct
    dirs never collide.
    """
    if not directory:
        return _epoch_base_dir() / _GLOBAL_FILE
    digest = hashlib.sha256(directory.encode("utf-8")).hexdigest()[:32]
    return _epoch_base_dir() / f"d_{digest}"


@observe(tier="hot", metric="tools.recall_shadow.read_counter")
def _read_counter(path: Path) -> int:
    """Read a single-int counter file. Missing/partial/error → 0 (safe MISS)."""
    try:
        return int(path.read_text().strip())
    except Exception:  # pragma: no cover - missing file / transient error → 0
        return 0


@observe(tier="hot", metric="tools.recall_shadow.incr_counter")
def _incr_counter(path: Path) -> None:
    """flock-serialised read→+1→atomic-replace increment (cross-process safe)."""
    import fcntl  # noqa: PLC0415

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            current = _read_counter(path)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(str(current + 1))
            os.replace(tmp, path)  # atomic → readers never see a partial write
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


@observe(tier="hot", metric="tools.recall_shadow.bump_epoch")
def bump_epoch(directory: str | None) -> None:
    """Advance the structural epoch for *directory* (called from write paths).

    A concrete directory bumps only that directory's counter file (memorize/forget).
    A None/empty directory bumps the GLOBAL generation file, which invalidates keys
    for every directory (used for cross-directory events like consolidation's prior
    recompute).  Backed by the shared queue volume so a bump in the BACKEND process
    is visible to reads in the CORE process.  Fully guarded: never raises, never
    blocks the write path.
    """
    try:
        _incr_counter(_counter_path(directory))
    except Exception:  # pragma: no cover - instrumentation must never break writes
        pass


@observe(tier="hot", metric="tools.recall_shadow._current_epoch")
def _current_epoch(directory: str | None) -> int:
    # Effective epoch = per-directory counter + the global generation counter.
    # Bumping either advances the effective epoch, so a key recorded at the old
    # value misses.  Lock-free file reads (atomic os.replace on write); any error
    # → 0, i.e. a safe MISS, never a false HIT.
    try:
        dir_count = _read_counter(_counter_path(directory)) if directory else 0
        global_count = _read_counter(_counter_path(None))
        return int(dir_count + global_count)
    except Exception:  # pragma: no cover - read must never break a cache-key build
        return 0


def _make_key(p: RecallShadowParams) -> tuple:
    """Build the exact would-be cache key (normalised, hashable).

    v5.100.0: ``source`` is included so hook and tool calls occupy independent
    keyspaces — a hook hit for query Q must not register as a tool hit for Q.
    """
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
        p.source,  # v5.100.0 — independent keyspace per traffic source
    )


def observe_recall(params: RecallShadowParams) -> None:
    """Record a would-HIT or would-MISS for this recall. Instrumentation only.

    Fully guarded — a raise here must never affect the recall result or latency.
    """
    try:
        from yadgar._shared.metrics import (  # noqa: PLC0415
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
            yadgar_recall_shadow_cache_hits_total.labels(source=params.source).inc()
        else:
            yadgar_recall_shadow_cache_misses_total.labels(source=params.source).inc()
    except Exception:  # pragma: no cover - instrumentation must never break recall
        pass


@observe(tier="hot", metric="tools.recall_shadow.unlink_quiet")
def _unlink_quiet(path: Path) -> None:
    """Best-effort unlink — swallows errors (test cleanup only)."""
    try:
        path.unlink()
    except Exception:  # pragma: no cover - best-effort test cleanup
        pass


@observe(tier="hot", metric="tools.recall_shadow._reset_for_test")
def _reset_for_test() -> None:
    """Test hook: clear all shadow state — the process-local LRU AND the shared
    on-disk epoch counter files."""
    with _LOCK:
        _SHADOW_KEYS.clear()
    try:
        base = _epoch_base_dir()
        children = list(base.iterdir()) if base.is_dir() else []
    except Exception:  # pragma: no cover - best-effort test cleanup
        children = []
    for child in children:
        _unlink_quiet(child)
