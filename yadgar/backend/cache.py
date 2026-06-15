"""LRU cache with msgpack snapshot for backend hot-path caching (backend v5.4.0).

Two instances are created per backend process:
  - _ce_cache    — CE score cache, key=(query_sha:text_sha:ckpt_sha), value=float
  - _embed_cache — Embedding cache, key=(text_sha:ckpt_sha), value=list[float]

Design:
  - OrderedDict LRU (most-recently-used order); O(1) get/put.
  - Entry-count cap; max_entries=0 disables the cache (all puts are no-ops).
  - Periodic disk snapshot via msgpack. Format:
      YADCACHE\\0 (9 bytes magic)
      version byte (1 byte, currently 0x01)
      checkpoint_hash as UTF-8 length-prefixed string (4-byte LE len + bytes)
      msgpack-encoded list of [key, value] pairs (all remaining bytes)
  - On load: magic + version must match, checkpoint_hash must match current
    model hash — mismatch silently returns empty cache.
  - Thread-safe reads (no lock needed for OrderedDict get on CPython).
  - Snapshot write takes a shallow copy under a threading.Lock to avoid
    racing with concurrent puts.

I13: nesting ≤ 4. Module ≤ 300 LOC is acceptable as single-responsibility cache.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Snapshot file magic header + version
_MAGIC = b"YADCACHE\x00"
_VERSION = b"\x01"


class LRUCache:
    """OrderedDict-backed LRU cache with msgpack snapshot.

    Args:
        max_entries: Maximum number of entries. 0 = disabled (all puts no-op).
        checkpoint_hash: Hash of the model checkpoint. Snapshots written with a
            different hash are silently discarded on load.
    """

    def __init__(self, max_entries: int, checkpoint_hash: str) -> None:
        self._max = max_entries
        self._ckpt = checkpoint_hash
        self._store: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        # Counters (informational, not thread-safe at int level but acceptable
        # for metric reporting — off-by-one on counter in rare race is fine)
        self.hits: int = 0
        self.misses: int = 0
        self.evictions: int = 0

    # ── Core ops ─────────────────────────────────────────────────────────────

    def get(self, key: str) -> Any | None:
        """Return value for key, or None on miss. Promotes to MRU on hit."""
        if self._max == 0:
            self.misses += 1
            return None
        with self._lock:
            if key not in self._store:
                self.misses += 1
                return None
            # Move to end (most-recently-used)
            self._store.move_to_end(key)
            self.hits += 1
            return self._store[key]

    def put(self, key: str, value: Any) -> None:
        """Insert or update key → value. Evicts LRU entry if at cap."""
        if self._max == 0:
            return
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = value
            else:
                self._store[key] = value
                if len(self._store) > self._max:
                    self._store.popitem(last=False)  # evict LRU (oldest)
                    self.evictions += 1

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def size_entries(self) -> int:
        return len(self._store)

    @property
    def size_bytes(self) -> int:
        """Rough byte estimate via sys.getsizeof on the internal dict."""
        import sys

        with self._lock:
            return sys.getsizeof(self._store)

    # ── Snapshot I/O ─────────────────────────────────────────────────────────

    def save_snapshot(self, snap_dir: str, name: str) -> None:
        """Serialize cache to <snap_dir>/<name>.snap using msgpack.

        Takes a shallow copy under lock, then writes without holding the lock.
        Writes to a temp file and renames atomically.
        """
        try:
            import msgpack  # noqa: PLC0415
        except ImportError:
            logger.warning("cache.save_snapshot: msgpack not installed — skipping")
            return

        with self._lock:
            items = list(self._store.items())

        path = Path(snap_dir) / f"{name}.snap"
        tmp_path = path.with_suffix(".snap.tmp")

        ckpt_bytes = self._ckpt.encode("utf-8")
        ckpt_len = struct.pack("<I", len(ckpt_bytes))

        payload = msgpack.packb(items, use_bin_type=True)
        header = _MAGIC + _VERSION + ckpt_len + ckpt_bytes

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(header + payload)
            tmp_path.replace(path)
        except OSError as exc:
            logger.warning("cache.save_snapshot: write failed for %s: %s", path, exc)

    def load_snapshot(self, snap_dir: str, name: str) -> None:
        """Restore entries from <snap_dir>/<name>.snap.

        Silently discards on: missing file, magic mismatch, version mismatch,
        checkpoint hash mismatch, or any parse error.
        """
        try:
            import msgpack  # noqa: PLC0415
        except ImportError:
            logger.warning("cache.load_snapshot: msgpack not installed — skipping")
            return

        path = Path(snap_dir) / f"{name}.snap"
        if not path.exists():
            return

        try:
            data = path.read_bytes()
            # Check magic (9 bytes) + version (1 byte)
            if len(data) < 10 or data[:9] != _MAGIC or data[9:10] != _VERSION:
                logger.warning("cache.load_snapshot: bad header in %s — discarding", path)
                return

            offset = 10
            if len(data) < offset + 4:
                logger.warning("cache.load_snapshot: truncated ckpt len in %s", path)
                return
            ckpt_len = struct.unpack("<I", data[offset : offset + 4])[0]
            offset += 4

            if len(data) < offset + ckpt_len:
                logger.warning("cache.load_snapshot: truncated ckpt hash in %s", path)
                return
            stored_ckpt = data[offset : offset + ckpt_len].decode("utf-8", errors="replace")
            offset += ckpt_len

            if stored_ckpt != self._ckpt:
                logger.info(
                    "cache.load_snapshot: checkpoint mismatch (%s != %s) — discarding %s",
                    stored_ckpt[:16],
                    self._ckpt[:16],
                    path,
                )
                return

            items: list = msgpack.unpackb(data[offset:], raw=False)
            with self._lock:
                self._store.clear()
                for k, v in items:
                    self._store[k] = v
                    if self._max > 0 and len(self._store) > self._max:
                        self._store.popitem(last=False)
                        self.evictions += 1
        except Exception as exc:
            logger.warning("cache.load_snapshot: error loading %s: %s — discarding", path, exc)
            with self._lock:
                self._store.clear()

    def snapshot_age_seconds(self, snap_dir: str, name: str) -> float:
        """Return seconds since snapshot was last written, or -1 if no file."""
        path = Path(snap_dir) / f"{name}.snap"
        if not path.exists():
            return -1.0
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return -1.0
