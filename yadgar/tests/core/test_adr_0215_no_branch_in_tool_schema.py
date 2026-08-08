"""ADR-0215 Car 5 — no MCP tool schema exposes a branch parameter.

The Python signature and the *registered* MCP schema are two different things:
a signature can be edited without the schema regenerating, in which case the
tool still silently accepts the kwarg. This asserts against the schema the
server actually publishes via ``tools/list`` — the same document an MCP client
validates caller arguments against.
"""

from __future__ import annotations

import asyncio

import pytest

# NOTE: do NOT set OTEL_SDK_DISABLED at module scope — see the same note in
# test_tool_always_load_meta.py (ADR-0037).


@pytest.fixture(scope="module")
def tool_schemas() -> dict[str, dict]:
    """name -> inputSchema for every tool the live mcp_server publishes."""
    import yadgar.core.server._app as _app

    listed = asyncio.run(_app.mcp_server.list_tools())
    return {t.name: (t.input_schema or {}) for t in listed}


def test_no_tool_exposes_a_branch_parameter(tool_schemas):
    """ADR-0215: branch scoping left the MCP surface entirely."""
    offenders = {
        name: sorted(k for k in (schema.get("properties") or {}) if "branch" in k)
        for name, schema in tool_schemas.items()
    }
    offenders = {name: keys for name, keys in offenders.items() if keys}
    assert offenders == {}, f"tools still publishing a branch parameter: {offenders}"


@pytest.mark.parametrize(
    "tool_name",
    [
        "memorize",
        "recall",
        "anchor",
        "checkpoint",
        "project_brief",
        "wiki_add",
        "wiki_query",
        "wiki_read",
        "agent_dispatch_prelude",
        "agent_prompt_save",
    ],
)
def test_hot_tools_are_still_published(tool_schemas, tool_name):
    """Guard the guard: an empty tool list would make the sweep above vacuous."""
    assert tool_name in tool_schemas
