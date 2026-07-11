"""Unit tests for the session-side SR transition recorder (T2 Car B).

SRTransitionRecorder is the layer-shared half of the cognitive map: the core
recall seam records transitions through it; the numpy compute lives in the
backend CognitiveMap subclass. Pure-unit — storage is a MagicMock; the real
DB write path is covered by tests/backend/test_cognitive_map.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from yadgar._shared.runtime.sr_session import _MIN_TRANSITIONS, SRTransitionRecorder


def _recorder(storage=None):
    return SRTransitionRecorder(storage or MagicMock())


class TestRecordTransition:
    def test_new_transition_inserts_with_count_1(self):
        storage = MagicMock()
        storage.get_transition.return_value = None
        rec = _recorder(storage)

        rec.record_transition(1, 2, "sess")

        storage.insert_transition.assert_called_once_with(
            {"from_memory_id": 1, "to_memory_id": 2, "count": 1, "session_id": "sess"}
        )
        storage.increment_transition.assert_not_called()

    def test_existing_transition_increments(self):
        storage = MagicMock()
        storage.get_transition.return_value = {"count": 3}
        rec = _recorder(storage)

        rec.record_transition(1, 2)

        storage.increment_transition.assert_called_once_with(1, 2)
        storage.insert_transition.assert_not_called()

    def test_record_marks_dirty(self):
        rec = _recorder()
        rec._dirty = False
        rec.record_transition(1, 2)
        assert rec._dirty is True


class TestInvalidate:
    def test_invalidate_marks_dirty(self):
        rec = _recorder()
        rec._dirty = False
        rec.invalidate()
        assert rec._dirty is True


class TestIncrementalUpdate:
    def test_noop_on_session_recorder(self):
        """The TD matrix update is backend compute — the base recorder no-ops."""
        storage = MagicMock()
        rec = _recorder(storage)
        assert rec.incremental_update(1, 2) is None
        storage.assert_not_called()


class TestHasSufficientData:
    def test_below_threshold(self):
        storage = MagicMock()
        storage.get_all_transitions.return_value = [{"count": _MIN_TRANSITIONS - 1}]
        assert _recorder(storage).has_sufficient_data() is False

    def test_at_threshold(self):
        storage = MagicMock()
        storage.get_all_transitions.return_value = [{"count": _MIN_TRANSITIONS}]
        assert _recorder(storage).has_sufficient_data() is True

    def test_empty_transitions(self):
        storage = MagicMock()
        storage.get_all_transitions.return_value = []
        assert _recorder(storage).has_sufficient_data() is False

    def test_none_counts_tolerated(self):
        storage = MagicMock()
        storage.get_all_transitions.return_value = [{"count": None}, {"count": _MIN_TRANSITIONS}]
        assert _recorder(storage).has_sufficient_data() is True


class TestBackendSubclassRelationship:
    def test_cognitive_map_extends_recorder(self):
        """The backend compute class shares the recorder's transition-write logic."""
        from yadgar.backend.restoration.cognitive_map import CognitiveMap

        assert issubclass(CognitiveMap, SRTransitionRecorder)
        # record_transition is single-source: the subclass does NOT override it.
        assert CognitiveMap.record_transition is SRTransitionRecorder.record_transition
        # incremental_update IS overridden (real TD update backend-side).
        assert CognitiveMap.incremental_update is not SRTransitionRecorder.incremental_update
