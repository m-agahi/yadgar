"""I3 gate tests for conflict_resolver — env flag checked at import time.

Four tests:
  1. test_gate_off_no_httpx_client_constructed: OFF + import → httpx.Client never instantiated.
  2. test_gate_off_returns_noop_quickly: OFF + resolve_conflict() → NOOP in <1ms.
  3. test_gate_on_uses_client: ON + invoke → httpx.Client IS constructed (lazily on first call).
  4. test_gate_state_immutable_at_import: changing env AFTER import does not change behavior.
"""

from __future__ import annotations

import importlib
import os
import time
from unittest.mock import MagicMock, patch

import httpx

# ── helpers ───────────────────────────────────────────────────────────────────


def _candidate() -> dict:
    return {"content": "test memory", "tags": ["test"]}


def _reload_cr(env: dict[str, str]) -> object:
    """Reload conflict_resolver with a clean env snapshot."""
    with patch.dict(os.environ, env, clear=False):
        # Remove key if not in env so we get clean state
        if "YADGAR_CONFLICT_RESOLVER" not in env:
            os.environ.pop("YADGAR_CONFLICT_RESOLVER", None)
        import yadgar.backend.conflict_resolver.conflict_resolver as cr

        importlib.reload(cr)
        return cr


# ── Test 1 ────────────────────────────────────────────────────────────────────


def test_gate_off_no_httpx_client_constructed():
    """With flag OFF, importing the module must not construct an httpx.Client."""
    with patch("httpx.Client") as mock_client_cls:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YADGAR_CONFLICT_RESOLVER", None)
            import yadgar.backend.conflict_resolver.conflict_resolver as cr

            importlib.reload(cr)

        # Call resolve_conflict — should return NOOP without ever touching httpx.Client
        cr.resolve_conflict(_candidate())

    mock_client_cls.assert_not_called()


# ── Test 2 ────────────────────────────────────────────────────────────────────


def test_gate_off_returns_noop_quickly():
    """With flag OFF, resolve_conflict returns NOOP in well under 1ms."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("YADGAR_CONFLICT_RESOLVER", None)
        import yadgar.backend.conflict_resolver.conflict_resolver as cr

        importlib.reload(cr)

    start = time.perf_counter()
    result = cr.resolve_conflict(_candidate())
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result["op"] == "NOOP"
    assert result["target_id"] is None
    assert elapsed_ms < 1.0, (
        f"resolve_conflict with flag OFF took {elapsed_ms:.3f}ms — expected <1ms"
    )


# ── Test 3 ────────────────────────────────────────────────────────────────────


def test_gate_on_uses_client():
    """With flag ON, resolve_conflict constructs an httpx.Client lazily on first call."""
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "response": '{"op": "ADD", "target_id": null, "reason": "new fact"}'
    }

    mock_client_instance = MagicMock(spec=httpx.Client)
    mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
    mock_client_instance.__exit__ = MagicMock(return_value=False)
    mock_client_instance.post = MagicMock(return_value=mock_response)

    with patch("httpx.Client", return_value=mock_client_instance) as mock_client_cls:
        with patch.dict(os.environ, {"YADGAR_CONFLICT_RESOLVER": "on"}):
            import yadgar.backend.conflict_resolver.conflict_resolver as cr

            importlib.reload(cr)

            with patch(
                "yadgar.backend.conflict_resolver.conflict_resolver._fetch_similar", return_value=[]
            ):
                cr.resolve_conflict(_candidate())

        mock_client_cls.assert_called_once()


# ── Test 4 ────────────────────────────────────────────────────────────────────


def test_gate_state_immutable_at_import():
    """Gate is captured at import. Changing env after import does NOT change behavior.

    I3 contract: the flag is read once at module import and frozen.
    """
    # Reload with flag OFF — gate should be captured as disabled
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("YADGAR_CONFLICT_RESOLVER", None)
        import yadgar.backend.conflict_resolver.conflict_resolver as cr

        importlib.reload(cr)

    # Now flip env to ON — must not affect already-imported module
    with patch("httpx.Client") as mock_client_cls:
        with patch.dict(os.environ, {"YADGAR_CONFLICT_RESOLVER": "on"}):
            result = cr.resolve_conflict(_candidate())

    # Gate was OFF at import; env change afterward is ignored
    assert result["op"] == "NOOP", (
        f"Expected NOOP (gate frozen at import OFF), got {result['op']!r}"
    )
    mock_client_cls.assert_not_called()
