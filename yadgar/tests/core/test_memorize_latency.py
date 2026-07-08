"""Roadmap: memorize latency budget.

server.memorize() must return quickly for the async enqueue path.
The enqueue path returns {stored, queued, queue_id} without doing any
embedding or DB I/O in the calling thread — only a disk write for the
queue file.

This test verifies the async path is taken (no sync fallback) and
measures overhead excluding the disk-write with a mocked file queue
to give a stable, environment-independent signal.
"""

import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    from unittest.mock import patch

    from yadgar.core import server

    tmp_path = tmp_path_factory.mktemp("latency")
    server.init_engines(
        db_path=str(tmp_path / "latency.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    # v5.42.3: /latency/project is not a git repo; patch _detect_branch.
    with patch("yadgar.core.server._detect_branch", return_value="feat/test-branch"):
        yield
    server.shutdown()


def test_memorize_takes_async_path():
    """memorize() must use the async enqueue path (not sync DB write)."""
    from yadgar.core import server

    result = server.memorize(
        "latency budget test unique payload alpha bravo charlie delta epsilon",
        "/latency/project",
        ["perf"],
    )
    assert result.get("queued") is True, (
        f"Expected queued=True (async path), got {result!r}. "
        "Sync fallback would mean every memorize blocks on DB I/O."
    )


def test_memorize_enqueue_under_5ms():
    """memorize() CPU-only path must complete in < 5 ms.

    Mocks all I/O (FileQueue.enqueue + rules engine DB queries) to measure
    pure control-path overhead: secret detection, unicode check, and the
    enqueue dispatch itself.

    NOTE: the current rules-engine DB queries add ~50 ms per call.
    That bottleneck is a known architectural issue (T-perf-rules-cache)
    tracked separately.  This test establishes the budget for the
    non-DB code path so regressions there are caught.
    """
    from yadgar.backend.queue_drainer import FileQueue
    from yadgar.core import server

    # Ensure file queue is initialized (warm up)
    server.memorize("warmup for latency test setup", "/latency/project", ["warmup"])

    mock_enqueue = MagicMock(return_value="00000000-0000-0000-0000-000000000001")
    # Return (blocked=False, reason="", modified=None) so the write path proceeds
    mock_write_policy = MagicMock(return_value=(False, "", None))

    # Measure: take median of 10 calls with all I/O mocked
    samples = []
    with (
        patch.object(FileQueue, "enqueue", mock_enqueue),
        patch.object(
            server._rules_engine.__class__,
            "check_write_policy",
            mock_write_policy,
        )
        if server._rules_engine is not None
        else patch("builtins.id", side_effect=lambda x: x),  # no-op context
    ):
        for i in range(10):
            t0 = time.perf_counter()
            server.memorize(
                f"latency budget sample {i} unique phi psi omega kappa tau rho",
                "/latency/project",
                ["perf"],
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            samples.append(elapsed_ms)

    median_ms = sorted(samples)[len(samples) // 2]
    assert median_ms < 5.0, (
        f"memorize() CPU-path median={median_ms:.2f} ms, budget is 5 ms. "
        f"Samples: {[f'{s:.1f}' for s in samples]}. "
        "Budget covers: secret detection + unicode check + enqueue dispatch. "
        "Separate bottleneck: rules-engine DB queries (T-perf-rules-cache)."
    )
