"""TDD — ALL THREE hook sites forward to the backend /recall path.

Plans: docs/plans/hook-recall-forward-2026-07-06.md (#166: prompt-recall) +
ADR-0077/ADR-0078 (this hotfix: subagent-start + instructions-loaded too —
ADR-0078 kills ALL core DB paths; hooks are HTTP forwards only).

Properties under test:
- ALL hooks forward via _HookRecallForwarder with profile="fast" + right args.
- _forward_hook_recall passes the SHORT timeout (HOOK_RECALL_TIMEOUT_S) AND the
  deadline_ms budget to _forward_to_backend (#81 guard + ADR-0077 cancellation).
- The directory post-filter is still applied to forwarded results.
- Graceful degradation: backend raise / timeout -> {"text": ""}, never raises.
- Quality-neutral: handler emits backend rows unchanged (modulo dir filter).
- MCP recall keeps _forward_to_backend default timeout_s == 120.0.
- NO hook handler references _st._retriever anymore (source-level assert).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch


def _make_mock_request(params: dict) -> MagicMock:
    req = MagicMock()
    req.query_params = MagicMock()
    req.query_params.get = MagicMock(side_effect=lambda k, d="": params.get(k, d))
    return req


# ---------------------------------------------------------------------------
# 1 + 6. prompt-recall forwards with the right args; emits backend rows unchanged
# ---------------------------------------------------------------------------


class TestPromptRecallForwards:
    def test_forwards_with_profile_fast_and_dir(self):
        """hook_prompt_recall calls _forward_hook_recall with profile='fast',
        max_results=5, min_heat=0.0, and the caller directory."""
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        rows = [
            {"content": "m1", "directory_context": "/home/user/project"},
            {"content": "m2", "directory_context": "/home/user/project"},
        ]
        captured: dict = {}

        async def _fake_forward(forwarder, handler_name, *args, **kwargs):
            captured["forwarder"] = forwarder
            captured["handler"] = handler_name
            captured["args"] = args
            captured["kwargs"] = kwargs
            return rows

        req = _make_mock_request(
            {"query": "what is the architecture", "directory": "/home/user/project"}
        )

        async def _run():
            with patch.object(_st, "_retriever", MagicMock()):
                with patch(
                    "yadgar.core.server.http._recall_with_timeout", side_effect=_fake_forward
                ):
                    with patch.object(_st, "_last_session_context", {}):
                        with patch.object(_st, "_last_prompt_recall", {}):
                            return await _http.hook_prompt_recall(req)

        resp = asyncio.run(_run())
        kw = captured["kwargs"]
        assert kw.get("profile") == "fast", captured
        assert kw.get("max_results") == 5, captured
        assert kw.get("min_heat") == 0.0, captured
        # directory is threaded via the forwarder object (bound at construction),
        # so the backend can scope server-side.
        fwd = captured["forwarder"]
        assert isinstance(fwd, _http._HookRecallForwarder), captured
        assert fwd._directory == "/home/user/project", captured
        # quality-neutral: the emitted markdown contains the backend row contents
        body = json.loads(resp.body)
        assert "m1" in body["text"] and "m2" in body["text"], body

    def test_recall_with_timeout_runs_forward_callable_not_retriever_recall(self):
        """The prompt-recall handler must route recall through the forward path,
        NOT retriever.recall. Proven by: retriever.recall is never called."""
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        retriever = MagicMock()
        retriever.recall = MagicMock(return_value=[{"content": "SHOULD_NOT_APPEAR"}])

        forwarded = [{"content": "from-backend", "directory_context": "/d"}]

        async def _run():
            with patch.object(_st, "_retriever", retriever):
                with patch(
                    "yadgar.core.server.http._forward_hook_recall",
                    return_value=forwarded,
                ):
                    with patch.object(_st, "_last_session_context", {}):
                        with patch.object(_st, "_last_prompt_recall", {}):
                            req = _make_mock_request({"query": "real query", "directory": "/d"})
                            return await _http.hook_prompt_recall(req)

        resp = asyncio.run(_run())
        body = json.loads(resp.body)
        assert "from-backend" in body["text"], body
        retriever.recall.assert_not_called()


# ---------------------------------------------------------------------------
# 2. _forward_hook_recall passes the SHORT timeout to _forward_to_backend
# ---------------------------------------------------------------------------


class TestForwardHookRecallTimeout:
    def test_passes_short_hook_timeout(self):
        """_forward_hook_recall must pass timeout_s == HOOK_RECALL_TIMEOUT_S to
        _forward_to_backend (NOT the 120s MCP default) — #81 starvation guard.

        Car C7 (0047 §5 C7): ``_forward_hook_recall`` now also resolves
        ``project_id`` via ``hook_project_id(directory)`` before forwarding —
        and ``hook_project_id`` raises ``UnresolvedProjectError`` for ANY
        directory that is not a registered identity (ADR-0227: a directory is
        a filesystem hint, never a resolution source, on its own — see
        ``_project_param.resolve_effective_project``). This test is about
        FORWARDING (timeout_s / profile / directory / type_filter), not
        project resolution, so ``hook_project_id`` is monkeypatched to a fixed
        id — mirroring how ``yadgar.tests.core.conftest.TEST_PROJECT_ID``
        sidesteps the same resolver elsewhere in the suite.
        """
        import yadgar.core.server.http as _http
        from yadgar._shared.config import get_settings

        captured: dict = {}

        def _fake_backend(**kwargs):
            captured.update(kwargs)
            return []

        with (
            patch("yadgar.core.server.tools.recall._forward_to_backend", side_effect=_fake_backend),
            patch.object(_http, "hook_project_id", return_value="test-owner/test-repo"),
        ):
            _http._forward_hook_recall(
                "q",
                max_results=5,
                min_heat=0.0,
                directory="/home/user/project",
                profile="fast",
            )

        assert captured.get("timeout_s") == get_settings().HOOK_RECALL_TIMEOUT_S, captured
        assert captured.get("timeout_s") != 120.0, captured
        assert captured.get("profile") == "fast", captured
        assert captured.get("directory") == "/home/user/project", captured
        assert captured.get("type_filter") == "all", captured

    def test_directory_normalized_trailing_slash(self):
        """A trailing-slash directory is stripped before forwarding, so backend
        exact-string is_directory_eligible scoping does not silently return empty.

        ``hook_project_id`` monkeypatched for the same reason documented in
        ``test_passes_short_hook_timeout`` above — this test is about
        directory normalization, not project resolution.
        """
        import yadgar.core.server.http as _http

        captured: dict = {}

        def _fake_backend(**kwargs):
            captured.update(kwargs)
            return []

        with (
            patch("yadgar.core.server.tools.recall._forward_to_backend", side_effect=_fake_backend),
            patch.object(_http, "hook_project_id", return_value="test-owner/test-repo"),
        ):
            _http._forward_hook_recall(
                "q",
                max_results=5,
                min_heat=0.0,
                directory="/home/user/project/",  # trailing slash
                profile="fast",
            )

        assert captured.get("directory") == "/home/user/project", captured


# ---------------------------------------------------------------------------
# 3. Directory filter still applied to forwarded results
# ---------------------------------------------------------------------------


class TestDirectoryFilterStillApplied:
    def test_ineligible_rows_dropped(self):
        """_filter_prompt_recall_results drops rows whose project_id does not
        match the caller's project (idempotent atop backend scoping).

        Car C7 (0047 §5 C7) re-keyed this filter from ``directory_context``
        onto ``project_id`` + the ``'global'`` REACH TAG (was: the literal
        string ``directory_context="global"``). The row data below is
        updated to match: eligibility is now ``project_id`` equality OR
        ``"global" in tags`` — a plain ``directory_context`` field, if a row
        happened to carry one, is no longer read by this filter at all.
        """
        import yadgar.core.server.http as _http

        rows = [
            {"content": "keep", "project_id": "/home/user/project"},
            {"content": "drop", "project_id": "/some/other/project"},
            {"content": "global", "project_id": "/some/other/project", "tags": ["global"]},
        ]
        out = _http._filter_prompt_recall_results(rows, "/home/user/project")
        contents = {r["content"] for r in out}
        assert "keep" in contents
        assert "global" in contents  # sentinel (reach tag) always eligible
        assert "drop" not in contents


# ---------------------------------------------------------------------------
# 4 + 5. Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_backend_raise_returns_empty(self):
        """Backend raises -> handler returns {"text": ""} and does NOT raise."""
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        async def _boom(*_a, **_k):
            raise RuntimeError("YADGAR_EMBED_URL unreachable")

        async def _run():
            with patch.object(_st, "_retriever", MagicMock()):
                with patch("yadgar.core.server.http._recall_with_timeout", side_effect=_boom):
                    with patch.object(_st, "_last_session_context", {}):
                        with patch.object(_st, "_last_prompt_recall", {}):
                            req = _make_mock_request({"query": "q", "directory": "/d"})
                            return await _http.hook_prompt_recall(req)

        resp = asyncio.run(_run())
        body = json.loads(resp.body)
        assert body.get("text") == "", body

    def test_backend_timeout_returns_empty(self):
        """_recall_with_timeout returns None (timeout) -> {"text": ""}."""
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        async def _run():
            with patch.object(_st, "_retriever", MagicMock()):
                with patch(
                    "yadgar.core.server.http._recall_with_timeout",
                    new_callable=AsyncMock,
                    return_value=None,
                ):
                    with patch.object(_st, "_last_session_context", {}):
                        with patch.object(_st, "_last_prompt_recall", {}):
                            req = _make_mock_request({"query": "q", "directory": "/d"})
                            return await _http.hook_prompt_recall(req)

        resp = asyncio.run(_run())
        body = json.loads(resp.body)
        assert body.get("text") == "", body


# ---------------------------------------------------------------------------
# 7. MCP recall keeps the 120s default timeout
# ---------------------------------------------------------------------------


class TestMcpRecallTimeoutDefaultUnchanged:
    def test_forward_to_backend_default_timeout_is_120(self):
        """_forward_to_backend default timeout_s must remain 120.0 (MCP path)."""
        import inspect

        from yadgar.core.server.tools.recall import _forward_to_backend

        sig = inspect.signature(_forward_to_backend)
        assert sig.parameters["timeout_s"].default == 120.0, sig


# ---------------------------------------------------------------------------
# 8. ALL hooks forward (ADR-0078 — no in-core retrieval left in hook paths)
# ---------------------------------------------------------------------------


class TestAllHooksForward:
    def test_instructions_loaded_forwards_via_forwarder(self):
        """instructions-loaded now drives a _HookRecallForwarder (ADR-0078).
        It has no caller directory -> forwards with directory "" (whole-DB
        semantics server-side: empty scope.directory -> caller_dir None)."""
        import yadgar.core.server.http as _http

        captured: dict = {}

        async def _capture(target, handler_name, *args, **kwargs):
            captured["target"] = target
            captured["profile"] = kwargs.get("profile")
            return []

        req = _make_mock_request(
            {"file_path": "/home/user/.claude/CLAUDE.md", "load_reason": "session_start"}
        )

        async def _run():
            with patch("yadgar.core.server.http._recall_with_timeout", side_effect=_capture):
                return await _http.hook_instructions_loaded(req)

        asyncio.run(_run())
        assert isinstance(captured.get("target"), _http._HookRecallForwarder), captured
        assert captured["target"]._directory == "", captured
        assert captured.get("profile") == "fast", captured

    def test_subagent_start_forwards_via_forwarder_bound_to_cwd(self):
        """subagent-start forwards bound to its cwd (ADR-0078; the whole-DB ->
        directory-scoped change is the accepted behavior shift)."""
        import yadgar.core.server.http as _http

        captured: dict = {}

        async def _capture(target, handler_name, *args, **kwargs):
            captured["target"] = target
            captured["profile"] = kwargs.get("profile")
            return []

        req = _make_mock_request({"agent_type": "general-purpose", "cwd": "/home/user/project"})
        req.json = AsyncMock(
            return_value={"description": "analyze code", "cwd": "/home/user/project"}
        )

        async def _run():
            with patch("yadgar.core.server.http._recall_with_timeout", side_effect=_capture):
                return await _http.hook_subagent_start(req)

        asyncio.run(_run())
        assert isinstance(captured.get("target"), _http._HookRecallForwarder), captured
        assert captured["target"]._directory == "/home/user/project", captured
        assert captured.get("profile") == "fast", captured

    def test_prompt_recall_no_directory_still_forwards(self):
        """ADR-0078 deletes the in-core fallback: a directory-less prompt-recall
        forwards with directory "" (whole-DB semantics server-side) instead of
        falling back to _st._retriever."""
        import yadgar.core.server.http as _http

        captured: dict = {}

        async def _capture(target, handler_name, *args, **kwargs):
            captured["target"] = target
            return []

        req = _make_mock_request({"query": "real query"})  # no directory

        async def _run():
            import yadgar._shared.runtime.state as _st

            with patch("yadgar.core.server.http._recall_with_timeout", side_effect=_capture):
                with patch.object(_st, "_last_session_context", {}):
                    with patch.object(_st, "_last_prompt_recall", {}):
                        return await _http.hook_prompt_recall(req)

        asyncio.run(_run())
        assert isinstance(captured.get("target"), _http._HookRecallForwarder), captured
        assert captured["target"]._directory == "", captured


# ---------------------------------------------------------------------------
# 9. Source-level guard: no in-core retriever reference left in hook paths
# ---------------------------------------------------------------------------


class TestNoInCoreRetrieverInHookPaths:
    def test_hook_handlers_have_no_retriever_reference(self):
        """ADR-0078: hook handlers are HTTP forwards only — no _st._retriever,
        no retriever.recall. Source-level grep-assert (caller-audit pattern)."""
        import inspect

        import yadgar.core.server.http as _http

        for handler in (
            _http.hook_prompt_recall,
            _http.hook_instructions_loaded,
            _http.hook_subagent_start,
        ):
            src = inspect.getsource(handler)
            assert "_st._retriever" not in src, (
                f"{handler.__name__} still references _st._retriever (ADR-0078 violation)"
            )
            assert "retriever.recall" not in src, (
                f"{handler.__name__} still calls retriever.recall (ADR-0078 violation)"
            )


# ---------------------------------------------------------------------------
# 10. ADR-0077: the hook forward carries the client deadline budget
# ---------------------------------------------------------------------------


class TestForwardHookRecallDeadline:
    def test_passes_deadline_ms_budget(self):
        """_forward_hook_recall must pass deadline_ms == HOOK_RECALL_TIMEOUT_S in
        ms so the backend aborts stages once the client has given up.

        ``hook_project_id`` monkeypatched for the same reason documented in
        ``TestForwardHookRecallTimeout.test_passes_short_hook_timeout`` above.
        """
        import yadgar.core.server.http as _http
        from yadgar._shared.config import get_settings

        captured: dict = {}

        def _fake_backend(**kwargs):
            captured.update(kwargs)
            return []

        with (
            patch("yadgar.core.server.tools.recall._forward_to_backend", side_effect=_fake_backend),
            patch.object(_http, "hook_project_id", return_value="test-owner/test-repo"),
        ):
            _http._forward_hook_recall(
                "q",
                max_results=5,
                min_heat=0.0,
                directory="/home/user/project",
                profile="fast",
            )

        expected_ms = int(get_settings().HOOK_RECALL_TIMEOUT_S * 1000)
        assert captured.get("deadline_ms") == expected_ms, captured


# ---------------------------------------------------------------------------
# Car G (task #63): throttle response shape must include retry_after_seconds
# ---------------------------------------------------------------------------


class TestPromptRecallThrottleShape:
    """Car G (task #63): the 120 s throttle on /hooks/prompt-recall used to
    return ``{"text": "", "skipped": "rate_limited"}`` — indistinguishable
    from a real empty recall, so operators investigating "why is no
    injection happening" wasted time chasing a backend bug that was actually
    a throttle hit. The response must now carry ``retry_after_seconds`` and
    the handler must log a WARN.
    """

    def test_throttle_response_includes_retry_after_seconds(self):
        """When the prompt-recall throttle fires, the response body must
        include ``retry_after_seconds`` so a client (or operator reading
        curl output) can tell a throttle hit from a real empty recall.
        """
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        # Pre-stamp the throttle key with a recent timestamp so the 120 s
        # check at http.py fires on the very next call.
        throttle_key = "/proj/throttle-shape"
        now = __import__("time").monotonic()
        with (
            patch.object(_st, "_last_session_context", {}),
            patch.object(_st, "_last_prompt_recall", {throttle_key: now}),
        ):
            req = _make_mock_request(
                {"query": "anything", "directory": throttle_key, "project": "owner/repo"}
            )

            async def _run():
                # _recall_with_timeout is unreachable when the throttle fires;
                # patch it so any regression that lets the call through is
                # caught as a loud failure.
                with patch(
                    "yadgar.core.server.http._recall_with_timeout",
                    side_effect=AssertionError("throttle did not fire — handler hit recall"),
                ):
                    return await _http.hook_prompt_recall(req)

            resp = asyncio.run(_run())

        body = json.loads(resp.body)
        assert body.get("text") == "", body
        assert body.get("skipped") == "rate_limited", body
        # The new key — exactly what was missing before Car G.
        assert "retry_after_seconds" in body, (
            "prompt-recall throttle response must include retry_after_seconds "
            "(Car G task #63) so operators can tell a throttle hit from a "
            "real empty recall. Got: " + repr(body)
        )
        # retry_after_seconds is a non-negative int bounded by the 120 s window
        # (just-stamped: value should be very close to 120).
        retry = body["retry_after_seconds"]
        assert isinstance(retry, int), (
            f"retry_after_seconds must be int, got {type(retry).__name__}"
        )
        assert 0 <= retry <= 120, f"retry_after_seconds out of range: {retry}"
