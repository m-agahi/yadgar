"""archive_purge MCP tool — Phase 4, v5.49.0.

Power-gated + secret-gated one-shot purge of memory_archive rows.
dry_run=True (default): report candidates without deleting.
dry_run=False: perform purge up to circuit-breaker limit.
"""

from __future__ import annotations

import logging

from yadgar.secrets import gate_or_reject
from yadgar.server._app import _tool

logger = logging.getLogger(__name__)


@_tool(power=True)
def archive_purge(
    dry_run: bool = True,
    retention_days: int | None = None,
) -> dict:
    """Purge memory_archive rows older than retention threshold.

    dry_run=True (default): no deletion. Returns candidates count + sample of 10 IDs.
    dry_run=False: performs purge. Circuit breaker (default 500) enforced.
    retention_days=None: use configured MEMORY_ARCHIVE_RETENTION_DAYS.
                        Otherwise temporarily override for this call.

    Returns:
      {
        "candidates": int,
        "purged": int,                 # 0 if dry_run=True
        "skipped_protected": int,
        "skipped_anchor": int,
        "skipped_recent": int,
        "circuit_breaker_hit": bool,
        "sample": list[int],           # up to 10 candidate IDs
        "dry_run": bool,
        "retention_days": int,         # effective value used
      }
    """
    import sys as _sys

    # I26 secret gate — args are bool/int, never trip gate, but call is required.
    _gate = gate_or_reject(
        f"archive_purge:dry_run={dry_run}:retention_days={retention_days}",
        source="archive_purge",
    )
    if _gate is not None:
        return _gate

    # Resolve storage singleton (same pattern as admin_vacuum.py).
    _srv = _sys.modules.get("yadgar.server")
    if _srv is not None and hasattr(_srv, "_get_storage"):
        _get_storage_fn = _srv._get_storage
    else:
        from yadgar.server.lifecycle import _get_storage as _get_storage_fn  # noqa: PLC0415

    storage = _get_storage_fn()

    from yadgar.storage.ops import purge_expired_archives as _purge  # noqa: PLC0415

    raw = _purge(storage, dry_run=dry_run, retention_days_override=retention_days)

    # Resolve effective retention_days for caller visibility.
    effective_days: int
    if retention_days is not None:
        effective_days = retention_days
    else:
        from yadgar.config import get_settings  # noqa: PLC0415

        effective_days = get_settings().MEMORY_ARCHIVE_RETENTION_DAYS

    result = {
        "candidates": raw["candidates"],
        "purged": raw["purged"],
        "skipped_protected": raw["skipped_protected"],
        "skipped_anchor": raw["skipped_anchor"],
        "skipped_recent": raw["skipped_recent"],
        "circuit_breaker_hit": raw["circuit_breaker_hit"],
        "sample": raw.get("candidate_ids", [])[:10],
        "dry_run": dry_run,
        "retention_days": effective_days,
    }

    logger.info(
        "archive_purge: dry_run=%s retention_days=%d candidates=%d purged=%d",
        dry_run,
        effective_days,
        result["candidates"],
        result["purged"],
    )

    return result
