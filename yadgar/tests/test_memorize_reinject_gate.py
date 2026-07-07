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
    """Run memorize() with fully-stubbed sync path; return wall-clock seconds.

    After v5.49.5 refactor, phase functions live in _memorize_phases subpackage.
    Patch lifecycle functions where the phases import them, and patch _state directly.
    """
    import importlib

    import yadgar._shared.runtime.lifecycle as _lc
    import yadgar._shared.runtime.state as _state_mod
    import yadgar.core.file_queue as _fq

    _mem_mod = importlib.import_module("yadgar.core.server.tools.memorize")
    _validate_mod = importlib.import_module(
        "yadgar.core.server.tools._memorize_phases._phase_validate"
    )

    mock_storage = MagicMock()
    mock_storage.insert_memory.return_value = "memory:test000"
    mock_storage.get_memory.return_value = _FAKE_MEMORY

    mock_emb = MagicMock()
    mock_emb.encode.return_value = [0.1] * 384
    mock_emb.get_model_name.return_value = "test-model"

    mock_buffer = MagicMock()

    with (
        patch.object(_fq, "is_draining", return_value=True),
        patch.object(_validate_mod, "gate_or_reject", return_value=None),
        patch.object(_mem_mod, "settings", settings_stub),
        # patch lifecycle getters at the lifecycle module (phases use _lifecycle.getter())
        patch.object(_lc, "_get_embeddings", return_value=mock_emb),
        patch.object(_lc, "_get_storage", return_value=mock_storage),
        patch.object(_lc, "_get_buffer", return_value=mock_buffer),
        # patch _state attributes via the actual state module
        patch.object(_state_mod, "_rules_engine", None),
        patch.object(_state_mod, "_write_gate", None),
        patch.object(_state_mod, "_thermo", None),
        patch.object(_state_mod, "_curator", None),
        patch.object(_state_mod, "_pool", None),
        patch.object(_state_mod, "_consolidation", None),
        patch.object(_state_mod, "_prospective", None),
        patch.object(_state_mod, "_engram", None),
        patch.object(_state_mod, "_replay", None),
        patch.object(_state_mod, "_retriever", retriever_mock),
    ):
        # also patch settings in post_write path
        with patch("yadgar.core.server.tools._memorize_phases._phase_post_write._push_event"):
            from yadgar.core.server.tools.memorize import memorize

            start = time.perf_counter()
            memorize("hello world", context="test", tags=[], branch_hint="test-branch")
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
        import yadgar._shared.config as cfg

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
