"""Unit tests for the _recall_pipeline module extraction (Phase 2a: forward-only).

Tests:
  1. test_backend_tracing_provider_not_clobbered
  2. test_side_effect_split_both_halves_fire
  3. test_db_side_effects_only_boost
  4. test_session_side_effects_only_session
  Phase 2a additions:
  5. test_forward_to_backend_threads_mode_and_profile
  6. test_recall_forward_only_calls_session_side_effects
  7. test_recall_forward_only_loud_failure  (replaces deleted fallback test)
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch


def test_backend_tracing_provider_not_clobbered():
    """Backend TracerProvider must not be clobbered by yadgar.server import chain."""
    probe = (
        "import os; "
        "os.environ.pop('OTEL_SDK_DISABLED', None); "
        "os.environ['YADGAR_OTLP_ENDPOINT'] = ''; "
        "os.environ.setdefault('YADGAR_DB_PATH', '/tmp/probe-test.db'); "
        "from yadgar._shared.observability.tracing import setup_tracing; "
        "setup_tracing('yadgar-backend'); "
        "import yadgar.backend.retrieval.recall_pipeline; "
        "from opentelemetry import trace; "
        "provider = trace.get_tracer_provider(); "
        "svc = getattr(getattr(provider, 'resource', None), 'attributes', {}).get('service.name', 'MISSING'); "
        "print(svc)"
    )

    import os as _os

    env = {k: v for k, v in _os.environ.items() if k != "OTEL_SDK_DISABLED"}
    env["YADGAR_DB_PATH"] = "/tmp/probe-test.db"
    env["YADGAR_OTLP_ENDPOINT"] = ""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert result.returncode == 0, (
        f"Subprocess probe failed (rc={result.returncode})\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )

    service_name = result.stdout.strip()
    assert service_name == "yadgar-backend", (
        f"Backend TracerProvider was clobbered. Expected 'yadgar-backend', got {service_name!r}.\n"
        f"stderr: {result.stderr}"
    )


def test_side_effect_split_both_halves_fire():
    """_apply_recall_side_effects fires both DB half and session half."""
    from yadgar.backend.retrieval.recall_pipeline import _apply_recall_side_effects

    mem = {"id": 42, "heat": 0.5, "_source": "memory", "content": "test"}
    merged = [mem]
    storage = MagicMock()
    storage._now_iso.return_value = "2026-01-01T00:00:00+00:00"

    # T2 Car E2: the session half lives in _shared.runtime.recall_session —
    # patch its module state; the pipeline combiner delegates to it.
    with (
        patch("yadgar.backend.retrieval.recall_pipeline._st") as mock_db_st,
        patch("yadgar._shared.runtime.recall_session._st") as mock_st,
        patch("yadgar._shared.runtime.recall_session._record_recall_sr_transition") as mock_sr,
    ):
        mock_db_st._thermo = None
        mock_st._buffer = MagicMock()
        mock_st._replay = MagicMock()

        _apply_recall_side_effects(merged, "test query", storage)

        storage.boost_memories_access.assert_called_once()
        assert 42 in storage.boost_memories_access.call_args[0][0]
        mock_sr.assert_called_once_with(merged)
        mock_st._buffer.capture_action.assert_called_once()
        mock_st._replay.record_tool_call.assert_called_once()


def test_db_side_effects_only_boost():
    """_apply_recall_db_side_effects writes heat/thermo but NOT session state."""
    from yadgar.backend.retrieval.recall_pipeline import _apply_recall_db_side_effects

    mem = {"id": 7, "heat": 0.3, "_source": "memory", "content": "db test"}
    merged = [mem]
    storage = MagicMock()
    storage._now_iso.return_value = "2026-01-01T00:00:00+00:00"

    with patch("yadgar.backend.retrieval.recall_pipeline._st") as mock_st:
        mock_st._thermo = MagicMock()
        mock_st._buffer = MagicMock()
        mock_st._replay = MagicMock()
        mock_st._cognitive_map = None
        mock_st._last_recalled_ids = {}

        _apply_recall_db_side_effects(merged, "test query", storage)

        storage.boost_memories_access.assert_called_once()
        assert 7 in storage.boost_memories_access.call_args[0][0]
        assert abs(merged[0]["heat"] - 0.4) < 0.001
        mock_st._thermo.record_access.assert_called_once_with(7, was_useful=True)
        mock_st._buffer.capture_action.assert_not_called()
        mock_st._replay.record_tool_call.assert_not_called()


def test_session_side_effects_only_session():
    """_apply_recall_session_side_effects fires SR/buffer/replay but NOT boost."""
    from yadgar._shared.runtime.recall_session import _apply_recall_session_side_effects

    mem = {"id": 3, "heat": 0.6, "_source": "memory", "content": "session test"}
    merged = [mem]

    with (
        patch("yadgar._shared.runtime.recall_session._st") as mock_st,
        patch("yadgar._shared.runtime.recall_session._record_recall_sr_transition") as mock_sr,
    ):
        mock_st._buffer = MagicMock()
        mock_st._replay = MagicMock()
        mock_st._cognitive_map = None

        _apply_recall_session_side_effects(merged, "session query")

        mock_sr.assert_called_once_with(merged)
        mock_st._buffer.capture_action.assert_called_once()
        action_args = mock_st._buffer.capture_action.call_args[0]
        assert "session query" in action_args[2]
        mock_st._replay.record_tool_call.assert_called_once()


def test_db_side_effects_skips_wiki_rows():
    """_apply_recall_db_side_effects skips wiki rows."""
    from yadgar.backend.retrieval.recall_pipeline import _apply_recall_db_side_effects

    wiki_row = {"_source": "wiki", "id": "wiki:slug", "content": "wiki content"}
    merged = [wiki_row]
    storage = MagicMock()
    storage._now_iso.return_value = "2026-01-01T00:00:00+00:00"

    with patch("yadgar.backend.retrieval.recall_pipeline._st") as mock_st:
        mock_st._thermo = MagicMock()
        _apply_recall_db_side_effects(merged, "query", storage)
        storage.boost_memories_access.assert_not_called()
        mock_st._thermo.record_access.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 2a: _forward_to_backend payload contract (mode + profile)
# ---------------------------------------------------------------------------


def test_forward_to_backend_payload_and_auth():
    """_forward_to_backend sends correct payload + Bearer auth + mode + profile.

    Phase 2a: payload must include mode and profile so the backend can
    dispatch landscape and rerank_level-gated fanout variants.
    """
    import yadgar.core.server.tools  # noqa: F401

    _recall_module = sys.modules["yadgar.core.server.tools.recall"]
    from yadgar.backend.embed_service import RecallRequest

    captured = {}

    def _fake_post(url, *, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": [{"id": 1, "content": "x", "heat": 0.5}]}
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    with (
        patch("httpx.post", _fake_post),
        patch.dict(
            "os.environ",
            {"YADGAR_EMBED_URL": "http://backend:8001", "YADGAR_MCP_AUTH_TOKEN": "tok123"},
        ),
    ):
        results = _recall_module._forward_to_backend(
            query="test",
            max_results=5,
            min_heat=0.0,
            directory="/tmp",
            current_branch="master",
            default_branch="master",
            type_filter="all",
            tags=None,
            mode=None,
            profile=None,
        )

    assert captured["url"] == "http://backend:8001/recall"
    assert captured["headers"]["Authorization"] == "Bearer tok123"

    valid_fields = set(RecallRequest.model_fields.keys())
    payload_keys = set(captured["json"].keys())
    assert payload_keys <= valid_fields, (
        f"Payload has keys not in RecallRequest: {payload_keys - valid_fields}"
    )
    assert "mode" in payload_keys, "mode must be in payload"
    assert "profile" in payload_keys, "profile must be in payload"

    assert len(results) == 1
    assert results[0]["id"] == 1


def test_forward_to_backend_threads_mode_and_profile():
    """_forward_to_backend forwards mode and profile values verbatim.

    Phase 2a: mode='landscape' and profile='fast' must reach the backend /recall.
    """
    import yadgar.core.server.tools  # noqa: F401

    _recall_module = sys.modules["yadgar.core.server.tools.recall"]
    captured = {}

    def _fake_post(url, *, json=None, headers=None, timeout=None):
        captured["payload"] = json
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": []}
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    with (
        patch("httpx.post", _fake_post),
        patch.dict("os.environ", {"YADGAR_EMBED_URL": "http://backend:8001"}),
    ):
        _recall_module._forward_to_backend(
            query="explore everything",
            max_results=10,
            min_heat=0.0,
            directory="/home/test/proj",
            current_branch="feat/x",
            default_branch="master",
            type_filter="all",
            tags=["adr"],
            mode="landscape",
            profile="fast",
        )

    assert captured["payload"]["mode"] == "landscape"
    assert captured["payload"]["profile"] == "fast"
    assert captured["payload"]["tags"] == ["adr"]


def test_forward_to_backend_no_url_raises():
    """_forward_to_backend raises RuntimeError when YADGAR_EMBED_URL is unset."""
    import yadgar.core.server.tools  # noqa: F401

    _recall_module = sys.modules["yadgar.core.server.tools.recall"]

    with patch.dict("os.environ", {}, clear=True):
        try:
            _recall_module._forward_to_backend(
                query="test",
                max_results=5,
                min_heat=0.0,
                directory="/tmp",
                current_branch=None,
                default_branch=None,
                type_filter="all",
                tags=None,
                mode=None,
                profile=None,
            )
            raise AssertionError("Expected RuntimeError, got nothing")
        except RuntimeError as exc:
            assert "YADGAR_EMBED_URL" in str(exc)


# ---------------------------------------------------------------------------
# Phase 2a: forward-only recall() behaviour
# ---------------------------------------------------------------------------


def test_recall_forward_only_calls_session_side_effects():
    """recall() routes _apply_recall_session_side_effects on returned results.

    Phase 2a: forward-only path — no flag needed. recall() unconditionally
    forwards to _forward_to_backend and runs the session half on the returned
    list. T3 Car 2: the session half is DEFERRED off the response path, so the
    test drains the fork before asserting the (eventually-consistent) call.
    """
    import yadgar.core.server.tools  # noqa: F401
    from yadgar._shared.runtime.recall_side_effects_fork import (
        drain_session_side_effects,
    )

    _recall_module = sys.modules["yadgar.core.server.tools.recall"]
    _recall_fn = _recall_module.recall

    fake_results = [{"id": 99, "content": "forward result", "heat": 0.7, "_source": "memory"}]

    with (
        patch.object(_recall_module, "_forward_to_backend", return_value=fake_results),
        patch.object(_recall_module, "_apply_recall_session_side_effects") as mock_session,
        patch.object(_recall_module, "_st") as mock_st,
    ):
        mock_st._consolidation = None
        mock_st._pool = None

        with (
            patch("yadgar.core.server.tools.project._detect_branch", return_value="master"),
            patch("yadgar.core.server.tools.project._get_default_branch", return_value="master"),
        ):
            result = _recall_fn(query="forward test", directory="/tmp", max_results=5)

        # Deferred session half — drain so the assertion is deterministic.
        drain_session_side_effects(timeout=10.0)
        mock_session.assert_called_once_with(fake_results, "forward test")
        assert result == fake_results


def test_recall_forward_only_loud_failure():
    """recall() raises on backend error — NO in-core _fanout_recall fallback.

    Phase 2a: replaces deleted test_recall_backend_enabled_fallback_on_error.
    Backend error → loud raise; _fanout_recall must NOT be called in-core.
    """
    import yadgar.core.server.tools  # noqa: F401

    _recall_module = sys.modules["yadgar.core.server.tools.recall"]
    _recall_fn = _recall_module.recall

    # T2 Car E2: core no longer binds the pipeline executor AT ALL — the old
    # _fanout_recall re-export is gone (stronger than "not called": not importable).
    assert not hasattr(_recall_module, "_fanout_recall"), (
        "core recall module must not bind the retrieval executor (_fanout_recall)"
    )

    with (
        patch.object(
            _recall_module, "_forward_to_backend", side_effect=RuntimeError("backend down")
        ),
        patch.object(_recall_module, "_st") as mock_st,
    ):
        mock_st._consolidation = None
        mock_st._pool = None

        with (
            patch("yadgar.core.server.tools.project._detect_branch", return_value="master"),
            patch("yadgar.core.server.tools.project._get_default_branch", return_value="master"),
        ):
            try:
                _recall_fn(query="loud fail test", directory="/tmp", max_results=5)
                raise AssertionError("Expected recall() to raise on backend error")
            except RuntimeError as exc:
                assert "backend down" in str(exc), f"Wrong error: {exc}"
