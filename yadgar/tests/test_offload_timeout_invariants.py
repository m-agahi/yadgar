"""Offload timeout cascade invariants (#74 salvage — fix #3).

A `wait_for` cancellation that fires mid-rerank cannot cancel the uncancellable
worker — it leaks the pool slot until self-release. To keep that residual bounded
and never discard legitimately-completing work, the timeouts must form a coherent
chain:

    TOOL_SATURATION_GRACE_SEC > TOOL_TIMEOUT_SEC >= RERANK_BACKEND_TIMEOUT_SEC + headroom

CE inference is documented at 8-46s (RERANK_BACKEND_TIMEOUT_SEC provisioned to 90).
So TOOL_TIMEOUT_SEC must cover a realistic worst-case recall (incl. the rerank),
not cut it off at 30s. And TOOL_SATURATION_GRACE_SEC must stay above TOOL_TIMEOUT_SEC
(the existing O2 invariant) so a legit op that completes within the tool timeout
keeps resetting the saturation clock and only a genuinely leaked worker trips it.

These tests assert the default config encodes that ordering. OTEL untouched.
"""

from __future__ import annotations

import pytest

import yadgar.config as cfg


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        "YADGAR_TOOL_TIMEOUT_SEC",
        "YADGAR_TOOL_SATURATION_GRACE_SEC",
        "YADGAR_RERANK_BACKEND_TIMEOUT_SEC",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg.get_settings.cache_clear()
    yield
    cfg.get_settings.cache_clear()


def test_tool_timeout_covers_worst_case_rerank():
    """TOOL_TIMEOUT_SEC must be >= RERANK_BACKEND_TIMEOUT_SEC so wait_for does not
    cut off a legitimately-completing rerank and discard the work."""
    s = cfg.get_settings()
    assert s.TOOL_TIMEOUT_SEC >= s.RERANK_BACKEND_TIMEOUT_SEC, (
        f"TOOL_TIMEOUT_SEC ({s.TOOL_TIMEOUT_SEC}) must cover the backend rerank "
        f"timeout ({s.RERANK_BACKEND_TIMEOUT_SEC}); a smaller value cancels the "
        "coroutine mid-rerank and leaks an uncancellable worker"
    )


def test_saturation_grace_above_tool_timeout():
    """O2 invariant: grace > tool timeout so only leaked workers trip saturation."""
    s = cfg.get_settings()
    assert s.TOOL_SATURATION_GRACE_SEC > s.TOOL_TIMEOUT_SEC, (
        f"TOOL_SATURATION_GRACE_SEC ({s.TOOL_SATURATION_GRACE_SEC}) must exceed "
        f"TOOL_TIMEOUT_SEC ({s.TOOL_TIMEOUT_SEC}) — a legit op resets the clock, "
        "only a wedged worker trips /health saturation"
    )


def test_heavy_concurrency_below_pool_and_rerank_max():
    """The heavy fan-out must not exceed pool workers nor the backend rerank max."""
    s = cfg.get_settings()
    assert s.RECALL_HEAVY_CONCURRENCY <= s.TOOL_POOL_WORKERS
    assert s.RECALL_HEAVY_CONCURRENCY <= s.RERANK_MAX_CONCURRENCY
    assert s.RECALL_HEAVY_CONCURRENCY < s.TOOL_POOL_WORKERS, (
        "heavy gate must be strictly below pool size or it is a no-op (#74)"
    )
