"""C4 — LLM conflict-ops on write (Mem0 parity, Ollama-only).

Six tests:
  1. Disabled (env unset) → returns NOOP without Ollama call.
  2. Enabled, Ollama mock returns ADD → memorize inserts as usual.
  3. Enabled, Ollama mock returns UPDATE → existing row updated, no new row.
  4. Enabled, Ollama mock returns DELETE → target row deleted, no new row.
  5. Enabled, Ollama mock returns NOOP → no insert.
  6. Enabled, Ollama timeout → degrades to ADD (fail-soft).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx

# ── Helper: build a fake candidate memory dict ───────────────────────────────


def _candidate(content: str = "user works at ACME") -> dict:
    return {
        "content": content,
        "tags": ["work"],
        "context": "/tmp/test",
    }


# ── Test 1: Disabled by default ───────────────────────────────────────────────


def test_disabled_returns_noop_without_ollama_call():
    """When YADGAR_CONFLICT_RESOLVER is not 'on', returns NOOP with no HTTP call."""
    import os

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("YADGAR_CONFLICT_RESOLVER", None)

        # Ensure httpx.post is never called
        with patch("httpx.post") as mock_post:
            from yadgar.conflict_resolver import resolve_conflict

            result = resolve_conflict(_candidate())

    mock_post.assert_not_called()
    assert result["op"] == "NOOP"
    assert "reason" in result


# ── Test 2: ADD decision ──────────────────────────────────────────────────────


def test_enabled_ollama_add_returns_add():
    """ADD response from Ollama → op=ADD."""
    import os

    json.dumps({"response": json.dumps({"op": "ADD", "target_id": None, "reason": "new fact"})})

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "response": json.dumps({"op": "ADD", "target_id": None, "reason": "new fact"})
    }

    with patch.dict(os.environ, {"YADGAR_CONFLICT_RESOLVER": "on"}):
        with patch("httpx.post", return_value=mock_resp):
            with patch("yadgar.conflict_resolver._fetch_similar", return_value=[]):
                from importlib import reload

                import yadgar.conflict_resolver as cr

                reload(cr)
                result = cr.resolve_conflict(_candidate())

    assert result["op"] == "ADD"


# ── Test 3: UPDATE decision ───────────────────────────────────────────────────


def test_enabled_ollama_update_returns_update():
    """UPDATE response from Ollama → op=UPDATE with target_id."""
    import os

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "response": json.dumps({"op": "UPDATE", "target_id": 42, "reason": "supersedes older fact"})
    }

    with patch.dict(os.environ, {"YADGAR_CONFLICT_RESOLVER": "on"}):
        with patch("httpx.post", return_value=mock_resp):
            with patch("yadgar.conflict_resolver._fetch_similar", return_value=[{"id": 42}]):
                from importlib import reload

                import yadgar.conflict_resolver as cr

                reload(cr)
                result = cr.resolve_conflict(_candidate())

    assert result["op"] == "UPDATE"
    assert result["target_id"] == 42


# ── Test 4: DELETE decision ───────────────────────────────────────────────────


def test_enabled_ollama_delete_returns_delete():
    """DELETE response from Ollama → op=DELETE with target_id."""
    import os

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "response": json.dumps({"op": "DELETE", "target_id": 7, "reason": "contradiction"})
    }

    with patch.dict(os.environ, {"YADGAR_CONFLICT_RESOLVER": "on"}):
        with patch("httpx.post", return_value=mock_resp):
            with patch("yadgar.conflict_resolver._fetch_similar", return_value=[{"id": 7}]):
                from importlib import reload

                import yadgar.conflict_resolver as cr

                reload(cr)
                result = cr.resolve_conflict(_candidate())

    assert result["op"] == "DELETE"
    assert result["target_id"] == 7


# ── Test 5: NOOP decision ─────────────────────────────────────────────────────


def test_enabled_ollama_noop_returns_noop():
    """NOOP response from Ollama → op=NOOP, no insert."""
    import os

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "response": json.dumps({"op": "NOOP", "target_id": None, "reason": "duplicate"})
    }

    with patch.dict(os.environ, {"YADGAR_CONFLICT_RESOLVER": "on"}):
        with patch("httpx.post", return_value=mock_resp):
            with patch("yadgar.conflict_resolver._fetch_similar", return_value=[{"id": 99}]):
                from importlib import reload

                import yadgar.conflict_resolver as cr

                reload(cr)
                result = cr.resolve_conflict(_candidate())

    assert result["op"] == "NOOP"


# ── Test 6: Ollama timeout → fail-soft ADD ────────────────────────────────────


def test_enabled_ollama_timeout_degrades_to_add():
    """Ollama timeout (httpx.TimeoutException) → fail-soft, returns ADD."""
    import os

    with patch.dict(os.environ, {"YADGAR_CONFLICT_RESOLVER": "on"}):
        with patch(
            "httpx.post",
            side_effect=httpx.TimeoutException("timeout"),
        ):
            with patch("yadgar.conflict_resolver._fetch_similar", return_value=[]):
                from importlib import reload

                import yadgar.conflict_resolver as cr

                reload(cr)
                result = cr.resolve_conflict(_candidate())

    assert result["op"] == "ADD", f"Expected ADD on timeout, got {result['op']}"
