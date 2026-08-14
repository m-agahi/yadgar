"""Car B (0047 §3.3/§3.4) — tier 2 survives the tool-body thread offload.

The gap this closes: tier 2 resolves via a ``ContextVar`` that
``_build_tool_wrappers._instrumented_async`` stamps ON THE EVENT LOOP, while
the tool body runs on a worker thread via ``run_offloaded``. Three separate
pieces of ordering make that work, and every one of them is the kind of thing
a refactor silently breaks:

  1. ``kwargs.pop("ctx")`` happens BEFORE ``run_offloaded`` — otherwise the
     executor re-receives ``ctx`` and the binding is never read at all.
  2. ``set_current_session_project`` happens BEFORE ``run_offloaded`` — a
     ContextVar set after the dispatch is not in the copied Context.
  3. ``offload._ctx_wrap`` calls ``contextvars.copy_context()`` and runs the
     body inside it — without that the worker thread starts from a fresh
     Context and the ContextVar reads its ``None`` default.

Nothing exercised that chain end-to-end before: the existing tier-2 tests call
``resolve_effective_project`` directly on the calling thread, which cannot
observe a copy_context() regression.

Both tests below FORCE ``YADGAR_OFFLOAD_TOOLS=1`` — with offload disabled
``run_offloaded`` short-circuits to an inline call and the copied Context is
never involved, so an un-forced test would pass on the very regression it
exists to catch. ``test_tool_body_runs_on_a_worker_thread`` is the receipt
that the offload path was real rather than silently inline.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

# ── fake FastMCP Context ────────────────────────────────────────────────────
#
# ``_extract_session_project`` walks ctx.request_context.request.headers and
# ends at the ``x-yadgar-project-id`` header the SessionBindMiddleware sets.
# That is the whole contract, so the fake stops there — this is a unit test on
# the async wrapper, not an e2e over the real transport.


class _Headers:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = {k.lower(): v for k, v in mapping.items()}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._mapping.get(key.lower(), default)


class _Request:
    def __init__(self, headers: _Headers) -> None:
        self.headers = headers


class _RequestContext:
    def __init__(self, request: _Request) -> None:
        self.request = request


class _Ctx:
    """Minimal stand-in for ``mcp.server.fastmcp.server.Context``."""

    def __init__(self, project_id: str, session_id: str = "sid-test-1") -> None:
        self.request_context = _RequestContext(
            _Request(
                _Headers(
                    {
                        "mcp-session-id": session_id,
                        "x-yadgar-project-id": project_id,
                    }
                )
            )
        )


@pytest.fixture
def offload_on(monkeypatch: pytest.MonkeyPatch):
    """Force the offload path ON for the duration of one test.

    ``offload_enabled`` reads live ``os.environ`` first (``resolve_knob``
    precedence), so a monkeypatched env var takes effect immediately with no
    settings-cache lag.
    """
    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    yield


@pytest.fixture
def probe_tool():
    """A tool body that records what it saw FROM INSIDE the offloaded call.

    Reading the ContextVar after ``asyncio.run`` returns would prove nothing:
    the wrapper's ``finally`` calls ``reset_current_session_project``, so the
    outside read is ``None`` whether or not the binding ever reached the body.
    """
    seen: dict[str, object] = {}

    def _body(**kwargs):
        from yadgar._shared.runtime.session_project import get_current_session_project

        seen["project_id"] = get_current_session_project()
        seen["thread_ident"] = threading.get_ident()
        seen["kwargs_keys"] = sorted(kwargs)
        return {"ok": True}

    return _body, seen


def _async_wrapper(body):
    from yadgar.core.server._app import _build_tool_wrappers

    _sync, _async = _build_tool_wrappers(body, body, lambda _result: 0)
    return _async


class TestTier2SurvivesOffload:
    def test_bound_project_id_is_visible_inside_the_offloaded_body(
        self, offload_on, probe_tool
    ) -> None:
        """The whole point: the session binding stamped on the LOOP must be
        readable by ``get_current_session_project`` on the WORKER THREAD.

        RED if ``_ctx_wrap``'s ``contextvars.copy_context()`` is removed (the
        worker starts from a fresh Context → the ContextVar reads its ``None``
        default), and RED if ``kwargs.pop("ctx")`` / the ``set_...`` call move
        after ``run_offloaded`` (nothing is bound while the body runs)."""
        body, seen = probe_tool
        wrapper = _async_wrapper(body)

        result = asyncio.run(wrapper(ctx=_Ctx("m-agahi/yadgar")))

        assert result == {"ok": True}
        assert seen["project_id"] == "m-agahi/yadgar"

    def test_tool_body_runs_on_a_worker_thread(self, offload_on, probe_tool) -> None:
        """Receipt that the offload path was REAL.

        Without this, a regression that disables offload would make the test
        above pass for the wrong reason — the inline path keeps the loop's own
        Context, so the ContextVar resolves trivially."""
        body, seen = probe_tool
        wrapper = _async_wrapper(body)

        asyncio.run(wrapper(ctx=_Ctx("m-agahi/yadgar")))

        assert seen["thread_ident"] != threading.get_ident()

    def test_ctx_is_not_forwarded_into_the_tool_body(self, offload_on, probe_tool) -> None:
        """``ctx`` is popped BEFORE ``run_offloaded`` forwards ``**kwargs``.

        If the pop moves after the dispatch, the executor re-receives ``ctx``
        and the tool body sees a kwarg it never declared."""
        body, seen = probe_tool
        wrapper = _async_wrapper(body)

        asyncio.run(wrapper(ctx=_Ctx("m-agahi/yadgar"), directory="/home/max/git/yadgar"))

        assert seen["kwargs_keys"] == ["directory"]

    def test_unbound_session_leaves_the_contextvar_none(self, offload_on, probe_tool) -> None:
        """No ``ctx`` (stdio / stateless_http) → tier 2 stays unbound so the
        resolver falls through to ``project=`` / ``session_project`` rather
        than inheriting a previous request's binding."""
        body, seen = probe_tool
        wrapper = _async_wrapper(body)

        asyncio.run(wrapper())

        assert seen["project_id"] is None

    def test_binding_does_not_leak_between_calls(self, offload_on, probe_tool) -> None:
        """The ``finally`` resets the ContextVar. Two sequential calls on the
        same loop-and-pool must not see each other's project_id."""
        body, seen = probe_tool
        wrapper = _async_wrapper(body)

        async def _two_calls():
            await wrapper(ctx=_Ctx("m-agahi/yadgar"))
            first = seen["project_id"]
            await wrapper()
            return first, seen["project_id"]

        first, second = asyncio.run(_two_calls())
        assert first == "m-agahi/yadgar"
        assert second is None
