"""Car C2 of the 0047 PR-40 remediation train — killing the third identity scheme.

``http.py`` built a project identity as ``Path(directory).name`` — the basename
``"yadgar"`` — and fed it straight to ``task_list(project_id=…)``. That is a
LATENT BUG today, independent of the migration: two checkouts of different
repos that share a basename address the same ledger rows, and no checkout of
``m-agahi/yadgar`` addresses rows written under the real key.

ADR-0227 forbids core-server from deriving anything, so the correct value can
only arrive from the host-side mint as an explicit query parameter. These tests
pin: the parameter is read, it reaches the ledger nudge verbatim, an absent
parameter yields NO guessed value, and the minted key is persisted into the
always-injected ``current_project`` memory block so it survives compaction.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

_TEMPLATE = (
    Path(__file__).parent.parent.parent
    / "core"
    / "hooks"
    / "templates"
    / "stop_checkpoint_prompt.md"
)


def _make_client(token: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", token)

    from yadgar.core import server as _server
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    asgi_app = _server.mcp_server.streamable_http_app()
    return TestClient(BearerAuthMiddleware(asgi_app), raise_server_exceptions=False)


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("session_context_project_param")
    from yadgar.core import server

    server.init_engines(db_path=str(tmp_path / "test.db"), embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── the endpoint reads ``project`` and hands it to the ledger nudge ───────


def test_session_context_forwards_project_to_the_task_nudge(tmp_path, monkeypatch):
    """``?project=owner/repo`` reaches ``_task_list_restore_nudge`` verbatim."""
    from yadgar.core.server import http as _http

    seen: dict = {}

    async def _spy(directory, project=""):
        seen["directory"] = directory
        seen["project"] = project
        return ""

    token = "tok-c2-a"
    client = _make_client(token, monkeypatch)
    with patch.object(_http, "_task_list_restore_nudge", _spy):
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&project=m-agahi%2Fyadgar",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert seen.get("project") == "m-agahi/yadgar", (
        f"the minted project_id must reach the ledger nudge; got {seen!r}"
    )


def test_session_context_without_project_passes_no_guess(tmp_path, monkeypatch):
    """No ``project=`` → the nudge gets an empty string, never a basename.

    ADR-0227: never defaulted, never inferred, never silently substituted.
    """
    from yadgar.core.server import http as _http

    seen: dict = {}

    async def _spy(directory, project=""):
        seen["project"] = project
        return ""

    token = "tok-c2-b"
    client = _make_client(token, monkeypatch)
    with patch.object(_http, "_task_list_restore_nudge", _spy):
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert seen.get("project") == "", f"absent project must stay absent; got {seen!r}"
    assert seen.get("project") != Path(str(tmp_path)).name


# ── the nudge itself no longer derives from the path ──────────────────────


def test_task_nudge_uses_the_supplied_project_id_for_the_ledger_read(tmp_path):
    """``task_list`` is called with the minted key, not ``Path(directory).name``."""
    from yadgar.core.server import http as _http

    calls: dict = {}

    def _fake_task_list(project_id, status=None, **kw):
        calls["project_id"] = project_id
        return []

    with patch("yadgar.core.server.tools.task.task_list", _fake_task_list):
        asyncio.run(_http._task_list_restore_nudge(str(tmp_path), "m-agahi/yadgar"))

    assert calls.get("project_id") == "m-agahi/yadgar", (
        f"the ledger read must key on the minted project_id; got {calls!r}"
    )


def test_task_nudge_returns_empty_when_no_project_supplied(tmp_path):
    """No identity → no project-scoped read at all. It must NOT fall back."""
    from yadgar.core.server import http as _http

    calls: dict = {}

    def _fake_task_list(project_id, status=None, **kw):
        calls["project_id"] = project_id
        return [{"id": 1, "title": "leaked", "status": "pending"}]

    with patch("yadgar.core.server.tools.task.task_list", _fake_task_list):
        nudge, rows = asyncio.run(_http._task_list_restore_nudge(str(tmp_path), ""))

    assert nudge == "", f"a missing identity must produce no nudge; got {nudge!r}"
    # Car C: the rows feed the on-disk seeder. Leaking another project's rows
    # here would write them into this session's harness task list.
    assert rows == [], f"a missing identity must produce no rows to seed; got {rows!r}"
    assert "project_id" not in calls, (
        f"the ledger must not be read at all without an identity; got {calls!r}"
    )


def test_http_module_no_longer_derives_a_project_from_the_path():
    """Source-level: the ``Path(directory).name`` identity scheme is gone.

    Kept source-level because a behavioural test only covers the inputs it
    happens to drive — this fails even if a reintroduced fallback sits on an
    arm no test reaches.
    """
    import yadgar.core.server.http as _http

    src = Path(_http.__file__).read_text(encoding="utf-8")
    nudge_start = src.index("async def _task_list_restore_nudge")
    nudge_end = src.index("def _code_graph_suggest_line", nudge_start)
    body = src[nudge_start:nudge_end]
    assert "_Path(directory).name" not in body, (
        "the basename identity scheme must not survive in _task_list_restore_nudge"
    )


# ── the value survives compaction via the always-injected block ───────────


def test_session_context_writes_the_current_project_block(tmp_path, monkeypatch):
    """The minted key lands in the always-injected ``current_project`` block.

    The banner is a one-shot line in the context window; compaction eats it.
    Memory blocks are re-injected on every session-context render, so the block
    is what makes the identity durable within a long session.
    """
    from yadgar.core.server import http as _http

    written: dict = {}

    def _spy(directory, project):
        written["directory"] = directory
        written["project"] = project

    token = "tok-c2-c"
    client = _make_client(token, monkeypatch)
    with patch.object(_http, "_upsert_current_project_block", _spy):
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&project=m-agahi%2Fyadgar",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert written.get("project") == "m-agahi/yadgar", (
        f"the minted project_id must be persisted to the block; got {written!r}"
    )


def test_current_project_block_upsert_is_fail_open(tmp_path):
    """A block-write failure must never break session start."""
    from yadgar.core.server import http as _http

    with patch("yadgar.core.server.tools.blocks.block_update", side_effect=RuntimeError("db down")):
        with patch(
            "yadgar.core.server.tools.blocks.block_create", side_effect=RuntimeError("db down")
        ):
            _http._upsert_current_project_block(str(tmp_path), "m-agahi/yadgar")  # no raise


# ── the stop-hook template's third identity scheme ────────────────────────


def test_stop_template_project_is_not_the_basename():
    """``{project}`` in the checkpoint protocol must be the minted project_id.

    The template feeds ``{project}`` to ``task_list(project_id=…)`` and
    ``task_write(project_id=…)`` — ledger identity, not a display name. Defining
    it as "basename of {directory}" made every checkpoint write to the wrong
    namespace.
    """
    import re

    text = _TEMPLATE.read_text(encoding="utf-8")
    header = text.split("-->", 1)[0]
    assert not re.search(r"\{project\}\s*=\s*basename", header), (
        "the stop-hook template still DEFINES {project} as a basename"
    )
    assert re.search(r"\{project\}\s*=.*project_id", header), (
        "the template header must define {project} in terms of the minted project_id"
    )
