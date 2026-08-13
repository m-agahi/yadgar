"""Phase 4 hook tests — block-reflect PostToolUse + SessionStart inject (v5.35.1).

TDD: written before implementation.

Tests:
  A. blocks_render.render_blocks_section — shared DRY helper
     1. render_blocks_section empty list returns ""
     2. render_blocks_section project blocks rendered correctly
     3. render_blocks_section global blocks rendered correctly
     4. render_blocks_section both scopes rendered

  B. __init__.py exports — Phase 3 miss
     5. block_replace importable from yadgar.server.tools
     6. block_append importable from yadgar.server.tools

  C. /hooks/block-reflect endpoint
     7. GET /hooks/block-reflect returns {"text": str}
     8. block-reflect text contains block names when blocks exist

  D. hook_runner.py block-reflect handler
     9. hook_block_reflect registered in _HOOKS dict
    10. hook_block_reflect non-matching tool emits no output
    11. hook_block_reflect matching tool calls endpoint + prints text

  E. SessionStart block injection
    12. session-context endpoint prepends blocks to render text

  F. install_hooks PostToolUse wiring
    13. install_hooks PostToolUse has two entries: post-tool-capture + block-reflect
    14. block-reflect entry has correct matcher pattern
"""

from __future__ import annotations

import io
import json

import pytest

from yadgar.core import server

# R3 Car 3a: block_create forwards to the backend /admin endpoint. Route
# _forward_admin → run_admin_op directly (no HTTP) against the test's _st storage.
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

_PROJ_DIR = "/home/test/project_ph4"


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("blocks_hooks_phase4")
    server.init_engines(
        db_path=str(tmp_path / "ph4_server_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


# ---------------------------------------------------------------------------
# A. blocks_render.render_blocks_section — shared DRY helper
# ---------------------------------------------------------------------------


class TestBlocksRenderHelper:
    """render_blocks_section is a free function in yadgar.blocks_render."""

    def test_render_empty_returns_empty_string(self) -> None:
        from yadgar._shared.blocks_render import render_blocks_section

        result = render_blocks_section([], "/any/dir")
        assert result == ""

    def test_render_project_blocks(self) -> None:
        from yadgar._shared.blocks_render import render_blocks_section

        blocks = [
            {"scope": "project", "name": "current_task", "content": "Build Phase 4"},
        ]
        result = render_blocks_section(blocks, _PROJ_DIR)
        assert "current_task" in result
        assert "Build Phase 4" in result
        assert "Project blocks" in result

    def test_render_global_blocks(self) -> None:
        from yadgar._shared.blocks_render import render_blocks_section

        blocks = [
            {"scope": "global", "name": "rules", "content": "No terraform"},
        ]
        result = render_blocks_section(blocks, _PROJ_DIR)
        assert "rules" in result
        assert "No terraform" in result
        assert "Global blocks" in result

    def test_render_both_scopes(self) -> None:
        from yadgar._shared.blocks_render import render_blocks_section

        blocks = [
            {"scope": "global", "name": "rules", "content": "global rule"},
            {"scope": "project", "name": "task", "content": "project task"},
        ]
        result = render_blocks_section(blocks, _PROJ_DIR)
        assert "Global blocks" in result
        assert "Project blocks" in result
        assert "rules" in result
        assert "task" in result


# ---------------------------------------------------------------------------
# B. __init__.py exports — Phase 3 miss
# ---------------------------------------------------------------------------


class TestPhase3ExportsPresent:
    """block_replace and block_append must be importable from yadgar.server.tools."""

    def test_block_replace_exported(self) -> None:
        from yadgar.core.server import tools  # noqa: F401
        from yadgar.core.server.tools import block_replace

        assert callable(block_replace)

    def test_block_append_exported(self) -> None:
        from yadgar.core.server.tools import block_append

        assert callable(block_append)


# ---------------------------------------------------------------------------
# C. /hooks/block-reflect endpoint
# ---------------------------------------------------------------------------


def _make_block_reflect_client():
    """Build minimal Starlette test app for /hooks/block-reflect."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from yadgar.core.server.http import hook_block_reflect

    app = Starlette(routes=[Route("/hooks/block-reflect", hook_block_reflect, methods=["GET"])])
    return TestClient(app)


class TestBlockReflectEndpoint:
    """GET /hooks/block-reflect must return {"text": str}."""

    def test_endpoint_returns_text_key(self, tmp_path) -> None:
        """With no blocks, response has text key (possibly empty)."""
        client = _make_block_reflect_client()
        resp = client.get("/hooks/block-reflect", params={"directory": str(tmp_path)})
        assert resp.status_code == 200
        data = resp.json()
        assert "text" in data
        assert isinstance(data["text"], str)

    def test_endpoint_text_contains_blocks(self) -> None:
        """With project blocks seeded, block-reflect returns block content."""
        from yadgar.core.server.tools.blocks import block_create

        block_create(
            name="reflect_test",
            content="I am reflected",
            scope="project",
            directory=_PROJ_DIR,
        )

        client = _make_block_reflect_client()
        resp = client.get("/hooks/block-reflect", params={"directory": _PROJ_DIR})
        assert resp.status_code == 200
        data = resp.json()
        assert "reflect_test" in data["text"] or "I am reflected" in data["text"]


# ---------------------------------------------------------------------------
# C2. Car 8 — the server must actually USE ?project=, not just accept it
# ---------------------------------------------------------------------------

_SCOPED_PROJECT = "car8/scoped-project"
_SCOPED_DIR = "/home/test/project_ph4_car8"


class TestBlockReflectProjectScoping:
    """Car 2's client mints ?project=; before Car 8 the server dropped it
    (http.py read only `directory`, never `project`). These prove the fix:
    a block whose `directory` column does NOT match the caller's `directory`
    query param must still surface when `project` matches — i.e. the request
    reaches storage.list_blocks's project_id arm, not just the legacy
    directory arm.
    """

    def test_project_param_finds_block_directory_alone_would_miss(self) -> None:
        from yadgar.core.server.tools.blocks import block_create

        block_create(
            name="car8_reflect",
            content="found via project_id",
            scope="project",
            directory=_SCOPED_DIR,
            project=_SCOPED_PROJECT,
        )

        client = _make_block_reflect_client()
        # Deliberately send a directory that does NOT match the block's
        # stored directory — only `project` can find it.
        resp = client.get(
            "/hooks/block-reflect",
            params={"directory": "/some/unrelated/dir", "project": _SCOPED_PROJECT},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "car8_reflect" in data["text"] or "found via project_id" in data["text"], (
            f"?project= was not forwarded to storage.list_blocks: {data['text']!r}"
        )
        # render_blocks_section's 2nd arg is presentation-only (labels the
        # "Project blocks" header). Pin that Car 8 prefers the resolved
        # project identity over the (here mismatched) directory for that
        # label, rather than leaving it an untested side effect.
        assert _SCOPED_PROJECT in data["text"], (
            f"Project-blocks header did not use the resolved project identity: {data['text']!r}"
        )

    def test_missing_project_does_not_raise_or_500(self) -> None:
        """POLICY: block-reflect never raises on a directory-without-project
        request (unlike hook_project_id's hard-raise for fuzzy recall) — the
        legacy directory-only arm is an exact match, not a leak vector."""
        client = _make_block_reflect_client()
        resp = client.get("/hooks/block-reflect", params={"directory": _PROJ_DIR})
        assert resp.status_code == 200
        assert isinstance(resp.json().get("text"), str)

    def test_no_directory_or_project_does_not_use_container_cwd(self, monkeypatch) -> None:
        """v5.65 Fix D precedent: a missing directory must never fall back to
        os.getcwd() — the daemon's cwd is the CONTAINER's, not the caller's
        tree. Guards the next caller whose hook script omits `directory`.

        A non-raising spy (not a raising one): the unfixed handler evaluates
        `os.getcwd()` as a bare default-argument expression outside any
        try/except, so a raising spy escapes into Starlette/anyio's request
        handling and takes the whole test worker down with it rather than
        surfacing as a clean assertion failure.
        """
        import yadgar.core.server.http as http_mod

        real_getcwd = http_mod.os.getcwd
        calls: list[None] = []

        def _spy():
            calls.append(None)
            return real_getcwd()

        monkeypatch.setattr(http_mod.os, "getcwd", _spy)

        client = _make_block_reflect_client()
        resp = client.get("/hooks/block-reflect")
        assert resp.status_code == 200
        assert isinstance(resp.json().get("text"), str)
        assert not calls, (
            "hook_block_reflect called os.getcwd() — that resolves to the "
            "CONTAINER's cwd, never the caller's tree (v5.65 Fix D)"
        )


# ---------------------------------------------------------------------------
# D. hook_runner.py block-reflect handler
# ---------------------------------------------------------------------------


class TestHookBlockReflect:
    """hook_block_reflect in hook_runner._HOOKS."""

    def test_block_reflect_registered(self) -> None:
        """_HOOKS must contain 'block-reflect' key."""
        from yadgar.core.scripts.hook_runner import _HOOKS

        assert "block-reflect" in _HOOKS, (
            "'block-reflect' not in hook_runner._HOOKS — wiring missing"
        )

    def test_non_matching_tool_emits_nothing(self, capsys, monkeypatch) -> None:
        """Non-block tool name causes hook_block_reflect to emit nothing."""
        from yadgar.core.scripts.hook_runner import _HOOKS

        handler = _HOOKS["block-reflect"]
        stdin_data = json.dumps({"tool_name": "Bash", "cwd": _PROJ_DIR})
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_data))
        handler()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_matching_tool_calls_endpoint(self, capsys, monkeypatch) -> None:
        """mcp__yadgar__block_update causes hook to call /hooks/block-reflect."""
        # Car 0 moved the handler body to yadgar.core.cli.hook; the handler
        # resolves _http_get THERE, so patch the impl module (not the shim
        # re-export). hook_runner._HOOKS[...] is the same object either way.
        from yadgar.core.cli import hook as hook_runner

        calls = []

        def mock_http_get(path, params=None, timeout=2.0):
            calls.append((path, params))
            return {"text": "## Memory Blocks\n- `task`: do stuff\n"}

        monkeypatch.setattr(hook_runner, "_http_get", mock_http_get)

        stdin_data = json.dumps({"tool_name": "mcp__yadgar__block_update", "cwd": _PROJ_DIR})
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_data))

        hook_runner._HOOKS["block-reflect"]()

        assert len(calls) == 1
        assert calls[0][0] == "/hooks/block-reflect"
        assert calls[0][1].get("directory") == _PROJ_DIR

        captured = capsys.readouterr()
        assert "Memory Blocks" in captured.out or "task" in captured.out


# ---------------------------------------------------------------------------
# E. SessionStart block injection
# ---------------------------------------------------------------------------


class TestSessionContextBlocksInjection:
    """session-context endpoint prepends blocks to the rendered text."""

    def test_session_context_with_blocks(self) -> None:
        """Blocks seeded for directory appear in session-context output."""
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        from yadgar.core.server.http import hook_session_context
        from yadgar.core.server.tools.blocks import block_create

        block_create(
            name="session_block",
            content="session injected content",
            scope="project",
            directory=_PROJ_DIR,
        )

        app = Starlette(
            routes=[Route("/hooks/session-context", hook_session_context, methods=["GET"])]
        )
        client = TestClient(app)
        resp = client.get(
            "/hooks/session-context",
            params={"directory": _PROJ_DIR, "source": "startup"},
        )
        assert resp.status_code == 200
        text = resp.json().get("text", "")
        assert "session_block" in text or "session injected content" in text, (
            f"Blocks not found in session-context output: {text[:300]!r}"
        )


# ---------------------------------------------------------------------------
# F. install_hooks PostToolUse wiring
# ---------------------------------------------------------------------------


class TestInstallHooksBlockReflect:
    """install_hooks must add block-reflect as second PostToolUse entry."""

    def test_posttooluse_has_two_entries(self, tmp_path, monkeypatch) -> None:
        """PostToolUse hook list has exactly 2 entries after install_hooks."""
        monkeypatch.setenv("HOME", str(tmp_path))

        project_dir = tmp_path / "myproject"
        project_dir.mkdir(parents=True, exist_ok=True)

        server.install_hooks(project_directory=str(project_dir))

        settings_file = project_dir / ".claude" / "settings.json"
        if not settings_file.exists():
            settings_file = tmp_path / ".claude" / "settings.json"

        assert settings_file.exists(), "settings.json not written"

        settings = json.loads(settings_file.read_text())
        hooks = settings.get("hooks", {})
        post_tool_use = hooks.get("PostToolUse", [])

        assert len(post_tool_use) >= 2, (
            f"Expected >=2 PostToolUse entries (post-tool-capture + block-reflect), "
            f"got {len(post_tool_use)}: {post_tool_use}"
        )

    def test_block_reflect_entry_has_matcher(self, tmp_path, monkeypatch) -> None:
        """The block-reflect PostToolUse entry has matcher with 'block_' pattern."""
        monkeypatch.setenv("HOME", str(tmp_path))

        project_dir = tmp_path / "myproject2"
        project_dir.mkdir(parents=True, exist_ok=True)

        server.install_hooks(project_directory=str(project_dir))

        settings_file = project_dir / ".claude" / "settings.json"
        if not settings_file.exists():
            settings_file = tmp_path / ".claude" / "settings.json"

        assert settings_file.exists()

        settings = json.loads(settings_file.read_text())
        hooks = settings.get("hooks", {})
        post_tool_use = hooks.get("PostToolUse", [])

        matchers = [entry.get("matcher", "") for entry in post_tool_use]
        block_matchers = [m for m in matchers if "block" in m.lower()]
        assert block_matchers, (
            f"No PostToolUse entry with block matcher found. Matchers: {matchers}"
        )
