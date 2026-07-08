"""archive_purge MCP tool — Phase 4, v5.49.0.

Power-gated + secret-gated one-shot purge of memory_archive rows.
dry_run=True (default): report candidates without deleting.
dry_run=False: perform purge up to circuit-breaker limit.
"""

from __future__ import annotations

import logging

from yadgar._shared.secrets import gate_or_reject
from yadgar.core.server._app import _tool
from yadgar.core.server.tools._forward import _forward_admin

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
    # I26 secret gate — args are bool/int, never trip gate, but call is required.
    # The gate stays CORE-side (per the R5 forward pattern); only the DB write forwards.
    _gate = gate_or_reject(
        f"archive_purge:dry_run={dry_run}:retention_days={retention_days}",
        source="archive_purge",
    )
    if _gate is not None:
        return _gate

    # R3 Car 3b: the memory_archive purge (DB delete) forwards to backend /admin.
    return _forward_admin(
        "archive_purge",
        {"dry_run": dry_run, "retention_days": retention_days},
    )
