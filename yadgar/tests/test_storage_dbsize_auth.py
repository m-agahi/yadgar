"""Tests for get_db_size() bearer-auth fix (v5.1 A1).

TDD: tests written first before implementation.
Run with: YADGAR_TEST=1 pytest yadgar/tests/test_storage_dbsize_auth.py -x
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import httpx

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PAYLOAD = {
    "db_size_bytes": 1000,
    "vlog_size_bytes": 700,
    "sstables_size_bytes": 200,
    "wal_size_bytes": 100,
    "other_size_bytes": 0,
    "vlog_pct_of_total": 70,
}


def _make_storage(db_url: str = "http://localhost:8000") -> object:
    """Return a real StorageEngine instance stub with _db_url set, no actual DB."""
    from yadgar.storage import StorageEngine

    s = object.__new__(StorageEngine)
    s._db_url = db_url  # type: ignore[attr-defined]
    return s


def _mock_transport(status: int, body: dict | None = None) -> httpx.MockTransport:
    """Return an httpx MockTransport that always responds with given status."""
    content = json.dumps(body or {}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content, request=request)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetDbSizeAuth:
    def test_get_db_size_server_mode_sends_bearer_token(self, monkeypatch):
        """With YADGAR_MCP_AUTH_TOKEN set, outbound GET must include Authorization header."""
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")
        monkeypatch.setenv("YADGAR_BACKEND_EMBED_URL", "http://embed:8001")

        captured: list[httpx.Request] = []

        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = _PAYLOAD

            # Intercept and record the headers passed to httpx.get
            def fake_get(url, **kwargs):
                headers = kwargs.get("headers", {})
                req = httpx.Request("GET", url, headers=headers)
                captured.append(req)
                return mock_resp

            mock_get.side_effect = fake_get

            storage = _make_storage()
            storage.get_db_size()  # type: ignore[attr-defined]

        assert len(captured) == 1
        assert captured[0].headers.get("authorization") == "Bearer test-token"

    def test_get_db_size_server_mode_no_token_still_calls(self, monkeypatch):
        """Without YADGAR_MCP_AUTH_TOKEN, call still goes out but no Authorization header."""
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_BACKEND_EMBED_URL", "http://embed:8001")

        captured: list[httpx.Request] = []

        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = _PAYLOAD

            def fake_get(url, **kwargs):
                headers = kwargs.get("headers", {})
                req = httpx.Request("GET", url, headers=headers)
                captured.append(req)
                return mock_resp

            mock_get.side_effect = fake_get

            storage = _make_storage()
            storage.get_db_size()  # type: ignore[attr-defined]

        assert len(captured) == 1, "call must still happen"
        assert "authorization" not in {k.lower() for k in captured[0].headers.keys()}

    def test_get_db_size_server_mode_401_logs_status(self, monkeypatch, caplog):
        """On 401 response, log message must include the status code '401'."""
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "bad-token")
        monkeypatch.setenv("YADGAR_BACKEND_EMBED_URL", "http://embed:8001")

        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock(spec=httpx.Response)
            mock_resp.status_code = 401
            mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "401 Unauthorized",
                request=httpx.Request("GET", "http://embed:8001/admin/dbsize"),
                response=mock_resp,
            )
            mock_get.return_value = mock_resp

            storage = _make_storage()
            with caplog.at_level(logging.WARNING, logger="yadgar.storage"):
                result = storage.get_db_size()  # type: ignore[attr-defined]

        assert result["db_size_bytes"] == 0
        assert "401" in caplog.text

    def test_get_db_size_server_mode_200_returns_payload(self, monkeypatch):
        """200 response returns parsed payload plus size_warning derived from threshold."""
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "good-token")
        monkeypatch.setenv("YADGAR_BACKEND_EMBED_URL", "http://embed:8001")

        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = _PAYLOAD.copy()
            mock_get.return_value = mock_resp

            storage = _make_storage()
            result = storage.get_db_size()  # type: ignore[attr-defined]

        assert result["db_size_bytes"] == 1000
        assert result["vlog_size_bytes"] == 700
        assert result["sstables_size_bytes"] == 200
        assert result["wal_size_bytes"] == 100
        assert result["vlog_pct_of_total"] == 70
        # 1000 bytes < DB_SIZE_WARNING_BYTES (1 GiB default) → no warning
        assert result["size_warning"] is False
