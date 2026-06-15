"""backend v5.5.0 — model preload warm-up tests.

TDD: these tests verify _run_model_warmup() behaviour:
  - FLAG OFF  → returns immediately, no model loads
  - FLAG ON   → loads ce, then nli, then pair IN ORDER (sequential)
  - ONE MODEL RAISES → the other two still load (resilient)
  - CANCELLATION → CancelledError propagates cleanly
  - LIFESPAN → warm-up task is created alongside snap_task, not awaited before yield
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cleared_settings(monkeypatch, **env_overrides):
    """Set env vars + cache_clear so get_settings() returns fresh values."""
    import yadgar.config as cfg

    for key, val in env_overrides.items():
        monkeypatch.setenv(key, str(val))
    cfg.get_settings.cache_clear()


def _make_mock_reranker():
    """Return a MagicMock reranker with score_cross_encoder, score_nli, score_pair."""
    mock = MagicMock()
    mock.score_cross_encoder.return_value = [0.5]
    mock.score_nli.return_value = [0.5]
    mock.score_pair.return_value = 0.5
    return mock


# ---------------------------------------------------------------------------
# Tests: _run_model_warmup coroutine
# ---------------------------------------------------------------------------


class TestRunModelWarmup:
    """Tests for the _run_model_warmup() coroutine in isolation."""

    def test_flag_off_returns_without_loading(self, monkeypatch):
        """When MODEL_PRELOAD=false, coroutine returns immediately, no model load."""
        _cleared_settings(monkeypatch, YADGAR_MODEL_PRELOAD="false")

        mock_reranker = _make_mock_reranker()

        import yadgar.backend.embed_service as svc

        with patch.object(svc, "_get_reranker", return_value=mock_reranker):
            asyncio.run(svc._run_model_warmup())

        mock_reranker.score_cross_encoder.assert_not_called()
        mock_reranker.score_nli.assert_not_called()
        mock_reranker.score_pair.assert_not_called()

    def test_flag_on_loads_all_three_in_order(self, monkeypatch):
        """When MODEL_PRELOAD=true, ce → nli → pair loaded sequentially."""
        _cleared_settings(
            monkeypatch,
            YADGAR_MODEL_PRELOAD="true",
            YADGAR_MODEL_PRELOAD_DELAY_SEC="0",
        )

        mock_reranker = _make_mock_reranker()
        call_order: list[str] = []

        def _ce(query, texts):
            call_order.append("ce")
            return [0.5]

        def _nli(query, texts):
            call_order.append("nli")
            return [0.5]

        def _pair(query, text):
            call_order.append("pair")
            return 0.5

        mock_reranker.score_cross_encoder.side_effect = _ce
        mock_reranker.score_nli.side_effect = _nli
        mock_reranker.score_pair.side_effect = _pair

        import yadgar.backend.embed_service as svc

        with patch.object(svc, "_get_reranker", return_value=mock_reranker):
            asyncio.run(svc._run_model_warmup())

        assert call_order == ["ce", "nli", "pair"], f"Expected ce→nli→pair, got {call_order}"

    def test_ce_raises_nli_and_pair_still_load(self, monkeypatch):
        """When ce raises, nli and pair still load (resilient per-model try/except)."""
        _cleared_settings(
            monkeypatch,
            YADGAR_MODEL_PRELOAD="true",
            YADGAR_MODEL_PRELOAD_DELAY_SEC="0",
        )

        mock_reranker = _make_mock_reranker()
        loaded: list[str] = []

        def _ce_fail(query, texts):
            raise RuntimeError("CE model load error")

        def _nli_ok(query, texts):
            loaded.append("nli")
            return [0.5]

        def _pair_ok(query, text):
            loaded.append("pair")
            return 0.5

        mock_reranker.score_cross_encoder.side_effect = _ce_fail
        mock_reranker.score_nli.side_effect = _nli_ok
        mock_reranker.score_pair.side_effect = _pair_ok

        import yadgar.backend.embed_service as svc

        with patch.object(svc, "_get_reranker", return_value=mock_reranker):
            # Must not raise
            asyncio.run(svc._run_model_warmup())

        assert "nli" in loaded, "nli should load even when ce fails"
        assert "pair" in loaded, "pair should load even when ce fails"

    def test_nli_raises_ce_and_pair_still_load(self, monkeypatch):
        """When nli raises, ce and pair still load."""
        _cleared_settings(
            monkeypatch,
            YADGAR_MODEL_PRELOAD="true",
            YADGAR_MODEL_PRELOAD_DELAY_SEC="0",
        )

        mock_reranker = _make_mock_reranker()
        loaded: list[str] = []

        def _ce_ok(query, texts):
            loaded.append("ce")
            return [0.5]

        def _nli_fail(query, texts):
            raise RuntimeError("NLI model load error")

        def _pair_ok(query, text):
            loaded.append("pair")
            return 0.5

        mock_reranker.score_cross_encoder.side_effect = _ce_ok
        mock_reranker.score_nli.side_effect = _nli_fail
        mock_reranker.score_pair.side_effect = _pair_ok

        import yadgar.backend.embed_service as svc

        with patch.object(svc, "_get_reranker", return_value=mock_reranker):
            asyncio.run(svc._run_model_warmup())

        assert "ce" in loaded
        assert "pair" in loaded

    def test_warmup_is_cancellation_safe(self, monkeypatch):
        """CancelledError during sleep propagates and exits cleanly."""
        _cleared_settings(
            monkeypatch,
            YADGAR_MODEL_PRELOAD="true",
            YADGAR_MODEL_PRELOAD_DELAY_SEC="9999",  # Long delay — will be cancelled
        )

        import yadgar.backend.embed_service as svc

        async def _run():
            task = asyncio.create_task(svc._run_model_warmup())
            # Give it a tick to enter the sleep
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_run())

    def test_warmup_uses_executor_for_blocking_calls(self, monkeypatch):
        """Score methods are run via run_in_executor (blocking calls off event loop)."""
        _cleared_settings(
            monkeypatch,
            YADGAR_MODEL_PRELOAD="true",
            YADGAR_MODEL_PRELOAD_DELAY_SEC="0",
        )

        mock_reranker = _make_mock_reranker()
        executor_calls: list = []

        import yadgar.backend.embed_service as svc

        async def _run():
            loop = asyncio.get_running_loop()

            async def _tracked_executor(executor, func, *args):
                executor_calls.append(func)
                return func(*args)

            with (
                patch.object(svc, "_get_reranker", return_value=mock_reranker),
                patch.object(loop, "run_in_executor", side_effect=_tracked_executor),
            ):
                await svc._run_model_warmup()

        asyncio.run(_run())

        # Three executor calls — one per model
        assert len(executor_calls) == 3, f"Expected 3 executor calls, got {len(executor_calls)}"


# ---------------------------------------------------------------------------
# Tests: lifespan wiring
# ---------------------------------------------------------------------------


class TestLifespanWarmupWiring:
    """Verify warmup task is created in lifespan, not awaited before yield."""

    def test_warmup_task_does_not_block_startup(self, monkeypatch):
        """Lifespan startup completes quickly even with long warmup delay."""
        import importlib

        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_MODEL_PRELOAD", "true")
        monkeypatch.setenv("YADGAR_MODEL_PRELOAD_DELAY_SEC", "9999")  # Would block if awaited
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
        cfg.get_settings.cache_clear()

        import yadgar.backend.embed_service as svc

        importlib.reload(svc)

        # Patch heavy operations so lifespan runs fast
        with (
            patch.object(svc, "_get_engine", return_value=MagicMock()),
            patch.object(svc, "_get_reranker", return_value=_make_mock_reranker()),
            patch("asyncio.to_thread", new_callable=AsyncMock),
            patch.object(svc._ce_cache, "load_snapshot"),
            patch.object(svc._embed_cache, "load_snapshot"),
        ):
            import time

            async def _run():
                async with svc.lifespan(svc.app):
                    pass  # startup + yield + shutdown

            t0 = time.monotonic()
            asyncio.run(_run())
            elapsed = time.monotonic() - t0

        # Should finish in well under 5s (warmup delay is 9999s but runs in background)
        assert elapsed < 5.0, f"Lifespan took {elapsed:.1f}s — warmup likely blocked startup"
