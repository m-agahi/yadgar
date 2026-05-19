"""V2+V3: wiki click → content panel via /api/wiki/read endpoint.

Tests:
- /api/wiki/read?slug= returns page content correctly.
- /api/wiki/read with unknown slug returns 404.
- /api/wiki/read with no slug returns 400.
- /api/wiki/read when wiki not initialized returns 503.
- /api/wiki/read when wiki.read() raises returns 500.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_wiki_page(slug: str = "arch-decisions") -> dict:
    return {
        "id": 7,
        "slug": slug,
        "title": "Architecture Decisions",
        "content": "This page documents architecture decisions.",
        "category": "architecture",
        "tags": ["arch", "decision"],
        "updated_at": "2026-05-01T12:00:00",
    }


class TestApiWikiRead:
    def _make_app(self, wiki_store):
        from starlette.applications import Starlette
        from starlette.routing import Route

        from yadgar.server.http import api_wiki_read

        return Starlette(routes=[Route("/api/wiki/read", api_wiki_read, methods=["GET"])])

    def test_returns_page_content(self) -> None:
        page = _make_wiki_page()
        wiki = MagicMock()
        wiki.read.return_value = page

        with patch("yadgar.server.http._st") as mock_st:
            mock_st._wiki = wiki

            from starlette.testclient import TestClient

            app = self._make_app(wiki)
            client = TestClient(app)
            resp = client.get("/api/wiki/read?slug=arch-decisions")

        assert resp.status_code == 200
        data = resp.json()
        assert data["slug"] == "arch-decisions"
        assert data["content"] == page["content"]
        assert data["title"] == page["title"]
        assert data["category"] == page["category"]

    def test_unknown_slug_returns_404(self) -> None:
        wiki = MagicMock()
        wiki.read.return_value = None

        with patch("yadgar.server.http._st") as mock_st:
            mock_st._wiki = wiki

            from starlette.testclient import TestClient

            app = self._make_app(wiki)
            client = TestClient(app)
            resp = client.get("/api/wiki/read?slug=nonexistent")

        assert resp.status_code == 404

    def test_no_slug_returns_400(self) -> None:
        wiki = MagicMock()

        with patch("yadgar.server.http._st") as mock_st:
            mock_st._wiki = wiki

            from starlette.testclient import TestClient

            app = self._make_app(wiki)
            client = TestClient(app)
            resp = client.get("/api/wiki/read")

        assert resp.status_code == 400

    def test_wiki_not_initialized_returns_503(self) -> None:
        with patch("yadgar.server.http._st") as mock_st:
            mock_st._wiki = None

            from starlette.testclient import TestClient

            app = self._make_app(None)
            client = TestClient(app)
            resp = client.get("/api/wiki/read?slug=anything")

        assert resp.status_code == 503

    def test_wiki_read_error_returns_500(self) -> None:
        wiki = MagicMock()
        wiki.read.side_effect = RuntimeError("db error")

        with patch("yadgar.server.http._st") as mock_st:
            mock_st._wiki = wiki

            from starlette.testclient import TestClient

            app = self._make_app(wiki)
            client = TestClient(app)
            resp = client.get("/api/wiki/read?slug=broken")

        assert resp.status_code == 500

    def test_tags_returned_as_list(self) -> None:
        page = _make_wiki_page()
        wiki = MagicMock()
        wiki.read.return_value = page

        with patch("yadgar.server.http._st") as mock_st:
            mock_st._wiki = wiki

            from starlette.testclient import TestClient

            app = self._make_app(wiki)
            client = TestClient(app)
            resp = client.get("/api/wiki/read?slug=arch-decisions")

        data = resp.json()
        assert isinstance(data["tags"], list)
        assert "arch" in data["tags"]
