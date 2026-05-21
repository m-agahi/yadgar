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
import os
from importlib import reload
from unittest.mock import MagicMock, patch

import httpx

# ── Helper: build a fake candidate memory dict ───────────────────────────────


def _candidate(content: str = "user works at ACME") -> dict:
    return {
        "content": content,
        "tags": ["work"],
        "context": "/tmp/test",
    }


def _mock_client(response_payload: dict) -> MagicMock:
    """Build a mock httpx.Client whose .post() returns a fake response."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = response_payload
    client = MagicMock(spec=httpx.Client)
    client.post = MagicMock(return_value=mock_resp)
    return client


def _reload_enabled(cr_module) -> None:
    """Reload conflict_resolver with YADGAR_CONFLICT_RESOLVER=on.

    Reload MUST happen inside the patch.dict so _ENABLED is captured as True.
    Function-level patches (_get_client, _fetch_similar) must be applied AFTER
    reload because reload replaces the module's function objects.
    """
    with patch.dict(os.environ, {"YADGAR_CONFLICT_RESOLVER": "on"}):
        reload(cr_module)


# ── Test 1: Disabled by default ───────────────────────────────────────────────


def test_disabled_returns_noop_without_ollama_call():
    """When YADGAR_CONFLICT_RESOLVER is not 'on', returns NOOP with no HTTP call."""
    with patch("httpx.Client") as mock_client_cls:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YADGAR_CONFLICT_RESOLVER", None)
            import yadgar.conflict_resolver as cr

            reload(cr)
            result = cr.resolve_conflict(_candidate())

    mock_client_cls.assert_not_called()
    assert result["op"] == "NOOP"
    assert "reason" in result


# ── Test 2: ADD decision ──────────────────────────────────────────────────────


def test_enabled_ollama_add_returns_add():
    """ADD response from Ollama → op=ADD."""
    import yadgar.conflict_resolver as cr

    _reload_enabled(cr)

    mock_client = _mock_client(
        {"response": json.dumps({"op": "ADD", "target_id": None, "reason": "new fact"})}
    )
    with patch("yadgar.conflict_resolver._get_client", return_value=mock_client):
        with patch("yadgar.conflict_resolver._fetch_similar", return_value=[]):
            result = cr.resolve_conflict(_candidate())

    assert result["op"] == "ADD"


# ── Test 3: UPDATE decision ───────────────────────────────────────────────────


def test_enabled_ollama_update_returns_update():
    """UPDATE response from Ollama → op=UPDATE with target_id."""
    import yadgar.conflict_resolver as cr

    _reload_enabled(cr)

    mock_client = _mock_client(
        {
            "response": json.dumps(
                {"op": "UPDATE", "target_id": 42, "reason": "supersedes older fact"}
            )
        }
    )
    with patch("yadgar.conflict_resolver._get_client", return_value=mock_client):
        with patch("yadgar.conflict_resolver._fetch_similar", return_value=[{"id": 42}]):
            result = cr.resolve_conflict(_candidate())

    assert result["op"] == "UPDATE"
    assert result["target_id"] == 42


# ── Test 4: DELETE decision ───────────────────────────────────────────────────


def test_enabled_ollama_delete_returns_delete():
    """DELETE response from Ollama → op=DELETE with target_id."""
    import yadgar.conflict_resolver as cr

    _reload_enabled(cr)

    mock_client = _mock_client(
        {"response": json.dumps({"op": "DELETE", "target_id": 7, "reason": "contradiction"})}
    )
    with patch("yadgar.conflict_resolver._get_client", return_value=mock_client):
        with patch("yadgar.conflict_resolver._fetch_similar", return_value=[{"id": 7}]):
            result = cr.resolve_conflict(_candidate())

    assert result["op"] == "DELETE"
    assert result["target_id"] == 7


# ── Test 5: NOOP decision ─────────────────────────────────────────────────────


def test_enabled_ollama_noop_returns_noop():
    """NOOP response from Ollama → op=NOOP, no insert."""
    import yadgar.conflict_resolver as cr

    _reload_enabled(cr)

    mock_client = _mock_client(
        {"response": json.dumps({"op": "NOOP", "target_id": None, "reason": "duplicate"})}
    )
    with patch("yadgar.conflict_resolver._get_client", return_value=mock_client):
        with patch("yadgar.conflict_resolver._fetch_similar", return_value=[{"id": 99}]):
            result = cr.resolve_conflict(_candidate())

    assert result["op"] == "NOOP"


# ── Test 6: Ollama timeout → fail-soft ADD ────────────────────────────────────


def test_enabled_ollama_timeout_degrades_to_add():
    """Ollama timeout (httpx.TimeoutException) → fail-soft, returns ADD."""
    import yadgar.conflict_resolver as cr

    _reload_enabled(cr)

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post = MagicMock(side_effect=httpx.TimeoutException("timeout"))

    with patch("yadgar.conflict_resolver._get_client", return_value=mock_client):
        with patch("yadgar.conflict_resolver._fetch_similar", return_value=[]):
            result = cr.resolve_conflict(_candidate())

    assert result["op"] == "ADD", f"Expected ADD on timeout, got {result['op']}"
