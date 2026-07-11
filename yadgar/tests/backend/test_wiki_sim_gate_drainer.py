"""v5.41.5 similarity gate drainer tests.

Tests the new drainer-side gate path introduced in v5.41.5 (I9 fix).
The gate moved from the MCP request thread to the drainer pre-apply stage.

Contract:
- wait=False: returns {queued:True, similarity_check:"deferred"} immediately.
  Gate fires async; caller cannot observe sync rejection on this path.
- wait=True: gate runs in drainer, rejection surfaces synchronously via the
  DLQ terminal-file poll (FileQueue.wait_for_job) in _wiki_add_wait_path. Same
  observable contract for callers as v5.39 wait=True (DP-B: sync rejection
  preserved for wait=True).
- force=True, replace_slug, append=True: bypass gate in drainer.

Tests use a real in-process drainer (drain_now() nudges the flush; the wait
path polls archive/dlq for the job's terminal state).
"""

from __future__ import annotations

import pytest

from yadgar.backend.queue_drainer import FileQueue, QueueDrainer
from yadgar.core import server

_TEST_DIR = "/home/max/git/yadgar"

# ---------------------------------------------------------------------------
# Content fixtures (same as test_wiki_similarity_gate.py)
# ---------------------------------------------------------------------------

_ROADMAP_CONTENT_A = """# Yadgar Roadmap: Future Improvements

## Short-term (next 2 months)
- Implement wiki versioning (v5.41) to track page history
- Add similarity gate to wiki_add to prevent duplicate pages
- Improve embedding model to mpnet for better semantic search

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation
- Automated anchor hygiene with consolidation pass

## Long-term (6+ months)
- LLM-based duplicate resolution and wiki curation
- Retroactive deduplication of existing pages
- Distributed SurrealDB for large-scale deployment
"""

_ROADMAP_CONTENT_B = """# Yadgar Future Roadmap

## Near-term (next 2 months)
- Wiki versioning (v5.41) — track page history and enable rollback
- Similarity gate in wiki_add — block near-duplicate page creation
- Better embedding model (mpnet) for semantic search quality

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation across workspaces
- Automated anchor hygiene during consolidation cycles

## Long-term (6+ months)
- LLM-based wiki curation and duplicate resolution
- Retroactive dedup of existing pages (v5.45+)
- Distributed SurrealDB for large deployments
"""

_ARCH_CONTENT = """# Yadgar Architecture

## Core components
StorageEngine: SurrealDB wrapper. Mixins: _WikiMixin, _VectorMixin, _MemoryMixin.
WikiStore: hybrid FTS + vector search over wiki_page table.
EmbeddingsService: sentence-transformers, all-MiniLM-L6-v2 default.
"""


# ---------------------------------------------------------------------------
# Fixture: isolated server + drainer
# ---------------------------------------------------------------------------


@pytest.fixture()
def _drainer_env(tmp_path):
    """Isolated server with real FileQueue and a live QueueDrainer."""
    from unittest.mock import patch

    server.init_engines(
        db_path=str(tmp_path / "sim_gate_drainer.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    real_fq = FileQueue(tmp_path)

    import yadgar._shared.runtime.state as _state_mod
    import yadgar.core.lifecycle.lifecycle as _cl

    drainer = QueueDrainer(
        queue=real_fq,
        storage_factory=lambda: _state_mod._storage,
        drain_interval=9999,  # won't self-fire; tests call drain_now()
    )

    def _get_fq():
        return real_fq

    # Patch _get_file_queue in all the places that hold a direct reference.
    # v5.42.3: also patch _detect_branch so wiki_add calls without branch_hint work.
    with (
        patch.object(_cl, "_get_file_queue", _get_fq),
        patch("yadgar.core.server.tools.wiki._get_file_queue", _get_fq),
        patch.object(_state_mod, "_queue_drainer", drainer),
        patch.object(_state_mod, "_file_queue", real_fq),
        patch("yadgar.core.server._detect_branch", return_value="feat/test-branch"),
    ):
        yield drainer, real_fq

    server.shutdown()


def _write_sync(title: str, content: str, **kwargs) -> dict:
    """Commit a wiki page synchronously so it is a gate candidate for later writes.

    R3 Car 1 (write-half): wiki_add no longer has an in-process sync path — it
    always enqueues. To seed a committed page, we enqueue with force=True (bypass
    the drainer gate so the seed itself never self-rejects) and then drive the
    live drainer's drain_now() so the page lands in the DB (with an embedding)
    before the tests' wait=True near-duplicate writes run their gate.
    """
    import yadgar._shared.runtime.state as _st

    kwargs.setdefault("force", True)
    kwargs.setdefault("branch_hint", "feat/test-branch")
    kwargs.setdefault("directory", _TEST_DIR)
    result = server.wiki_add(title=title, content=content, **kwargs)
    if _st._queue_drainer is not None:
        _st._queue_drainer.drain_now()
    return result


# ---------------------------------------------------------------------------
# Tests: wait=False deferred path
# ---------------------------------------------------------------------------


class TestWaitFalseDeferredPath:
    """wait=False returns deferred immediately; no sync rejection."""

    def test_wait_false_returns_immediately_with_deferred_check(self, _drainer_env):
        """Handler returns sub-5ms with similarity_check=deferred (I9 budget).

        Warmup: 5 calls to settle import-time costs before measuring.
        """
        import time
        import uuid

        drainer, fq = _drainer_env

        # Warmup: let import-time costs settle before measuring.
        for _ in range(5):
            server.wiki_add(
                title=f"Warmup {uuid.uuid4().hex}",
                content="warmup content",
                wait=False,
                branch_hint="feat/test-branch",
                directory=_TEST_DIR,
            )

        t0 = time.perf_counter()
        result = server.wiki_add(
            title=f"Deferred Path Test {uuid.uuid4().hex}",
            content="Content for testing the deferred similarity check path.",
            wait=False,
            branch_hint="feat/test-branch",
            directory=_TEST_DIR,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert result.get("queued") is True
        assert result.get("similarity_check") == "deferred"
        assert elapsed_ms < 5.0, (
            f"wait=False handler took {elapsed_ms:.2f}ms — exceeds I9 budget of 5ms"
        )

    def test_wait_false_no_sync_rejection_for_duplicate(self, _drainer_env):
        """wait=False does NOT return sync rejection even for near-duplicates."""
        drainer, fq = _drainer_env

        # Insert page A via sync path (is_draining=True).
        _write_sync("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        # Page B is a near-duplicate — but wait=False defers the gate.
        result = server.wiki_add(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            wait=False,
            branch_hint="feat/test-branch",
            directory=_TEST_DIR,
        )
        # Must NOT return sync rejection.
        assert result.get("reason") != "duplicate_detected", (
            f"wait=False returned sync rejection — should be deferred: {result}"
        )
        assert result.get("queued") is True
        assert result.get("similarity_check") == "deferred"


# ---------------------------------------------------------------------------
# Tests: wait=True sync rejection (DP-B)
# ---------------------------------------------------------------------------


class TestWaitTrueSyncRejection:
    """wait=True surfaces drainer gate rejection synchronously (DP-B)."""

    def test_wait_true_returns_rejection_synchronously(self, _drainer_env):
        """Near-clone gets rejected synchronously when wait=True."""
        drainer, fq = _drainer_env

        # Insert page A via sync path.
        _write_sync("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        # Page B via wait=True — gate fires in drainer.
        r2 = server.wiki_add(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            wait=True,
            branch_hint="feat/test-branch",
            directory=_TEST_DIR,
        )
        assert r2.get("stored") is False, (
            f"Gate should have blocked near-duplicate via wait=True. Got: {r2}"
        )
        assert r2.get("reason") == "duplicate_detected"
        assert "candidates" in r2
        assert len(r2["candidates"]) >= 1
        slugs = [c["slug"] for c in r2["candidates"]]
        assert "yadgar-roadmap-future-improvements" in slugs

    def test_wait_true_distinct_pages_pass_gate(self, _drainer_env):
        """Distinct pages are not rejected via wait=True."""
        drainer, fq = _drainer_env
        _write_sync("Yadgar Architecture", _ARCH_CONTENT)

        r2 = server.wiki_add(
            title="Yadgar Configuration Guide",
            content="""# Yadgar Configuration Guide

## Config file location
~/.yadgar/config.yaml overrides defaults, overridden by env vars.

## Priority order
1. Environment variables (YADGAR_*)
2. ~/.yadgar/config.yaml
3. Built-in defaults in config.py
""",
            wait=True,
            branch_hint="feat/test-branch",
            directory=_TEST_DIR,
        )
        assert r2.get("reason") != "duplicate_detected", (
            f"False positive: distinct page blocked by gate. Got: {r2}"
        )
        assert r2.get("committed") is True


# ---------------------------------------------------------------------------
# Tests: bypass conditions (DP-B: force/replace_slug/append bypass drainer gate)
# ---------------------------------------------------------------------------


class TestDrainerGateBypass:
    """force, replace_slug, append bypass gate in drainer (matching v5.39 bypass semantics)."""

    def test_force_bypasses_gate(self, _drainer_env):
        """force=True skips drainer gate; near-duplicate is written."""
        drainer, fq = _drainer_env
        _write_sync("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        r2 = server.wiki_add(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            force=True,
            wait=True,
            branch_hint="feat/test-branch",
            directory=_TEST_DIR,
        )
        assert r2.get("reason") != "duplicate_detected", (
            f"force=True should bypass drainer gate. Got: {r2}"
        )
        assert r2.get("committed") is True

    def test_replace_slug_bypasses_gate(self, _drainer_env):
        """replace_slug skips drainer gate (overwrite semantics)."""
        drainer, fq = _drainer_env
        _write_sync("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        r2 = server.wiki_add(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            replace_slug="yadgar-roadmap-future-improvements",
            wait=True,
            branch_hint="feat/test-branch",
            directory=_TEST_DIR,
        )
        assert r2.get("reason") != "duplicate_detected", (
            f"replace_slug should bypass drainer gate. Got: {r2}"
        )

    def test_append_bypasses_gate(self, _drainer_env):
        """append=True skips drainer gate (update semantics, not create)."""
        drainer, fq = _drainer_env
        _write_sync("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        r2 = server.wiki_add(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            append=True,
            wait=True,
            branch_hint="feat/test-branch",
            directory=_TEST_DIR,
        )
        assert r2.get("reason") != "duplicate_detected", (
            f"append=True should bypass drainer gate. Got: {r2}"
        )


# ---------------------------------------------------------------------------
# Tests: Prometheus rejection counter (I23)
# ---------------------------------------------------------------------------


class TestDrainerRejectionMetric:
    """yadgar_wiki_add_rejected_total counter increments on drainer gate rejection."""

    def test_drainer_emits_rejection_metric(self, _drainer_env):
        """Gate rejection in drainer increments yadgar_wiki_add_rejected_total."""

        drainer, fq = _drainer_env
        _write_sync("Yadgar Roadmap Future Improvements", _ROADMAP_CONTENT_A)

        # Get before count
        def _get_count() -> float:
            try:
                from yadgar._shared.observability.metrics import yadgar_wiki_add_rejected_total

                return yadgar_wiki_add_rejected_total.labels(
                    reason="duplicate_detected"
                )._value.get()
            except Exception:
                return 0.0

        before = _get_count()

        # Trigger rejection via wait=True.
        r2 = server.wiki_add(
            title="Yadgar Future Roadmap",
            content=_ROADMAP_CONTENT_B,
            wait=True,
            branch_hint="feat/test-branch",
            directory=_TEST_DIR,
        )
        assert r2.get("reason") == "duplicate_detected"

        after = _get_count()
        assert after > before, (
            f"yadgar_wiki_add_rejected_total{'{reason=duplicate_detected}'} "
            f"did not increment: before={before}, after={after}"
        )
