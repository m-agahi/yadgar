"""ADR-0047 / #45 — @_tool(always_load=True) emits anthropic/alwaysLoad meta.

TDD red-first test: verifies that the 7 hot tools carry
  tool.meta == {"anthropic/alwaysLoad": True}
AND their wire-protocol _meta field matches, while cold tools have no meta.
"""

from __future__ import annotations

import asyncio

import pytest

# NOTE: do NOT set OTEL_SDK_DISABLED at module scope. The OTel SDK reads it at
# TracerProvider construction time, process-wide; under xdist --dist loadgroup
# this module can be collected on the same worker as span-emission tests and
# make every provider they build born no-op (empty span sets). ADR-0037.
# yadgar's tracing does not explode on an absent collector; export noise is
# already suppressed by YADGAR_OTLP_ENDPOINT='' in the test env.

HOT_TOOLS = frozenset(
    {
        "recall",
        "memorize",
        "project_brief",
        "checkpoint",
        "restore",
        "anchor",
        "agent_dispatch_prelude",
    }
)

# A tool that is NOT hot — used to assert the negative.
COLD_TOOL = "dlq_inspect"

EXPECTED_META = {"anthropic/alwaysLoad": True}


@pytest.fixture(scope="module")
def listed_tools():
    """Return the list of Tool objects from the live mcp_server."""
    import yadgar.server._app as _app

    return asyncio.run(_app.mcp_server.list_tools())


@pytest.fixture(scope="module")
def tool_map(listed_tools):
    return {t.name: t for t in listed_tools}


class TestAlwaysLoadMeta:
    def test_hot_tools_have_meta(self, tool_map):
        """Each of the 7 hot tools must carry meta == EXPECTED_META."""
        for name in HOT_TOOLS:
            tool = tool_map.get(name)
            assert tool is not None, f"Tool '{name}' not found in mcp_server"
            assert tool.meta == EXPECTED_META, (
                f"Tool '{name}' has meta={tool.meta!r}, want {EXPECTED_META!r}"
            )

    def test_hot_tools_wire_meta(self, tool_map):
        """Wire-protocol field _meta must be present for all hot tools."""
        for name in HOT_TOOLS:
            tool = tool_map.get(name)
            assert tool is not None, f"Tool '{name}' not found in mcp_server"
            dumped = tool.model_dump(by_alias=True)
            assert dumped.get("_meta") == EXPECTED_META, (
                f"Tool '{name}' wire _meta={dumped.get('_meta')!r}, want {EXPECTED_META!r}"
            )

    def test_cold_tool_has_no_meta(self, tool_map):
        """A non-hot tool must NOT carry alwaysLoad meta."""
        tool = tool_map.get(COLD_TOOL)
        assert tool is not None, f"Cold tool '{COLD_TOOL}' not found in mcp_server"
        assert tool.meta is None or tool.meta.get("anthropic/alwaysLoad") is not True, (
            f"Cold tool '{COLD_TOOL}' unexpectedly has alwaysLoad meta"
        )
