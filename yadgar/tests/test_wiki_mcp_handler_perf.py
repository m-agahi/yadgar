"""v5.41.3 — MCP-handler perf test for wiki_add I9 budget.

I9 invariant: write-path MCP handler MUST complete in ≤5ms p50. This measures
the handler's own time-to-return (file enqueue path only — NOT storage layer).

Layer model:
  MCP handler   wiki_add(wait=False)  ← THIS TEST — I9 budget ≤5ms p50
  File queue    Path.write_text(json) — sub-ms expected; IS in I9 scope
  Queue worker  QueueDrainer._apply() — NOT I9; heavy work allowed here (I2/I4)
  Storage layer update_wiki_page()    — NOT I9; ~89ms on embedded SurrealKV
                                        See test_wiki_versioning_atomicity.py
                                        ::TestStorageUpdatePerfRegressionGuard

CONCRETE BASELINE (measured v5.41.2, 2026-06-02): wait=False p50 ≈ 48ms.
9.6× over I9 budget. Root cause investigation slot: v5.41.5.

This test is marked xfail(strict=True) — it MUST fail on the v5.41.2 baseline
(p50 ≈ 48ms >> 5ms). When v5.41.5 lands the fix, the test will start passing
and strict=True will turn the xpass into a GREEN signal, removing the marker.

Drainer strategy:
  - Real tmp_path for the queue dir (Path.write_text cost is in I9 scope).
  - Drainer is NOT started — _queue_drainer stays None so the handler cannot
    spawn processing on the request thread.
  - Explicit assert_not_called() on QueueDrainer._apply_with_stage_metrics
    confirms drainer did not run on the request thread during measurement.
  - Similarity gate: each call gets a UUID-suffixed title to avoid duplicate
    detection short-circuiting the measured path.
"""

from __future__ import annotations

import statistics
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from yadgar import server
from yadgar.file_queue import FileQueue

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def _handler_env(tmp_path):
    """Isolated server with real file queue dir but NO running drainer.

    The FileQueue writes real .json files to tmp_path/queue — that file-write
    cost is part of the I9-budgeted handler path and must NOT be mocked.

    _queue_drainer is patched to None so the handler cannot call drain_now()
    or _apply_with_stage_metrics() on the request thread.
    """
    server.init_engines(
        db_path=str(tmp_path / "mcp_handler_perf.db"),
        embedding_model="all-MiniLM-L6-v2",
    )

    # Build a real FileQueue rooted in tmp_path so queue writes land on disk.
    real_fq = FileQueue(tmp_path)

    # Patch server's file queue and drainer: real queue, no drainer.
    with (
        patch("yadgar.server._state._file_queue", real_fq),
        patch("yadgar.server._state._queue_drainer", None),
        patch("yadgar.server.lifecycle._st") as _lifecycle_st,
    ):
        import yadgar.server._state as _state_mod

        _lifecycle_st._file_queue = real_fq
        _lifecycle_st._queue_drainer = None
        _lifecycle_st._wiki = _state_mod._wiki
        _lifecycle_st._rules_engine = _state_mod._rules_engine
        _lifecycle_st._queue_lock = _state_mod._queue_lock

        yield real_fq

    server.shutdown()


# ── Helper ────────────────────────────────────────────────────────────────────


def _unique_title(base: str = "MCP Perf Test") -> str:
    """UUID-suffix to bypass the similarity gate every call."""
    return f"{base} {uuid.uuid4().hex}"


# ── Test ──────────────────────────────────────────────────────────────────────


@pytest.mark.xfail(
    reason=(
        "I9 violation: wiki_add(wait=False) MCP handler p50 ≈ 48ms "
        "(9.6× over ≤5ms I9 budget). Fix slot: v5.41.5. "
        "BASELINE 2026-06-02: ~48ms p50 on the same machine."
    ),
    strict=True,
)
def test_wiki_add_handler_p50_within_i9_budget(tmp_path):
    """wiki_add(wait=False) handler must return in ≤5ms p50 (I9 budget).

    Measures the HANDLER path: secret-gate + rules check + sim-gate + enqueue.
    File write to tmp_path queue dir IS included (it is in I9 scope).
    Drainer is NOT started — storage write NOT included (not I9).

    EXPECTED TO FAIL on v5.41.2 baseline: p50 ≈ 48ms >> 5ms budget.
    xfail(strict=True): suite stays green; xpass after v5.41.5 fix removes marker.

    Drainer assertion: QueueDrainer._apply_with_stage_metrics must NOT be called
    on the request thread during measurement (confirms handler returns before
    drainer processes the job).
    """
    # Boot a server with a real file queue but no drainer.
    server.init_engines(
        db_path=str(tmp_path / "i9_perf.db"),
        embedding_model="all-MiniLM-L6-v2",
    )

    real_fq = FileQueue(tmp_path)

    drainer_apply_mock = MagicMock()

    try:
        import yadgar.server._state as _state_mod
        import yadgar.server.lifecycle as _lifecycle_mod

        # Patch lifecycle so _get_file_queue() returns our real queue
        # without spawning a real drainer thread.

        def _patched_get_fq():
            return real_fq

        with (
            patch.object(_lifecycle_mod, "_get_file_queue", _patched_get_fq),
            patch.object(_state_mod, "_queue_drainer", None),
            patch(
                "yadgar.file_queue.QueueDrainer._apply_with_stage_metrics",
                drainer_apply_mock,
            ),
        ):
            # Warm up: let import-time costs settle.
            for _ in range(5):
                server.wiki_add(
                    title=_unique_title("Warmup"),
                    content="warmup content",
                    tags=["perf-warmup"],
                    wait=False,
                )

            # Measure 100 calls.
            latencies_ms: list[float] = []
            for _ in range(100):
                t0 = time.perf_counter()
                result = server.wiki_add(
                    title=_unique_title("I9 Perf"),
                    content="MCP handler I9 perf measurement content",
                    tags=["perf"],
                    wait=False,
                )
                latencies_ms.append((time.perf_counter() - t0) * 1000)

                # Each call must have enqueued (not fallen back to sync write)
                # for the measurement to be valid.
                assert result.get("queued") is True, (
                    f"Call fell through to sync write path — measurement invalid. result={result}"
                )

            # Drainer must NOT have been called on the request thread.
            drainer_apply_mock.assert_not_called()

    finally:
        server.shutdown()

    p50 = statistics.median(latencies_ms)
    p90 = sorted(latencies_ms)[89]
    p_min = min(latencies_ms)
    p_max = max(latencies_ms)

    # Surface baseline in the failure message for v5.41.5 "before" reference.
    assert p50 <= 5.0, (
        f"wiki_add(wait=False) MCP handler p50={p50:.2f}ms exceeds I9 budget of 5ms. "
        f"[BASELINE v5.41.2: ~48ms] "
        f"p90={p90:.2f}ms min={p_min:.2f}ms max={p_max:.2f}ms "
        f"n=100 calls. "
        f"I9 governs the handler only (enqueue + gate checks). "
        f"Storage-layer latency is a separate concern — see "
        f"test_wiki_versioning_atomicity.py::TestStorageUpdatePerfRegressionGuard."
    )
