"""Car G3 — fail-open host client (ADR-0163).

``yadgar.core.runtime_config_client.get(key, directory=None, default=None)``:
  * GET http://127.0.0.1:{YADGAR_PORT}/api/runtime-config/{key}?directory=...
  * Bearer auth from YADGAR_MCP_AUTH_TOKEN when set.
  * 200 + {"value": v} → v.
  * value null/missing → default.
  * ANY error (connection refused / timeout / non-200 / malformed JSON) → default,
    NEVER raise (fail-open — the stop-hook depends on this).

stdlib urllib only (no httpx) — the host has no app deps.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

from yadgar.core import runtime_config_client as client


def _fake_response(payload: dict):
    """A minimal object matching the urlopen context-manager .read() contract."""

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp(json.dumps(payload).encode())


class TestHostClientHappyPath:
    def test_returns_value_on_200(self):
        with patch.object(client._req, "urlopen", return_value=_fake_response({"value": True})):
            assert client.get("code_graph.enabled", default=False) is True

    def test_null_value_returns_default(self):
        with patch.object(client._req, "urlopen", return_value=_fake_response({"value": None})):
            assert client.get("k", default="fallback") == "fallback"

    def test_missing_value_key_returns_default(self):
        with patch.object(client._req, "urlopen", return_value=_fake_response({})):
            assert client.get("k", default=7) == 7

    def test_directory_and_key_encoded_in_url(self):
        seen: dict = {}

        def _capture(req, timeout=None):
            # req may be a Request or a str; normalize.
            seen["url"] = req.full_url if hasattr(req, "full_url") else req
            return _fake_response({"value": 1})

        with patch.object(client._req, "urlopen", side_effect=_capture):
            client.get("code_graph.enabled", directory="/home/x/my proj", default=0)
        assert "/api/runtime-config/code_graph.enabled" in seen["url"]
        # directory query param is URL-encoded (space → %20 or +)
        assert "directory=" in seen["url"]
        assert " " not in seen["url"].split("directory=", 1)[1]

    def test_bearer_header_set_when_token_present(self, monkeypatch):
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "sekret")
        seen: dict = {}

        def _capture(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return _fake_response({"value": 1})

        with patch.object(client._req, "urlopen", side_effect=_capture):
            client.get("k", default=0)
        assert seen["auth"] == "Bearer sekret"


class TestHostClientFailOpen:
    def test_connection_refused_returns_default(self):
        with patch.object(
            client._req, "urlopen", side_effect=urllib.error.URLError("Connection refused")
        ):
            assert client.get("k", default="D") == "D"

    def test_timeout_returns_default(self):
        with patch.object(client._req, "urlopen", side_effect=TimeoutError("timed out")):
            assert client.get("k", default=False) is False

    def test_http_error_non_200_returns_default(self):
        # HTTPError subclasses tempfile._TemporaryFileWrapper (via addbase) on
        # py3.14; an unclosed instance fires a ResourceWarning from its
        # deallocator at GC time — which pytest-xdist mis-attributes to whatever
        # test happens to be in setup, erroring an unrelated case. Close it in a
        # finally so the wrapper is released deterministically before GC.
        err = urllib.error.HTTPError("url", 500, "boom", hdrs=None, fp=None)
        try:
            with patch.object(client._req, "urlopen", side_effect=err):
                assert client.get("k", default=123) == 123
        finally:
            err.close()

    def test_http_error_is_closed_by_client(self):
        """The client CLOSES the HTTPError it catches (no leaked tempfile wrapper).

        Regression: an unclosed HTTPError's deallocator fires a spurious
        ResourceWarning at a later GC, which pytest-xdist mis-attributes to an
        unrelated test. The stop-hook's dir-aware code_graph gate hits a live
        daemon that may 401/404, so this path is exercised in normal test runs.
        """
        err = urllib.error.HTTPError("url", 404, "Not Found", hdrs=None, fp=None)
        closed = {"n": 0}
        _orig_close = err.close

        def _tracking_close():
            closed["n"] += 1
            _orig_close()

        err.close = _tracking_close  # type: ignore[method-assign]
        with patch.object(client._req, "urlopen", side_effect=err):
            assert client.get("k", default="D") == "D"
        assert closed["n"] >= 1, "client must close the caught HTTPError"

    def test_malformed_json_returns_default(self):
        class _Bad(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(client._req, "urlopen", return_value=_Bad(b"not json{{{")):
            assert client.get("k", default="safe") == "safe"

    def test_never_raises(self):
        with patch.object(client._req, "urlopen", side_effect=Exception("anything")):
            # must not propagate
            assert client.get("k", default=None) is None


# ---------------------------------------------------------------------------
# Car G5 — host WRITE client (set / delete). UNLIKE get(), NOT fail-open:
# daemon-down / non-2xx → False so the caller can report "couldn't enable".
# ---------------------------------------------------------------------------


def _fake_2xx(status: int = 200):
    """A urlopen context-manager stand-in exposing .status + .read()."""

    class _Resp(io.BytesIO):
        def __init__(self):
            super().__init__(b"{}")
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


class TestHostClientSet:
    def test_returns_true_on_2xx(self):
        with patch.object(client._req, "urlopen", return_value=_fake_2xx(200)):
            assert client.set("code_graph.enabled", True, scope="global") is True

    def test_posts_body_and_path(self):
        seen: dict = {}

        def _capture(req, timeout=None):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["body"] = json.loads(req.data.decode())
            return _fake_2xx(200)

        with patch.object(client._req, "urlopen", side_effect=_capture):
            client.set("code_graph.enabled", True, scope="project", directory="/home/x/p")
        assert "/api/runtime-config/code_graph.enabled" in seen["url"]
        assert seen["method"] == "POST"
        assert seen["body"] == {"value": True, "scope": "project", "directory": "/home/x/p"}

    def test_bearer_header_set_when_token_present(self, monkeypatch):
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "sekret")
        seen: dict = {}

        def _capture(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return _fake_2xx(200)

        with patch.object(client._req, "urlopen", side_effect=_capture):
            client.set("k", 1, scope="global")
        assert seen["auth"] == "Bearer sekret"

    def test_connection_refused_returns_false(self):
        with patch.object(
            client._req, "urlopen", side_effect=urllib.error.URLError("Connection refused")
        ):
            assert client.set("k", True, scope="global") is False

    def test_timeout_returns_false(self):
        with patch.object(client._req, "urlopen", side_effect=TimeoutError("timed out")):
            assert client.set("k", True, scope="global") is False

    def test_http_error_returns_false_and_closes(self):
        err = urllib.error.HTTPError("url", 400, "Bad Request", hdrs=None, fp=None)
        closed = {"n": 0}
        _orig = err.close

        def _tracking_close():
            closed["n"] += 1
            _orig()

        err.close = _tracking_close  # type: ignore[method-assign]
        with patch.object(client._req, "urlopen", side_effect=err):
            assert client.set("k", True, scope="global") is False
        assert closed["n"] >= 1, "set() must close the caught HTTPError"

    def test_never_raises(self):
        with patch.object(client._req, "urlopen", side_effect=Exception("anything")):
            assert client.set("k", True, scope="global") is False


class TestHostClientDelete:
    def test_returns_true_on_2xx(self):
        with patch.object(client._req, "urlopen", return_value=_fake_2xx(200)):
            assert client.delete("k", scope="global") is True

    def test_issues_delete_method(self):
        seen: dict = {}

        def _capture(req, timeout=None):
            seen["method"] = req.get_method()
            seen["url"] = req.full_url
            return _fake_2xx(200)

        with patch.object(client._req, "urlopen", side_effect=_capture):
            client.delete("k", scope="project", directory="/p")
        assert seen["method"] == "DELETE"
        assert "/api/runtime-config/k" in seen["url"]
        assert "scope=project" in seen["url"]
        assert "directory=" in seen["url"]

    def test_daemon_down_returns_false(self):
        with patch.object(client._req, "urlopen", side_effect=urllib.error.URLError("refused")):
            assert client.delete("k", scope="global") is False

    def test_http_error_returns_false_and_closes(self):
        err = urllib.error.HTTPError("url", 404, "Not Found", hdrs=None, fp=None)
        try:
            with patch.object(client._req, "urlopen", side_effect=err):
                assert client.delete("k", scope="global") is False
        finally:
            err.close()
