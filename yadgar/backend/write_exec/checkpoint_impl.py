"""Backend checkpoint replay entry (R3 Car 1 write-half).

The queue drainer replays a queued ``checkpoint`` job by writing it synchronously
via the replay engine. The enqueue fast-path + secret/branch gating live in the
core checkpoint shell; this module owns the sync execution.

Imports ``_shared`` + backend only — no ``yadgar.core.*`` edge.
"""

from __future__ import annotations

import logging

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe
from yadgar._shared.restoration import CheckpointContext
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
def run_checkpoint_replay(  # noqa: PLR0913 — mirrors the checkpoint MCP signature
    directory: str,
    current_task: str = "",
    files_being_edited: list[str] | None = None,
    key_decisions: list[str] | None = None,
    open_questions: list[str] | None = None,
    next_steps: list[str] | None = None,
    active_errors: list[str] | None = None,
    custom_context: str = "",
) -> dict:
    """Write a checkpoint synchronously (drain-replay). Never enqueues."""
    replay = _get_replay()

    enriched_context = _enrich_checkpoint_context(custom_context)
    ctx = _build_checkpoint_ctx(
        current_task,
        files_being_edited,
        key_decisions,
        open_questions,
        next_steps,
        active_errors,
        enriched_context,
    )
    return replay.create_checkpoint(directory, ctx)
