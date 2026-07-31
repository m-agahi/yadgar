"""Regression tests for the backend /admin bearer-token gate.

Covers the security fix that scopes the ``YADGAR_ALLOW_ROOT`` auth bypass to
pytest-only: the flag must be *ignored* outside a test process, so setting it in
a real deployment (or leaking it via a shared env file) can never disable admin
auth on the loopback-reachable backend. Also asserts the constant-time compare
rejects a wrong token.

These call the dependency function directly (no TestClient) so they run without
the httpx2 test-transport dependency.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from yadgar.backend.embed_service.embed_service import _require_admin_token


def _creds(token: str | None) -> HTTPAuthorizationCredentials | None:
    if token is None:
        return None
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _call(credentials: HTTPAuthorizationCredentials | None) -> None:
    asyncio.run(_require_admin_token(credentials))


def test_allow_root_bypass_honoured_under_pytest(monkeypatch) -> None:
    """In-process (PYTEST_CURRENT_TEST present), ALLOW_ROOT=1 skips auth entirely."""
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
    # PYTEST_CURRENT_TEST is set by pytest during the test body — do not remove it.
    _call(None)  # must not raise


def test_allow_root_bypass_ignored_without_pytest_env(monkeypatch) -> None:
    """SECURITY REGRESSION: outside pytest, ALLOW_ROOT=1 must NOT disable auth.

    With no token configured the gate fails secure (500) even though ALLOW_ROOT
    is truthy — proving the escape hatch cannot open the door in production.

    ADR-0180 / task:0090: this was a 503 for months and that one wrong digit
    misled the fresh-VM diagnosis for weeks — 503 reads as "retry later" for what
    is a permanent server misconfiguration. 500 is the operator-error signal; see
    _require_admin_token's docstring for why not 401 and why not 424.
    """
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(HTTPException) as exc:
        _call(_creds("anything"))
    assert exc.value.status_code == 500, (
        "an unconfigured admin token is a permanent server misconfiguration; "
        "reporting it as 503 (transient unavailability) is the ADR-0180 defect"
    )


def test_allow_root_ignored_wrong_token_rejected(monkeypatch) -> None:
    """Outside pytest, ALLOW_ROOT=1 + a configured token still rejects a bad token."""
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "s3cret-token")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(HTTPException) as exc:
        _call(_creds("wrong-token"))
    assert exc.value.status_code == 401


def test_correct_token_accepted(monkeypatch) -> None:
    """A matching token passes (constant-time compare succeeds)."""
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "0")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "s3cret-token")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    _call(_creds("s3cret-token"))  # must not raise
