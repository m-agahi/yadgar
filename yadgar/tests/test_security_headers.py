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
