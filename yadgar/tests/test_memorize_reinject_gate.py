"""P7 reinject-gate tests — TDD: written before implementation.

Tests that YADGAR_REINJECT_ON_WRITE (default OFF) gates the reinjection
recall block in memorize.py.

Note: memorize() returns early via the async queue path when is_draining()
is False.  To exercise the sync path (which contains the reinjection block),
we patch is_draining to return True, bypassing the early return.

Note: memorize.py captures ``settings = get_settings()`` at module import time.
Tests that need a specific REINJECT_ON_WRITE value must patch
``yadgar.server.tools.memorize.settings`` directly.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Shared fixture data
# ---------------------------------------------------------------------------

_FAKE_MEMORY = {
    "id": "memory:test000",
    "content": "hello world",
    "heat": 0.5,
    "tags": [],
    "context": "test",
    "branch": None,
}


def _make_settings_stub(reinject_on_write: bool) -> MagicMock:
    """Return a Settings-like stub with REINJECT_ON_WRITE set."""
    stub = MagicMock()
    stub.REINJECT_ON_WRITE = reinject_on_write
    stub.REINJECTION_ENABLED = True
    stub.REINJECTION_MAX_RESULTS = 3
    stub.CONTEXTUAL_PREFIX_ENABLED = False
    stub.MICRO_CHECKPOINT_ENABLED = False
    stub.ACTION_STREAM_ENABLED = False
    stub.CRDT_AGENT_ID = "test-agent"
    stub.DECISION_AUTO_PROTECT = False
    return stub


def _run_memorize_with_settings(settings_stub: MagicMock, retriever_mock: MagicMock) -> float:
    """Run memorize() with fully-stubbed sync path; return wall-clock seconds."""
    with (
        patch("yadgar.server.tools.memorize.is_draining", return_value=True),
        patch("yadgar.server.tools.memorize.gate_or_reject", return_value=None),
        patch("yadgar.server.tools.memorize.settings", settings_stub),
        patch("yadgar.server.tools.memorize._st") as mock_st,
        patch("yadgar.server.tools.memorize._get_storage") as mock_get_storage,
        patch("yadgar.server.tools.memorize._get_embeddings") as mock_get_emb,
        patch("yadgar.server.tools.memorize._get_buffer"),
        patch("yadgar.server.tools.memorize._get_file_queue"),
    ):
        mock_st._retriever = retriever_mock
        mock_st._replay = None
        mock_st._buffer = None
        mock_st._write_gate = None
        mock_st._rules_engine = None
        mock_st._thermo = None
        mock_st._curator = None
        mock_st._pool = None
        mock_st._consolidation = None
        mock_st._prospective = None
        mock_st._engram = None

        mock_storage = MagicMock()
        mock_storage.insert_memory.return_value = "memory:test000"
        mock_storage.get_memory.return_value = _FAKE_MEMORY
        mock_get_storage.return_value = mock_storage

        mock_emb = MagicMock()
        mock_emb.encode.return_value = [0.1] * 384
        mock_get_emb.return_value = mock_emb

        from yadgar.server.tools.memorize import memorize

        start = time.perf_counter()
        memorize("hello world", context="test", tags=[])
        return time.perf_counter() - start


# ---------------------------------------------------------------------------
# 1. Flag OFF → retriever.recall NOT called
# ---------------------------------------------------------------------------


class TestReinjectOffSkipsRecall:
    def test_reinject_off_skips_recall(self):
        """REINJECT_ON_WRITE=False → _st._retriever.recall never called."""
        mock_retriever = MagicMock()
        mock_retriever.recall = MagicMock(return_value=[])

        _run_memorize_with_settings(_make_settings_stub(False), mock_retriever)

        mock_retriever.recall.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Flag ON → retriever.recall called once
# ---------------------------------------------------------------------------


class TestReinjectOnCallsRecall:
    def test_reinject_on_calls_recall(self):
        """REINJECT_ON_WRITE=True → _st._retriever.recall called exactly once."""
        mock_retriever = MagicMock()
        mock_retriever.recall = MagicMock(return_value=[])

        _run_memorize_with_settings(_make_settings_stub(True), mock_retriever)

        mock_retriever.recall.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Default Settings.REINJECT_ON_WRITE is False
# ---------------------------------------------------------------------------


class TestReinjectDefaultIsOff:
    def test_reinject_default_is_off(self, monkeypatch):
        """Import Settings with no YADGAR_REINJECT_ON_WRITE env → field is False."""
        import yadgar.config as cfg

        monkeypatch.delenv("YADGAR_REINJECT_ON_WRITE", raising=False)
        cfg.get_settings.cache_clear()

        settings = cfg.Settings()
        assert settings.REINJECT_ON_WRITE is False, (
            "REINJECT_ON_WRITE must default to False (v5.4 P7 contract)"
        )


# ---------------------------------------------------------------------------
# 4. Flag OFF path completes faster than flag ON path (50ms mocked sleep)
# ---------------------------------------------------------------------------


class TestReinjectLatencyDrop:
    def test_reinject_latency_drop(self):
        """OFF path completes faster than ON path (ON mocked with 50ms sleep)."""

        def slow_recall(*args, **kwargs):
            time.sleep(0.05)
            return []

        mock_retriever_on = MagicMock()
        mock_retriever_on.recall = MagicMock(side_effect=slow_recall)

        mock_retriever_off = MagicMock()
        mock_retriever_off.recall = MagicMock(return_value=[])

        t_off = _run_memorize_with_settings(_make_settings_stub(False), mock_retriever_off)
        t_on = _run_memorize_with_settings(_make_settings_stub(True), mock_retriever_on)

        assert t_off < t_on, (
            f"OFF path ({t_off * 1000:.1f}ms) should be faster than ON path ({t_on * 1000:.1f}ms)"
        )
