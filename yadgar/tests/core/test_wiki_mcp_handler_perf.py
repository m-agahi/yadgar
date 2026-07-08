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

CONCRETE BASELINES:
  v5.41.2 (pre-fix): wait=False p50 ≈ 28.89ms (task header) / ~48ms (xfail).
  v5.41.5 (post-fix): wait=False p50 < 1ms. Similarity gate moved to drainer.
  Root cause: find_similar_wiki_pages (embed+KNN) ran on request thread.
  Fix: gate deferred to drainer pre-apply stage. Handler now ~0.04ms total.

v5.41.5: xfail REMOVED — test now passes GREEN. Budget ≤5ms met.

Drainer strategy:
  - Real tmp_path for the queue dir (Path.write_text cost is in I9 scope).
  - Drainer is NOT started — _queue_drainer stays None so the handler cannot
    spawn processing on the request thread.
  - Explicit assert_not_called() on QueueDrainer._apply_with_stage_metrics
    confirms drainer did not run on the request thread during measurement.
  - Similarity gate: gate no longer on request thread (v5.41.5). UUID-suffix
    titles still used so test is self-documenting.
"""

from __future__ import annotations

import statistics
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from yadgar.backend.queue_drainer import FileQueue
from yadgar.core import server

_TEST_DIR = "/home/max/git/yadgar"

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
        patch("yadgar._shared.runtime.state._file_queue", real_fq),
        patch("yadgar._shared.runtime.state._queue_drainer", None),
        patch("yadgar._shared.runtime.lifecycle._st") as _lifecycle_st,
    ):
        import yadgar._shared.runtime.state as _state_mod

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


def test_wiki_add_handler_p50_within_i9_budget(tmp_path):
    """wiki_add(wait=False) handler must return in ≤5ms p50 (I9 budget).

    v5.41.5: xfail removed. Similarity gate moved to drainer — handler now
    ~0.04ms p50 (secret-gate + slug-gen + enqueue). Budget ≤5ms met.

    Measures the HANDLER path: secret-gate + rules check + slug-gen + enqueue.
    Similarity gate NOT on handler path anymore (moved to drainer, I9 fix).
    File write to tmp_path queue dir IS included (it is in I9 scope).
    Drainer is NOT started — storage write NOT included (not I9).

    BASELINE v5.41.5: p50 < 1ms. PASSES GREEN.

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
        import yadgar._shared.runtime.state as _state_mod
        import yadgar.core.lifecycle as _cl

        # Patch lifecycle so _get_file_queue() returns our real queue
        # without spawning a real drainer thread.

        def _patched_get_fq():
            return real_fq

        with (
            patch.object(_cl, "_get_file_queue", _patched_get_fq),
            patch.object(_state_mod, "_queue_drainer", None),
            patch(
                "yadgar.backend.queue_drainer.QueueDrainer._apply_with_stage_metrics",
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
                    branch_hint="feat/test-branch",
                    directory=_TEST_DIR,
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
                    branch_hint="feat/test-branch",
                    directory=_TEST_DIR,
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
        f"[BASELINE v5.41.5: <1ms — gate moved to drainer] "
        f"p90={p90:.2f}ms min={p_min:.2f}ms max={p_max:.2f}ms "
        f"n=100 calls. "
        f"I9 governs the handler only (secret-gate + slug-gen + enqueue). "
        f"Similarity gate now in drainer — not on request thread. "
        f"Storage-layer latency is a separate concern — see "
        f"test_wiki_versioning_atomicity.py::TestStorageUpdatePerfRegressionGuard."
    )
