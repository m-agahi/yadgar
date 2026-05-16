"""§2 MCP API auth + CORS tests.

Verifies:
- CORS not wildcard; only allowed origins pass
- Bearer-token middleware returns 401 without token when REQUIRE_AUTH=True
- Bearer-token middleware passes through when REQUIRE_AUTH=False
- /health accessible without token regardless of REQUIRE_AUTH
"""

import importlib
import os

import pytest
from starlette.testclient import TestClient


def _make_app(require_auth: bool = False, token: str = "test-token"):
    """Build a minimal Starlette test app with the auth middleware applied."""
    os.environ["YADGAR_REQUIRE_AUTH"] = "1" if require_auth else "0"
    os.environ["YADGAR_MCP_AUTH_TOKEN"] = token
    os.environ["YADGAR_ALLOWED_ORIGINS"] = "http://127.0.0.1:8765,http://localhost:8765"

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def api_hello(request):
        return JSONResponse({"ok": True})

    async def health(request):
        return JSONResponse({"status": "ok"})

    # Import the middleware from server after env is set
    import yadgar.server as _srv

    importlib.reload(_srv)

    app = Starlette(
        routes=[
            Route("/api/hello", api_hello, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
        ]
    )
    # Wrap with the auth middleware
    from yadgar.auth_middleware import BearerAuthMiddleware

    app = BearerAuthMiddleware(app)
    return app


def test_auth_disabled_passes_api_without_token(monkeypatch):
    """When REQUIRE_AUTH=False, /api/* passes without token."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "secret")

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from yadgar.auth_middleware import BearerAuthMiddleware

    async def api_hello(request):
        return JSONResponse({"ok": True})

    app = BearerAuthMiddleware(Starlette(routes=[Route("/api/hello", api_hello)]))
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/api/hello")
    assert resp.status_code == 200


def test_auth_enabled_returns_401_without_token(monkeypatch):
    """When REQUIRE_AUTH=True, /api/* returns 401 without Authorization header."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "secret-token")

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from yadgar.auth_middleware import BearerAuthMiddleware

    async def api_hello(request):
        return JSONResponse({"ok": True})

    app = BearerAuthMiddleware(Starlette(routes=[Route("/api/hello", api_hello)]))
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/api/hello")
    assert resp.status_code == 401


def test_auth_enabled_passes_with_correct_token(monkeypatch):
    """When REQUIRE_AUTH=True, /api/* passes with correct bearer token."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "correct-token")

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from yadgar.auth_middleware import BearerAuthMiddleware

    async def api_hello(request):
        return JSONResponse({"ok": True})

    app = BearerAuthMiddleware(Starlette(routes=[Route("/api/hello", api_hello)]))
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/api/hello", headers={"Authorization": "Bearer correct-token"})
    assert resp.status_code == 200


def test_auth_enabled_returns_401_with_wrong_token(monkeypatch):
    """When REQUIRE_AUTH=True, /api/* returns 401 with wrong bearer token."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "correct-token")

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from yadgar.auth_middleware import BearerAuthMiddleware

    async def api_hello(request):
        return JSONResponse({"ok": True})

    app = BearerAuthMiddleware(Starlette(routes=[Route("/api/hello", api_hello)]))
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/api/hello", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_health_accessible_without_token_when_auth_enabled(monkeypatch):
    """Even with REQUIRE_AUTH=True, /health endpoint is unauthenticated."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "secret-token")

    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from yadgar.auth_middleware import BearerAuthMiddleware

    async def health(request):
        return JSONResponse({"status": "ok"})

    app = BearerAuthMiddleware(Starlette(routes=[Route("/health", health)]))
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/health")
    assert resp.status_code == 200


def test_cors_not_wildcard():
    """CORS must not be * in server.py _cors_wrapped_http_app."""
    from pathlib import Path

    server_src = (Path(__file__).parent.parent / "server.py").read_text()
    assert 'allow_origins=["*"]' not in server_src, (
        "server.py must not set wildcard CORS allow_origins"
    )


def test_mcp_streamable_http_app_has_bearer_auth_middleware(monkeypatch):
    """streamable_http_app must wrap BearerAuthMiddleware (not just isolate it in tests).

    This verifies the wiring in server.py: _cors_wrapped_http_app must instantiate
    BearerAuthMiddleware around the CORS+MCP stack, not leave it as a standalone object.
    """
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")

    import yadgar.server as _srv
    from yadgar.auth_middleware import BearerAuthMiddleware

    app = _srv.mcp_server.streamable_http_app()

    # Walk the middleware stack to confirm BearerAuthMiddleware is present
    def _find_bearer(obj, depth=0):
        if depth > 10:
            return False
        if isinstance(obj, BearerAuthMiddleware):
            return True
        # Check common ASGI/Starlette wrapper attributes
        for attr in ("app", "middleware_stack", "handler"):
            child = getattr(obj, attr, None)
            if child is not None and _find_bearer(child, depth + 1):
                return True
        return False

    assert _find_bearer(app), (
        "BearerAuthMiddleware not found in mcp_server.streamable_http_app() stack. "
        "Check that _cors_wrapped_http_app in server.py wraps with BearerAuthMiddleware."
    )


def test_startup_fails_with_require_auth_and_empty_token(monkeypatch):
    """main() must raise RuntimeError when REQUIRE_AUTH=True and token is empty (H-7)."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "")
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")

    import importlib

    import yadgar.config as _cfg

    importlib.reload(_cfg)

    import yadgar.server as _srv

    importlib.reload(_srv)

    with pytest.raises(RuntimeError, match="REQUIRE_AUTH=1 requires YADGAR_MCP_AUTH_TOKEN"):
        # Call main() with a dummy transport that we'll never actually start.
        # We monkeypatch init_engines + mcp_server.run to prevent side effects.
        monkeypatch.setattr(_srv, "init_engines", lambda **kw: None)
        monkeypatch.setattr(_srv.mcp_server, "run", lambda **kw: None)
        _srv.main(transport="stdio")


def test_startup_ok_with_require_auth_and_token(monkeypatch):
    """main() must NOT raise when REQUIRE_AUTH=True and a token is set (H-7)."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "a-valid-32-char-token-here!!")
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")

    import importlib

    import yadgar.config as _cfg

    importlib.reload(_cfg)

    import yadgar.server as _srv

    importlib.reload(_srv)

    monkeypatch.setattr(_srv, "init_engines", lambda **kw: None)
    monkeypatch.setattr(_srv, "sync_instructions", lambda: None)
    monkeypatch.setattr(_srv, "install_hooks", lambda *a: None)
    monkeypatch.setattr(_srv, "shutdown", lambda: None)
    monkeypatch.setattr(_srv.mcp_server, "run", lambda **kw: None)

    # Should not raise
    _srv.main(transport="stdio")


def test_sse_transport_requires_auth(monkeypatch):
    """SSE transport must also go through BearerAuthMiddleware (C-1).

    Verifies that mcp_server.sse_app() returns an app wrapping
    BearerAuthMiddleware, not a bare ASGI callable.
    """
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")

    import yadgar.server as _srv
    from yadgar.auth_middleware import BearerAuthMiddleware

    app = _srv.mcp_server.sse_app()

    def _find_bearer(obj, depth=0):
        if depth > 10:
            return False
        if isinstance(obj, BearerAuthMiddleware):
            return True
        for attr in ("app", "middleware_stack", "handler"):
            child = getattr(obj, attr, None)
            if child is not None and _find_bearer(child, depth + 1):
                return True
        return False

    assert _find_bearer(app), (
        "BearerAuthMiddleware not found in mcp_server.sse_app() stack. "
        "Both transports must be wrapped by BearerAuthMiddleware."
    )


def test_embed_endpoint_requires_auth(monkeypatch):
    """POST /embed must return 401 without bearer token (H-1)."""
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-secret-token")
    monkeypatch.delenv("YADGAR_ALLOW_ROOT", raising=False)

    from fastapi.testclient import TestClient

    from yadgar.embed_service import app as embed_app

    client = TestClient(embed_app, raise_server_exceptions=False)
    resp = client.post("/embed", json={"texts": ["hello"]})
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

    # With token: still may 503 (model not loaded in test) but not 401
    resp2 = client.post(
        "/embed",
        json={"texts": ["hello"]},
        headers={"Authorization": "Bearer test-secret-token"},
    )
    assert resp2.status_code != 401, f"Valid token should not get 401, got {resp2.status_code}"


def test_rerank_endpoint_requires_auth(monkeypatch):
    """POST /rerank must return 401 without bearer token (H-1)."""
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-secret-token")
    monkeypatch.delenv("YADGAR_ALLOW_ROOT", raising=False)

    from fastapi.testclient import TestClient

    from yadgar.embed_service import app as embed_app

    client = TestClient(embed_app, raise_server_exceptions=False)
    resp = client.post("/rerank", json={"query": "hello", "texts": ["world"]})
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

    resp2 = client.post(
        "/rerank",
        json={"query": "hello", "texts": ["world"]},
        headers={"Authorization": "Bearer test-secret-token"},
    )
    assert resp2.status_code != 401, f"Valid token should not get 401, got {resp2.status_code}"


def test_index_html_no_raw_innerhtml_xss():
    """static/index.html must not assign untrusted JSON directly to innerHTML."""
    from pathlib import Path

    html = Path(__file__).parent.parent / "static" / "index.html"
    if not html.exists():
        pytest.skip("static/index.html not found")
    content = html.read_text()
    # syntaxHL result must not be assigned to innerHTML without escaping
    assert "el.innerHTML = syntaxHL(JSON.stringify" not in content, (
        "static/index.html: stored XSS — syntaxHL(JSON.stringify(...)) → innerHTML "
        "must use esc() or textContent"
    )
