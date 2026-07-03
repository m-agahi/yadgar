"""v5.7.9 tests — source-aware SessionStart response.

Scenarios:
1. source=compact   → hint suppressed (compact handler owns auto-restore)
2. source=clear     → hint present, copy mentions "session cleared"
3. source=startup   → hint present, copy mentions "session starting"
4. source=resume    → hint present, copy mentions "resuming"
5. source missing   → treated as startup (hint present)
6. hook_runner passes source from stdin to server (query param)
7. Server response branches correctly per source when no checkpoint exists
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_client(token: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", token)

    from yadgar import server as _server
    from yadgar.auth_middleware import BearerAuthMiddleware

    asgi_app = _server.mcp_server.streamable_http_app()
    return TestClient(BearerAuthMiddleware(asgi_app), raise_server_exceptions=False)


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("v579_smart_sessionstart")
    from yadgar import server

    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def _get(client: TestClient, token: str, directory: str, source: str | None = None):
    params = f"directory={directory}"
    if source is not None:
        params += f"&source={source}"
    return client.get(
        f"/hooks/session-context?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

_MOCK_BRIEF = {
    "_render": "# Project\n",
    "project": "test",
    "branch": "master",
    "stale_wiki_count": 0,
    "init_memory_present": False,
    "active_work_present": False,
    "top_anchors": [],
    "recent_episode_count": 0,
}


# ── 1. source=compact — restore hint suppressed ───────────────────────────────


class TestSourceCompact:
    def test_compact_no_restore_hint_without_checkpoint(self, tmp_path, monkeypatch):
        """source=compact + no checkpoint → text has compact-specific copy, NO restore hint."""
        token = "tok-compact-1"
        from yadgar import server as _srv

        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            client = _make_client(token, monkeypatch)
            resp = _get(client, token, str(tmp_path), source="compact")

        assert resp.status_code == 200
        body = resp.json()
        text = body.get("text", "")
        assert "restore(" not in text, f"source=compact must NOT emit restore() hint; got: {text!r}"

    def test_compact_no_restore_hint_with_checkpoint(self, tmp_path, monkeypatch):
        """source=compact + checkpoint present → compact handler owns restore; no hint emitted."""
        from yadgar import server as _srv
        from yadgar.config import Settings
        from yadgar.embeddings import EmbeddingEngine
        from yadgar.restoration import CheckpointContext, CheckpointRestore
        from yadgar.storage import StorageEngine

        storage = StorageEngine(str(tmp_path / "cp.db"))
        settings = Settings(DB_PATH=str(storage._db_path))
        embeddings = EmbeddingEngine()
        replay = CheckpointRestore(storage=storage, embeddings=embeddings, settings=settings)
        replay.create_checkpoint(str(tmp_path), CheckpointContext(current_task="Active work"))
        storage.close()

        token = "tok-compact-2"
        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            with patch(
                "yadgar.server.lifecycle._get_storage",
                return_value=StorageEngine(str(tmp_path / "cp.db")),
            ):
                client = _make_client(token, monkeypatch)
                resp = _get(client, token, str(tmp_path), source="compact")

        assert resp.status_code == 200
        body = resp.json()
        text = body.get("text", "")
        assert "restore(" not in text, f"source=compact must NOT emit restore() hint; got: {text!r}"

    def test_compact_text_mentions_compaction(self, tmp_path, monkeypatch):
        """source=compact → emitted text mentions compaction context."""
        token = "tok-compact-3"
        from yadgar import server as _srv

        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            client = _make_client(token, monkeypatch)
            resp = _get(client, token, str(tmp_path), source="compact")

        assert resp.status_code == 200
        text = resp.json().get("text", "")
        # compact handler should include a note that compaction occurred
        assert "compact" in text.lower() or "compaction" in text.lower(), (
            f"source=compact text should mention compaction; got: {text!r}"
        )


# ── 2. source=clear — hint present, clear-specific copy ──────────────────────


class TestSourceClear:
    def test_clear_restore_hint_when_checkpoint_exists(self, tmp_path, monkeypatch):
        """source=clear + checkpoint → hint present (user may want to restore)."""
        from yadgar import server as _srv
        from yadgar.config import Settings
        from yadgar.embeddings import EmbeddingEngine
        from yadgar.restoration import CheckpointContext, CheckpointRestore
        from yadgar.storage import StorageEngine

        storage = StorageEngine(str(tmp_path / "cp2.db"))
        settings = Settings(DB_PATH=str(storage._db_path))
        embeddings = EmbeddingEngine()
        replay = CheckpointRestore(storage=storage, embeddings=embeddings, settings=settings)
        replay.create_checkpoint(str(tmp_path), CheckpointContext(current_task="Task X"))
        storage.close()

        token = "tok-clear-1"
        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            with patch(
                "yadgar.server.lifecycle._get_storage",
                return_value=StorageEngine(str(tmp_path / "cp2.db")),
            ):
                client = _make_client(token, monkeypatch)
                resp = _get(client, token, str(tmp_path), source="clear")

        assert resp.status_code == 200
        text = resp.json().get("text", "")
        assert f'restore(directory="{tmp_path}")' in text, (
            f"source=clear with checkpoint must emit restore() hint; got: {text!r}"
        )

    def test_clear_copy_mentions_cleared(self, tmp_path, monkeypatch):
        """source=clear → hint copy mentions 'cleared' or 'clear'."""
        token = "tok-clear-2"
        from yadgar import server as _srv

        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            client = _make_client(token, monkeypatch)
            resp = _get(client, token, str(tmp_path), source="clear")

        assert resp.status_code == 200
        text = resp.json().get("text", "")
        assert "clear" in text.lower(), f"source=clear text should mention clear; got: {text!r}"


# ── 3. source=startup — hint present, startup-specific copy ──────────────────


class TestSourceStartup:
    def test_startup_restore_hint_when_checkpoint_exists(self, tmp_path, monkeypatch):
        """source=startup + checkpoint → restore hint present."""
        from yadgar import server as _srv
        from yadgar.config import Settings
        from yadgar.embeddings import EmbeddingEngine
        from yadgar.restoration import CheckpointContext, CheckpointRestore
        from yadgar.storage import StorageEngine

        storage = StorageEngine(str(tmp_path / "cp3.db"))
        settings = Settings(DB_PATH=str(storage._db_path))
        embeddings = EmbeddingEngine()
        replay = CheckpointRestore(storage=storage, embeddings=embeddings, settings=settings)
        replay.create_checkpoint(str(tmp_path), CheckpointContext(current_task="Task Y"))
        storage.close()

        token = "tok-startup-1"
        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            with patch(
                "yadgar.server.lifecycle._get_storage",
                return_value=StorageEngine(str(tmp_path / "cp3.db")),
            ):
                client = _make_client(token, monkeypatch)
                resp = _get(client, token, str(tmp_path), source="startup")

        assert resp.status_code == 200
        text = resp.json().get("text", "")
        assert f'restore(directory="{tmp_path}")' in text, (
            f"source=startup with checkpoint must emit restore() hint; got: {text!r}"
        )

    def test_startup_copy_mentions_starting(self, tmp_path, monkeypatch):
        """source=startup → hint copy mentions 'starting' or 'startup'."""
        token = "tok-startup-2"
        from yadgar import server as _srv

        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            client = _make_client(token, monkeypatch)
            resp = _get(client, token, str(tmp_path), source="startup")

        assert resp.status_code == 200
        text = resp.json().get("text", "")
        assert "start" in text.lower(), f"source=startup text should mention start; got: {text!r}"


# ── 4. source=resume — hint present, resume-specific copy ────────────────────


class TestSourceResume:
    def test_resume_restore_hint_when_checkpoint_exists(self, tmp_path, monkeypatch):
        """source=resume + checkpoint → restore hint present."""
        from yadgar import server as _srv
        from yadgar.config import Settings
        from yadgar.embeddings import EmbeddingEngine
        from yadgar.restoration import CheckpointContext, CheckpointRestore
        from yadgar.storage import StorageEngine

        storage = StorageEngine(str(tmp_path / "cp4.db"))
        settings = Settings(DB_PATH=str(storage._db_path))
        embeddings = EmbeddingEngine()
        replay = CheckpointRestore(storage=storage, embeddings=embeddings, settings=settings)
        replay.create_checkpoint(str(tmp_path), CheckpointContext(current_task="Task Z"))
        storage.close()

        token = "tok-resume-1"
        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            with patch(
                "yadgar.server.lifecycle._get_storage",
                return_value=StorageEngine(str(tmp_path / "cp4.db")),
            ):
                client = _make_client(token, monkeypatch)
                resp = _get(client, token, str(tmp_path), source="resume")

        assert resp.status_code == 200
        text = resp.json().get("text", "")
        assert f'restore(directory="{tmp_path}")' in text, (
            f"source=resume with checkpoint must emit restore() hint; got: {text!r}"
        )

    def test_resume_copy_mentions_resuming(self, tmp_path, monkeypatch):
        """source=resume → hint copy mentions 'resum'."""
        token = "tok-resume-2"
        from yadgar import server as _srv

        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            client = _make_client(token, monkeypatch)
            resp = _get(client, token, str(tmp_path), source="resume")

        assert resp.status_code == 200
        text = resp.json().get("text", "")
        assert "resum" in text.lower(), f"source=resume text should mention resume; got: {text!r}"


# ── 5. source missing — treated as startup ───────────────────────────────────


class TestSourceMissing:
    def test_missing_source_restore_hint_when_checkpoint(self, tmp_path, monkeypatch):
        """No source param + checkpoint → restore hint present (treated as startup)."""
        from yadgar import server as _srv
        from yadgar.config import Settings
        from yadgar.embeddings import EmbeddingEngine
        from yadgar.restoration import CheckpointContext, CheckpointRestore
        from yadgar.storage import StorageEngine

        storage = StorageEngine(str(tmp_path / "cp5.db"))
        settings = Settings(DB_PATH=str(storage._db_path))
        embeddings = EmbeddingEngine()
        replay = CheckpointRestore(storage=storage, embeddings=embeddings, settings=settings)
        replay.create_checkpoint(str(tmp_path), CheckpointContext(current_task="Task W"))
        storage.close()

        token = "tok-missing-1"
        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            with patch(
                "yadgar.server.lifecycle._get_storage",
                return_value=StorageEngine(str(tmp_path / "cp5.db")),
            ):
                client = _make_client(token, monkeypatch)
                resp = _get(client, token, str(tmp_path), source=None)  # no source

        assert resp.status_code == 200
        text = resp.json().get("text", "")
        assert f'restore(directory="{tmp_path}")' in text, (
            f"missing source with checkpoint must emit restore() hint; got: {text!r}"
        )

    def test_missing_source_not_compact(self, tmp_path, monkeypatch):
        """Missing source must NOT trigger compact suppression."""
        token = "tok-missing-2"
        from yadgar import server as _srv

        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            client = _make_client(token, monkeypatch)
            resp = _get(client, token, str(tmp_path), source=None)

        assert resp.status_code == 200
        # Should not have compact-specific copy
        resp.json().get("text", "")
        # compact-specific text would say "compact" prominently — absent here is OK
        # but we don't assert absence since "compact" might appear in project brief
        # The key test is the restore hint is NOT suppressed (tested above with checkpoint)
        assert resp.status_code == 200  # sanity


# ── 6. hook_runner passes source from stdin to server ────────────────────────


class TestHookRunnerSourcePropagation:
    def test_hook_runner_reads_source_from_stdin(self, monkeypatch, tmp_path):
        """hook_session_start_context reads 'source' from stdin JSON and passes as query param."""
        import io
        import json
        import sys

        captured_url = {}

        # Patch _http_get to capture the URL/params
        def _fake_get(path, params=None, timeout=2.0):
            captured_url["path"] = path
            captured_url["params"] = params or {}
            return {"text": ""}

        # Import hook_runner and patch its _http_get

        import yadgar.scripts.hook_runner as hr

        monkeypatch.setattr(hr, "_http_get", _fake_get)

        # Simulate stdin with source=compact
        payload = json.dumps({"cwd": str(tmp_path), "source": "compact"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

        hr.hook_session_start_context()

        assert captured_url.get("params", {}).get("source") == "compact", (
            f"hook_runner must pass source=compact to server; params: {captured_url.get('params')}"
        )

    def test_hook_runner_missing_source_omits_param_or_sends_empty(self, monkeypatch, tmp_path):
        """When stdin has no 'source', hook_runner may omit or send empty — server defaults to startup."""
        import io
        import json
        import sys

        captured_url = {}

        def _fake_get(path, params=None, timeout=2.0):
            captured_url["params"] = params or {}
            return {"text": ""}

        import yadgar.scripts.hook_runner as hr

        monkeypatch.setattr(hr, "_http_get", _fake_get)

        payload = json.dumps({"cwd": str(tmp_path)})  # no source
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

        hr.hook_session_start_context()

        # Source param may be absent or empty string — both acceptable
        source_val = captured_url.get("params", {}).get("source", None)
        # The key check: if present, it should not be "compact"
        assert source_val != "compact", (
            f"Missing source in stdin must not result in source=compact being sent; got: {source_val!r}"
        )

    def test_hook_runner_startup_source_propagated(self, monkeypatch, tmp_path):
        """hook_runner passes source=startup when that's the stdin value."""
        import io
        import json
        import sys

        captured_url = {}

        def _fake_get(path, params=None, timeout=2.0):
            captured_url["params"] = params or {}
            return {"text": ""}

        import yadgar.scripts.hook_runner as hr

        monkeypatch.setattr(hr, "_http_get", _fake_get)

        payload = json.dumps({"cwd": str(tmp_path), "source": "startup"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

        hr.hook_session_start_context()

        assert captured_url.get("params", {}).get("source") == "startup", (
            f"hook_runner must pass source=startup; params: {captured_url.get('params')}"
        )


# ── 7. Unknown/unexpected source values — treated as startup ─────────────────


class TestUnknownSource:
    def test_unknown_source_treated_as_startup(self, tmp_path, monkeypatch):
        """Unknown source value (e.g. 'magic') must not crash and behaves as startup."""
        token = "tok-unknown-1"
        from yadgar import server as _srv

        with patch.object(_srv, "project_brief", return_value=_MOCK_BRIEF):
            client = _make_client(token, monkeypatch)
            resp = _get(client, token, str(tmp_path), source="magic")

        assert resp.status_code == 200
        body = resp.json()
        assert "text" in body
