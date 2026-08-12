"""Backend checkpoint replay entry (R3 Car 1 write-half).

The queue drainer replays a queued ``checkpoint`` job by writing it synchronously
via the replay engine. The enqueue fast-path + secret gating live in the core
checkpoint shell; this module owns the sync execution.

Imports ``_shared`` + backend only — no ``yadgar.core.*`` edge.
"""

from __future__ import annotations

import logging

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe
from yadgar._shared.restoration.contract import CheckpointContext
from yadgar._shared.runtime.lifecycle import _get_replay

logger = logging.getLogger(__name__)


def _enrich_checkpoint_context(custom_context: str) -> str:
    """Enrich custom_context with the action buffer summary if available."""
    buffer = _st._buffer
    if buffer is not None:
        action_summary = buffer.get_action_summary()
        if action_summary:
            return f"{custom_context}\n\n{action_summary}" if custom_context else action_summary
    return custom_context


def _build_checkpoint_ctx(  # noqa: PLR0913
    current_task: str,
    files_being_edited: list[str] | None,
    key_decisions: list[str] | None,
    open_questions: list[str] | None,
    next_steps: list[str] | None,
    active_errors: list[str] | None,
    enriched_context: str,
) -> CheckpointContext:
    """Construct a CheckpointContext from checkpoint parameters."""
    return CheckpointContext(
        current_task=current_task,
        files_being_edited=files_being_edited or [],
        key_decisions=key_decisions or [],
        open_questions=open_questions or [],
        next_steps=next_steps or [],
        active_errors=active_errors or [],
        custom_context=enriched_context,
    )


@observe(tier="boundary", metric="write_exec.checkpoint_replay")
def run_checkpoint_replay(payload: dict) -> dict:
    """Write a checkpoint synchronously (drain-replay). Never enqueues.

    Takes the queue PAYLOAD, matching ``run_action_log_replay`` two branches
    away in the same drainer dispatch. C11 (0047 PR#40 §5) collapsed the
    nine-parameter mirror-of-the-MCP-signature form into this one: adding
    ``project_id`` to it crossed the I30 HARD parameter cap, and the payload
    form is both the established sibling pattern and one fewer place for a key
    to be dropped in transit — which is precisely how ``project_id`` got lost
    before this car (the drainer validated it, then never passed it on).

    payload: {directory, current_task?, files_being_edited?, key_decisions?,
    open_questions?, next_steps?, active_errors?, custom_context?, project_id?}

    ``project_id`` is the host-minted identity that rides the enqueue payload
    (``core/server/tools/misc.py::checkpoint``). Migration 033 gave the
    ``checkpoint`` table a column for it. Nothing is derived here — a payload
    without one stamps NONE (ADR-0227).
    """
    replay = _get_replay()

    enriched_context = _enrich_checkpoint_context(payload.get("custom_context", ""))
    ctx = _build_checkpoint_ctx(
        payload.get("current_task", ""),
        payload.get("files_being_edited"),
        payload.get("key_decisions"),
        payload.get("open_questions"),
        payload.get("next_steps"),
        payload.get("active_errors"),
        enriched_context,
    )
    return replay.create_checkpoint(payload["directory"], ctx, project_id=payload.get("project_id"))
