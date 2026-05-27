"""Tests for MLClient protocol, RemoteMLClient, and LocalMLClient.

TDD: tests written before implementation.
"""

from __future__ import annotations

import sys
from unittest.mock import ANY, MagicMock, patch

# ── RemoteMLClient tests ─────────────────────────────────────────────


class TestRemoteMLClientScoreCE:
    def test_remote_score_ce_calls_backend(self):
        """POST /rerank with mode=ce; returns scores list."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"scores": [0.9, 0.3], "mode": "ce"}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            from yadgar.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")
            scores = client.score_cross_encoder(
                "who is alice", ["Alice works here", "Bob works here"]
            )

        mock_client.post.assert_called_once_with(
            "/rerank",
            json={
                "query": "who is alice",
                "texts": ["Alice works here", "Bob works here"],
                "mode": "ce",
            },
            timeout=ANY,
        )
        assert scores == [0.9, 0.3]


class TestRemoteMLClientScoreNLI:
    def test_remote_score_nli_calls_backend(self):
        """POST /rerank with mode=nli; returns scores list."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"scores": [0.7, 0.1, 0.4], "mode": "nli"}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            from yadgar.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")
            scores = client.score_nli(
                "alice works here", ["Alice works here", "Bob works here", "Charlie"]
            )

        mock_client.post.assert_called_once_with(
            "/rerank",
            json={
                "query": "alice works here",
                "texts": ["Alice works here", "Bob works here", "Charlie"],
                "mode": "nli",
            },
            timeout=ANY,
        )
        assert scores == [0.7, 0.1, 0.4]


class TestRemoteMLClientScorePair:
    def test_remote_score_pair_calls_backend(self):
        """POST /rerank with mode=pair; passes single text; returns first score."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"scores": [0.85], "mode": "pair"}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            from yadgar.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")
            score = client.score_pair("who is alice", "Alice works here")

        mock_client.post.assert_called_once_with(
            "/rerank",
            json={"query": "who is alice", "texts": ["Alice works here"], "mode": "pair"},
            timeout=ANY,
        )
        assert score == 0.85


class TestRemoteMLClientUnload:
    def test_remote_unload_is_noop(self):
        """unload_if_idle on RemoteMLClient must not call HTTP and must not raise."""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            from yadgar.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")
            client.unload_if_idle(idle_seconds=0.0)

        # No HTTP calls whatsoever
        mock_client.post.assert_not_called()
        mock_client.get.assert_not_called()


class TestRemoteMLClientHTTPError:
    def test_remote_score_ce_http_error_returns_none(self):
        """HTTPStatusError degrades gracefully — returns None, doesn't raise (N4: circuit breaker)."""
        import httpx

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            # Simulate a server error response
            mock_response = MagicMock()
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock()
            )
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            from yadgar.ml_client import RemoteMLClient

            client = RemoteMLClient("http://localhost:8001")
            scores = client.score_cross_encoder("query", ["text1", "text2"])

        assert scores is None


# ── LocalMLClient tests ──────────────────────────────────────────────


class TestRerankEndpointCE:
    def test_rerank_endpoint_ce(self):
        """POST /rerank with mode=ce returns scores via LocalMLClient.score_cross_encoder."""
        from unittest.mock import patch

        from starlette.testclient import TestClient

        from yadgar.embed_service import app

        with patch("yadgar.embed_service._get_reranker") as mock_get_reranker:
            mock_ml = MagicMock()
            mock_ml.score_cross_encoder.return_value = [0.8, 0.2]
            mock_get_reranker.return_value = mock_ml

            client = TestClient(app)
            resp = client.post(
                "/rerank",
                json={
                    "query": "who is alice",
                    "texts": ["Alice works here", "Bob works here"],
                    "mode": "ce",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["scores"] == [0.8, 0.2]
        assert data["mode"] == "ce"
        mock_ml.score_cross_encoder.assert_called_once_with(
            "who is alice", ["Alice works here", "Bob works here"]
        )


class TestRerankEndpointNLI:
    def test_rerank_endpoint_nli(self):
        """POST /rerank with mode=nli routes to score_nli."""
        from starlette.testclient import TestClient

        from yadgar.embed_service import app

        with patch("yadgar.embed_service._get_reranker") as mock_get_reranker:
            mock_ml = MagicMock()
            mock_ml.score_nli.return_value = [0.9, 0.1]
            mock_get_reranker.return_value = mock_ml

            client = TestClient(app)
            resp = client.post(
                "/rerank",
                json={
                    "query": "alice entails",
                    "texts": ["Alice works", "Bob works"],
                    "mode": "nli",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["scores"] == [0.9, 0.1]
        assert data["mode"] == "nli"
        mock_ml.score_nli.assert_called_once_with("alice entails", ["Alice works", "Bob works"])


class TestRerankEndpointPair:
    def test_rerank_endpoint_pair(self):
        """POST /rerank with mode=pair routes to score_pair."""
        from starlette.testclient import TestClient

        from yadgar.embed_service import app

        with patch("yadgar.embed_service._get_reranker") as mock_get_reranker:
            mock_ml = MagicMock()
            mock_ml.score_pair.return_value = 0.77
            mock_get_reranker.return_value = mock_ml

            client = TestClient(app)
            resp = client.post(
                "/rerank",
                json={"query": "who is alice", "texts": ["Alice works here"], "mode": "pair"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["scores"] == [0.77]
        assert data["mode"] == "pair"
        mock_ml.score_pair.assert_called_once_with("who is alice", "Alice works here")


class TestRerankEndpointInvalidMode:
    def test_rerank_endpoint_invalid_mode_returns_422(self):
        """POST /rerank with unknown mode returns 422 validation error."""
        from starlette.testclient import TestClient

        from yadgar.embed_service import app

        client = TestClient(app)
        resp = client.post(
            "/rerank",
            json={"query": "q", "texts": ["t"], "mode": "unknown"},
        )
        assert resp.status_code == 422


class TestLocalMLClientNoModuleLevelImport:
    def test_local_ml_client_no_import_error_without_torch(self):
        """LocalMLClient can be instantiated without sentence_transformers loaded at import time.

        Strategy: verify that importing ml_client does NOT add sentence_transformers
        to sys.modules (i.e. no module-level import of the heavy ML deps).
        """
        # Ensure the module is freshly imported (it may already be cached)

        # Remove cached module to force re-import
        for key in list(sys.modules.keys()):
            if key == "yadgar.ml_client":
                del sys.modules[key]

        # Snapshot modules before import
        before = set(sys.modules.keys())

        import yadgar.ml_client  # noqa: F401

        after = set(sys.modules.keys())
        new_modules = after - before

        # sentence_transformers (and torch) must NOT be imported at module level
        assert "sentence_transformers" not in new_modules
        assert "torch" not in new_modules
