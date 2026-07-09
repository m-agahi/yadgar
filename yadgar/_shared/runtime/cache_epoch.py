"""Cache-invalidation epoch bus — shared across core and backend processes.

The structural epoch is the cache-invalidation signal for the shipped core
read-tool caches (project_brief / wiki_read / wiki_query / agent_prompt_prelude —
Car 1 and Car 2).  A WRITE lands in the BACKEND process and bumps the epoch; a
CORE read-tool cache keys on the epoch and must observe that bump.  A
process-local counter (module dict) would be split-brained, so the epoch is
backed by small counter FILES under the SHARED QUEUE VOLUME (the same volume
both processes already mount for the file queue — see MIGRATION_NOTES Car 0).

Layout under ``<base>/cache_epoch/``:
  * per-directory counter → ``<safe(dir)>``  (sanitized + hashed dir name)
  * global generation      → ``_global``

``_current_epoch(dir)`` = per-dir counter + global counter, so a bump of either
advances the effective epoch (a global bump invalidates every dir).  Writes are
flock-serialised read→+1→atomic-replace (cross-process monotonic, bounded size —
a single int per file, never an append log).  Reads are lock-free (atomic
os.replace makes a partial read impossible) and default to 0 on a missing file
or any error, so the safe failure direction is a MISS (recompute), never a false
HIT (stale serve).

NOTE: The recall-output shadow hit-rate instrumentation (``RecallShadowParams``,
``observe_recall``, ``_SHADOW_KEYS``) was removed in ADR-0071 (Car 3 killed —
0 % organic tool-path hit-rate).  Only the epoch bus remains.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from yadgar._shared.observability.observe import observe

# ── Shared cross-process epoch store (R3 Car 2) ──────────────────────────────
# Sentinel filename for the global generation counter (bumped by cross-directory
# structural events, folded into every dir's effective epoch).
_GLOBAL_FILE = "_global"
_EPOCH_SUBDIR = "cache_epoch"


@observe(tier="hot", metric="tools.cache_epoch.epoch_base_dir")
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


@observe(tier="hot", metric="tools.cache_epoch.counter_path")
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


@observe(tier="hot", metric="tools.cache_epoch.read_counter")
def _read_counter(path: Path) -> int:
    """Read a single-int counter file. Missing/partial/error → 0 (safe MISS)."""
    try:
        return int(path.read_text().strip())
    except Exception:  # pragma: no cover - missing file / transient error → 0
        return 0


@observe(tier="hot", metric="tools.cache_epoch.incr_counter")
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


@observe(tier="hot", metric="tools.cache_epoch.bump_epoch")
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


@observe(tier="hot", metric="tools.cache_epoch.current_epoch")
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


@observe(tier="hot", metric="tools.cache_epoch.unlink_quiet")
def _unlink_quiet(path: Path) -> None:
    """Best-effort unlink — swallows errors (test cleanup only)."""
    try:
        path.unlink()
    except Exception:  # pragma: no cover - best-effort test cleanup
        pass


@observe(tier="hot", metric="tools.cache_epoch.reset_for_test")
def _reset_for_test() -> None:
    """Test hook: clear the shared on-disk epoch counter files back to zero."""
    try:
        base = _epoch_base_dir()
        children = list(base.iterdir()) if base.is_dir() else []
    except Exception:  # pragma: no cover - best-effort test cleanup
        children = []
    for child in children:
        _unlink_quiet(child)
