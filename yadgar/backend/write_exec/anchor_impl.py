"""Backend anchor replay entry (R3 Car 1 write-half).

The queue drainer replays a queued ``anchor`` job by writing it synchronously
via the replay engine. The enqueue fast-path + validation live in the core
anchor shell; this module owns the sync execution.

Imports ``_shared`` + backend only — no ``yadgar.core.*`` edge.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_replay

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="write_exec.anchor_replay")
def run_anchor_replay(
    content: str,
    context: str,
    reason: str = "",
    tier: str | None = None,
    valid_until: str | None = None,
) -> dict:
    """Write an anchored memory synchronously (drain-replay). Never enqueues."""
    replay = _get_replay()
    tags = ["_anchor"]
    if reason:
        tags.append(f"anchor:{reason}")
    memory_id = replay.anchor_memory(
        content,
        context,
        tags,
        reason,
        tier=tier,
        valid_until=valid_until,
    )
    return {
        "memory_id": memory_id,
        "status": "anchored",
        "is_protected": True,
        "reason": reason,
        "tier": tier,
    }
