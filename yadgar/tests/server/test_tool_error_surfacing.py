"""A structured ``YadgarError`` must reach the MCP client with its message.

THE DEFECT
----------
``mcp.server.mcpserver.tools.base.Tool.run`` sorts every failure into two
buckets. A ``ToolError`` is an *anticipated* failure: its text is forwarded to
the model as ``Error executing tool <name>: <message>``. Anything else is a
*crash*: the model receives the bare string ``Error executing tool <name>``, the
original text is deliberately withheld, and the traceback stays server-side.

Every yadgar tool raised its structured errors as plain exceptions, so the SDK
classified all of them as crashes. Observed live on 2026-08-31: thirteen
consecutive ``recall`` calls, plus ``recent_memories`` and
``agent_dispatch_prelude``, came back to the client as bare
``Error executing tool recall`` with no detail — while the exception the daemon
had actually raised carried the precise remedy::

    recall: no project_id was supplied and none can be derived (ADR-0227:
    yadgar never guesses an identity). Fix: pass project="owner/repo".

``yadgar/_shared/errors.py``'s module docstring states the intent outright —
"the reader of this error is an agent, not a human ... every raise therefore
names the TOOL that could not resolve and the FIX that would make the same call
succeed". The SDK was discarding exactly that sentence, so an agent that hit the
condition learned only that something broke, retried the identical call, and hit
it again.

WHY ``YadgarError`` IS THE LINE
-------------------------------
It is the typed hierarchy that exists *because* these failures are anticipated
and caller-fixable — it carries ``error_code`` and a structured ``payload`` for
that reason. A blanket ``except Exception`` would forward genuine crash text to
the client and cost the ERROR-level traceback the SDK logs for one, which is the
protection ``UnexpectedToolError`` is there to give. So: yadgar's own structured
errors become ``ToolError``; everything else stays a crash.

WHY THE ASYNC WRAPPER ONLY
--------------------------
``_build_tool_wrappers`` returns a pair. The async wrapper is what is registered
with the MCP server, so it is the only one a client ever reaches. The sync
wrapper is the module-level name that internal and test callers invoke directly;
translating there would change the in-process contract for callers that catch
``UnresolvedProjectError`` by type, and would buy nothing — no client is on the
other end of it.

WHY THIS ASSERTS ``ToolError`` AND NOT THE SDK'S OWN SORT
---------------------------------------------------------
The two-bucket sort above is mcp **2.1.x** behaviour. Under 2.0.0 the crash
branch reads ``raise ToolError(f"Error executing tool {name}: {e}")`` — every
message reached the client and this defect did not exist. ``pyproject.toml``
declares ``mcp>=2.0.0`` with no upper bound and the Dockerfile installs with
``pip install /app`` rather than from ``uv.lock``, so the shipped image resolved
2.1.1 while the locked test environment stayed on 2.0.0: the contract changed
under the daemon and no test could see it.

So this module asserts the property that holds on BOTH versions — a yadgar
structured error leaves the wrapper as a ``ToolError`` carrying its own text —
rather than naming ``UnexpectedToolError``, which does not exist in 2.0.0.
Raising ``ToolError`` ourselves is what makes the tool surface independent of
which of the two sorts the installed SDK applies.
"""

from __future__ import annotations

import asyncio

import pytest

from yadgar._shared.errors import PROJECT_FIX_HINT, UnresolvedProjectError


def _wrappers(fn):
    """Build the real (sync, async) instrumented pair for *fn*."""
    from yadgar.core.server._app import _build_tool_wrappers

    return _build_tool_wrappers(fn, fn, lambda _r: 0)


class TestStructuredErrorsReachTheClient:
    def test_unresolved_project_becomes_a_tool_error_carrying_the_fix(self) -> None:
        """The remedy sentence must survive the SDK's crash/anticipated sort."""
        from mcp.server.mcpserver.exceptions import ToolError

        def _tool():
            raise UnresolvedProjectError("recall", detail="(directory='/x' is a hint)")

        _sync, async_wrapper = _wrappers(_tool)

        with pytest.raises(ToolError) as excinfo:
            asyncio.run(async_wrapper())

        message = str(excinfo.value)
        assert PROJECT_FIX_HINT in message, message
        assert "recall" in message, message

    def test_the_original_error_stays_the_cause(self) -> None:
        """Translating must not lose the type — the server log still needs it."""
        from mcp.server.mcpserver.exceptions import ToolError

        original = UnresolvedProjectError("checkpoint")

        def _tool():
            raise original

        _sync, async_wrapper = _wrappers(_tool)

        with pytest.raises(ToolError) as excinfo:
            asyncio.run(async_wrapper())

        assert excinfo.value.__cause__ is original

    def test_an_ordinary_exception_still_reaches_the_sdk_as_a_crash(self) -> None:
        """The negative half. Without it the fix could be a blanket catch.

        A bug inside a tool body must keep its ERROR-level traceback and must
        NOT have its text forwarded to the model — that is what the SDK's
        ``UnexpectedToolError`` branch is for, and this test pins that the
        translation does not swallow it.
        """
        from mcp.server.mcpserver.exceptions import ToolError

        def _tool():
            raise RuntimeError("internal detail that must not reach the client")

        _sync, async_wrapper = _wrappers(_tool)

        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(async_wrapper())

        assert not isinstance(excinfo.value, ToolError)

    def test_the_sync_wrapper_keeps_raising_the_original_type(self) -> None:
        """Direct/internal callers catch by yadgar type; that must not move."""

        def _tool():
            raise UnresolvedProjectError("recall")

        sync_wrapper, _async = _wrappers(_tool)

        with pytest.raises(UnresolvedProjectError):
            sync_wrapper()
