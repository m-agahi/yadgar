"""Unit tests for the T2 Car B restore-forward contract.

Covers:
  1. _forward_restore sends {directory} to ${YADGAR_EMBED_URL}/restore with
     Bearer auth, and unwraps the backend {"result": ...} envelope.
  2. _forward_restore raises RuntimeError when YADGAR_EMBED_URL is unset
     (forward-only — the restore impl no longer exists in the core process).
  3. The restore MCP tool is a thin forwarder (returns _forward_restore output).
  4. pre_compact_drain is a registered /admin op and its backend body delegates
     to CheckpointRestore.pre_compact_drain.

These are pure-unit (no live backend): httpx.post / the forward helper are
patched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1 + 2: _forward_restore HTTP contract
# ---------------------------------------------------------------------------


def test_forward_restore_payload_and_auth():
    """_forward_restore POSTs {directory} to /restore with Bearer auth; unwraps result."""
    from yadgar.core.server.tools._forward import _forward_restore

    captured: dict = {}

    def _fake_post(url, *, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        resp = MagicMock()
        resp.json.return_value = {"result": {"formatted": "# Restored", "epoch": 3}}
        resp.raise_for_status.return_value = None
        return resp

    with (
        patch("httpx.post", _fake_post),
        patch.dict(
            "os.environ",
            {"YADGAR_EMBED_URL": "http://backend:8001", "YADGAR_MCP_AUTH_TOKEN": "tok123"},
        ),
    ):
        result = _forward_restore("/my/project")

    assert captured["url"] == "http://backend:8001/restore"
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["json"] == {"directory": "/my/project"}
    # Envelope unwrapped: caller gets the inner restore payload dict.
    assert result == {"formatted": "# Restored", "epoch": 3}


def test_forward_restore_no_url_raises():
    """_forward_restore raises RuntimeError when YADGAR_EMBED_URL is unset (forward-only)."""
    from yadgar.core.server.tools._forward import _forward_restore

    with patch.dict("os.environ", {"YADGAR_EMBED_URL": ""}, clear=False):
        with pytest.raises(RuntimeError) as exc:
            _forward_restore("/my/project")
    assert "YADGAR_EMBED_URL" in str(exc.value)


# ---------------------------------------------------------------------------
# 3: restore MCP tool is a thin forwarder
# ---------------------------------------------------------------------------


def test_restore_tool_forwards_to_backend():
    """The restore MCP tool returns the forwarded backend payload verbatim."""
    import yadgar.core.server.tools.misc as misc_mod

    payload = {"formatted": "# Restored", "anchored_memories": 2}
    with patch.object(misc_mod, "_forward_restore", return_value=payload) as fwd:
        result = misc_mod.restore(directory="/proj")

    fwd.assert_called_once_with("/proj")
    assert result is payload


# ---------------------------------------------------------------------------
# 4: pre_compact_drain admin op (backend body)
# ---------------------------------------------------------------------------


def test_pre_compact_drain_is_registered_admin_op():
    from yadgar.backend.admin_exec import admin_ops

    assert "pre_compact_drain" in admin_ops()


def test_pre_compact_drain_op_delegates_to_replay():
    """The backend op body runs CheckpointRestore.pre_compact_drain(directory)."""
    import yadgar.backend.admin_exec.restoration as resto_mod

    replay = MagicMock()
    replay.pre_compact_drain.return_value = {
        "status": "drained",
        "epoch": 4,
        "auto_checkpoint_created": True,
    }
    with (
        patch.object(resto_mod, "ensure_restoration_engines"),
        patch.object(resto_mod, "_get_replay", return_value=replay),
    ):
        result = resto_mod.pre_compact_drain({"directory": "/proj"})

    # HOOKS Car 2: op body forwards transcript_path (None when absent).
    # v5.135 drain car: also forwards host-parsed in_flight (None when absent).
    replay.pre_compact_drain.assert_called_once_with("/proj", transcript_path=None, in_flight=None)
    assert result == {"status": "drained", "epoch": 4, "auto_checkpoint_created": True}


def test_pre_compact_drain_op_forwards_transcript_path():
    """HOOKS Car 2: an explicit transcript_path in the payload reaches replay."""
    import yadgar.backend.admin_exec.restoration as resto_mod

    replay = MagicMock()
    replay.pre_compact_drain.return_value = {"status": "drained"}
    with (
        patch.object(resto_mod, "ensure_restoration_engines"),
        patch.object(resto_mod, "_get_replay", return_value=replay),
    ):
        resto_mod.pre_compact_drain({"directory": "/proj", "transcript_path": "/tmp/s.jsonl"})

    replay.pre_compact_drain.assert_called_once_with(
        "/proj", transcript_path="/tmp/s.jsonl", in_flight=None
    )
