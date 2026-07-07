"""Tests for SubagentStart hook — v5.3.2 SS1–SS4.

Covers:
1. Payload parse with all fields present.
2. Payload parse with missing fields (safe defaults).
3. Daemon HTTP call is made with task description in body.
4. install_hooks registers SubagentStart event.
"""

from __future__ import annotations

import json

# ── Test 1: payload parse — all fields ────────────────────────────────────


class TestPayloadParseFull:
    """_parse_payload extracts all SubagentStart fields."""

    def test_all_fields_present(self):
        from yadgar.core.hooks.subagent_start import _parse_payload

        data = {
            "session_id": "sess-abc",
            "hook_event_name": "SubagentStart",
            "agent_type": "general-purpose",
            "agent_id": "agent-xyz",
            "cwd": "/home/user/project",
            "description": "Investigate the failing tests in yadgar/tests/",
        }
        parsed = _parse_payload(data)
        assert parsed["agent_type"] == "general-purpose"
        assert parsed["agent_id"] == "agent-xyz"
        assert parsed["cwd"] == "/home/user/project"
        assert parsed["description"] == "Investigate the failing tests in yadgar/tests/"
        assert parsed["session_id"] == "sess-abc"


# ── Test 2: payload parse — missing fields ────────────────────────────────


class TestPayloadParseMissing:
    """_parse_payload returns safe defaults when fields are absent."""

    def test_empty_payload_defaults(self):
        from yadgar.core.hooks.subagent_start import _parse_payload

        parsed = _parse_payload({})
        assert parsed["agent_type"] == "general-purpose"
        assert parsed["agent_id"] == ""
        assert isinstance(parsed["cwd"], str)
        assert parsed["description"] == ""

    def test_partial_payload(self):
        from yadgar.core.hooks.subagent_start import _parse_payload

        data = {"agent_type": "Explore", "cwd": "/tmp"}
        parsed = _parse_payload(data)
        assert parsed["agent_type"] == "Explore"
        assert parsed["cwd"] == "/tmp"
        assert parsed["description"] == ""

    def test_prompt_field_used_as_fallback(self):
        """Some Claude Code versions may send 'prompt' instead of 'description'."""
        from yadgar.core.hooks.subagent_start import _parse_payload

        data = {"prompt": "Search for all usages of memorize()"}
        parsed = _parse_payload(data)
        assert parsed["description"] == "Search for all usages of memorize()"


# ── Test 3: daemon call ───────────────────────────────────────────────────


class TestDaemonCall:
    """_call_daemon POSTs to /hooks/subagent-start and returns text."""

    def test_successful_call_returns_text(self, monkeypatch):
        from yadgar.core.hooks import subagent_start as _ss

        class _FakeResp:
            def read(self):
                return json.dumps({"text": "# Yadgar — Subagent Context\n- fact: foo"}).encode()

        def _fake_urlopen(req, timeout=None):
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        text = _ss._call_daemon("general-purpose", "/tmp/proj", "investigate tests")
        assert "Yadgar" in text

    def test_daemon_down_returns_empty(self, monkeypatch):
        from yadgar.core.hooks import subagent_start as _ss

        def _raise(*a, **kw):
            raise ConnectionRefusedError("no daemon")

        monkeypatch.setattr("urllib.request.urlopen", _raise)
        text = _ss._call_daemon("Explore", "/tmp", "find usages")
        assert text == ""

    def test_posts_to_correct_url(self, monkeypatch):
        from yadgar.core.hooks import subagent_start as _ss

        captured = {}

        class _FakeResp:
            def read(self):
                return json.dumps({"text": ""}).encode()

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data.decode())
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        _ss._call_daemon("general-purpose", "/home/user/proj", "do a thing")

        assert "/hooks/subagent-start" in captured["url"]
        assert "agent_type=general-purpose" in captured["url"] or "agent_type" in captured.get(
            "url", ""
        )
        assert captured["data"].get("description") == "do a thing"
        assert captured["data"].get("cwd") == "/home/user/proj"

    def test_empty_description_still_calls(self, monkeypatch):
        """Even with no description, daemon should be called (may return empty text)."""
        from yadgar.core.hooks import subagent_start as _ss

        called = []

        class _FakeResp:
            def read(self):
                return json.dumps({"text": ""}).encode()

        def _fake_urlopen(req, timeout=None):
            called.append(True)
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        _ss._call_daemon("Explore", "/tmp", "")
        assert called


# ── Test 4: install_hooks registers SubagentStart ─────────────────────────


class TestInstallHooksSubagentStart:
    """install_hooks registers SubagentStart with append-if-absent semantics."""

    def test_fresh_install_adds_subagent_start(self, tmp_path):
        from yadgar.core.install_hooks_lib import install_hooks_impl

        result = install_hooks_impl(
            home_dir=tmp_path,
            scope="global",
            project_directory=str(tmp_path / "project"),
            dry_run=True,
        )
        preview = result.get("preview", {})
        hooks = preview.get("hooks", {})
        assert "SubagentStart" in hooks, "SubagentStart not in installed hooks"
        entries = hooks["SubagentStart"]
        assert isinstance(entries, list) and len(entries) > 0

    def test_idempotent_does_not_duplicate(self, tmp_path):
        from yadgar.core.install_hooks_lib import install_hooks_impl

        install_hooks_impl(
            home_dir=tmp_path, scope="global", project_directory=str(tmp_path), dry_run=False
        )
        install_hooks_impl(
            home_dir=tmp_path, scope="global", project_directory=str(tmp_path), dry_run=False
        )

        settings_path = tmp_path / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        entries = settings.get("hooks", {}).get("SubagentStart", [])
        assert len(entries) == 1, f"Expected 1 SubagentStart entry, got {len(entries)}"
