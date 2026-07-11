"""Backend action_log replay entry (T2 Car E1 write-half).

The queue drainer replays a queued ``action_log`` job by inserting the row
synchronously via storage. The enqueue fast-paths live in core (the
``/hooks/auto-capture`` flush, the team-inbox ingest, and the ``yadgar
capture`` CLI) — this module owns the sync execution so core performs zero
direct ``insert_action_log`` writes (ADR-0078).

Imports ``_shared`` + backend only — no ``yadgar.core.*`` edge.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="backend.write.action_log")
def run_action_log_replay(payload: dict) -> None:
    """Insert one action_log row from a queued ``action_log`` job.

    payload keys: ``tool_name`` (required), ``summary``, ``directory``,
    ``session_id``, ``timestamp`` (ISO string; defaults to now when absent —
    enqueue normally stamps it, so the default only covers hand-crafted jobs).
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    storage = _get_storage()
    storage.insert_action_log(
        tool_name=payload["tool_name"],
        tool_input_summary=payload.get("summary", ""),
        directory=payload.get("directory", ""),
        session_id=payload.get("session_id", ""),
        timestamp=payload.get("timestamp") or datetime.now(UTC).isoformat(),
    )
