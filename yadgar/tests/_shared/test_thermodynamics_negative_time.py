"""Tests for thermodynamics negative-time edge cases (Q12, Q18, Q84).

§10 remaining: thermodynamics.py
- Q12: compute_decay with negative hours_elapsed must not amplify heat
- Q18: apply_session_coherence with future created_at (negative hours) must not crash
- Q84: compute_importance can now return exactly 1.0 via round(min(score, 1.0), 10)
"""

import math

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.thermodynamics import MemoryThermodynamics


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def thermo(settings, tmp_path):
    # MemoryThermodynamics needs storage + embeddings — use mocks
    class _FakeStorage:
        def get_memory(self, mid):
            return None

        def update_memory_metamemory(self, *a, **kw):
            pass

    class _FakeEmbeddings:
        def encode(self, text):
            return None

    return MemoryThermodynamics(_FakeStorage(), _FakeEmbeddings(), settings)


class TestComputeDecayNegativeTime:
    """Q12: negative hours_elapsed must not amplify heat."""

    def test_negative_hours_clamps_to_zero(self, thermo):
        """Negative elapsed time → no decay (identity, or at least no amplification)."""
        memory = {
            "heat": 0.5,
            "importance": 0.5,
            "emotional_valence": 0.0,
            "confidence": 1.0,
        }
        result = thermo.compute_decay(memory, hours_elapsed=-1.0)
        # With clamped hours_elapsed=0: factor^0 = 1.0 → heat unchanged
        assert result == pytest.approx(0.5, abs=1e-9)

    def test_large_negative_hours_does_not_blow_up(self, thermo):
        """Large negative hours must not produce heat > initial heat."""
        memory = {
            "heat": 0.8,
            "importance": 0.3,
            "emotional_valence": 0.0,
            "confidence": 1.0,
        }
        result = thermo.compute_decay(memory, hours_elapsed=-10000.0)
        assert result <= 0.8 + 1e-9  # cannot amplify
        assert math.isfinite(result)

    def test_zero_hours_no_decay(self, thermo):
        """Zero elapsed → heat unchanged (regression)."""
        """Zero elapsed → heat unchanged (regression: same as before)."""
        memory = {
            "heat": 0.6,
            "importance": 0.5,
            "emotional_valence": 0.0,
            "confidence": 1.0,
        }
        result = thermo.compute_decay(memory, hours_elapsed=0.0)
        assert result == pytest.approx(0.6, abs=1e-9)


class TestApplySessionCoherenceNegativeTime:
    """Q18: future created_at (negative hours) must not crash or return weird values."""

    def test_future_timestamp_no_crash(self, thermo):
        """Memory created in the future (clock skew) — must return heat unchanged."""
        from datetime import UTC, datetime, timedelta

        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        result = thermo.apply_session_coherence(0.5, future)
        # negative hours → clamped to 0 → freshness=1.0, bonus applied (capped at 1.0)
        # OR no change — either is acceptable; must not raise, must be finite
        assert math.isfinite(result)
        assert 0.0 <= result <= 1.0

    def test_far_future_no_crash(self, thermo):
        """Far future timestamp — must not propagate negative into power computation."""
        from datetime import UTC, datetime, timedelta

        far_future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
        result = thermo.apply_session_coherence(0.3, far_future)
        assert math.isfinite(result)
        assert 0.0 <= result <= 1.0


class TestComputeImportanceIEEE:
    """Q84: compute_importance must be able to return exactly 1.0."""

    def test_max_score_rounds_to_one(self, thermo):
        """All signals present → score = 1.0 after round(min(score, 1.0), 10)."""
        # Trigger all 6 signals:
        # error keywords (+0.2), decision keywords (+0.3), architecture keywords (+0.2),
        # 3+ tags (+0.1), content > 500 chars (+0.1), code block (+0.1) = 1.0
        content = (
            "Error: exception traceback — decided to refactor architecture design pattern. "
            "```python\ndef foo(): pass\n```\n/path/to/file.py\n" + "x" * 500
        )
        tags = ["a", "b", "c"]
        result = thermo.compute_importance(content, tags)
        assert result == pytest.approx(1.0, abs=1e-9)
        assert result <= 1.0  # never exceeds 1.0
