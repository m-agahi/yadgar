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
    """Yield (mock_st, mock_retriever, mock_wiki).

    T2 Car E2 seam migration: api_viz_search no longer reads _st._retriever —
    memory recall now forwards to the backend /recall via
    ``_HookRecallForwarder("").recall`` → ``_forward_hook_recall`` (the same
    seam the hook siblings use, ADR-0078). We patch ``_forward_hook_recall`` and
    route it through the ``mock_retriever.recall`` surface so every test body's
    ``retriever.recall.return_value`` / ``.side_effect`` wiring migrates verbatim
    (mechanism moved, guarded property — id→node-id mapping — preserved). Wiki
    still resolves in-core via ``_st._wiki``.
    """
    mock_retriever = MagicMock()
    mock_wiki = MagicMock()

    def _fwd(query, *, max_results=5, min_heat=0.0, directory="", profile="fast", **_):
        return mock_retriever.recall(
            query, max_results=max_results, min_heat=min_heat, profile=profile
        )

    with (
        patch("yadgar.core.server.http._st") as mock_st,
        patch("yadgar.core.server.http._forward_hook_recall", side_effect=_fwd),
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

        from yadgar.core.server.http import api_viz_search

        app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
        client = TestClient(app)
        return client.get(f"/api/viz/search?q={q}")

    def test_empty_query_returns_empty(self, _patch_state) -> None:
        mock_st, retriever, wiki = _patch_state

        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from yadgar.core.server.http import api_viz_search

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

        from yadgar.core.server.http import api_viz_search

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

        from yadgar.core.server.http import api_viz_search

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

        from yadgar.core.server.http import api_viz_search

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

        from yadgar.core.server.http import api_viz_search

        app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
        client = TestClient(app)
        resp = client.get("/api/viz/search?q=broken")
        assert resp.status_code == 200
        data = resp.json()
        # Wiki results still present
        assert "wiki:3" in data["node_ids"]

    def test_no_retriever_no_wiki_returns_empty(self) -> None:
        with patch("yadgar.core.server.http._st") as mock_st:
            mock_st._retriever = None
            mock_st._wiki = None
            mock_st._storage = None

            from starlette.applications import Starlette
            from starlette.routing import Route
            from starlette.testclient import TestClient

            from yadgar.core.server.http import api_viz_search

            app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
            client = TestClient(app)
            resp = client.get("/api/viz/search?q=anything")
            assert resp.status_code == 200
            assert resp.json()["node_ids"] == []

    # ------------------------------------------------------------------
    # P0.2 — exact-title precedence (viz-fix-plan-2026-06-27)
    #
    # Bug: search routes through recall() WRRF capped at top-5, so a memory
    # whose content EXACTLY matches the query can fall out of the top-5 and
    # never light up (user searched a title, the wrong node highlighted).
    # Fix: query memories whose content exactly/prefix-matches the query and
    # prepend their ids to node_ids (deduped), regardless of recall ranking.
    # ------------------------------------------------------------------

    def test_exact_title_match_prepended_even_when_outside_recall_top5(self, _patch_state) -> None:
        mock_st, retriever, wiki = _patch_state
        wiki.query.return_value = []

        # recall returns 5 DECOY memories that out-rank the exact match — the
        # exact-title node (id 777) is NOT among them, reproducing the bug.
        retriever.recall.return_value = [_make_recall_result(i) for i in range(1, 6)]

        # storage._q resolves the exact/prefix-title match to memory id 777.
        mock_st._storage = MagicMock()
        mock_st._storage._q.return_value = [{"id": 777, "content": "Project Phoenix launch plan"}]

        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from yadgar.core.server.http import api_viz_search

        app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
        client = TestClient(app)
        resp = client.get("/api/viz/search?q=Project Phoenix launch plan")
        assert resp.status_code == 200
        node_ids = resp.json()["node_ids"]
        # The exact-title node is present despite being outside recall's top-5...
        assert "mem:777" in node_ids
        # ...and takes precedence (prepended ahead of the WRRF recall results).
        assert node_ids[0] == "mem:777"

    def test_exact_title_storage_error_swallowed(self, _patch_state) -> None:
        mock_st, retriever, wiki = _patch_state
        retriever.recall.return_value = [_make_recall_result(42)]
        wiki.query.return_value = []
        mock_st._storage = MagicMock()
        mock_st._storage._q.side_effect = RuntimeError("db down")

        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from yadgar.core.server.http import api_viz_search

        app = Starlette(routes=[Route("/api/viz/search", api_viz_search, methods=["GET"])])
        client = TestClient(app)
        resp = client.get("/api/viz/search?q=test")
        assert resp.status_code == 200
        # recall path still works even when the title-precedence query fails
        assert "mem:42" in resp.json()["node_ids"]
