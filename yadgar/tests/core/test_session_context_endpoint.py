"""Tests for §28 /hooks/session-context endpoint (project_brief pipe).

TDD — written BEFORE the implementation.
Covers:
- Endpoint requires bearer auth (401 without token)
- Returns markdown body from project_brief._render
- Integrates project_brief catalog payload
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Test app setup
# ---------------------------------------------------------------------------


def _make_client(token: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a TestClient for the session-context endpoint."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", token)

    from yadgar.core import server as _server
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    # Use streamable_http_app() to get the actual callable ASGI app
    asgi_app = _server.mcp_server.streamable_http_app()
    return TestClient(BearerAuthMiddleware(asgi_app), raise_server_exceptions=False)


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("session_context_endpoint")
    from yadgar.core import server

    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


def test_session_context_requires_bearer_token(tmp_path, monkeypatch):
    """/hooks/session-context returns 401 without Authorization header."""
    client = _make_client("secret-token", monkeypatch)
    resp = client.get(f"/hooks/session-context?directory={tmp_path}")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"


def test_session_context_accepts_valid_bearer(tmp_path, monkeypatch):
    """/hooks/session-context returns 200 with valid bearer token."""
    token = "valid-token-xyz"
    client = _make_client(token, monkeypatch)
    resp = client.get(
        f"/hooks/session-context?directory={tmp_path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


def test_session_context_rejects_wrong_token(tmp_path, monkeypatch):
    """/hooks/session-context returns 401 with wrong bearer token."""
    client = _make_client("correct-token", monkeypatch)
    resp = client.get(
        f"/hooks/session-context?directory={tmp_path}",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Response body — markdown from project_brief
# ---------------------------------------------------------------------------


def test_session_context_returns_text_field(tmp_path, monkeypatch):
    """Response JSON must contain a 'text' field."""
    token = "tok"
    client = _make_client(token, monkeypatch)
    resp = client.get(
        f"/hooks/session-context?directory={tmp_path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "text" in body, f"Response missing 'text' field: {body}"


def test_session_context_text_is_string(tmp_path, monkeypatch):
    """'text' field in response is a string."""
    token = "tok2"
    client = _make_client(token, monkeypatch)
    resp = client.get(
        f"/hooks/session-context?directory={tmp_path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["text"], str)


def test_session_context_integrates_project_brief(tmp_path, monkeypatch):
    """Endpoint calls project_brief and uses its _render field as text."""
    token = "tok3"

    mock_brief = {
        "_render": "# My Project\n\nTest render content.",
        "project": "yadgar",
        "branch": "master",
        "stale_wiki_count": 0,
        "init_memory_present": False,
        "active_work_present": False,
        "top_anchors": [],
        "recent_episode_count": 0,
    }

    from yadgar.core import server as _server

    with patch.object(_server, "project_brief", return_value=mock_brief) as mock_pb:
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    # v5.7.9: response text now includes a source-aware prefix line before _render.
    # Assert the render content is present rather than exact equality.
    assert "# My Project\n\nTest render content." in body["text"], (
        f"Response must include _render content; got: {body['text']!r}"
    )
    mock_pb.assert_called_once()


def test_session_context_uses_directory_param(tmp_path, monkeypatch):
    """Endpoint passes directory query param to project_brief."""
    token = "tok4"
    from yadgar.core import server as _server

    captured_dir = {}

    def _fake_brief(directory, mode="catalog", branch_hint=None):
        captured_dir["dir"] = directory
        return {
            "_render": f"# Project at {directory}",
            "project": "test",
            "branch": "master",
            "stale_wiki_count": 0,
            "init_memory_present": False,
            "active_work_present": False,
            "top_anchors": [],
            "recent_episode_count": 0,
        }

    with patch.object(_server, "project_brief", side_effect=_fake_brief):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert captured_dir["dir"] == str(tmp_path)


def test_session_context_graceful_on_storage_error(tmp_path, monkeypatch):
    """Endpoint returns 200 with empty text if project_brief fails."""
    token = "tok5"
    from yadgar.core import server as _server

    with patch.object(_server, "project_brief", side_effect=RuntimeError("DB down")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["text"], str)


# ---------------------------------------------------------------------------
# Dedup — double catalog injection on source=compact
# ---------------------------------------------------------------------------


def _brief_stub(render_text: str):
    return {
        "_render": render_text,
        "project": "yadgar",
        "branch": "master",
        "stale_wiki_count": 0,
        "init_memory_present": False,
        "active_work_present": False,
        "top_anchors": [],
        "recent_episode_count": 0,
    }


def test_session_context_compact_suppresses_catalog(tmp_path, monkeypatch):
    """On source=compact the /hooks/post-compact handler already restores the
    catalog via restore(). session-context must NOT re-inject the project_brief
    _render catalog (~500-tok duplicate). Expect empty text."""
    token = "tokdedup1"
    from yadgar.core import server as _server

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&source=compact",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    # Catalog (project_brief _render) must be gone — restore() already injected it.
    assert "CATALOG BODY" not in body["text"], (
        f"compact source must not re-inject the catalog; got: {body['text']!r}"
    )
    # But the v5.7.9 one-line compaction note is preserved (restore() does not
    # emit it, so it is not a duplicate).
    assert "compact" in body["text"].lower(), (
        f"compact source must preserve the v5.7.9 compaction note; got: {body['text']!r}"
    )


def test_session_context_non_compact_still_renders_catalog(tmp_path, monkeypatch):
    """Regression guard: non-compact sources (startup/clear/resume) MUST still
    receive the project_brief catalog. The dedup guard is compact-only."""
    token = "tokdedup2"
    from yadgar.core import server as _server

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&source=startup",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "CATALOG BODY" in body["text"], (
        f"non-compact source must still render the catalog; got: {body['text']!r}"
    )


# ---------------------------------------------------------------------------
# Task-list mirror restore-nudge (existence-checked, main-thread-only)
# ---------------------------------------------------------------------------

_NUDGE_MARKER = "Saved task list found"


def _seed_task_list_page(directory: str, branch: str | None) -> None:
    """Seed a <project>-task-list wiki page the way Edit 1 (the stop-hook step)
    writes it: page_type='task_list', scoped to `directory`, branch=`branch`.
    """
    from pathlib import Path

    from yadgar._shared.runtime.lifecycle import _get_storage

    project = Path(directory).name
    storage = _get_storage()
    assert storage is not None, "storage engine not initialised"
    storage.insert_wiki_page(
        {
            "slug": f"{project}-task-list",
            "title": f"{project} task list",
            "content": f"## Meta\n- project: {project}\n- open: 1 · completed: 0\n\n"
            "## task:0001\n- subject: seed\n- status: pending\n",
            "tags": ["task-list"],
            "page_type": "task_list",
            "wiki_schema_version": 1,
            "directory_context": directory,
        },
        branch=branch,
    )


def test_task_list_nudge_present_when_page_exists(tmp_path, monkeypatch):
    """Startup session-context CONTAINS the restore-nudge when the page exists.

    The stop-hook step writes the task-list page CANONICALLY (no branch_hint →
    branch=None slot) so it is reachable from any caller branch via §25 step-2
    (dir + branch IS NULL). Seed it that way and assert the nudge fires.
    """
    token = "tl-present"
    from yadgar.core import server as _server

    # Seed with branch=None (canonical) so §25 step-2 (dir + branch IS NULL)
    # resolves for any caller branch — the robust read path.
    _seed_task_list_page(str(tmp_path), branch=None)

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&source=startup",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert _NUDGE_MARKER in body["text"], (
        f"nudge must be present when the task-list page exists; got: {body['text']!r}"
    )
    # The nudge must name the slug + the restore mechanism.
    assert f"{tmp_path.name}-task-list" in body["text"]
    assert "TaskCreate" in body["text"]


def test_task_list_nudge_absent_for_default_branch_pinned_row(tmp_path, monkeypatch):
    """REGRESSION TRAP (memory 531352 / ADR-log branch-pin bug class).

    The task-list page MUST be written canonically (branch=None). If a future
    edit reverts the template to write it with branch_hint="{default_branch}"
    (branch='master' row), it becomes UNREACHABLE from any feature-branch
    session: the endpoint resolves the page under the caller's CURRENT branch,
    and §25 (dir+branch → dir+NULL → global) never matches a master-pinned row
    when the caller is on a feature branch.

    Here we seed the page the WRONG (default-branch-pinned) way and query with a
    feature branch — the nudge MUST be ABSENT, proving the master-pin is a dead
    write for the common feature-branch case. Anyone who re-adds branch_hint to
    the template Step 4c write will turn this red.
    """
    token = "tl-masterpin"
    from yadgar.core import server as _server

    _seed_task_list_page(str(tmp_path), branch="master")

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&source=startup&branch=feat/some-work",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert _NUDGE_MARKER not in body["text"], (
        "a default-branch-pinned task-list row must be unreachable from a "
        "feature-branch session — the template MUST write canonically "
        f"(branch=None). Got: {body['text']!r}"
    )


def test_task_list_nudge_absent_when_page_missing(tmp_path, monkeypatch):
    """No page seeded → nudge ABSENT (the key R2 existence-check assertion)."""
    token = "tl-absent"
    from yadgar.core import server as _server

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&source=startup",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert _NUDGE_MARKER not in body["text"], (
        f"nudge must be absent when no task-list page exists; got: {body['text']!r}"
    )


def test_task_list_nudge_absent_on_compact(tmp_path, monkeypatch):
    """source=compact early-returns → nudge ABSENT even if the page exists."""
    token = "tl-compact"
    from yadgar.core import server as _server

    _seed_task_list_page(str(tmp_path), branch=None)

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&source=compact",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert _NUDGE_MARKER not in body["text"], (
        f"compact source must not inject the nudge; got: {body['text']!r}"
    )


def test_task_list_nudge_absent_from_subagent_start(tmp_path, monkeypatch):
    """Isolation lock: the nudge NEVER appears in the subagent-start endpoint
    output, even when the page exists. hook_subagent_start is a distinct handler
    reached by SubagentStart only — it must not surface the main-thread nudge."""
    token = "tl-subagent"

    _seed_task_list_page(str(tmp_path), branch=None)

    client = _make_client(token, monkeypatch)
    resp = client.post(
        f"/hooks/subagent-start?agent_type=general-purpose&cwd={tmp_path}",
        json={"description": "restore the task list", "cwd": str(tmp_path)},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert _NUDGE_MARKER not in body["text"], (
        f"subagent-start must never surface the task-list nudge; got: {body['text']!r}"
    )


def test_task_list_nudge_absent_from_dispatch_prelude(tmp_path):
    """Isolation lock: agent_dispatch_prelude output never carries the nudge.

    The prelude assembles recall + wiki_query context (dispatch_helper); it does
    NOT call hook_session_context / project_brief. Even with a seeded page, the
    prelude must not surface the main-thread nudge string.
    """
    _seed_task_list_page(str(tmp_path), branch=None)

    from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

    out = agent_dispatch_prelude(
        pattern="",
        task_topic="restore the task list",
        directory=str(tmp_path),
        include_context=True,
    )
    text = out if isinstance(out, str) else str(out)
    assert _NUDGE_MARKER not in text, (
        f"agent_dispatch_prelude must not surface the task-list nudge; got: {text[:400]!r}"
    )


def test_task_list_nudge_fail_open_on_existence_check_error(tmp_path, monkeypatch):
    """Fail-open: if the existence check raises, the endpoint still returns the
    rest of the render (no 500, catalog preserved)."""
    token = "tl-failopen"
    from yadgar._shared.runtime import lifecycle as _lifecycle
    from yadgar.core import server as _server

    _seed_task_list_page(str(tmp_path), branch=None)

    real_storage = _lifecycle._get_storage()

    class _Boom:
        def __getattr__(self, name):
            if name == "get_wiki_page_by_slug_directory_branch":

                def _raise(*_a, **_k):
                    raise RuntimeError("existence check down")

                return _raise
            return getattr(real_storage, name)

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        with patch.object(_lifecycle, "_get_storage", return_value=_Boom()):
            client = _make_client(token, monkeypatch)
            resp = client.get(
                f"/hooks/session-context?directory={tmp_path}&source=startup",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert resp.status_code == 200
    body = resp.json()
    # Fail-open: nudge omitted but the catalog render survives.
    assert _NUDGE_MARKER not in body["text"]
    assert "CATALOG BODY" in body["text"]
