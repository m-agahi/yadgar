"""v5.46.9 — F6 regression guard: hook_subagent_stop stores findings with provenance.

TDD — written BEFORE the fix.

F6 root cause: _fake_memorize in test_subagent_stop_hook.py lacked branch_hint parameter.
The production endpoint calls memorize(..., branch_hint=_branch_hint) which causes
a TypeError in the fake → caught silently → stored=0.

This test file provides a standalone regression guard that:
1. Patches memorize with a fake that accepts branch_hint
2. Verifies stored count = number of findings
3. Verifies provenance_agent is passed correctly
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import patch

import pytest

from yadgar import server


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "test_f6.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _make_request(body: bytes):
    """Minimal ASGI-compatible mock Request for hook_subagent_stop."""
    from starlette.datastructures import Headers
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/hooks/subagent-stop",
        "query_string": b"",
        "headers": Headers({"content-type": "application/json"}).raw,
    }

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, _receive)


class TestSubagentStopFindingsF6:
    """hook_subagent_stop must store all findings — F6 regression guard."""

    def test_stores_findings_branch_hint_accepted(self):
        """_fake_memorize with branch_hint param — stored count equals findings count."""
        import yadgar.server.http as _http

        stored_calls = []

        def _fake_memorize(
            content,
            context,
            tags,
            is_protected=False,
            provenance_agent="default",
            branch_hint=None,  # F6 fix: must accept branch_hint
        ):
            stored_calls.append(
                {
                    "content": content,
                    "context": context,
                    "tags": tags,
                    "provenance_agent": provenance_agent,
                    "branch_hint": branch_hint,
                }
            )
            return {"stored": True, "queued": True, "queue_id": "test-q"}

        _srv = sys.modules.get("yadgar.server")

        with patch.object(_srv, "memorize", _fake_memorize, create=True):
            body = json.dumps(
                {
                    "agent_type": "general-purpose",
                    "cwd": "/tmp/proj",
                    "findings": [
                        "fact: migration 005 adds column",
                        "anchor: some-slug — detail",
                    ],
                }
            ).encode()
            req = _make_request(body)
            resp = asyncio.run(_http.hook_subagent_stop(req))
            data = json.loads(resp.body)

        assert data["status"] == "ok"
        assert data["stored"] == 2, (
            f"Expected stored=2, got {data['stored']}. "
            "F6: _fake_memorize must accept branch_hint kwarg."
        )
        assert data["agent_type"] == "general-purpose"
        assert all(c["provenance_agent"] == "general-purpose" for c in stored_calls)
        assert all("from-subagent" in c["tags"] for c in stored_calls)

    def test_stores_zero_findings_when_empty(self):
        """Empty findings list → stored=0."""
        import yadgar.server.http as _http

        stored_calls = []

        def _fake_memorize(
            content,
            context,
            tags,
            is_protected=False,
            provenance_agent="default",
            branch_hint=None,
        ):
            stored_calls.append(content)
            return {"stored": True, "queued": True, "queue_id": "test-q"}

        _srv = sys.modules.get("yadgar.server")

        with patch.object(_srv, "memorize", _fake_memorize, create=True):
            body = json.dumps(
                {
                    "agent_type": "general-purpose",
                    "cwd": "/tmp/proj",
                    "findings": [],
                }
            ).encode()
            req = _make_request(body)
            resp = asyncio.run(_http.hook_subagent_stop(req))
            data = json.loads(resp.body)

        assert data["status"] == "ok"
        assert data["stored"] == 0
        assert stored_calls == []
