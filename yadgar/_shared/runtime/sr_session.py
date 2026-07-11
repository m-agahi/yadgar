"""Session-side SR transition buffer — the core-resident half of the cognitive map.

T2 Car B (layer-boundary train, census verdicts #5/#7): the numpy SR-matrix
compute (``CognitiveMap``) moved to ``yadgar.backend.restoration.cognitive_map``
behind the backend ``POST /restore`` forward. What stays layer-shared is ONLY
the session-side transition recording that the core recall path performs after
every forwarded recall (``recall_pipeline._record_recall_sr_transition``):
a storage write per observed memory→memory access transition plus the
sufficiency check the core status readout (``admin_other``) reports.

``CognitiveMap`` (backend) subclasses this recorder and adds the matrix /
navigation compute — the transition-write logic stays single-source here.
The ``insert_transition`` / ``increment_transition`` writes flow through the
genuinely-dual ``_shared.storage`` engine, unchanged by Car B.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine

logger = logging.getLogger(__name__)

# Minimum transitions before SR is considered useful
_MIN_TRANSITIONS = 20


class SRTransitionRecorder:
    """Record memory→memory access transitions (session-side SR buffer).

    Core holds an instance of THIS class in ``_st._cognitive_map`` (built by the
    shared composition root). The backend upgrades the same slot to the full
    ``CognitiveMap`` subclass via ``ensure_restoration_engines`` — the matrix /
    prediction compute never runs in the core process (census verdict #7).
    """

    def __init__(self, storage: StorageEngine) -> None:
        self._storage = storage
        # Matrix-staleness flag. Meaningless for the bare recorder; the backend
        # CognitiveMap subclass reads it to know when to rebuild the SR matrix.
        self._dirty = True

    # -- Recording --

    @observe(tier="boundary", metric="cognitive_map.record_transition")
    def record_transition(
        self, from_memory_id: int, to_memory_id: int, session_id: str = ""
    ) -> None:
        """Record that memory 'to' was accessed right after memory 'from'."""
        existing = self._storage.get_transition(from_memory_id, to_memory_id)
        if existing:
            self._storage.increment_transition(from_memory_id, to_memory_id)
        else:
            self._storage.insert_transition(
                {
                    "from_memory_id": from_memory_id,
                    "to_memory_id": to_memory_id,
                    "count": 1,
                    "session_id": session_id,
                }
            )
        self._dirty = True

    def invalidate(self) -> None:
        """Mark the SR matrix stale so the next compute rebuilds it from storage.

        Car B: transitions are recorded in the CORE process while the matrix is
        computed in the BACKEND process — the ``_dirty`` flag no longer crosses
        automatically. The backend restore path calls this before each restore so
        predictions always reflect the transitions the core wrote to the DB.
        No-op consequence for the bare recorder (it holds no matrix).
        """
        self._dirty = True

    @observe(tier="stage", metric="cognitive_map.incremental_update", span=False)
    def incremental_update(self, from_id: int, to_id: int) -> None:
        """TD-learning matrix update — no-op on the session-side recorder.

        The SR matrix lives in the backend process (Car B); the core session
        seam only RECORDS transitions. Behavior-preserving: pre-Car-B the core
        matrix was only ever built by a local ``restore()`` — which is now
        forwarded — so this call was already a no-op on the core hot path
        (``CognitiveMap.incremental_update`` early-returns when no matrix is
        built). The backend ``CognitiveMap`` subclass overrides with the real
        TD update.
        """
        return

    @observe(tier="stage", metric="cognitive_map.has_sufficient_data")
    def has_sufficient_data(self) -> bool:
        """Check if enough transitions exist for meaningful SR computation."""
        transitions = self._storage.get_all_transitions()
        total_count = sum(t.get("count") or 0 for t in transitions) if transitions else 0
        return total_count >= _MIN_TRANSITIONS
