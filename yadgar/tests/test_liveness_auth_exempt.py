"""/health/live must be auth-exempt (#74 salvage — fix #1).

The container P0 healthcheck `curl`s the liveness endpoint with no bearer token.
If /health/live were behind BearerAuthMiddleware it would return 401 → P0 kills
the core anyway, defeating the whole fix. /health/live MUST be in the exempt set
exactly like /health and /metrics.
"""

from __future__ import annotations

from yadgar.core.auth_middleware import _EXEMPT_PATHS, _is_protected


def test_liveness_path_is_auth_exempt():
    assert "/health/live" in _EXEMPT_PATHS, (
        "/health/live must be auth-exempt so the tokenless P0 curl reaches it"
    )


def test_liveness_path_not_protected():
    assert _is_protected("/health/live") is False
