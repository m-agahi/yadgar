"""F5-A — concurrent-inference semaphore tests for embed_service.

TDD: these tests are written before the semaphore implementation.
They verify that:
  - concurrent /rerank calls are bounded by a per-mode semaphore
  - a held semaphore causes the endpoint to return 503 quickly
  - normal calls proceed when semaphore is free
  - env var YADGAR_RERANK_MAX_CONCURRENCY controls the slot count
  - env var YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC controls wait time

Tests use FastAPI TestClient + monkeypatching (no live model loading).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(monkeypatch, max_concurrency: int = 1, acquire_timeout: float = 2.0):
    """Return a fresh FastAPI TestClient with embed_service patched to avoid model load."""
    import importlib

    import yadgar.config as cfg

    monkeypatch.setenv("YADGAR_RERANK_MAX_CONCURRENCY", str(max_concurrency))
    monkeypatch.setenv("YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC", str(acquire_timeout))
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
    cfg.get_settings.cache_clear()

    # Reload embed_service so module-level semaphores pick up new env vars
    import yadgar.embed_service as es

    importlib.reload(es)

    from fastapi.testclient import TestClient

    return TestClient(es.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. Normal call succeeds when semaphore is free
# ---------------------------------------------------------------------------


class TestSemaphoreNormalCall:
    def test_rerank_succeeds_when_semaphore_free(self, monkeypatch):
        """POST /rerank returns 200 when semaphore has capacity and reranker works."""
        import importlib

        import yadgar.config as cfg
        import yadgar.embed_service as es

        monkeypatch.setenv("YADGAR_RERANK_MAX_CONCURRENCY", "1")
        monkeypatch.setenv("YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC", "2.0")
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
        cfg.get_settings.cache_clear()
        importlib.reload(es)

        mock_ml = MagicMock()
        mock_ml.score_cross_encoder.return_value = [0.9]

        from fastapi.testclient import TestClient

        with patch.object(es, "_get_reranker", return_value=mock_ml):
            client = TestClient(es.app, raise_server_exceptions=False)
            resp = client.post(
                "/rerank",
                json={"query": "test", "texts": ["doc"], "mode": "ce"},
                headers={"Authorization": "Bearer dummy"},
            )

        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        assert resp.json()["scores"] == [0.9]


# ---------------------------------------------------------------------------
# 2. Held semaphore causes 503 within acquire_timeout
# ---------------------------------------------------------------------------


class TestSemaphoreHeldReturns503:
    def test_held_semaphore_returns_503(self, monkeypatch):
        """When semaphore is already held, /rerank returns 503 without blocking."""
        import importlib
        import time

        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_RERANK_MAX_CONCURRENCY", "1")
        monkeypatch.setenv("YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC", "0.1")
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
        cfg.get_settings.cache_clear()

        import yadgar.embed_service as es

        importlib.reload(es)

        # Pre-acquire the ce semaphore so capacity is exhausted
        loop = asyncio.new_event_loop()
        try:
            # Grab the semaphore in a new event loop context
            sem = es._rerank_semaphores["ce"]
            # Manually drain all slots
            loop.run_until_complete(sem.acquire())

            from fastapi.testclient import TestClient

            mock_ml = MagicMock()
            mock_ml.score_cross_encoder.return_value = [0.9]

            with patch.object(es, "_get_reranker", return_value=mock_ml):
                client = TestClient(es.app, raise_server_exceptions=False)
                start = time.monotonic()
                resp = client.post(
                    "/rerank",
                    json={"query": "q", "texts": ["t"], "mode": "ce"},
                    headers={"Authorization": "Bearer dummy"},
                )
                elapsed = time.monotonic() - start
        finally:
            loop.close()

        assert resp.status_code == 503, f"expected 503 when semaphore held, got {resp.status_code}"
        # Should fail fast — well under 2s
        assert elapsed < 1.5, f"semaphore-held response took {elapsed:.2f}s (expected <1.5s)"


# ---------------------------------------------------------------------------
# 3. Per-mode isolation — ce semaphore held does not block nli/pair
# ---------------------------------------------------------------------------


class TestSemaphorePerModeIsolation:
    def test_ce_held_does_not_block_nli(self, monkeypatch):
        """Holding ce semaphore should not affect nli calls."""
        import importlib

        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_RERANK_MAX_CONCURRENCY", "1")
        monkeypatch.setenv("YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC", "0.1")
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
        cfg.get_settings.cache_clear()

        import yadgar.embed_service as es

        importlib.reload(es)

        loop = asyncio.new_event_loop()
        try:
            # Hold ce semaphore only
            loop.run_until_complete(es._rerank_semaphores["ce"].acquire())

            mock_ml = MagicMock()
            mock_ml.score_nli.return_value = [0.7]

            from fastapi.testclient import TestClient

            with patch.object(es, "_get_reranker", return_value=mock_ml):
                client = TestClient(es.app, raise_server_exceptions=False)
                resp = client.post(
                    "/rerank",
                    json={"query": "q", "texts": ["t"], "mode": "nli"},
                    headers={"Authorization": "Bearer dummy"},
                )
        finally:
            loop.close()

        assert resp.status_code == 200, (
            f"nli should succeed even when ce semaphore held; got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# 4. YADGAR_RERANK_MAX_CONCURRENCY=2 allows 2 concurrent slots
# ---------------------------------------------------------------------------


class TestSemaphoreConcurrencyN:
    def test_concurrency_2_allows_two_slots(self, monkeypatch):
        """With N=2, holding 1 slot still allows a second call through."""
        import importlib

        import yadgar.config as cfg

        monkeypatch.setenv("YADGAR_RERANK_MAX_CONCURRENCY", "2")
        monkeypatch.setenv("YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC", "0.1")
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
        cfg.get_settings.cache_clear()

        import yadgar.embed_service as es

        importlib.reload(es)

        loop = asyncio.new_event_loop()
        try:
            # Acquire 1 of 2 slots — one should remain free
            loop.run_until_complete(es._rerank_semaphores["ce"].acquire())

            mock_ml = MagicMock()
            mock_ml.score_cross_encoder.return_value = [0.5]

            from fastapi.testclient import TestClient

            with patch.object(es, "_get_reranker", return_value=mock_ml):
                client = TestClient(es.app, raise_server_exceptions=False)
                resp = client.post(
                    "/rerank",
                    json={"query": "q", "texts": ["t"], "mode": "ce"},
                    headers={"Authorization": "Bearer dummy"},
                )
        finally:
            loop.close()

        assert resp.status_code == 200, (
            f"second slot should be free with N=2; got {resp.status_code}: {resp.text}"
        )
