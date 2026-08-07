"""ADR-0216 exit criterion — the gitness chain, end-to-end, both directions.

ADR-0216 forbids splitting the ``hook -> endpoint -> persist -> cache -> read``
chain across cars: a partial edit leaves the endpoint sending a field the persist
layer no longer stores, or a cache keyed on a shape the reader no longer expects.
Five layers can each look locally sane while disagreeing, so a layer-local test
cannot catch a half-edit. This module is the shape that can.

One test per direction, each traversing the whole chain for real:

  1. HOOK      — ``_compute_git_facts`` returns gitness ALONE (not a tuple), and
                 the query params the hook builds carry ``gitness`` and NO
                 ``default_branch`` / ``branch``.
  2. ENDPOINT  — a real GET against ``/hooks/session-context`` with that param.
  3. PERSIST   — the durable ``_dir_branch_context`` memory row's JSON blob
                 contains ``gitness`` and NO ``default_branch`` key.
  4. CACHE+READ— ``_dir_branch.get_context(dir)`` returns ``{found, gitness}``
                 with no ``default_branch``.
  5. CONSUMER  — ``wiki_add(directory=<that dir>)`` with NO branch context of any
                 kind STORES at the MCP boundary.

Step 5 is the half that cannot pass on pre-ADR-0216 code. Pre-car, a git
directory with no ``branch``/``branch_hint`` hit the four-flow router's flow 2b
and was hard-rejected ``missing_branch``; the non-git direction was flow 3 and
already stored. Branch enforcement is therefore set ON explicitly — with it off,
the old router returned before ever consulting gitness and the assertion would be
vacuous in both directions.

The wiki_add assertion is made at the MCP boundary (``stored``), not through a
drain: the drainer's own branch reject is a separate car's deletion, and a
boundary ``stored: True`` from a git dir with no branch is already impossible on
pre-car code.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

_GIT_DIR = "/home/user/gitness-chain-git"
_NONGIT_DIR = "/home/user/gitness-chain-nongit"


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

    tmp_path = tmp_path_factory.mktemp("gitness_chain")
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


@pytest.fixture(autouse=True)
def _reset_dir_branch_cache():
    from yadgar.core.server.tools import _dir_branch

    _dir_branch._get_cache().clear()
    yield
    _dir_branch._get_cache().clear()


def _load_hook_module():
    """Import the SessionStart hook script by path (bypasses the __main__ guard)."""
    import importlib.util
    from pathlib import Path

    hook = Path(__file__).parent.parent.parent / "core" / "hooks" / "session-start-context.py"
    spec = importlib.util.spec_from_file_location("_session_start_hook_chain", hook)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hook_params(cwd: str, *, gitness: bool) -> dict:
    """Build the query params the hook sends, driving _compute_git_facts for real.

    Layer 1 of the chain. ``_compute_git_facts`` shells out to git; the git-ness
    of a synthetic path is not the thing under test here, so the subprocess is
    stubbed to the answer we want and the FACT UNDER TEST is the hook's own
    contract: a single bool (not a ``(gitness, default_branch)`` tuple) and a
    param dict with no branch keys.
    """
    from unittest.mock import MagicMock, patch

    hook_mod = _load_hook_module()

    def _fake_run(cmd, **_kw):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "true\n" if gitness else "false\n"
        return result

    with patch.object(hook_mod.subprocess, "run", side_effect=_fake_run):
        fact = hook_mod._compute_git_facts(cwd)

    # The hook returns ONE bool now, not a (gitness, default_branch) tuple.
    assert isinstance(fact, bool), f"_compute_git_facts must return a bool, got {fact!r}"
    assert fact is gitness

    params = {"directory": cwd, "gitness": "true" if fact else "false"}
    assert "default_branch" not in params
    assert "branch" not in params
    return params


def _persisted_blob(directory: str) -> dict:
    """Layer 3: read the durable ``_dir_branch_context`` memory row's raw JSON."""
    from yadgar.core import server

    rows = server._wiki._storage._q(
        "SELECT content FROM memory WHERE directory_context = $dir "
        "AND '_dir_branch_context' INSIDE tags LIMIT 1",
        {"dir": directory},
    )
    assert rows, f"no _dir_branch_context row persisted for {directory}"
    return json.loads(rows[0]["content"])


def _assert_chain(directory: str, *, gitness: bool, token: str, monkeypatch) -> dict:
    """Drive layers 1-5 for one direction, asserting each, and return the wiki_add result."""
    from yadgar.core import server
    from yadgar.core.server.tools import _dir_branch

    # Enforcement ON — otherwise the pre-car router short-circuited before the
    # gitness lookup and the wiki_add assertion below would be vacuous.
    monkeypatch.setenv("YADGAR_BRANCH_ENFORCEMENT", "true")
    monkeypatch.setenv("YADGAR_DIRECTORY_ENFORCEMENT", "true")

    # ── 1. HOOK ────────────────────────────────────────────────────────────────
    params = _hook_params(directory, gitness=gitness)

    # ── 2. ENDPOINT ────────────────────────────────────────────────────────────
    client = _make_client(token, monkeypatch)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    resp = client.get(
        f"/hooks/session-context?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    # ── 3. PERSIST ─────────────────────────────────────────────────────────────
    blob = _persisted_blob(directory)
    assert blob["gitness"] is gitness, blob
    assert "default_branch" not in blob, (
        f"the durable blob must not carry default_branch any more; got {blob!r}"
    )

    # ── 4. CACHE + READ ────────────────────────────────────────────────────────
    ctx = _dir_branch.get_context(directory)
    assert ctx == {"found": True, "gitness": gitness}, ctx

    # ── 5. CONSUMER ────────────────────────────────────────────────────────────
    # No branch, no branch_hint, no YADGAR_CI_BRANCH — the exact condition the
    # v5.42.3 guard rejected in a git directory.
    monkeypatch.delenv("YADGAR_CI_BRANCH", raising=False)
    result = server.wiki_add(
        title=f"gitness chain page {'git' if gitness else 'nongit'}",
        content="content written with no branch context of any kind",
        category="reference",
        directory=directory,
    )
    assert result.get("error") is None, result
    return result


def test_chain_nongit_directory(monkeypatch, _unit_backend_harness):
    """Non-git direction: gitness=false persists, reads back, and wiki_add stores."""
    result = _assert_chain(
        _NONGIT_DIR, gitness=False, token="chain-nongit", monkeypatch=monkeypatch
    )
    assert result.get("stored") is True, result


def test_chain_git_directory(monkeypatch, _unit_backend_harness):
    """Git direction: gitness=true persists, reads back, and wiki_add STILL stores.

    This is the assertion that fails on pre-ADR-0216 code — a git directory with
    no branch context was flow 2b, hard-rejected ``missing_branch``.
    """
    result = _assert_chain(_GIT_DIR, gitness=True, token="chain-git", monkeypatch=monkeypatch)
    assert result.get("stored") is True, result
