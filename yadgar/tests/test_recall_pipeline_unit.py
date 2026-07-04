"""Unit tests for the _recall_pipeline module extraction (Train 1).

Tests:
  1. test_backend_tracing_provider_not_clobbered — subprocess-isolated test:
     calling setup_tracing("yadgar-backend") first then importing the recall
     pipeline path must NOT replace the backend provider. Guards the fragile
     startup-order invariant the backend safety depends on.
  2. test_side_effect_split_both_halves_fire — asserts BOTH DB-side and session-side
     writes happen when _apply_recall_side_effects runs (the combined wrapper).
  3. test_db_side_effects_only_boost — asserts _apply_recall_db_side_effects writes
     heat but does NOT mutate session state (SR/buffer/replay).
  4. test_session_side_effects_only_session — asserts _apply_recall_session_side_effects
     fires SR/buffer/replay but does NOT call boost_memories_access.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 1. Provider-invariant: backend tracing must survive server.* import chain
# ---------------------------------------------------------------------------


def test_backend_tracing_provider_not_clobbered():
    """Importing the recall pipeline after setup_tracing("yadgar-backend") must NOT
    replace the backend's global TracerProvider.

    This is the real invariant the backend safety relies on: OTel's set-once
    guarantee means the FIRST setup_tracing() call wins.  The backend calls
    setup_tracing("yadgar-backend") at lifespan startup; the transitive import
    of yadgar.server.__init__ (which fires on the first /recall request) then
    calls setup_tracing("yadgar-core") — but OTel ignores that second call.

    Must run in a subprocess: the in-suite process has already had
    yadgar.server imported (by other tests), so the OTel global provider is
    already set — re-checking in-process can't distinguish "backend won" from
    "some other test loaded it first".  A clean subprocess gives a fresh OTel
    state and proves the ordering invariant is non-vacuous.

    Regression guard: if a future top-level import in embed_service.py or
    ml_client.py loads yadgar.server BEFORE the lifespan setup_tracing call,
    the provider would be "yadgar-core" and this test would fail.
    """
    # The subprocess must NOT have OTEL_SDK_DISABLED set (we need a real provider).
    # It imports and calls setup_tracing("yadgar-backend") first, then imports
    # the recall pipeline (which fires the transitive yadgar.server chain and
    # calls setup_tracing("yadgar-core")), then asserts service.name is still
    # "yadgar-backend".
    probe = (
        "import os; "
        "os.environ.pop('OTEL_SDK_DISABLED', None); "
        # Disable OTLP exporter — no collector in test env; retries waste time
        "os.environ['YADGAR_OTLP_ENDPOINT'] = ''; "
        "os.environ.setdefault('YADGAR_DB_PATH', '/tmp/probe-test.db'); "
        "from yadgar.tracing import setup_tracing; "
        "setup_tracing('yadgar-backend'); "
        # Now trigger the transitive import chain (fires yadgar.server.__init__ -> _app -> setup_tracing('yadgar-core'))
        "import yadgar.server.tools._recall_pipeline; "
        "from opentelemetry import trace; "
        "provider = trace.get_tracer_provider(); "
        "svc = getattr(getattr(provider, 'resource', None), 'attributes', {}).get('service.name', 'MISSING'); "
        "print(svc)"
    )

    import os as _os

    # Pass through minimal env — exclude OTEL_SDK_DISABLED so OTel actually initialises.
    env = {k: v for k, v in _os.environ.items() if k != "OTEL_SDK_DISABLED"}
    env["YADGAR_DB_PATH"] = "/tmp/probe-test.db"
    env["YADGAR_OTLP_ENDPOINT"] = ""  # suppress OTLP retries (no collector in test env)

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
        f"Backend TracerProvider was clobbered by the server.* import chain.\n"
        f"Expected service.name='yadgar-backend', got {service_name!r}.\n"
        f"This means yadgar.server was imported BEFORE setup_tracing('yadgar-backend') "
        f"ran — the startup ordering invariant is broken.\n"
        f"stderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# 2. Combined wrapper fires BOTH halves
# ---------------------------------------------------------------------------


def test_side_effect_split_both_halves_fire():
    """_apply_recall_side_effects fires both DB half and session half."""
    from yadgar.server.tools._recall_pipeline import _apply_recall_side_effects

    mem = {"id": 42, "heat": 0.5, "_source": "memory", "content": "test"}
    merged = [mem]

    storage = MagicMock()
    storage._now_iso.return_value = "2026-01-01T00:00:00+00:00"

    with (
        patch("yadgar.server.tools._recall_pipeline._st") as mock_st,
        patch("yadgar.server.tools._recall_pipeline._record_recall_sr_transition") as mock_sr,
    ):
        mock_st._thermo = None
        mock_st._buffer = MagicMock()
        mock_st._replay = MagicMock()

        _apply_recall_side_effects(merged, "test query", storage)

        # DB half: boost was called with the memory id
        storage.boost_memories_access.assert_called_once()
        args = storage.boost_memories_access.call_args[0]
        assert 42 in args[0]

        # Session half: SR + buffer + replay all fired
        mock_sr.assert_called_once_with(merged)
        mock_st._buffer.capture_action.assert_called_once()
        mock_st._replay.record_tool_call.assert_called_once()


# ---------------------------------------------------------------------------
# 3. DB-only half: boost fired, session state NOT touched
# ---------------------------------------------------------------------------


def test_db_side_effects_only_boost():
    """_apply_recall_db_side_effects writes heat/thermo but NOT session state."""
    from yadgar.server.tools._recall_pipeline import _apply_recall_db_side_effects

    mem = {"id": 7, "heat": 0.3, "_source": "memory", "content": "db test"}
    merged = [mem]

    storage = MagicMock()
    storage._now_iso.return_value = "2026-01-01T00:00:00+00:00"

    with patch("yadgar.server.tools._recall_pipeline._st") as mock_st:
        mock_st._thermo = MagicMock()
        mock_st._buffer = MagicMock()
        mock_st._replay = MagicMock()
        mock_st._cognitive_map = None
        mock_st._last_recalled_ids = {}

        _apply_recall_db_side_effects(merged, "test query", storage)

        # DB side: boost written
        storage.boost_memories_access.assert_called_once()
        assert 7 in storage.boost_memories_access.call_args[0][0]

        # Heat stamp on the dict
        assert abs(merged[0]["heat"] - 0.4) < 0.001

        # Thermo access recorded
        mock_st._thermo.record_access.assert_called_once_with(7, was_useful=True)

        # Session-side NOT called
        mock_st._buffer.capture_action.assert_not_called()
        mock_st._replay.record_tool_call.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Session-only half: SR/buffer/replay fired, boost NOT called
# ---------------------------------------------------------------------------


def test_session_side_effects_only_session():
    """_apply_recall_session_side_effects fires SR/buffer/replay but NOT boost."""
    from yadgar.server.tools._recall_pipeline import _apply_recall_session_side_effects

    mem = {"id": 3, "heat": 0.6, "_source": "memory", "content": "session test"}
    merged = [mem]

    with (
        patch("yadgar.server.tools._recall_pipeline._st") as mock_st,
        patch("yadgar.server.tools._recall_pipeline._record_recall_sr_transition") as mock_sr,
    ):
        mock_st._buffer = MagicMock()
        mock_st._replay = MagicMock()
        mock_st._cognitive_map = None

        _apply_recall_session_side_effects(merged, "session query")

        # SR transition fired
        mock_sr.assert_called_once_with(merged)

        # Buffer capture_action fired
        mock_st._buffer.capture_action.assert_called_once()
        action_args = mock_st._buffer.capture_action.call_args[0]
        assert "session query" in action_args[2]  # query appears in the metadata arg

        # Replay ticked
        mock_st._replay.record_tool_call.assert_called_once()


# ---------------------------------------------------------------------------
# 5. Wiki rows skipped by DB half (no integer id or _source==wiki)
# ---------------------------------------------------------------------------


def test_db_side_effects_skips_wiki_rows():
    """_apply_recall_db_side_effects skips wiki rows (no integer id / _source=wiki)."""
    from yadgar.server.tools._recall_pipeline import _apply_recall_db_side_effects

    wiki_row = {"_source": "wiki", "id": "wiki:slug", "content": "wiki content"}
    merged = [wiki_row]

    storage = MagicMock()
    storage._now_iso.return_value = "2026-01-01T00:00:00+00:00"

    with patch("yadgar.server.tools._recall_pipeline._st") as mock_st:
        mock_st._thermo = MagicMock()

        _apply_recall_db_side_effects(merged, "query", storage)

        # Wiki row: no boost
        storage.boost_memories_access.assert_not_called()
        # Thermo not called for wiki
        mock_st._thermo.record_access.assert_not_called()


# ---------------------------------------------------------------------------
# 6. _forward_to_backend — payload contract + Bearer header
# ---------------------------------------------------------------------------


def test_forward_to_backend_payload_and_auth():
    """_forward_to_backend sends correct payload keys and Bearer auth header."""
    import sys
    from unittest.mock import MagicMock, patch

    # Import via sys.modules to get actual module (not the @_tool-decorated attribute
    # on the yadgar.server.tools package, which shadows the module).
    import yadgar.server.tools  # noqa: F401 — triggers module loading

    _recall_module = sys.modules["yadgar.server.tools.recall"]

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

    # httpx is lazy-imported inside _forward_to_backend; patch the canonical name.
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
        )

    # Correct URL
    assert captured["url"] == "http://backend:8001/recall"

    # Bearer auth present
    assert captured["headers"]["Authorization"] == "Bearer tok123"

    # All payload keys are valid RecallRequest fields (no extra=forbid 422 bombs)
    valid_fields = set(RecallRequest.model_fields.keys())
    payload_keys = set(captured["json"].keys())
    assert payload_keys <= valid_fields, (
        f"Payload has keys not in RecallRequest: {payload_keys - valid_fields}"
    )

    # Results returned correctly
    assert len(results) == 1
    assert results[0]["id"] == 1


def test_forward_to_backend_no_url_raises():
    """_forward_to_backend raises RuntimeError when YADGAR_EMBED_URL is unset."""
    import sys
    from unittest.mock import patch

    import yadgar.server.tools  # noqa: F401

    _recall_module = sys.modules["yadgar.server.tools.recall"]

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
            )
            raise AssertionError("Expected RuntimeError, got nothing")
        except RuntimeError as exc:
            assert "YADGAR_EMBED_URL" in str(exc)


def test_recall_backend_enabled_flag_calls_session_side_effects():
    """When RECALL_BACKEND_ENABLED=True, _apply_recall_session_side_effects fires after forward."""
    import sys
    from unittest.mock import MagicMock, patch

    # Get actual module object (not the package-level decorated attribute)
    import yadgar.server.tools  # noqa: F401

    _recall_module = sys.modules["yadgar.server.tools.recall"]
    _recall_fn = _recall_module.recall

    fake_results = [{"id": 99, "content": "flagged result", "heat": 0.7, "_source": "memory"}]

    with (
        patch.object(_recall_module, "get_settings") as mock_settings,
        patch.object(_recall_module, "_forward_to_backend", return_value=fake_results),
        patch.object(_recall_module, "_apply_recall_session_side_effects") as mock_session,
        patch.object(_recall_module, "_get_storage"),
        patch.object(_recall_module, "_st") as mock_st,
    ):
        settings = MagicMock()
        settings.UNIFIED_RECALL_ENABLED = True
        settings.RECALL_BACKEND_ENABLED = True
        mock_settings.return_value = settings
        mock_st._consolidation = None
        mock_st._pool = None

        with (
            patch("yadgar.server.tools.project._detect_branch", return_value="master"),
            patch("yadgar.server.tools.project._get_default_branch", return_value="master"),
        ):
            result = _recall_fn(
                query="flagged test",
                directory="/tmp",
                max_results=5,
            )

        # Session side effects MUST fire on the returned results
        mock_session.assert_called_once_with(fake_results, "flagged test")

        # Result is the backend's results
        assert result == fake_results


def test_recall_backend_enabled_fallback_on_error():
    """When RECALL_BACKEND_ENABLED=True but forward fails, falls back to in-core."""
    import sys
    from unittest.mock import MagicMock, patch

    import yadgar.server.tools  # noqa: F401

    _recall_module = sys.modules["yadgar.server.tools.recall"]
    _recall_fn = _recall_module.recall

    with (
        patch.object(_recall_module, "get_settings") as mock_settings,
        patch.object(
            _recall_module, "_forward_to_backend", side_effect=RuntimeError("backend down")
        ),
        patch.object(_recall_module, "_fanout_recall") as mock_fanout,
        patch.object(_recall_module, "_apply_recall_side_effects"),
        patch.object(_recall_module, "_get_storage") as mock_storage,
        patch.object(_recall_module, "_st") as mock_st,
    ):
        settings = MagicMock()
        settings.UNIFIED_RECALL_ENABLED = True
        settings.RECALL_BACKEND_ENABLED = True
        mock_settings.return_value = settings
        mock_st._consolidation = None
        mock_st._pool = None
        mock_storage.return_value._now_iso.return_value = "2026-01-01T00:00:00+00:00"
        mock_fanout.return_value = [{"id": 1, "content": "fallback", "heat": 0.5}]

        with (
            patch("yadgar.server.tools.project._detect_branch", return_value="master"),
            patch("yadgar.server.tools.project._get_default_branch", return_value="master"),
        ):
            result = _recall_fn(
                query="fallback test",
                directory="/tmp",
                max_results=5,
            )

        # In-core fanout was called (fallback path)
        mock_fanout.assert_called_once()
        # Result is from in-core fallback
        assert result[0]["id"] == 1
