"""test_bookmarks_search.py — v5.50.1 Bookmarks tab search route tests.

Tests:
  1. GET /api/wiki_query — semantic mode delegates to wiki.query() (embedding path)
  2. GET /api/wiki_query — keyword mode: substring filter in Python, no FULLTEXT
  3. GET /api/wiki_query — slug mode: prefix match via list_wiki_pages
  4. GET /api/wiki_query — missing q returns empty list (not error)
  5. GET /api/wiki_query — invalid mode defaults to semantic
  6. GET /api/wiki_query — limit parameter respected
  7. Route is registered on the mcp_server (inspectable without live HTTP)
  8. Keyword mode matches on title (case-insensitive)
  9. Keyword mode matches on slug (case-insensitive)
  10. Slug mode returns only pages whose slug starts with q

Run:
  OTEL_SDK_DISABLED=true uv run --extra test pytest yadgar/tests/test_bookmarks_search.py -p no:xdist -v
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from yadgar.core import server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("bookmarks_search")
    server.init_engines(
        db_path=str(tmp_path / "bm_search_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _wiki():
    return server._wiki


def _storage():
    return server._get_storage()


def _insert(slug, title="Test", content="content here", tags=None):
    return _storage().insert_wiki_page(
        {
            "slug": slug,
            "title": title,
            "content": content,
            "category": "reference",
            "tags": tags or [],
            "confidence": "medium",
            "source_memory_ids": [],
            "links": [],
        }
    )


# ---------------------------------------------------------------------------
# 1. Route registration
# ---------------------------------------------------------------------------


class TestWikiQueryRouteRegistered:
    def test_http_wiki_versioning_module_importable(self):
        """http_wiki_versioning module imports without error."""
        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        assert hasattr(http_wiki_versioning, "api_wiki_query")

    def test_api_wiki_query_is_callable(self):
        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        assert callable(http_wiki_versioning.api_wiki_query)

    def test_api_wiki_query_handles_semantic_mode(self):
        """Route source calls wiki.query() (embedding path)."""
        import pathlib  # noqa: PLC0415

        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        src = pathlib.Path(http_wiki_versioning.__file__).read_text()
        assert "wiki.query" in src, "semantic mode must use WikiStore.query (embedding path)"

    def test_api_wiki_query_keyword_no_surreal_fts(self):
        """File must not contain SurrealDB FTS SQL syntax (DEFINE ANALYZER or search::score)."""
        import pathlib  # noqa: PLC0415

        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        src = pathlib.Path(http_wiki_versioning.__file__).read_text()
        # These are the actual SurrealDB FTS constructs that would break embedded tests
        assert "DEFINE ANALYZER" not in src, "must NOT define SurrealDB FULLTEXT ANALYZER"
        assert "search::score" not in src, "must NOT use SurrealDB search::score FTS function"
        assert "SEARCH ANALYZER" not in src, "must NOT use SurrealDB SEARCH ANALYZER index"


# ---------------------------------------------------------------------------
# 2. Semantic mode
# ---------------------------------------------------------------------------


class TestWikiQuerySemantic:
    def test_semantic_mode_calls_wiki_query(self):
        """semantic mode must call wiki.query() (embedding path)."""
        _insert("embed-test-page", "Embed Test Page", "neural network embeddings for memory")

        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        # Mock wiki.query to verify it is called, not a DB FULLTEXT query
        wiki_mock = MagicMock()
        wiki_mock.query.return_value = [
            {"slug": "embed-test-page", "title": "Embed Test Page", "score": 0.9}
        ]

        with patch.object(server._state_mod, "_wiki", wiki_mock):
            request = _make_request({"q": "neural embeddings", "mode": "semantic"})
            resp = asyncio.run(http_wiki_versioning.api_wiki_query(request))

        wiki_mock.query.assert_called_once()
        assert resp.status_code == 200

    def test_semantic_mode_strips_embedding_field(self):
        """Response must not include 'embedding' key (heavy field)."""
        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        wiki_mock = MagicMock()
        wiki_mock.query.return_value = [
            {"slug": "p1", "title": "P1", "score": 0.8, "embedding": [0.1, 0.2, 0.3]}
        ]
        storage_mock = MagicMock()

        with (
            patch.object(server._state_mod, "_wiki", wiki_mock),
            patch.object(server._state_mod, "_storage", storage_mock),
        ):
            request = _make_request({"q": "test", "mode": "semantic"})
            resp = asyncio.run(http_wiki_versioning.api_wiki_query(request))

        import json

        body = json.loads(resp.body)
        assert isinstance(body, list)
        for item in body:
            assert "embedding" not in item, "embedding field must be stripped from response"


# ---------------------------------------------------------------------------
# 3. Keyword mode (Python substring filter — no FULLTEXT)
# ---------------------------------------------------------------------------


class TestWikiQueryKeyword:
    def test_keyword_matches_title(self):
        """keyword mode finds pages whose title contains the query."""
        _insert("alpha-page", "Alpha Testing Guide", "content")
        _insert("unrelated-page", "Unrelated Content", "content")

        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        request = _make_request({"q": "alpha", "mode": "keyword"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_query(request))

        import json

        body = json.loads(resp.body)
        slugs = [r["slug"] for r in body]
        assert "alpha-page" in slugs
        assert "unrelated-page" not in slugs

    def test_keyword_matches_slug(self):
        """keyword mode finds pages whose slug contains the query."""
        _insert("benchmarks-q4", "Q4 Benchmarks", "content")
        _insert("other-page", "Other Page", "content")

        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        request = _make_request({"q": "benchmarks", "mode": "keyword"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_query(request))

        import json

        body = json.loads(resp.body)
        slugs = [r["slug"] for r in body]
        assert "benchmarks-q4" in slugs

    def test_keyword_case_insensitive(self):
        """keyword mode is case-insensitive."""
        _insert("competitor-catalog", "Competitor Catalog", "content")

        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        request = _make_request({"q": "COMPETITOR", "mode": "keyword"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_query(request))

        import json

        body = json.loads(resp.body)
        slugs = [r["slug"] for r in body]
        assert "competitor-catalog" in slugs

    def test_keyword_no_surreal_fts_syntax(self):
        """Keyword mode uses Python .lower() filter — no SurrealDB FTS SQL constructs."""
        import pathlib  # noqa: PLC0415

        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        src = pathlib.Path(http_wiki_versioning.__file__).read_text()
        assert "DEFINE ANALYZER" not in src
        assert "search::score" not in src
        # Python substring filter must be present
        assert ".lower()" in src, "keyword mode must use Python .lower() for substring match"


# ---------------------------------------------------------------------------
# 4. Slug mode
# ---------------------------------------------------------------------------


class TestWikiQuerySlug:
    def test_slug_prefix_match(self):
        """slug mode returns pages whose slug starts with the query."""
        _insert("roadmap-2026", "Roadmap 2026", "content")
        _insert("roadmap-q1", "Roadmap Q1", "content")
        _insert("benchmarks-2026", "Benchmarks", "content")

        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        request = _make_request({"q": "roadmap", "mode": "slug"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_query(request))

        import json

        body = json.loads(resp.body)
        slugs = [r["slug"] for r in body]
        assert "roadmap-2026" in slugs
        assert "roadmap-q1" in slugs
        assert "benchmarks-2026" not in slugs


# ---------------------------------------------------------------------------
# 5. Edge cases
# ---------------------------------------------------------------------------


class TestWikiQueryEdgeCases:
    def test_empty_q_returns_empty_list(self):
        """Empty query returns [] without error."""
        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        request = _make_request({"q": ""})
        resp = asyncio.run(http_wiki_versioning.api_wiki_query(request))

        import json

        assert resp.status_code == 200
        assert json.loads(resp.body) == []

    def test_invalid_mode_defaults_to_semantic(self):
        """Unknown mode value is treated as semantic."""
        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        wiki_mock = MagicMock()
        wiki_mock.query.return_value = []
        storage_mock = MagicMock()

        with (
            patch.object(server._state_mod, "_wiki", wiki_mock),
            patch.object(server._state_mod, "_storage", storage_mock),
        ):
            request = _make_request({"q": "test", "mode": "nonsense"})
            asyncio.run(http_wiki_versioning.api_wiki_query(request))

        wiki_mock.query.assert_called_once()

    def test_limit_respected(self):
        """limit parameter caps result count."""
        for i in range(5):
            _insert(f"limit-test-{i}", f"Limit Test {i}", "content")

        from yadgar.core.server import http_wiki_versioning  # noqa: PLC0415

        request = _make_request({"q": "limit", "mode": "keyword", "limit": "2"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_query(request))

        import json

        body = json.loads(resp.body)
        assert len(body) <= 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(params: dict):
    """Build a minimal fake Starlette Request with query params."""
    from starlette.requests import Request  # noqa: PLC0415

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/wiki_query",
        "query_string": b"&".join(f"{k}={v}".encode() for k, v in params.items()),
        "headers": [],
    }
    return Request(scope)
