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

    def _fake_brief(directory, mode="catalog"):
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

_NUDGE_MARKER = "restore your task list"


# Car E (0047 spine train §16) refs:
#   - the task-list source of truth moved from the wiki page
#     '{project}-task-list' (page_type='task_list') to the SQL `task` ledger.
#   - the session-context nudge generator now reads from
#     `yadgar.core.server.tools.task.task_list(project_id=..., status=[...])`
#     (the Car D admin op surface), not from wiki pages.
#   - legacy wiki-page parsing is a fallback only when the Car D tools are
#     not importable; primary path is the ledger.
#
# The seed below installs BOTH surfaces so the test exercises the post-Car-E
# PRIMARY path. The wiki page is kept as a marker-only side effect so any
# remaining legacy-wiki test plumbing still finds the slug where it expects
# it.


def _parse_task_sections(content: str) -> list[dict]:
    """Parse a legacy `{project}-task-list` markdown body into ledger rows.

    Only open tasks (pending / in_progress) are returned — the nudge reads
    exactly those. The markdown schema is documented in the protocol template
    (Car E: the schema was retired, but the seed reproduces it verbatim so
    the fixture stays decoupled from any Car-D schema changes).
    """
    import re

    rows: list[dict] = []
    # Match `## task:<id>` headline (with optional `origin/` prefix, D11),
    # followed by `- key: value` bullets until the next `## ` or EOF.
    _section_re = re.compile(
        r"^##\s+task:(?:(\S+?)/)?(\S+)\s*\n((?:(?!\n## ).)*)", re.DOTALL | re.MULTILINE
    )
    _field_re = re.compile(r"-\s+(\w+):\s*(.*)")
    next_id = 1
    for m in _section_re.finditer(content):
        body = m.group(3)
        # First non-empty line of the body is the first bullet; pad the row
        # with sensible defaults so the nudge can render even when the page
        # omits one.
        fields = {"id": next_id, "title": "", "status": "pending"}
        for line in body.splitlines():
            fm = _field_re.match(line.strip())
            if not fm:
                continue
            key, value = fm.group(1).strip(), fm.group(2).strip()
            if key == "subject":
                fields["title"] = value
            elif key == "status":
                fields["status"] = value
        next_id += 1
        if fields["status"] in ("pending", "in_progress"):
            rows.append(fields)
    return rows


def _seed_task_list_page(
    directory: str,
    branch: str | None,
    content: str | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> None:
    """Seed the post-Car-E task-list surface so the nudge generator sees rows.

    Car E (0047 §16): the source of truth is the SQL `task` ledger. The
    test seeds the ledger by monkeypatching
    `yadgar.core.server.tools.task.task_list` to return a fixture list of
    parsed rows for the project's project_id. The wiki page is also written
    (legacy marker) so any remaining legacy-wiki test plumbing still resolves
    the slug.

    `content` overrides the default 1-open-task body. Pass None (default) to
    get the original single-pending-task seed used by existing callers.
    `monkeypatch` is required for the ledger stub; legacy callers that pass
    only the legacy kwargs can omit it (the legacy wiki page is written
    unconditionally, but the ledger stub is skipped).
    """
    from pathlib import Path

    from yadgar._shared.runtime.lifecycle import _get_storage

    project = Path(directory).name
    storage = _get_storage()
    assert storage is not None, "storage engine not initialised"
    if content is None:
        content = (
            f"## Meta\n- project: {project}\n- open: 1 · completed: 0\n\n"
            "## task:0001\n- subject: seed\n- status: pending\n"
        )

    # 1. Legacy wiki marker (NOT the source of truth on the primary path but
    # kept so any legacy-wiki test can still resolve the slug).
    storage.insert_wiki_page(
        {
            "slug": f"{project}-task-list",
            "title": f"{project} task list",
            "content": content,
            "tags": ["task-list"],
            "page_type": "task_list",
            "wiki_schema_version": 1,
            "directory_context": directory,
        },
        branch=branch,
    )

    # 2. Ledger stub — the PRIMARY path the nudge now reads. We install a
    # canned `task_list(project_id, status=...)` callable that returns the
    # parsed markdown rows. The http.py handler uses `asyncio.to_thread` so
    # a plain sync callable is fine.
    if monkeypatch is not None:
        _rows = _parse_task_sections(content)

        def _fake_task_list(project_id: str, status=None):
            return [r for r in _rows if r.get("status", "pending") in (status or [])]

        try:
            from yadgar.core.server.tools import task as _task_tools

            monkeypatch.setattr(_task_tools, "task_list", _fake_task_list)
        except ImportError:
            pass  # legacy-without-Car-D: the http.py handler falls back to wiki-parsing


def test_task_list_nudge_present_when_page_exists(tmp_path, monkeypatch):
    """Startup session-context CONTAINS the restore-nudge when the ledger has rows.

    Car E (0047 §16): the source of truth is the SQL `task` ledger. The seed
    writes a `{project}-task-list` legacy wiki marker AND stubs the ledger
    `task_list(project_id=..., status=...)` reader to return one open row.
    """
    token = "tl-present"
    from yadgar.core import server as _server

    # Seed both surfaces so the post-Car-E PRIMARY path (ledger) drives the
    # nudge; the wiki marker is the legacy fallback that should also resolve.
    _seed_task_list_page(str(tmp_path), branch=None, monkeypatch=monkeypatch)

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&source=startup",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert _NUDGE_MARKER in body["text"], (
        f"nudge must be present when the ledger has rows; got: {body['text']!r}"
    )
    # The nudge (post-Car-E) names the project_id + the restore mechanism.
    # project_id = basename(directory); the legacy wiki slug is gone from the
    # PRIMARY path — assert on project_id, not the wiki slug.
    assert tmp_path.name in body["text"], (
        f"project_id ({tmp_path.name}) must appear in the nudge; "
        f"the legacy wiki slug is no longer the source of truth"
    )
    assert "TaskCreate" in body["text"]


def test_task_list_nudge_absent_when_page_missing(tmp_path, monkeypatch):
    """No rows seeded → nudge ABSENT (the key R2 existence-check assertion)."""
    token = "tl-absent"
    from yadgar.core import server as _server

    # Monkeypatch the ledger reader so a CI ghost row from another test cannot
    # accidentally trip this assertion (task_list is module-level cached after
    # first import).
    try:
        from yadgar.core.server.tools import task as _task_tools

        monkeypatch.setattr(_task_tools, "task_list", lambda project_id, status=None: [])
    except ImportError:
        pass

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&source=startup",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert _NUDGE_MARKER not in body["text"], (
        f"nudge must be absent when no ledger rows exist; got: {body['text']!r}"
    )


def test_task_list_nudge_absent_on_compact(tmp_path, monkeypatch):
    """source=compact early-returns → nudge ABSENT even if the ledger has rows."""
    token = "tl-compact"
    from yadgar.core import server as _server

    _seed_task_list_page(str(tmp_path), branch=None, monkeypatch=monkeypatch)

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
    output, even when the ledger has rows. hook_subagent_start is a distinct
    handler reached by SubagentStart only — must not surface the main-thread
    nudge."""
    token = "tl-subagent"

    _seed_task_list_page(str(tmp_path), branch=None, monkeypatch=monkeypatch)

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


def test_task_list_nudge_absent_from_dispatch_prelude(tmp_path, monkeypatch):
    """Isolation lock: agent_dispatch_prelude output never carries the nudge.

    The prelude assembles recall + wiki_query context (dispatch_helper); it does
    NOT call hook_session_context / project_brief. Even with seeded ledger rows,
    the prelude must not surface the main-thread nudge string.
    """
    _seed_task_list_page(str(tmp_path), branch=None, monkeypatch=monkeypatch)

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
    rest of the render (no 500, catalog preserved).

    Car E (0047 §16): the existence check is the ledger ``task_list`` call
    (not the legacy wiki-page reader). The fail-open arm catches the raise
    in ``_task_list_restore_nudge`` and returns "" so the nudge is omitted
    while the catalog render still ships.
    """
    token = "tl-failopen"
    from yadgar.core import server as _server

    # Monkeypatch task_list to raise on every call — this is the post-Car-E
    # existence check. The handler must catch the raise and omit the nudge.
    def _raise(*_a, **_k):
        raise RuntimeError("ledger read down")

    try:
        from yadgar.core.server.tools import task as _task_tools

        monkeypatch.setattr(_task_tools, "task_list", _raise)
    except ImportError:
        # Without Car D tools, the legacy wiki path runs; not the subject of
        # this test.
        pytest.skip("Car D `task` tools not importable — legacy path tested elsewhere")

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
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


# ---------------------------------------------------------------------------
# Inline open-task summary (checkpoint-symmetric, v5.142.0)
# ---------------------------------------------------------------------------

_MIXED_CONTENT = """\
## Meta
- project: myproj
- open: 3 · completed: 1

## task:0001
- subject: Write failing tests
- status: in_progress
- description: TDD first pass

## task:0002
- subject: Implement inline summary
- status: pending

## task:0003
- subject: Done already
- status: completed

## task:0004
- subject: Another open task
- status: pending
"""

_ALL_COMPLETED_CONTENT = """\
## Meta
- project: myproj
- open: 0 · completed: 2

## task:0001
- subject: Old done task
- status: completed

## task:0002
- subject: Another done
- status: completed
"""


def _seed_many_open_tasks(directory: str, n: int) -> str:
    """Return content string with `n` pending tasks + 1 completed."""
    from pathlib import Path

    project = Path(directory).name
    lines = [f"## Meta\n- project: {project}\n- open: {n} · completed: 1\n"]
    for i in range(1, n + 1):
        lines.append(f"\n## task:{i:04d}\n- subject: task {i}\n- status: pending\n")
    lines.append(f"\n## task:{n + 1:04d}\n- subject: done task\n- status: completed\n")
    return "".join(lines)


def test_task_list_nudge_inlines_open_task_subjects(tmp_path, monkeypatch):
    """Render CONTAINS subjects + count for pending/in_progress tasks (v5.142.0).

    The nudge must inline a compact open-task summary — not just note row
    existence — mirroring how the checkpoint hint inlines task + timestamp.
    """
    token = "tl-inline"
    from pathlib import Path

    from yadgar.core import server as _server

    project = Path(str(tmp_path)).name
    content = _MIXED_CONTENT.replace("myproj", project)
    _seed_task_list_page(str(tmp_path), branch=None, content=content, monkeypatch=monkeypatch)

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&source=startup",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    text = resp.json()["text"]

    # Header must include open count. _MIXED_CONTENT has 3 open tasks
    # (1 in_progress + 2 pending) and 1 completed.
    assert "3 open task" in text, f"expected open task count in render; got: {text!r}"
    # Subjects for open tasks must appear inline.
    assert "Write failing tests" in text, f"expected subject 'Write failing tests'; got: {text!r}"
    assert "Implement inline summary" in text, (
        f"expected subject 'Implement inline summary'; got: {text!r}"
    )
    # Car E: TaskCreate instruction still hoists; the legacy wiki slug is gone
    # from the PRIMARY render path (ledger-keyed).
    assert "TaskCreate" in text
    # project_id (basename) appears in the nudge header.
    assert project in text, f"project_id ({project}) must appear in the nudge; got: {text!r}"
    # v5.149 (Option B): forcing form + hoisted FIRST so it is not buried under the
    # project-brief catalog (the advisory tail nudge was ignored).
    assert "ACTION REQUIRED" in text, f"expected forcing nudge; got: {text!r}"
    assert text.lstrip().startswith("[yadgar] ACTION REQUIRED"), (
        f"task-restore nudge must lead the render (first), not be appended; got: {text[:120]!r}"
    )


def test_task_list_nudge_excludes_completed_tasks(tmp_path, monkeypatch):
    """Completed tasks MUST NOT appear in the inline summary (v5.142.0)."""
    token = "tl-excl-done"
    from pathlib import Path

    from yadgar.core import server as _server

    project = Path(str(tmp_path)).name
    content = _MIXED_CONTENT.replace("myproj", project)
    _seed_task_list_page(str(tmp_path), branch=None, content=content, monkeypatch=monkeypatch)

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&source=startup",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    text = resp.json()["text"]
    assert "Done already" not in text, (
        f"completed task subject must be excluded from inline summary; got: {text!r}"
    )


def test_task_list_nudge_caps_at_12_open_tasks(tmp_path, monkeypatch):
    """When >12 open tasks exist, render shows 12 + '…and N more' (v5.142.0)."""
    token = "tl-cap12"
    from yadgar.core import server as _server

    n = 15
    content = _seed_many_open_tasks(str(tmp_path), n)
    _seed_task_list_page(str(tmp_path), branch=None, content=content, monkeypatch=monkeypatch)

    with patch.object(_server, "project_brief", return_value=_brief_stub("# CATALOG BODY")):
        client = _make_client(token, monkeypatch)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}&source=startup",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    text = resp.json()["text"]

    # Must show exactly 12 tasks (subjects task 1–12) and a "…and 3 more" tail.
    assert "task 12" in text, f"expected 12th task to appear; got: {text!r}"
    assert "task 13" not in text, f"task 13 must be hidden behind the cap; got: {text!r}"
    assert "and 3 more" in text, f"expected '…and 3 more' overflow marker; got: {text!r}"
