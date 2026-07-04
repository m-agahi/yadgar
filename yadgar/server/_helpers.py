"""Shared utility functions used across server submodules.

Imports from _state only — no imports from other yadgar.server.* modules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import OrderedDict
from pathlib import Path

import yadgar.server._state as _st
from yadgar.config import get_settings
from yadgar.observability.observe import observe

logger = logging.getLogger(__name__)

settings = get_settings()

# Strong decision patterns for auto-protection
_DECISION_STRONG_RE = re.compile(
    r"\b(chose .+ over|decided to use|switched from .+ to|migrated from|"
    r"will use .+ instead|going with|opted for|selected .+ because|"
    r"choosing .+ approach|picking .+ strategy)\b",
    re.IGNORECASE,
)


@observe(tier="stage")
def _q_with_timeout(
    storage, surql: str, params: dict | None = None, timeout_seconds: int = 60
) -> list:  # noqa: E501
    """Run a storage query with an optional per-request timeout.

    In server (httpx) mode the httpx Client timeout is temporarily widened to
    *timeout_seconds*.  In embedded mode _q handles its own retry.  Always routes
    through storage._q so test stubs patching _q remain effective.
    """
    http = getattr(storage, "_http", None)
    if http is not None:
        try:
            import httpx as _httpx
        except ImportError:
            return storage._q(surql, params)
        old_timeout = http.timeout
        try:
            http.timeout = _httpx.Timeout(float(timeout_seconds))
            return storage._q(surql, params)
        finally:
            http.timeout = old_timeout
    return storage._q(surql, params)


@observe(tier="stage")
def _has_unpaired_surrogate(s: str) -> bool:
    """Return True if the string contains unpaired UTF-16 surrogate code points,
    which cannot be encoded as UTF-8 and would crash the storage pipeline."""
    if not s:
        return False
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


@observe(tier="stage")
def _push_event(event: dict) -> None:
    """Append an event to the ring buffer with a monotonic sequence number."""
    with _st._event_lock:
        _st._event_seq += 1
        _st._event_queue.append({"seq": _st._event_seq, **event})


@observe(tier="stage")
def _bounded_set(d: OrderedDict, key, value, max_size: int = _st._DICT_MAX_SIZE) -> None:
    """Insert key→value, evicting oldest entry if dict exceeds max_size."""
    d[key] = value
    if len(d) > max_size:
        d.popitem(last=False)  # remove LRU (first inserted)


@observe(tier="stage")
def _is_episodic_query(query: str) -> bool:
    """Return True if the query is temporal/episodic — wiki blending is skipped."""
    q = query.lower()
    for kw in settings.TEMPORAL_KEYWORDS.split(","):
        kw = kw.strip()
        if kw and kw in q:
            return True
    return False


@observe(tier="stage")
def _file_hash(filepath: str) -> str | None:
    """Compute SHA-256 hash of a file if it is under a registered project root.

    §4 security requirements:
    - Only hashes files under directories registered via seed_project.
    - Skips files larger than YADGAR_MAX_HASH_BYTES (default 10 MiB).
    - Streams in 64 KiB chunks — never reads the full file into memory.
    """
    try:
        p = Path(filepath).expanduser().resolve()
    except Exception:
        return None
    if not p.is_file():
        return None

    # Whitelist: only hash files under a registered project root.
    str_path = str(p)
    if _st._project_roots:
        allowed = any(
            str_path == root or str_path.startswith(root + os.sep) for root in _st._project_roots
        )
        if not allowed:
            logger.debug("_file_hash: %s outside registered project roots — skipped", str_path)
            return None

    # Size cap — skip files larger than YADGAR_MAX_HASH_BYTES.
    from yadgar.config import get_settings as _get_settings

    max_bytes = _get_settings().MAX_HASH_BYTES
    try:
        if p.stat().st_size > max_bytes:
            logger.debug(
                "_file_hash: %s exceeds MAX_HASH_BYTES (%d) — skipped", str_path, max_bytes
            )
            return None
    except OSError:
        return None

    # Stream-hash in 64 KiB chunks — no read_bytes().
    h = hashlib.sha256()
    try:
        with p.open("rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


@observe(tier="stage")
def _build_dlq_alert_text() -> str:
    """Return a markdown warning string if any items are in the DLQ, else ''."""
    try:
        data_dir = Path(os.environ.get("YADGAR_DATA_DIR", settings.DATA_DIR))
        dlq_dir = data_dir / "dlq"
        if not dlq_dir.exists():
            return ""
        alerts = []
        for sidecar in sorted(dlq_dir.glob("*.json.error.json")):
            try:
                meta = json.loads(sidecar.read_text())
                meta["_file"] = sidecar.name[: -len(".error.json")]
                alerts.append(meta)
            except Exception:
                logger.warning("DLQ alert: failed to parse sidecar %s", sidecar, exc_info=True)
        if not alerts:
            return ""
        lines = [f"# Yadgar DLQ Alert — {len(alerts)} item(s) stuck\n"]
        lines.append("These writes failed permanently and will not be retried automatically.")
        lines.append(
            "Run `dlq_inspect()` for details, `dlq_requeue(filename)` after fixing root cause.\n"
        )
        for a in alerts[:5]:
            lines.append(
                f"- {a.get('op_type', '?')}  attempts={a.get('attempts')}  "
                f"moved={a.get('moved_to_dlq_at', '')[:19]}  "
                f"error={str(a.get('last_error', ''))[:80]}"
            )
        if len(alerts) > 5:
            lines.append(f"  ... and {len(alerts) - 5} more")
        return "\n".join(lines)
    except Exception:
        return ""
