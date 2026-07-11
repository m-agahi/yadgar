"""Tests for InstructionsLoaded hook — v5.3.2 H1.

Covers:
1. Payload parse + throttle decisions (only fire on session_start / compact).
2. Daemon HTTP call is made with correct params.
3. install_hooks registers InstructionsLoaded event.
"""

from __future__ import annotations

import json

# ── Test 1: payload parse + throttle decisions ─────────────────────────────


class TestThrottleDecisions:
    """_should_fire returns True only for session_start / compact load_reason."""

    def test_session_start_fires(self):
        from yadgar.core.hooks.instructions_loaded import _should_fire

        data = {
            "session_id": "abc",
            "hook_event_name": "InstructionsLoaded",
            "file_path": "/home/user/.claude/CLAUDE.md",
            "memory_type": "global",
            "load_reason": "session_start",
        }
        assert _should_fire(data) is True

    def test_compact_fires(self):
        from yadgar.core.hooks.instructions_loaded import _should_fire

        data = {"load_reason": "compact"}
        assert _should_fire(data) is True

    def test_nested_traversal_skipped(self):
        from yadgar.core.hooks.instructions_loaded import _should_fire

        data = {"load_reason": "nested_traversal"}
        assert _should_fire(data) is False

    def test_path_glob_match_skipped(self):
        from yadgar.core.hooks.instructions_loaded import _should_fire

        data = {"load_reason": "path_glob_match"}
        assert _should_fire(data) is False

    def test_include_skipped(self):
        from yadgar.core.hooks.instructions_loaded import _should_fire

        data = {"load_reason": "include"}
        assert _should_fire(data) is False

    def test_missing_load_reason_skipped(self):
        from yadgar.core.hooks.instructions_loaded import _should_fire

        data = {}
        assert _should_fire(data) is False

    def test_extracts_file_path(self):
        from yadgar.core.hooks.instructions_loaded import _parse_payload

        data = {
            "file_path": "/home/user/.claude/CLAUDE.md",
            "load_reason": "session_start",
            "session_id": "test-session",
        }
        parsed = _parse_payload(data)
        assert parsed["file_path"] == "/home/user/.claude/CLAUDE.md"
        assert parsed["load_reason"] == "session_start"
        assert parsed["session_id"] == "test-session"

    def test_parse_payload_defaults(self):
        from yadgar.core.hooks.instructions_loaded import _parse_payload

        parsed = _parse_payload({})
        assert parsed["file_path"] == ""
        assert parsed["load_reason"] == ""
        assert parsed["session_id"] == ""


# ── Test 2: daemon HTTP call ───────────────────────────────────────────────


class TestDaemonCall:
    """_call_daemon POSTs to /hooks/instructions-loaded with correct params."""

    def test_get_returns_text(self, monkeypatch):
        from yadgar.core.hooks import instructions_loaded as _il

        class _FakeResp:
            def read(self):
                return json.dumps({"text": "## Yadgar Context\n- anchor: foo"}).encode()

        def _fake_urlopen(req, timeout=None):
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

        text = _il._call_daemon("/home/user/.claude/CLAUDE.md", "session_start")
        assert "Yadgar" in text

    def test_daemon_down_returns_empty(self, monkeypatch):
        from yadgar.core.hooks import instructions_loaded as _il

        def _raise(*a, **kw):
            raise ConnectionRefusedError("no daemon")

        monkeypatch.setattr("urllib.request.urlopen", _raise)
        text = _il._call_daemon("/home/user/.claude/CLAUDE.md", "session_start")
        assert text == ""

    def test_uses_correct_url_params(self, monkeypatch):
        from yadgar.core.hooks import instructions_loaded as _il

        captured = {}

        class _FakeResp:
            def read(self):
                return json.dumps({"text": ""}).encode()

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        _il._call_daemon("/path/to/CLAUDE.md", "compact")

        assert "/hooks/instructions-loaded" in captured["url"]
        assert "file_path=" in captured["url"]
        assert "load_reason=compact" in captured["url"]


# ── Test 3: install_hooks registers InstructionsLoaded ─────────────────────


class TestInstallHooksInstructionsLoaded:
    """install_hooks registers InstructionsLoaded with append-if-absent semantics."""

    def test_fresh_install_adds_instructions_loaded(self, tmp_path):
        from yadgar.core.install.install_hooks_lib import install_hooks_impl

        result = install_hooks_impl(
            home_dir=tmp_path,
            scope="global",
            project_directory=str(tmp_path / "project"),
            dry_run=True,
        )
        preview = result.get("preview", {})
        hooks = preview.get("hooks", {})
        assert "InstructionsLoaded" in hooks, "InstructionsLoaded not in installed hooks"
        entries = hooks["InstructionsLoaded"]
        assert isinstance(entries, list) and len(entries) > 0

    def test_idempotent_does_not_duplicate(self, tmp_path):
        from yadgar.core.install.install_hooks_lib import install_hooks_impl

        install_hooks_impl(
            home_dir=tmp_path, scope="global", project_directory=str(tmp_path), dry_run=False
        )
        install_hooks_impl(
            home_dir=tmp_path, scope="global", project_directory=str(tmp_path), dry_run=False
        )

        settings_path = tmp_path / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        entries = settings.get("hooks", {}).get("InstructionsLoaded", [])
        assert len(entries) == 1, f"Expected 1 InstructionsLoaded entry, got {len(entries)}"
