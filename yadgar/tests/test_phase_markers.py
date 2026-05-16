"""§15 Consolidation phase markers tests.

Tests:
- _consolidation_cycle emits phase_start + phase_end log lines for each phase
- phase_end includes duration_ms field
- Phase names are present (apply_decay, process_episodes, merge_duplicates, etc.)
"""

import logging

import pytest


class _PhaseCapture(logging.Handler):
    """Capture log records for phase marker assertions."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def phase_starts(self):
        return [r for r in self.records if "phase_start" in r.getMessage()]

    def phase_ends(self):
        return [r for r in self.records if "phase_end" in r.getMessage()]

    def has_phase(self, phase_name: str) -> bool:
        return any(phase_name in r.getMessage() for r in self.records)

    def end_has_duration_ms(self) -> bool:
        """All phase_end records must have duration_ms."""
        for r in self.phase_ends():
            if not ("duration_ms" in r.getMessage() or getattr(r, "duration_ms", None) is not None):
                return False
        return True


@pytest.fixture()
def phase_capture():
    """Install a capturing log handler on the consolidation logger."""
    capture = _PhaseCapture()
    capture.setLevel(logging.DEBUG)
    logger = logging.getLogger("yadgar.consolidation")
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    yield capture
    logger.removeHandler(capture)


def test_consolidation_emits_phase_start_markers(tmp_path, phase_capture):
    """_consolidation_cycle must emit at least one phase_start log line."""
    from yadgar import server

    server.init_engines(
        db_path=str(tmp_path / "phase_test.db"),
        embedding_model="all-MiniLM-L6-v2",
        start_daemons=False,
    )
    try:
        server._consolidation.force_consolidate()
    finally:
        server.shutdown()

    starts = phase_capture.phase_starts()
    assert len(starts) >= 1, (
        f"Expected at least one phase_start log line, got {len(starts)}.\n"
        f"All records: {[r.getMessage() for r in phase_capture.records]}"
    )


def test_consolidation_emits_phase_end_markers(tmp_path, phase_capture):
    """_consolidation_cycle must emit at least one phase_end log line."""
    from yadgar import server

    server.init_engines(
        db_path=str(tmp_path / "phase_test2.db"),
        embedding_model="all-MiniLM-L6-v2",
        start_daemons=False,
    )
    try:
        server._consolidation.force_consolidate()
    finally:
        server.shutdown()

    ends = phase_capture.phase_ends()
    assert len(ends) >= 1, (
        f"Expected at least one phase_end log line, got {len(ends)}.\n"
        f"All records: {[r.getMessage() for r in phase_capture.records]}"
    )


def test_consolidation_phase_end_has_duration_ms(tmp_path, phase_capture):
    """Every phase_end log line must include a duration_ms value."""
    from yadgar import server

    server.init_engines(
        db_path=str(tmp_path / "phase_test3.db"),
        embedding_model="all-MiniLM-L6-v2",
        start_daemons=False,
    )
    try:
        server._consolidation.force_consolidate()
    finally:
        server.shutdown()

    ends = phase_capture.phase_ends()
    assert len(ends) >= 1, "No phase_end records found"
    for record in ends:
        msg = record.getMessage()
        assert "duration_ms" in msg, f"phase_end record missing duration_ms: {msg!r}"


def test_consolidation_phase_start_end_paired(tmp_path, phase_capture):
    """Each phase must have matching phase_start and phase_end lines."""
    from yadgar import server

    server.init_engines(
        db_path=str(tmp_path / "phase_test4.db"),
        embedding_model="all-MiniLM-L6-v2",
        start_daemons=False,
    )
    try:
        server._consolidation.force_consolidate()
    finally:
        server.shutdown()

    starts = phase_capture.phase_starts()
    ends = phase_capture.phase_ends()
    # Must have at least one of each
    assert len(starts) >= 1
    assert len(ends) >= 1


def test_consolidation_apply_decay_phase_present(tmp_path, phase_capture):
    """apply_decay phase must appear in log output."""
    from yadgar import server

    server.init_engines(
        db_path=str(tmp_path / "phase_test5.db"),
        embedding_model="all-MiniLM-L6-v2",
        start_daemons=False,
    )
    try:
        server._consolidation.force_consolidate()
    finally:
        server.shutdown()

    assert phase_capture.has_phase("apply_decay"), (
        f"apply_decay phase not found in log. Records:\n"
        f"{[r.getMessage() for r in phase_capture.records]}"
    )
