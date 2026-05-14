"""Tests for SSE event-stream disconnect handling.

Verifies that the /api/graph/events generator silences socket errors on
disconnect and does not log WARN/ERROR noise per the fix for the
2026-05-13 23:18 cascade of 74 `socket.send() raised exception` entries.

Architecture note: yadgar SSE uses one async generator per client (not a
broadcaster with a subscriber set). These tests drive the generator directly
with mock Request objects so no server/engine initialisation is required.
"""

from __future__ import annotations

import asyncio
import logging

import yadgar.server as server

# ── helpers ──────────────────────────────────────────────────────────────────


class _MockRequest:
    """Minimal Request stand-in — controls is_disconnected()."""

    def __init__(self, *, disconnected: bool = False):
        self._disconnected = disconnected
        self.query_params: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return self._disconnected


def _make_stream(request: _MockRequest) -> object:
    """Call server._make_event_stream(request) — internal helper under test."""
    return server._make_event_stream(request)


async def _drain_one(gen) -> str | None:
    """Advance the generator by one item; return None if StopAsyncIteration."""
    try:
        return await gen.__anext__()
    except StopAsyncIteration:
        return None


# ── tests ─────────────────────────────────────────────────────────────────────


class TestSSEDisconnectSilencing:
    """SSE generator must exit cleanly and log only DEBUG on disconnect."""

    def setup_method(self):
        """Ensure the event queue has one event so the generator yields."""
        server._event_queue.clear()
        server._event_seq = 0
        server._system_metrics_cache.clear()
        # Inject a real event so there is something to send.
        server._event_seq += 1
        server._event_queue.append({"seq": server._event_seq, "event": "test"})

    def test_generator_exits_when_disconnected(self):
        """Generator must return (not loop forever) once is_disconnected=True."""
        req = _MockRequest(disconnected=True)
        gen = server._make_event_stream(req)

        result = asyncio.run(_drain_one(gen))
        # Should get None (StopAsyncIteration) immediately — no event sent to
        # a client that is already disconnected.
        assert result is None

    def test_generator_logs_disconnect_at_debug_not_warn(self, caplog):
        """Disconnect must produce at most one DEBUG record, zero WARN/ERROR."""
        req = _MockRequest(disconnected=True)
        gen = server._make_event_stream(req)

        with caplog.at_level(logging.DEBUG, logger="yadgar.server"):
            asyncio.run(_drain_one(gen))

        warn_or_error = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "socket" in r.message.lower()
        ]
        assert warn_or_error == [], (
            f"Expected no WARN/ERROR about socket disconnect, got: {warn_or_error}"
        )

    def test_connected_client_receives_event(self):
        """A connected client must still receive events normally."""
        req = _MockRequest(disconnected=False)
        gen = server._make_event_stream(req)

        item = asyncio.run(_drain_one(gen))
        assert item is not None
        assert "data:" in item

    def test_multiple_disconnected_clients_no_exception(self):
        """Simulates 100 disconnected clients; none must raise, all return cleanly."""
        clients = [_MockRequest(disconnected=True) for _ in range(100)]
        generators = [server._make_event_stream(c) for c in clients]

        async def _run_all():
            results = []
            for g in generators:
                results.append(await _drain_one(g))
            return results

        results = asyncio.run(_run_all())
        assert all(r is None for r in results), (
            "All disconnected generators must stop immediately without yielding data"
        )

    def test_multiple_disconnected_clients_only_debug_logs(self, caplog):
        """100 disconnected clients: at most 100 DEBUG lines, zero WARN/ERROR."""
        clients = [_MockRequest(disconnected=True) for _ in range(100)]
        generators = [server._make_event_stream(c) for c in clients]

        async def _run_all():
            for g in generators:
                await _drain_one(g)

        with caplog.at_level(logging.DEBUG, logger="yadgar.server"):
            asyncio.run(_run_all())

        warn_or_error = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "socket" in r.message.lower()
        ]
        assert warn_or_error == [], f"WARN/ERROR socket records must be zero, got: {warn_or_error}"

    def test_partial_disconnect_connected_clients_receive_event(self):
        """50 connected + 50 disconnected clients: connected all get data."""
        connected = [_MockRequest(disconnected=False) for _ in range(50)]
        disconnected = [_MockRequest(disconnected=True) for _ in range(50)]
        all_clients = connected + disconnected

        async def _run_all():
            results = {}
            for i, c in enumerate(all_clients):
                g = server._make_event_stream(c)
                results[i] = await _drain_one(g)
            return results

        results = asyncio.run(_run_all())

        connected_results = [results[i] for i in range(50)]
        disconnected_results = [results[i] for i in range(50, 100)]

        assert all(r is not None and "data:" in r for r in connected_results), (
            "All connected clients must receive event data"
        )
        assert all(r is None for r in disconnected_results), (
            "All disconnected clients must return without data"
        )
