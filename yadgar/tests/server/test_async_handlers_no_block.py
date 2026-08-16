"""§9 async correctness — verify event loop not blocked during DB calls.

Tests:
- _action_batch protected by asyncio.Lock (Q2)
- storage.insert_action_log called via asyncio.to_thread (Q1)
- httpx in health_check uses async client (Q5)
- hook_post_compact and hook_session_context use asyncio.to_thread (#58)
- SSE event-queue read guarded by _event_lock (#58)
"""

import asyncio
from unittest.mock import MagicMock


def _effective_source(fn, depth: int = 2) -> str:
    """Source of ``fn`` plus the source of the module-level helpers it calls.

    These guards assert a property of the REQUEST PATH — "this blocking call
    runs off the event loop" — not a property of one function body. Reading
    only ``inspect.getsource(handler)`` conflates the two, and the difference
    is not academic: ``_checkpoint_resume_hint`` was extracted out of
    ``hook_session_context`` to keep it under the I30 function-length cap. The
    call is still awaited from the handler and still wrapped in
    ``asyncio.to_thread`` — behaviour identical — but the handler-only guard
    went blind and reported a regression that did not exist.

    Lowering the bar (a smaller count, a looser regex) would have made the
    guard weaker than the property it protects. Following the extraction
    instead keeps it exactly as strong, and survives the next one: ``depth=2``
    covers a helper that later extracts a helper of its own.

    Resolution is deliberately narrow — only names bound on ``fn``'s OWN
    defining module are followed, so an unrelated same-named symbol elsewhere
    cannot smuggle a passing match into the union.
    """
    import inspect
    import re

    module = inspect.getmodule(inspect.unwrap(fn))
    seen: set[str] = set()
    chunks: list[str] = []

    def _walk(target, level: int) -> None:
        try:
            src = inspect.getsource(target)
        except (OSError, TypeError):  # C-level / dynamically built callables  # fmt: skip
            return
        chunks.append(src)
        if level <= 0:
            return
        for name in sorted(set(re.findall(r"\b(_[A-Za-z0-9_]+)\s*\(", src))):
            if name in seen:
                continue
            seen.add(name)
            helper = getattr(module, name, None)
            if helper is None or not callable(helper):
                continue
            if inspect.getmodule(inspect.unwrap(helper)) is not module:
                continue
            _walk(helper, level - 1)

    _walk(fn, depth)
    return "\n".join(chunks)


class TestActionBatchLock:
    """Q2: _action_batch must be protected by asyncio.Lock."""

    def test_action_batch_lock_exists(self):
        """_action_batch_lock must be an asyncio.Lock instance."""
        import yadgar.core.server as srv

        assert hasattr(srv, "_action_batch_lock"), "_action_batch_lock must exist"
        assert isinstance(srv._action_batch_lock, asyncio.Lock), (
            "_action_batch_lock must be asyncio.Lock"
        )

    def test_concurrent_batch_appends_no_race(self):
        """Concurrent requests must not corrupt the batch dict."""
        import yadgar.core.server as srv

        orig_batch = srv._action_batch.copy()
        orig_storage = srv._storage

        mock_storage = MagicMock()
        mock_storage.insert_action_log = MagicMock()

        async def _run() -> int:
            srv._storage = mock_storage
            srv._action_batch.clear()

            async def _one_append(i):
                async with srv._action_batch_lock:
                    batch = srv._action_batch.setdefault("test_session", [])
                    batch.append({"item": i})

            await asyncio.gather(*[_one_append(i) for i in range(10)])
            return len(srv._action_batch.get("test_session", []))

        try:
            count = asyncio.run(_run())
            assert count == 10, "concurrent appends must not race under asyncio.Lock"
        finally:
            srv._action_batch.clear()
            srv._action_batch.update(orig_batch)
            srv._storage = orig_storage


class TestStorageCallOffThread:
    """Q1: blocking storage calls must use asyncio.to_thread."""

    def test_insert_action_log_called_via_to_thread(self):
        """hook_auto_capture source must use asyncio.to_thread for insert_action_log."""
        import inspect

        import yadgar.core.server as srv

        source = inspect.getsource(srv.hook_auto_capture)
        assert "asyncio.to_thread" in source, (
            "hook_auto_capture must call storage.insert_action_log via asyncio.to_thread"
        )


class TestHealthCheckAsync:
    """Q5: health_check must use async httpx, not blocking httpx.get."""

    def test_health_check_uses_async_client(self):
        """The health probe path must use AsyncClient.get, not blocking httpx.get.

        C2 P1 hoisted the probe logic out of health_check into the module-level
        _build_health_payload helper (so health_check stays under the complexity
        cap); the Q5 invariant now lives there.
        """
        import inspect

        import yadgar.core.server.http as srv_http

        source = inspect.getsource(srv_http._build_health_payload)
        assert "AsyncClient" in source, (
            "health probe path must use httpx.AsyncClient (Q5 — not blocking httpx.get)"
        )
        assert "httpx.get" not in source, "health probe path must not use blocking httpx.get"


class TestMetricsLock:
    """Q6: _system_metrics_cache must be read under _metrics_lock."""

    def test_metrics_lock_exists(self):
        """_metrics_lock must be a threading.Lock."""
        import threading

        import yadgar.core.server as srv

        assert hasattr(srv, "_metrics_lock"), "_metrics_lock must exist"
        assert isinstance(srv._metrics_lock, type(threading.Lock())), (
            "_metrics_lock must be a threading.Lock instance"
        )

    def test_api_system_snapshots_under_lock(self):
        """api_system handler must copy metrics dict under lock."""
        import inspect

        import yadgar.core.server as srv

        source = inspect.getsource(srv.api_system)
        assert "_metrics_lock" in source, (
            "api_system must read _system_metrics_cache under _metrics_lock"
        )


class TestBlockingCallsOffThread:
    """#58: hook_post_compact and hook_session_context must not block the event loop.

    AST/source guard — hermetic, no live server required.
    """

    def test_hook_post_compact_uses_to_thread(self):
        """hook_post_compact must call replay.restore via asyncio.to_thread (#58)."""
        import inspect

        import yadgar.core.server as srv

        source = inspect.getsource(srv.hook_post_compact)
        assert "asyncio.to_thread" in source, (
            "hook_post_compact must wrap replay.restore in asyncio.to_thread (#58)"
        )

    def test_hook_post_compact_returns_500_on_exception(self):
        """hook_post_compact must catch exceptions and return status_code=500 (#58)."""
        import inspect

        import yadgar.core.server as srv

        source = inspect.getsource(srv.hook_post_compact)
        assert "status_code=500" in source, (
            "hook_post_compact must return JSONResponse with status_code=500 on exception (#58)"
        )
        assert "except Exception" in source, (
            "hook_post_compact must have an except-Exception block (#58)"
        )

    def test_hook_session_context_project_brief_uses_to_thread(self):
        """hook_session_context must call _pb (project_brief) via asyncio.to_thread (#58).

        Named, not counted. A bare ``count("asyncio.to_thread") >= 3`` is
        satisfied by ANY three occurrences — including three wrappings of some
        other call — so it never actually pinned project_brief to a worker
        thread. The regex does, and it cannot be satisfied by accident when the
        effective source widens.
        """
        import re

        import yadgar.core.server as srv

        source = _effective_source(srv.hook_session_context)
        assert re.search(r"asyncio\.to_thread\(\s*_pb\b", source), (
            "hook_session_context must wrap project_brief (_pb) in asyncio.to_thread (#58)"
        )

    def test_hook_session_context_checkpoint_uses_to_thread(self):
        """get_active_checkpoint on the session-context path must be off-thread (#58).

        Reads the effective source (handler + the helpers it calls) so the
        guard follows ``_checkpoint_resume_hint``, which was extracted out of
        the handler for the I30 length cap without changing what it does.
        """
        import re

        import yadgar.core.server as srv

        source = _effective_source(srv.hook_session_context)
        # whitespace-tolerant: ruff may wrap the call across lines after `(`.
        assert re.search(r"asyncio\.to_thread\(\s*_storage\.get_active_checkpoint", source), (
            "the session-context path must wrap _storage.get_active_checkpoint "
            "in asyncio.to_thread (#58)"
        )

    def test_hook_session_context_list_blocks_uses_to_thread(self):
        """list_blocks on the session-context path must be off-thread (#58)."""
        import re

        import yadgar.core.server as srv

        source = _effective_source(srv.hook_session_context)
        # whitespace-tolerant: ruff may wrap the call across lines after `(`.
        assert re.search(r"asyncio\.to_thread\(\s*_storage2\.list_blocks", source), (
            "the session-context path must wrap _storage2.list_blocks in asyncio.to_thread (#58)"
        )

    def test_effective_source_actually_follows_an_extraction(self):
        """The walker must reach an extracted helper — otherwise the three
        guards above are just the old handler-only read wearing a new name.

        Pinned to ``_checkpoint_resume_hint`` because that IS the extraction
        that blinded them: its body is absent from the handler's own source and
        present in the effective source.
        """
        import inspect

        import yadgar.core.server as srv

        handler_only = inspect.getsource(srv.hook_session_context)
        effective = _effective_source(srv.hook_session_context)

        needle = "_storage.get_active_checkpoint"
        assert needle not in handler_only, (
            "premise broken: the checkpoint read is back inside the handler, so "
            "this test no longer proves the walker follows anything"
        )
        assert needle in effective, (
            "the effective source must reach the extracted _checkpoint_resume_hint"
        )


class TestEventQueueLock:
    """#58: SSE handler must read _event_queue under _event_lock to prevent deque races."""

    def test_sse_event_queue_read_uses_event_lock(self):
        """SSE event drain must hold _event_lock while reading _event_queue (#58)."""
        import inspect

        import yadgar.core.server as srv

        # The SSE drain loop lives in the module-level _make_event_stream generator
        # (api_graph_events just returns StreamingResponse(_make_event_stream(...))).
        source = inspect.getsource(srv._make_event_stream)
        assert "_event_lock" in source, (
            "_make_event_stream SSE loop must acquire _event_lock before reading _event_queue (#58)"
        )
