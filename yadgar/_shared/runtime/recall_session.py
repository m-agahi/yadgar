"""Recall SESSION-side bookkeeping (T2 Car E2 split, dual by the placement laws).

The core process calls ``_apply_recall_session_side_effects`` after every
FORWARDED recall (SR transitions, action buffer, auto-checkpoint tick — all
in-process session state, no retrieval imports). The backend recall pipeline's
``_apply_recall_side_effects`` combiner calls the same half on the legacy/test
path. Split out of ``recall_pipeline`` when the pipeline (a retrieval
EXECUTOR) sank to ``yadgar.backend.retrieval`` — this half must stay
importable from core without dragging the retrieval package across the layer
boundary (ADR-0078).
"""

from __future__ import annotations

import logging

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.recall_utils import _bounded_set

logger = logging.getLogger(__name__)


@observe(tier="stage", metric="tools.recall._record_recall_sr_transition")
def _record_recall_sr_transition(merged: list[dict]) -> None:
    """Record an SR (successor-representation) transition: previous recall → this one.

    Links the top MEMORY of the current recall to the top memory of the prior recall
    on the cognitive map (wiki rows are not map nodes). Split out of
    _apply_recall_side_effects to keep nesting within the I13 cap.
    """
    if _st._cognitive_map is None or not merged:
        return
    session_key = "default"
    top_id = next(
        (m.get("id") for m in merged if m.get("_source") != "wiki" and m.get("id") is not None),
        None,
    )
    if top_id is None:
        return
    prev_id = _st._last_recalled_ids.get(session_key)
    if prev_id is not None and prev_id != top_id:
        try:
            _st._cognitive_map.record_transition(prev_id, top_id, session_key)
            _st._cognitive_map.incremental_update(prev_id, top_id)
        except Exception:
            logger.debug("SR transition recording failed")
    _bounded_set(_st._last_recalled_ids, session_key, top_id)


@observe(tier="stage", metric="tools.recall._apply_recall_session_side_effects")
def _apply_recall_session_side_effects(merged: list[dict], query: str) -> None:
    """Core-local session bookkeeping half: SR transitions, action buffer, replay counter.

    Runs in the core process after _fanout_recall returns (both in-core and
    backend-forwarded paths).  Does NOT do any DB writes — those are in
    _apply_recall_db_side_effects.

    Writes performed:
      - _record_recall_sr_transition: updates _st._cognitive_map + _st._last_recalled_ids
        (T2 Car B: core-side the slot holds the session SRTransitionRecorder —
        record_transition writes via storage; incremental_update is a no-op there)
      - buffer.capture_action: appends to the in-process action stream
      - _replay.record_tool_call: ticks the auto-checkpoint counter (T2 Car B:
        _st._replay is None in the core process — guarded no-op there; the drainer's
        memorize post-write phase ticks the backend counter instead)

    Args:
        merged: Result list from _fanout_recall (or returned by the backend).
        query: Original query string used for action log entry.
    """
    from yadgar._shared.observability.tracing import span  # noqa: PLC0415

    # span(): curated landmark name — inline CM can't auto-derive (ADR-0061 exception)
    with span("recall.side_effects.session", results=len(merged)):
        # SR transitions: link previous recall → current recall.
        _record_recall_sr_transition(merged)

        # Action stream: log this recall operation.
        buffer = _st._buffer
        if buffer is not None:
            result_count = len(merged)
            buffer.capture_action(
                "recall",
                "",
                f"query='{query[:80]}' results={result_count}",
                f"found_{result_count}",
            )

        # Track tool call for auto-checkpoint interval.
        if _st._replay is not None:
            _st._replay.record_tool_call()
