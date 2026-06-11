"""test_bookmarks_versions.py — v5.50.1 Bookmarks tab versioning route tests.

Tests:
  1. GET /api/wiki_history — happy path returns versions list
  2. GET /api/wiki_history — bad slug returns 404
  3. GET /api/wiki_history — missing slug returns 400
  4. GET /api/wiki_read_version — happy path returns full snapshot
  5. GET /api/wiki_read_version — missing version returns 404
  6. GET /api/wiki_read_version — bad slug returns 404
  7. GET /api/wiki_read_version — non-integer version returns 400
  8. GET /api/wiki_diff — happy path unified diff has --- / +++
  9. GET /api/wiki_diff — bad slug returns 404
  10. GET /api/wiki_diff — missing v1/v2 returns 400
  11. POST /api/wiki_restore — confirmation-gated: creates new version
  12. POST /api/wiki_restore — missing slug returns 400
  13. POST /api/wiki_restore — bad slug returns 404

Run:
  OTEL_SDK_DISABLED=true uv run --extra test pytest yadgar/tests/test_bookmarks_versions.py -p no:xdist -v
"""

from __future__ import annotations

import asyncio
import json

import pytest

from yadgar import server
from yadgar.storage.migrations import _migration_013_wiki_page_version

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "bm_versions_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    _migration_013_wiki_page_version(server._get_storage())
    yield
    server.shutdown()


def _storage():
    return server._get_storage()


def _wiki():
    return server._wiki


def _insert(slug, title="Test", content="initial content"):
    return _storage().insert_wiki_page(
        {
            "slug": slug,
            "title": title,
            "content": content,
            "category": "reference",
            "tags": [],
            "confidence": "medium",
            "source_memory_ids": [],
            "links": [],
        }
    )


# ---------------------------------------------------------------------------
# 1–3. /api/wiki_history
# ---------------------------------------------------------------------------


class TestWikiHistoryRoute:
    def test_happy_path_returns_versions(self):
        """wiki_history returns versions list for known slug."""
        pid = _insert("hist-happy", "Hist Happy", "v1")
        _storage().update_wiki_page(pid, {"content": "v2"})

        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_history", {"slug": "hist-happy"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_history(req))

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert "versions" in body
        assert len(body["versions"]) >= 2
        # newest first
        versions = [v["version"] for v in body["versions"]]
        assert versions == sorted(versions, reverse=True)

    def test_versions_have_required_fields(self):
        """Each version entry has created_at, change_summary, size_bytes."""
        _insert("hist-fields", "Hist Fields", "content")

        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_history", {"slug": "hist-fields"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_history(req))

        body = json.loads(resp.body)
        entry = body["versions"][0]
        assert "version" in entry
        assert "created_at" in entry
        assert "change_summary" in entry
        assert "size_bytes" in entry
        # Must NOT include full content (light payload)
        assert "content" not in entry

    def test_bad_slug_returns_404(self):
        """Unknown slug → 404."""
        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_history", {"slug": "no-such-page-xyz"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_history(req))

        assert resp.status_code == 404
        body = json.loads(resp.body)
        assert "error" in body

    def test_missing_slug_returns_400(self):
        """No slug param → 400."""
        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_history", {})
        resp = asyncio.run(http_wiki_versioning.api_wiki_history(req))

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 4–7. /api/wiki_read_version
# ---------------------------------------------------------------------------


class TestWikiReadVersionRoute:
    def test_happy_path_returns_full_snapshot(self):
        """wiki_read_version returns full snapshot with content."""
        pid = _insert("ver-happy", "Ver Happy", "original content v1")
        _storage().update_wiki_page(pid, {"content": "updated v2"})

        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_read_version", {"slug": "ver-happy", "version": "1"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_read_version(req))

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body.get("content") == "original content v1"
        assert body.get("version") == 1
        assert "title" in body
        assert "created_at" in body

    def test_version_2_returns_new_content(self):
        """Version 2 returns the updated content."""
        pid = _insert("ver-2", "Ver 2", "original")
        _storage().update_wiki_page(pid, {"content": "updated"})

        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_read_version", {"slug": "ver-2", "version": "2"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_read_version(req))

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body.get("content") == "updated"

    def test_missing_version_returns_404(self):
        """Out-of-range version → 404 with max_version hint."""
        _insert("ver-missing", "Ver Missing", "content")

        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_read_version", {"slug": "ver-missing", "version": "99"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_read_version(req))

        assert resp.status_code == 404
        body = json.loads(resp.body)
        assert "error" in body

    def test_bad_slug_returns_404(self):
        """Unknown slug → 404."""
        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_read_version", {"slug": "no-such-page", "version": "1"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_read_version(req))

        assert resp.status_code == 404

    def test_non_integer_version_returns_400(self):
        """Non-integer version param → 400."""
        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_read_version", {"slug": "any", "version": "abc"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_read_version(req))

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 8–10. /api/wiki_diff
# ---------------------------------------------------------------------------


class TestWikiDiffRoute:
    def test_happy_path_unified_diff(self):
        """Unified diff response contains --- and +++ markers."""
        pid = _insert("diff-route", "Diff Route", "line one\nline two\n")
        _storage().update_wiki_page(pid, {"content": "line one\nline three\n"})

        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_diff", {"slug": "diff-route", "v1": "1", "v2": "2"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_diff(req))

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert "diff" in body
        assert "---" in body["diff"]
        assert "+++" in body["diff"]
        assert body.get("v1") == 1
        assert body.get("v2") == 2

    def test_bad_slug_returns_404(self):
        """Unknown slug → 404."""
        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_diff", {"slug": "no-such", "v1": "1", "v2": "2"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_diff(req))

        assert resp.status_code == 404

    def test_missing_v1_v2_returns_400(self):
        """Missing v1/v2 → 400."""
        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _get_request("/api/wiki_diff", {"slug": "any"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_diff(req))

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 11–13. /api/wiki_restore (confirmation-gated)
# ---------------------------------------------------------------------------


class TestWikiRestoreRoute:
    def test_confirmation_gate_creates_new_version(self):
        """POST wiki_restore with valid slug+version creates a new version."""
        pid = _insert("restore-route", "Restore Route", "original content")
        _storage().update_wiki_page(pid, {"content": "overwritten"})

        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _post_request("/api/wiki_restore", {"slug": "restore-route", "version": 1})
        resp = asyncio.run(http_wiki_versioning.api_wiki_restore(req))

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body.get("restored") is True
        assert body.get("slug") == "restore-route"
        assert body.get("restored_from_version") == 1
        assert body.get("new_version") == 3

    def test_missing_slug_returns_400(self):
        """Body without slug → 400."""
        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _post_request("/api/wiki_restore", {"version": 1})
        resp = asyncio.run(http_wiki_versioning.api_wiki_restore(req))

        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body.get("restored") is False

    def test_bad_slug_returns_404(self):
        """Unknown slug → 404."""
        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _post_request("/api/wiki_restore", {"slug": "no-such-page", "version": 1})
        resp = asyncio.run(http_wiki_versioning.api_wiki_restore(req))

        assert resp.status_code == 404

    def test_non_integer_version_returns_400(self):
        """Non-integer version in body → 400."""
        from yadgar.server import http_wiki_versioning  # noqa: PLC0415

        req = _post_request("/api/wiki_restore", {"slug": "any", "version": "bad"})
        resp = asyncio.run(http_wiki_versioning.api_wiki_restore(req))

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_request(path: str, params: dict):
    """Build a minimal fake GET Request with query params."""
    from starlette.requests import Request  # noqa: PLC0415

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": "&".join(f"{k}={v}" for k, v in params.items()).encode(),
        "headers": [],
    }
    return Request(scope)


def _post_request(path: str, body: dict):
    """Build a minimal fake POST Request with JSON body."""
    import json as _json  # noqa: PLC0415

    from starlette.requests import Request  # noqa: PLC0415

    raw_body = _json.dumps(body).encode()

    async def _receive():
        return {"type": "http.request", "body": raw_body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    req = Request(scope, receive=_receive)
    return req
