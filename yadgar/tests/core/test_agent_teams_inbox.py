"""Tests for Agent Teams JSONL inbox mirror — v5.3.6 M1.

Covers:
1. Hook script filters team_inbox path, ignores other paths.
2. JSONL parse + action_log writes (mocked).
3. Re-reads handle new lines only (file position tracking).
4. Endpoint accepts payload + writes per-line entries.
5. Malformed JSONL → skip with warning, don't crash.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Shared helpers ──────────────────────────────────────────────────────────

_INBOX_RE = re.compile(
    r"[/\\]\.claude[/\\]team_inbox[/\\]([^/\\]+)[/\\]([^/\\]+)[/\\]([^/\\]+)\.jsonl$"
)


def _team_inbox_path(
    home: str,
    project: str = "proj1",
    team: str = "team1",
    agent: str = "agent1",
) -> str:
    """Build a team_inbox path under a fake home dir."""
    return str(Path(home) / ".claude" / "team_inbox" / project / team / f"{agent}.jsonl")


def _other_path(home: str) -> str:
    return str(Path(home) / "some" / "other" / "file.py")


def _write_inbox(path: str, messages: list) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(json.dumps(m) for m in messages) + "\n", encoding="utf-8")


# ── Test 1: hook script path filtering ─────────────────────────────────────


class TestHookScriptFilter:
    """Hook script only acts on team_inbox paths; ignores everything else."""

    def test_team_inbox_path_detected(self, tmp_path):
        from yadgar.core.hooks.file_changed import is_team_inbox_path

        path = _team_inbox_path(str(tmp_path))
        assert is_team_inbox_path(path) is True

    def test_non_team_inbox_path_ignored(self, tmp_path):
        from yadgar.core.hooks.file_changed import is_team_inbox_path

        assert is_team_inbox_path(_other_path(str(tmp_path))) is False
        assert is_team_inbox_path("/home/user/projects/foo/bar.py") is False
        assert is_team_inbox_path("/home/user/.claude/settings.json") is False

    def test_team_inbox_with_nested_path_segments(self, tmp_path):
        from yadgar.core.hooks.file_changed import is_team_inbox_path

        path = _team_inbox_path(str(tmp_path), project="my-proj", team="teamA", agent="agentX")
        assert is_team_inbox_path(path) is True

    def test_main_skips_non_inbox_non_plan_path(self, tmp_path, monkeypatch):
        """main() exits without POSTing when path matches neither filter."""
        from yadgar.core.hooks import file_changed

        posted = []
        monkeypatch.setattr(file_changed, "_post_file_changed", lambda *a: posted.append(a))

        import io
        import sys

        payload = json.dumps({"file_path": _other_path(str(tmp_path)), "file_action": "modified"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        file_changed.main()
        assert posted == []

    def test_main_posts_for_team_inbox_path(self, tmp_path, monkeypatch):
        """main() calls _post_file_changed for a team_inbox path."""
        from yadgar.core.hooks import file_changed

        posted = []
        monkeypatch.setattr(file_changed, "_post_file_changed", lambda *a: posted.append(a))

        import io
        import sys

        path = _team_inbox_path(str(tmp_path))
        payload = json.dumps({"file_path": path, "file_action": "modified"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        file_changed.main()
        assert len(posted) == 1
        assert posted[0][0] == path


# ── Test 2: JSONL parse + action_log writes ─────────────────────────────────


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


class TestJsonlParse:
    """Endpoint parses JSONL lines and writes action_log entries per message."""

    def test_jsonl_lines_produce_action_log_entries(self, tmp_path, storage):
        """Each valid JSONL line → one action_log entry with tool=team_message."""
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.http import _handle_team_inbox

        inbox_path = _team_inbox_path(str(tmp_path))
        messages = [
            {"subagent_type": "general-purpose", "content": "First finding"},
            {"agent_type": "Explore", "text": "Second finding"},
        ]
        _write_inbox(inbox_path, messages)
        _st._team_inbox_positions.clear()

        match = _INBOX_RE.search(inbox_path)
        result_json = json.loads(asyncio.run(_handle_team_inbox(inbox_path, match, storage)).body)

        assert result_json["status"] == "ok"
        assert result_json["stored"] == 2
        assert result_json["skipped"] == 0

    def test_missing_file_returns_skipped(self, tmp_path):
        """Non-existent file → skipped (no crash)."""
        from yadgar.core.server.http import _handle_team_inbox

        inbox_path = _team_inbox_path(str(tmp_path), agent="ghost")
        match = _INBOX_RE.search(inbox_path)
        mock_storage = MagicMock()

        result = json.loads(asyncio.run(_handle_team_inbox(inbox_path, match, mock_storage)).body)
        assert result["status"] == "skipped"


# ── Test 3: file position tracking (new lines only) ─────────────────────────


class TestFilePositionTracking:
    """Re-reads only ingest new lines since last call."""

    def test_only_new_lines_ingested_on_second_read(self, tmp_path, storage):
        """Second call with same file only processes newly appended lines."""
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.http import _handle_team_inbox

        inbox_path = _team_inbox_path(str(tmp_path), agent="tracker")
        Path(inbox_path).parent.mkdir(parents=True, exist_ok=True)
        Path(inbox_path).write_text(json.dumps({"content": "first"}) + "\n", encoding="utf-8")
        _st._team_inbox_positions.clear()

        match = _INBOX_RE.search(inbox_path)

        # First read — processes 1 line
        d1 = json.loads(asyncio.run(_handle_team_inbox(inbox_path, match, storage)).body)
        assert d1["new_lines"] == 1

        # Append a second line
        with Path(inbox_path).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"content": "second"}) + "\n")

        # Second read — should only process 1 new line
        d2 = json.loads(asyncio.run(_handle_team_inbox(inbox_path, match, storage)).body)
        assert d2["new_lines"] == 1
        assert d2["stored"] == 1


# ── Test 4: full endpoint roundtrip ─────────────────────────────────────────


class TestFileChangedEndpoint:
    """Full endpoint accepts payload and routes to correct handler."""

    def test_endpoint_routes_team_inbox(self, tmp_path, storage):
        """POST /hooks/file-changed with team_inbox path → stored >= 0."""
        import urllib.parse

        from starlette.requests import Request as StarRequest

        import yadgar._shared.runtime.state as _st

        inbox_path = _team_inbox_path(str(tmp_path), agent="e2e")
        Path(inbox_path).parent.mkdir(parents=True, exist_ok=True)
        Path(inbox_path).write_text(json.dumps({"content": "hello"}) + "\n", encoding="utf-8")
        _st._team_inbox_positions.clear()

        body_json = json.dumps({"file_path": inbox_path, "file_action": "modified"}).encode()

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/hooks/file-changed",
            "query_string": urllib.parse.urlencode({"path": inbox_path}).encode(),
            "headers": [(b"content-type", b"application/json")],
        }

        async def receive():
            return {"type": "http.request", "body": body_json, "more_body": False}

        request = StarRequest(scope, receive)

        with patch("yadgar._shared.runtime.state._storage", storage):
            from yadgar.core.server.http import hook_file_changed

            result = json.loads(asyncio.run(hook_file_changed(request)).body)

        assert result["status"] == "ok"
        assert result.get("stored", 0) >= 0


# ── Test 5: malformed JSONL ──────────────────────────────────────────────────


class TestMalformedJsonl:
    """Malformed JSONL lines are skipped with a warning — no crash."""

    def test_malformed_line_skipped_valid_lines_stored(self, tmp_path, storage):
        """Mixed valid/malformed JSONL → valid stored, malformed skipped."""
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.http import _handle_team_inbox

        inbox_path = _team_inbox_path(str(tmp_path), agent="malformed")
        Path(inbox_path).parent.mkdir(parents=True, exist_ok=True)
        Path(inbox_path).write_text(
            "\n".join(
                [
                    json.dumps({"content": "valid line"}),
                    "NOT JSON {{{{",
                    json.dumps({"content": "another valid"}),
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        _st._team_inbox_positions.clear()

        match = _INBOX_RE.search(inbox_path)
        result = json.loads(asyncio.run(_handle_team_inbox(inbox_path, match, storage)).body)

        assert result["status"] == "ok"
        assert result["stored"] == 2
        assert result["skipped"] == 1  # the malformed line
