"""Tests for SubagentStop hook — v5.3.0 A3.

Covers:
1. Hook script extracts findings from a mock report.
2. Hook script POSTs to endpoint with correct payload.
3. Endpoint memorize-loops findings with right provenance.
4. install_hooks registers SubagentStop with append-if-absent semantics.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

# ── Helper: build a fake transcript JSONL ─────────────────────────────────


def _make_transcript(report_text: str) -> Path:
    """Write a minimal transcript JSONL with one assistant turn."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
    entry = {
        "message": {
            "role": "assistant",
            "content": report_text,
        }
    }
    tmp.write(json.dumps(entry) + "\n")
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


# ── Test 1: extract findings from mock report ──────────────────────────────


class TestExtractFindings:
    """_extract_findings parses ## Yadgar findings section correctly."""

    def test_basic_bullets_extracted(self):
        from yadgar.hooks.subagent_stop import _extract_findings

        report = """\
Some analysis text.

## Yadgar findings

- anchor: slug-foo — key fact about foo
- fact: bar module depends on baz
- fact: migration #005 adds provenance_agent column

## Next steps

- Do something else
"""
        findings = _extract_findings(report)
        assert len(findings) == 3
        assert any("slug-foo" in f for f in findings)
        assert any("bar module" in f for f in findings)
        assert any("provenance_agent" in f for f in findings)

    def test_none_sentinel_skipped(self):
        from yadgar.hooks.subagent_stop import _extract_findings

        report = "## Yadgar findings\n- none\n"
        findings = _extract_findings(report)
        assert findings == []

    def test_section_absent_returns_empty(self):
        from yadgar.hooks.subagent_stop import _extract_findings

        report = "No findings section here.\n- Some bullet\n"
        findings = _extract_findings(report)
        assert findings == []

    def test_agent_tag_in_heading_handled(self):
        from yadgar.hooks.subagent_stop import _extract_findings

        report = "## Yadgar findings [agent: general-purpose]\n- fact: something useful\n"
        findings = _extract_findings(report)
        assert len(findings) == 1
        assert "something useful" in findings[0]

    def test_comment_lines_skipped(self):
        from yadgar.hooks.subagent_stop import _extract_findings

        report = "## Yadgar findings\n<!-- anchors: -->\n- fact: real finding\n"
        findings = _extract_findings(report)
        assert len(findings) == 1
        assert "real finding" in findings[0]


# ── Test 2: hook script POSTs to endpoint with correct payload ─────────────


class TestHookScriptPost:
    """_post_findings calls the right URL with the expected payload."""

    def test_posts_to_subagent_stop_url(self, monkeypatch):
        from yadgar.hooks import subagent_stop as _hs

        captured = {}

        class _FakeResponse:
            def read(self):
                return b"{}"

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data.decode())
            return _FakeResponse()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

        _hs._post_findings("general-purpose", "/tmp/proj", ["fact: test finding"])

        assert captured["url"] == "http://127.0.0.1:8765/hooks/subagent-stop"
        assert captured["data"]["agent_type"] == "general-purpose"
        assert captured["data"]["cwd"] == "/tmp/proj"
        assert "fact: test finding" in captured["data"]["findings"]

    def test_post_silent_on_connection_error(self, monkeypatch):
        """Connection refused must not raise."""
        from yadgar.hooks import subagent_stop as _hs

        def _raise(*a, **kw):
            raise ConnectionRefusedError("no daemon")

        monkeypatch.setattr("urllib.request.urlopen", _raise)
        # Must not raise
        _hs._post_findings("Explore", "/tmp", ["fact: something"])

    def test_empty_findings_does_not_post(self, monkeypatch):
        from yadgar.hooks import subagent_stop as _hs

        called = []

        def _fake_urlopen(req, timeout=None):
            called.append(True)

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        _hs._post_findings("general-purpose", "/tmp", [])
        assert called == []


# ── Test 3: endpoint memorize-loops findings with right provenance ─────────


def _make_request(body: bytes):
    """Build a minimal Starlette POST Request with the given body."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/hooks/subagent-stop",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    called = False

    async def _receive():
        nonlocal called
        if not called:
            called = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive=_receive)


class TestSubagentStopEndpoint:
    """POST /hooks/subagent-stop stores each finding with correct provenance."""

    def test_endpoint_stores_findings_with_provenance(self):
        """Endpoint calls memorize() once per finding with provenance_agent=agent_type."""
        import asyncio
        import sys

        import yadgar.server.http as _http

        stored_calls = []

        def _fake_memorize(content, context, tags, is_protected=False, provenance_agent="default"):
            stored_calls.append(
                {
                    "content": content,
                    "context": context,
                    "tags": tags,
                    "provenance_agent": provenance_agent,
                }
            )
            return {"stored": True, "queued": True, "queue_id": "test-q"}

        _srv = sys.modules.get("yadgar.server")

        with patch.object(_srv, "memorize", _fake_memorize, create=True):
            body = json.dumps(
                {
                    "agent_type": "general-purpose",
                    "cwd": "/tmp/proj",
                    "findings": ["fact: migration 005 adds column", "anchor: some-slug — detail"],
                }
            ).encode()
            req = _make_request(body)
            resp = asyncio.run(_http.hook_subagent_stop(req))
            data = json.loads(resp.body)

        assert data["status"] == "ok"
        assert data["stored"] == 2
        assert data["agent_type"] == "general-purpose"

        assert all(c["provenance_agent"] == "general-purpose" for c in stored_calls)
        assert all("from-subagent" in c["tags"] for c in stored_calls)
        assert all("agent-type:general-purpose" in c["tags"] for c in stored_calls)

    def test_endpoint_invalid_json_returns_400(self):
        import asyncio

        import yadgar.server.http as _http

        req = _make_request(b"not-json")
        resp = asyncio.run(_http.hook_subagent_stop(req))
        assert resp.status_code == 400

    def test_endpoint_empty_findings_returns_zero(self):
        import asyncio

        import yadgar.server.http as _http

        body = json.dumps({"agent_type": "Explore", "cwd": "/tmp", "findings": []}).encode()
        req = _make_request(body)
        resp = asyncio.run(_http.hook_subagent_stop(req))
        data = json.loads(resp.body)
        assert data["status"] == "ok"
        assert data["stored"] == 0


# ── Test 4: install_hooks registers SubagentStop append-if-absent ──────────


class TestInstallHooksSubagentStop:
    """install_hooks registers SubagentStop with append-if-absent semantics."""

    def test_fresh_install_adds_subagent_stop(self, tmp_path):
        from yadgar.install_hooks_lib import install_hooks_impl

        result = install_hooks_impl(
            home_dir=tmp_path,
            scope="global",
            project_directory=str(tmp_path / "project"),
            dry_run=True,
        )
        preview = result.get("preview", {})
        hooks = preview.get("hooks", {})
        assert "SubagentStop" in hooks, "SubagentStop not in installed hooks"
        subagent_entries = hooks["SubagentStop"]
        assert isinstance(subagent_entries, list) and len(subagent_entries) > 0

    def test_idempotent_does_not_duplicate(self, tmp_path):
        from yadgar.install_hooks_lib import install_hooks_impl

        # First install (real write)
        install_hooks_impl(
            home_dir=tmp_path, scope="global", project_directory=str(tmp_path), dry_run=False
        )
        # Second install
        install_hooks_impl(
            home_dir=tmp_path, scope="global", project_directory=str(tmp_path), dry_run=False
        )
        # Verify only one SubagentStop entry
        settings_path = tmp_path / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        subagent_entries = settings.get("hooks", {}).get("SubagentStop", [])
        assert len(subagent_entries) == 1, (
            f"Expected 1 SubagentStop entry, got {len(subagent_entries)}"
        )

    def test_existing_user_hook_preserved(self, tmp_path):
        """User-defined SubagentStop hooks must not be removed."""
        from yadgar.install_hooks_lib import install_hooks_impl

        # Pre-populate settings with a user hook
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True)
        settings_path = claude_dir / "settings.json"
        user_hook = {
            "matcher": "my-agent",
            "hooks": [{"type": "command", "command": "python3 /home/user/my-hook.py"}],
        }
        settings_path.write_text(json.dumps({"hooks": {"SubagentStop": [user_hook]}}))

        install_hooks_impl(
            home_dir=tmp_path, scope="global", project_directory=str(tmp_path), dry_run=False
        )

        settings = json.loads(settings_path.read_text())
        subagent_entries = settings.get("hooks", {}).get("SubagentStop", [])
        # Both user hook and yadgar hook present
        assert len(subagent_entries) == 2, (
            f"Expected 2 SubagentStop entries (user + yadgar), got {len(subagent_entries)}"
        )
        cmds = [e.get("hooks", [{}])[0].get("command", "") for e in subagent_entries]
        assert any("my-hook.py" in c for c in cmds), "user hook was removed"
        assert any("yadgar-subagent-stop" in c for c in cmds), "yadgar hook not added"
