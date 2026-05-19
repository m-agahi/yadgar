"""V1: /api/viz/search endpoint tests.

Tests:
- Empty query returns empty node_ids list.
- Recall results are mapped to mem:<id> node IDs.
- Wiki results are mapped to wiki:<id> node IDs.
- Both recall + wiki results are merged and deduplicated.
- Errors in recall/wiki are swallowed (endpoint still returns 200).
- State=None (no retriever/wiki) returns empty list gracefully.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_recall_result(raw_id: int) -> dict:
    return {"id": raw_id, "content": f"memory {raw_id}", "heat": 0.5}


def _make_wiki_result(raw_id: int, slug: str = "test-page") -> dict:
    return {"id": raw_id, "title": slug, "slug": slug}


@pytest.fixture()
def _patch_state():
    """Yield a namespace that patches _st._retriever and _st._wiki."""
    mock_retriever = MagicMock()
    mock_wiki = MagicMock()

    with (
        patch("yadgar.server.http._st") as mock_st,
    ):
        mock_st._retriever = mock_retriever
        mock_st._wiki = mock_wiki
        yield mock_st, mock_retriever, mock_wiki


class TestVizSearchEndpoint:
    def _call(self, q: str, _patch_state_fixture) -> dict:

        from starlette.testclient import TestClient

        mock_st, retriever, wiki = _patch_state_fixture

        # Build minimal starlette app with just our route
        from starlette.applications import Starlette
        from starlette.routing import Route

        from yadgar.server.http import api_viz_search

        app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
        client = TestClient(app)
        return client.get(f"/api/viz/search?q={q}")

    def test_empty_query_returns_empty(self, _patch_state) -> None:
        mock_st, retriever, wiki = _patch_state

        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from yadgar.server.http import api_viz_search

        app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
        client = TestClient(app)
        resp = client.get("/api/viz/search?q=")
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_ids"] == []
        assert data["query"] == ""

    def test_recall_results_mapped_to_mem_ids(self, _patch_state) -> None:
        mock_st, retriever, wiki = _patch_state
        retriever.recall.return_value = [_make_recall_result(42), _make_recall_result(99)]
        wiki.query.return_value = []

        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from yadgar.server.http import api_viz_search

        app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
        client = TestClient(app)
        resp = client.get("/api/viz/search?q=test")
        assert resp.status_code == 200
        data = resp.json()
        assert "mem:42" in data["node_ids"]
        assert "mem:99" in data["node_ids"]

    def test_wiki_results_mapped_to_wiki_ids(self, _patch_state) -> None:
        mock_st, retriever, wiki = _patch_state
        retriever.recall.return_value = []
        wiki.query.return_value = [_make_wiki_result(7, "arch-page")]

        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from yadgar.server.http import api_viz_search

        app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
        client = TestClient(app)
        resp = client.get("/api/viz/search?q=architecture")
        assert resp.status_code == 200
        data = resp.json()
        assert "wiki:7" in data["node_ids"]

    def test_deduplication(self, _patch_state) -> None:
        mock_st, retriever, wiki = _patch_state
        # Same memory id returned twice
        retriever.recall.return_value = [_make_recall_result(5), _make_recall_result(5)]
        wiki.query.return_value = []

        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from yadgar.server.http import api_viz_search

        app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
        client = TestClient(app)
        resp = client.get("/api/viz/search?q=dup")
        data = resp.json()
        assert data["node_ids"].count("mem:5") == 1

    def test_recall_error_swallowed(self, _patch_state) -> None:
        mock_st, retriever, wiki = _patch_state
        retriever.recall.side_effect = RuntimeError("backend down")
        wiki.query.return_value = [_make_wiki_result(3)]

        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from yadgar.server.http import api_viz_search

        app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
        client = TestClient(app)
        resp = client.get("/api/viz/search?q=broken")
        assert resp.status_code == 200
        data = resp.json()
        # Wiki results still present
        assert "wiki:3" in data["node_ids"]

    def test_no_retriever_no_wiki_returns_empty(self) -> None:
        with patch("yadgar.server.http._st") as mock_st:
            mock_st._retriever = None
            mock_st._wiki = None

            from starlette.applications import Starlette
            from starlette.routing import Route
            from starlette.testclient import TestClient

            from yadgar.server.http import api_viz_search

            app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
            client = TestClient(app)
            resp = client.get("/api/viz/search?q=anything")
            assert resp.status_code == 200
            assert resp.json()["node_ids"] == []
