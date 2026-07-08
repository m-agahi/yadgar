"""TDD tests for v5.51.0 §4.3 — hook recall latency budget (asyncio.wait_for timeout).

Tests verify:
- Timeout raises TimeoutError → _recall_with_timeout returns None
- Handler receives None → returns {"text": ""}
- yadgar_hook_recall_timeout_total{handler} counter is incremented on timeout
- Fast recall under timeout returns normally
- All three hook handlers (prompt-recall, instructions-loaded, subagent-start)
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch


def _slow_retriever(delay: float = 0.3) -> MagicMock:
    """A retriever whose sync recall blocks `delay`s. #81: recalls run in the
    bounded hook pool via run_in_executor, so the SLOW SEAM is now recall itself
    (a real sleep exceeding the budget), not a patched asyncio.to_thread."""
    r = MagicMock()
    r.recall = lambda *a, **kw: time.sleep(delay)
    return r


# ---------------------------------------------------------------------------
# Unit tests for _recall_with_timeout helper
# ---------------------------------------------------------------------------


class TestRecallWithTimeoutHelper:
    """Direct tests of the _recall_with_timeout shared helper."""

    def test_timeout_returns_none(self):
        """When asyncio.wait_for raises TimeoutError, helper returns None."""
        import yadgar.core.server.http as _http

        mock_retriever = _slow_retriever()

        async def _run():
            with patch(
                "yadgar._shared.config.get_settings",
                return_value=MagicMock(HOOK_RECALL_TIMEOUT_S=0.01),
            ):
                result = await _http._recall_with_timeout(
                    mock_retriever, "prompt-recall", "test query"
                )
            return result

        result = asyncio.run(_run())
        assert result is None, (
            f"_recall_with_timeout must return None on timeout, not raise. Got: {result!r}"
        )

    def test_fast_recall_returns_results(self):
        """When recall completes within timeout, helper returns the results."""
        import yadgar.core.server.http as _http

        expected = [{"content": "memory1"}, {"content": "memory2"}]
        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = expected

        async def _run():
            async def _fake_to_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch(
                    "yadgar._shared.config.get_settings",
                    return_value=MagicMock(HOOK_RECALL_TIMEOUT_S=5.0),
                ):
                    result = await _http._recall_with_timeout(
                        mock_retriever,
                        "subagent-start",
                        "task query",
                        max_results=5,
                        min_heat=0.0,
                        profile="fast",
                    )
            return result

        result = asyncio.run(_run())
        assert result == expected, (
            f"Expected {expected!r}, got {result!r}. "
            "Fast recall within timeout must propagate results normally."
        )

    def test_timeout_increments_prometheus_counter(self):
        """On timeout, yadgar_hook_recall_timeout_total{handler} is incremented."""
        import yadgar._shared.metrics as _metrics
        import yadgar.core.server.http as _http

        handler_name = "instructions-loaded"

        # Read counter before
        before = _metrics.yadgar_hook_recall_timeout_total.labels(handler=handler_name)._value.get()

        mock_retriever = _slow_retriever()

        async def _run():
            with patch(
                "yadgar._shared.config.get_settings",
                return_value=MagicMock(HOOK_RECALL_TIMEOUT_S=0.01),
            ):
                await _http._recall_with_timeout(mock_retriever, handler_name, "query")

        asyncio.run(_run())

        after = _metrics.yadgar_hook_recall_timeout_total.labels(handler=handler_name)._value.get()

        assert after == before + 1, (
            f"Counter yadgar_hook_recall_timeout_total{{handler={handler_name!r}}} "
            f"expected {before + 1}, got {after}. "
            "Counter MUST increment on timeout (I23 writer requirement)."
        )

    def test_non_timeout_exception_reraises(self):
        """Non-timeout exceptions from recall are NOT swallowed — they propagate."""
        import yadgar.core.server.http as _http

        class _RecallError(RuntimeError):
            pass

        mock_retriever = MagicMock()

        def _failing_recall(*a, **kw):
            raise _RecallError("DB connection lost")

        mock_retriever.recall = _failing_recall

        async def _run():
            with patch(
                "yadgar._shared.config.get_settings",
                return_value=MagicMock(HOOK_RECALL_TIMEOUT_S=5.0),
            ):
                await _http._recall_with_timeout(mock_retriever, "prompt-recall", "query")

        try:
            asyncio.run(_run())
            raise AssertionError("_RecallError should have propagated to caller")
        except _RecallError:
            pass  # expected


# ---------------------------------------------------------------------------
# Integration tests: hook handlers return {"text": ""} on timeout
# ---------------------------------------------------------------------------


def _make_mock_request(params: dict) -> MagicMock:
    """Create a minimal mock Starlette request with query params."""
    req = MagicMock()
    req.query_params = MagicMock()
    req.query_params.get = MagicMock(side_effect=lambda k, d="": params.get(k, d))
    return req


class TestPromptRecallHandlerTimeout:
    """prompt_recall hook returns empty text on timeout."""

    def test_timeout_returns_empty_text(self):
        """On timeout, prompt_recall handler returns {"text": ""}."""
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        mock_retriever = MagicMock()
        mock_request = _make_mock_request(
            {
                "query": "what is the architecture",
                "directory": "/home/user/project",
            }
        )

        async def _run():
            # Patch _recall_with_timeout to return None (simulating timeout)
            with patch.object(_st, "_retriever", mock_retriever):
                with patch(
                    "yadgar.core.server.http._recall_with_timeout",
                    new_callable=AsyncMock,
                    return_value=None,
                ):
                    # Also need to bypass rate limiters
                    with patch.object(_st, "_last_session_context", {}):
                        with patch.object(_st, "_last_prompt_recall", {}):
                            resp = await _http.hook_prompt_recall(mock_request)
            return resp

        resp = asyncio.run(_run())
        data = resp.body if hasattr(resp, "body") else b"{}"
        import json

        body = json.loads(data)
        assert body.get("text") == "", (
            f'Expected {{"text": ""}}, got {body!r}. '
            "prompt_recall must return empty text when recall times out."
        )


class TestInstructionsLoadedHandlerTimeout:
    """hook_instructions_loaded returns empty text on timeout."""

    def test_timeout_returns_empty_text(self):
        """On timeout, hook_instructions_loaded handler returns {"text": ""}."""
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        mock_retriever = MagicMock()
        mock_request = _make_mock_request(
            {
                "file_path": "/home/user/.claude/CLAUDE.md",
                "load_reason": "session_start",
            }
        )

        async def _run():
            with patch.object(_st, "_retriever", mock_retriever):
                with patch(
                    "yadgar.core.server.http._recall_with_timeout",
                    new_callable=AsyncMock,
                    return_value=None,
                ):
                    resp = await _http.hook_instructions_loaded(mock_request)
            return resp

        resp = asyncio.run(_run())
        import json

        body = json.loads(resp.body)
        assert body.get("text") == "", (
            f'Expected {{"text": ""}}, got {body!r}. '
            "hook_instructions_loaded must return empty text when recall times out."
        )


class TestSubagentStartHandlerTimeout:
    """hook_subagent_start returns empty text on timeout."""

    def test_timeout_returns_empty_text(self):
        """On timeout, hook_subagent_start handler returns {"text": ""}."""
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        mock_retriever = MagicMock()
        mock_request = _make_mock_request(
            {
                "agent_type": "general-purpose",
                "cwd": "/home/user/project",
            }
        )
        mock_request.json = AsyncMock(
            return_value={"description": "analyze code", "cwd": "/home/user/project"}
        )

        async def _run():
            with patch.object(_st, "_retriever", mock_retriever):
                with patch(
                    "yadgar.core.server.http._recall_with_timeout",
                    new_callable=AsyncMock,
                    return_value=None,
                ):
                    resp = await _http.hook_subagent_start(mock_request)
            return resp

        resp = asyncio.run(_run())
        import json

        body = json.loads(resp.body)
        assert body.get("text") == "", (
            f'Expected {{"text": ""}}, got {body!r}. '
            "hook_subagent_start must return empty text when recall times out."
        )


class TestTimeoutCounterAllHandlers:
    """Counter incremented for each handler name."""

    def test_counter_increments_for_prompt_recall(self):
        """yadgar_hook_recall_timeout_total{handler='prompt-recall'} increments on timeout."""
        import yadgar._shared.metrics as _metrics
        import yadgar.core.server.http as _http

        before = _metrics.yadgar_hook_recall_timeout_total.labels(
            handler="prompt-recall"
        )._value.get()

        async def _run():
            with patch(
                "yadgar._shared.config.get_settings",
                return_value=MagicMock(HOOK_RECALL_TIMEOUT_S=0.01),
            ):
                await _http._recall_with_timeout(_slow_retriever(), "prompt-recall", "q")

        asyncio.run(_run())
        after = _metrics.yadgar_hook_recall_timeout_total.labels(
            handler="prompt-recall"
        )._value.get()
        assert after == before + 1, f"Counter for prompt-recall: expected {before + 1}, got {after}"

    def test_counter_increments_for_subagent_start(self):
        """yadgar_hook_recall_timeout_total{handler='subagent-start'} increments on timeout."""
        import yadgar._shared.metrics as _metrics
        import yadgar.core.server.http as _http

        before = _metrics.yadgar_hook_recall_timeout_total.labels(
            handler="subagent-start"
        )._value.get()

        async def _run():
            with patch(
                "yadgar._shared.config.get_settings",
                return_value=MagicMock(HOOK_RECALL_TIMEOUT_S=0.01),
            ):
                await _http._recall_with_timeout(_slow_retriever(), "subagent-start", "q")

        asyncio.run(_run())
        after = _metrics.yadgar_hook_recall_timeout_total.labels(
            handler="subagent-start"
        )._value.get()
        assert after == before + 1, (
            f"Counter for subagent-start: expected {before + 1}, got {after}"
        )


# ---------------------------------------------------------------------------
# §7 #8 — core-resident hook decision (v5.108.0, Phase 2c)
# ---------------------------------------------------------------------------


class TestHookCoreResidentDecision:
    """Assert the SPLIT hook disposition (v5.113.0, hook-recall-forward-2026-07-06.md).

    §5.4 (recall-forward-only-2026-07-05.md) originally kept ALL THREE hook sites
    core-resident. v5.113.0 REVERSES that for prompt-recall ONLY (the cache-train
    trigger; the deployed prompt-recall hook always passes ?directory= so the
    backend can scope, making the forward quality-neutral). instructions-loaded +
    subagent-start STAY core-resident (no directory / whole-DB — forwarding would
    regress; see the plan's Trap 4).

    So:
      - prompt-recall → FORWARDS: _recall_with_timeout drives a _HookRecallForwarder
        (profile='fast'), NOT the raw Retriever.
      - instructions-loaded + subagent-start → still drive retriever.recall(profile='fast')
        in the bounded pool.

    This class encodes that split. The prompt-recall case is the authorized
    forward-path assertion the OLD docstring pointed to ("replace this test with a
    forward-path assertion") — an intentional #52 substitution, not weakening.
    """

    def test_prompt_recall_hook_forwards_via_forwarder(self):
        """prompt-recall handler drives _recall_with_timeout with a
        _HookRecallForwarder (forward-to-backend path), profile='fast'."""
        import asyncio
        from unittest.mock import MagicMock, patch

        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        mock_retriever = MagicMock()
        mock_request = MagicMock()
        mock_request.query_params = MagicMock()
        mock_request.query_params.get = MagicMock(
            side_effect=lambda k, d="": {
                "query": "test forward query",
                "directory": "/home/user/project",
            }.get(k, d)
        )

        captured: dict = {}

        async def _capture_recall(retriever, handler_name, *args, **kwargs):
            captured["retriever"] = retriever
            captured["kwargs"] = kwargs
            return []

        async def _run():
            with patch.object(_st, "_retriever", mock_retriever):
                with patch(
                    "yadgar.core.server.http._recall_with_timeout",
                    side_effect=_capture_recall,
                ):
                    with patch.object(_st, "_last_session_context", {}):
                        with patch.object(_st, "_last_prompt_recall", {}):
                            await _http.hook_prompt_recall(mock_request)

        asyncio.run(_run())
        assert captured["kwargs"].get("profile") == "fast", captured
        assert isinstance(captured["retriever"], _http._HookRecallForwarder), (
            "prompt-recall must forward via _HookRecallForwarder (v5.113.0), "
            f"not the raw retriever. Got {type(captured['retriever']).__name__}."
        )
        assert captured["retriever"]._directory == "/home/user/project", captured

    def test_instructions_loaded_hook_passes_profile_fast(self):
        """instructions-loaded handler calls _recall_with_timeout with profile='fast'."""
        import asyncio
        from unittest.mock import MagicMock, patch

        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        mock_retriever = MagicMock()
        mock_request = MagicMock()
        mock_request.query_params = MagicMock()
        mock_request.query_params.get = MagicMock(
            side_effect=lambda k, d="": {
                "file_path": "/home/user/.claude/CLAUDE.md",
                "load_reason": "session_start",
            }.get(k, d)
        )

        captured_kwargs = {}

        async def _capture_recall(retriever, handler_name, *args, **kwargs):
            captured_kwargs.update(kwargs)
            return []

        async def _run():
            with patch.object(_st, "_retriever", mock_retriever):
                with patch(
                    "yadgar.core.server.http._recall_with_timeout",
                    side_effect=_capture_recall,
                ):
                    await _http.hook_instructions_loaded(mock_request)

        asyncio.run(_run())
        assert captured_kwargs.get("profile") == "fast", (
            f"instructions-loaded hook must call _recall_with_timeout with profile='fast'. "
            f"Got profile={captured_kwargs.get('profile')!r}."
        )

    def test_subagent_start_hook_passes_profile_fast(self):
        """hook_subagent_start handler calls _recall_with_timeout with profile='fast'."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        mock_retriever = MagicMock()
        mock_request = MagicMock()
        mock_request.query_params = MagicMock()
        mock_request.query_params.get = MagicMock(
            side_effect=lambda k, d="": {
                "agent_type": "general-purpose",
                "cwd": "/home/user/project",
            }.get(k, d)
        )
        mock_request.json = AsyncMock(
            return_value={"description": "analyze code", "cwd": "/home/user/project"}
        )

        captured_kwargs = {}

        async def _capture_recall(retriever, handler_name, *args, **kwargs):
            captured_kwargs.update(kwargs)
            return []

        async def _run():
            with patch.object(_st, "_retriever", mock_retriever):
                with patch(
                    "yadgar.core.server.http._recall_with_timeout",
                    side_effect=_capture_recall,
                ):
                    await _http.hook_subagent_start(mock_request)

        asyncio.run(_run())
        assert captured_kwargs.get("profile") == "fast", (
            f"subagent-start hook must call _recall_with_timeout with profile='fast'. "
            f"Got profile={captured_kwargs.get('profile')!r}."
        )
