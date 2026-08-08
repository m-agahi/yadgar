"""ADR-0217 exit criterion — directory enforcement, end-to-end, both directions.

This module replaces the ADR-0216 chain test. That file asserted the
``hook -> endpoint -> persist -> cache -> read`` chain for a trusted host-side
per-directory git fact, which ADR-0217 deletes as redundant with project identity
(a ``local/<basename>`` key already means "no git remote here").

What must NOT be lost with it is the CONSUMER half of that chain — the assertion
that the ``wiki_add`` MCP boundary still gates on the caller-supplied directory
alone. ADR-0216 believed directory enforcement depended on that git fact; ADR-0217
found the claim FALSE against the code (``_check_wiki_add_context`` only ever
tested ``(directory or "").strip()`` plus the enforcement flag). These two tests
are the standing proof of that, so it cannot quietly rot:

  1. A directory that is NOT a git repository still STORES. Nothing in the write
     path asks whether the path is a work-tree, and nothing may start.
  2. An EMPTY directory, with ``YADGAR_DIRECTORY_ENFORCEMENT`` on, is REJECTED
     with ``missing_directory``. Enforcement survived losing its supposed input.

The endpoint leg is kept too: ``/hooks/session-context`` must answer 200 for a
plain ``?directory=`` with no git facts of any kind attached — the shape the
SessionStart hook now sends.

Assertions are made at the MCP boundary (``stored`` / ``error``), not through a
drain, matching the predecessor file.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


def _make_client(token: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a TestClient for the session-context endpoint (mirrors the endpoint tests)."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", token)

    from yadgar.core import server as _server
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    asgi_app = _server.mcp_server.streamable_http_app()
    return TestClient(BearerAuthMiddleware(asgi_app), raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _engines(tmp_path_factory):
    from yadgar.core import server

    tmp_path = tmp_path_factory.mktemp("directory_chain")
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def test_nongit_directory_still_stores(tmp_path, monkeypatch, _unit_backend_harness):
    """A non-git directory STORES — the write path never asks about git-ness.

    ``tmp_path`` is a real directory that is emphatically not a work-tree. With
    directory enforcement ON it must still pass the boundary: the check is
    "was a directory supplied", not "is it a repo".
    """
    from yadgar.core import server

    monkeypatch.setenv("YADGAR_DIRECTORY_ENFORCEMENT", "true")

    # The endpoint leg: a plain ?directory= with no git facts attached answers 200.
    token = "dir-chain-nongit"
    client = _make_client(token, monkeypatch)
    resp = client.get(
        f"/hooks/session-context?directory={tmp_path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    result = server.wiki_add(
        title="directory chain page nongit",
        content="content written from a directory that is not a git work-tree",
        category="reference",
        directory=str(tmp_path),
    )
    assert result.get("error") is None, result
    assert result.get("stored") is True, result


def test_empty_directory_rejected_when_enforcement_on(monkeypatch, _unit_backend_harness):
    """An EMPTY directory is REJECTED ``missing_directory`` with enforcement ON.

    The other half of the ADR-0217 proof: enforcement is live, so the passing
    non-git case above is a real answer and not a disabled gate.
    """
    from yadgar.core import server

    monkeypatch.setenv("YADGAR_DIRECTORY_ENFORCEMENT", "true")

    result = server.wiki_add(
        title="directory chain page empty",
        content="content written with no directory context at all",
        category="reference",
        directory="",
    )
    assert result.get("stored") is False, result
    assert result.get("error") == "missing_directory", result
    assert result.get("field") == "directory", result
