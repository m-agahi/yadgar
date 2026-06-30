"""Wiring tests for Fix A (daemon-offload-A): AP1 + Claim-1 startup assert.

AP1 — `functools.wraps` over an `async def` _instrumented preserves coroutine
detection, so FastMCP takes its `await fn(...)` branch (func_metadata.py:92).

Claim-1 — offload ON + no remote engine (no YADGAR_EMBED_URL) → fail loud at
engine init, because the local torch engine would run CPU on the worker and
break the GIL premise.

OTEL is NOT touched at module scope.
"""

from __future__ import annotations

import inspect

import pytest


def test_ap1_registered_tool_is_coroutine_but_module_name_stays_sync():
    """Dual-wrapper invariant (Fix A):

    - The function REGISTERED with FastMCP must be async so the SDK dispatches via
      `await fn(...)` (func_metadata.py:92) and the offload path runs.
    - The function RETURNED as the module-level name must stay SYNC so direct
      callers (tests, cross-tool calls, background tasks) keep the pre-Fix-A
      contract (call it, get a result — not a coroutine).
    """
    from yadgar.server._app import _tool, mcp_server

    def sample_sync_tool_xyz(x: int) -> int:
        return x + 1

    returned = _tool()(sample_sync_tool_xyz)

    # Module-level name is sync and directly callable.
    assert not inspect.iscoroutinefunction(returned), "module name must stay sync"
    assert returned(2) == 3, "direct sync call must return the result, not a coroutine"

    # Registered tool (FastMCP) is async → offload-friendly branch.
    tool = mcp_server._tool_manager.get_tool("sample_sync_tool_xyz")
    assert tool is not None, "tool must be registered with FastMCP"
    assert inspect.iscoroutinefunction(tool.fn), (
        "the FastMCP-registered tool must be a coroutine function so the SDK takes "
        "its `await fn(...)` branch and the body is offloaded off the loop"
    )


def test_async_tool_body_rejected():
    """Sync-only guard: an async @_tool() body fails loud at decoration."""
    from yadgar.server._app import _tool

    async def bad_async_tool() -> int:  # pragma: no cover — body never runs
        return 1

    with pytest.raises(TypeError, match="sync def"):
        _tool()(bad_async_tool)


def test_claim1_offload_on_local_engine_fails_loud(monkeypatch):
    """Offload ON + no YADGAR_EMBED_URL → engine init refuses to start."""
    from yadgar.config import get_settings
    from yadgar.server.lifecycle import _init_embedding_client

    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    monkeypatch.delenv("YADGAR_EMBED_URL", raising=False)

    with pytest.raises(RuntimeError, match="OFFLOAD_TOOLS"):
        _init_embedding_client(None, get_settings())


def test_claim1_offload_off_local_engine_allowed(monkeypatch):
    """Offload OFF + local mode is fine — the assert only bites when offload is ON."""
    from yadgar.config import get_settings
    from yadgar.server.lifecycle import _init_embedding_client

    monkeypatch.delenv("YADGAR_OFFLOAD_TOOLS", raising=False)
    monkeypatch.delenv("YADGAR_EMBED_URL", raising=False)

    # Builds local engines without raising (model load is lazy / no-op here).
    embeddings, ml_client = _init_embedding_client(None, get_settings())
    assert embeddings is not None
    assert ml_client is not None
