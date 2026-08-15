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


# ── the `context` kwarg collision ───────────────────────────────────────────


class TestBusinessContextParamIsNotEatenAsTheSdkContext:
    """A tool parameter literally named ``context`` must reach the tool body.

    ``_instrumented_async`` looks for the FastMCP Context object under BOTH
    ``ctx`` and ``context`` (SDK shapes differ). But ``context`` is also a
    real business parameter on three registered tools, and the pop is
    unconditional — so the caller's value is removed from kwargs and the body
    is invoked without it:

      * ``adr_add(context=...)``  — REQUIRED  -> TypeError, ADR writes dead
      * ``anchor(context=...)``   — REQUIRED  -> TypeError, anchoring dead
      * ``memorize(context=...)`` — optional  -> SILENTLY dropped, no error

    Reproduced live 2026-08-15 against core 5.183.1:
    ``adr_add() missing 1 required positional argument: 'context'`` with
    ``context`` plainly supplied. The optional case is the dangerous one — it
    loses the staleness-detection path with no signal at all.

    The fix keys on the WRAPPED FUNCTION'S OWN SIGNATURE: a tool that declares
    ``context`` owns that name, so the wrapper must never claim it.
    """

    @staticmethod
    def _wrap(func):
        from yadgar.core.server._app import _build_tool_wrappers

        return _build_tool_wrappers(func, func, lambda _r: 1)[1]  # async wrapper

    def test_required_context_reaches_the_body(self):
        def tool_with_context(directory: str, context: str) -> dict:
            return {"directory": directory, "context": context}

        wrapper = self._wrap(tool_with_context)
        out = asyncio.run(wrapper(directory="/d", context="the ADR background"))
        assert out == {"directory": "/d", "context": "the ADR background"}

    def test_optional_context_is_not_silently_dropped(self):
        def tool_with_optional_context(content: str, context: str | None = None) -> dict:
            return {"content": content, "context": context}

        wrapper = self._wrap(tool_with_optional_context)
        out = asyncio.run(wrapper(content="x", context="/home/max/file.py"))
        assert out["context"] == "/home/max/file.py", (
            "the caller's context was swallowed by the SDK-Context lookup"
        )

    def test_sdk_context_is_still_consumed_for_tools_without_the_param(self):
        """Tools that do NOT declare ``context`` keep the old behaviour."""

        def tool_without_context(directory: str) -> dict:
            return {"directory": directory}

        wrapper = self._wrap(tool_without_context)
        # A stray `context` kwarg must not reach a body that cannot accept it.
        out = asyncio.run(wrapper(directory="/d", context=_Ctx("owner/repo")))
        assert out == {"directory": "/d"}


class _HeaderOnlyCtx:
    """A Context whose request carries the project header but NO session id.

    This is what the daemon actually sees: ``_startup.py`` sets
    ``stateless_http=True`` for the streamable-http transport (deliberately —
    it makes daemon restarts transparent), and stateless mode issues no
    ``Mcp-Session-Id`` at all. Verified live 2026-08-15: an ``initialize``
    against the running daemon returns no such header.
    """

    def __init__(self, project_id: str) -> None:
        self.request_context = _RequestContext(
            _Request(_Headers({"x-yadgar-project-id": project_id}))
        )


class TestProjectHeaderWorksWithoutASessionId:
    """Tier 2 must resolve from the project header in STATELESS mode.

    ``_extract_session_project`` reads ``X-Yadgar-Project-Id`` — but only
    after an early ``return None`` when ``mcp-session-id`` is absent. The
    daemon runs stateless, so that header is never present, so the reader
    bails before it ever looks at the project header. Net effect: tier 2 is
    dead in the only transport mode the daemon actually runs, and every
    wiki/ADR write without an explicit ``project=`` raises
    ``unresolved_project``.

    A session id is the wrong precondition for a value carried in its own
    header — a static per-client header (``.claude.json`` mcpServers supports
    them) survives statelessness and daemon restarts alike.
    """

    def test_header_resolves_without_a_session_id(self):
        from yadgar.core.server._app import _extract_session_project

        assert _extract_session_project(_HeaderOnlyCtx("m-agahi/yadgar")) == "m-agahi/yadgar"

    def test_session_id_path_still_works(self):
        """The stateful path must keep working where a session id exists."""
        from yadgar.core.server._app import _extract_session_project

        assert _extract_session_project(_Ctx("quinyx/flux")) == "quinyx/flux"

    def test_no_headers_at_all_is_still_none(self):
        from yadgar.core.server._app import _extract_session_project

        class _Bare:
            request_context = _RequestContext(_Request(_Headers({})))

        assert _extract_session_project(_Bare()) is None


class TestMiddlewareHonoursTheProjectHeader:
    """The ASGI middleware is the path that sees EVERY request.

    The tool wrapper only gets a FastMCP ``Context`` when the SDK chooses to
    inject one; the middleware reads the raw ASGI scope unconditionally. In
    stateless mode the nonce lookup can never resolve, so the middleware must
    fall back to the static ``X-Yadgar-Project-Id`` header or tier 2 stays
    dead no matter what the wrapper does.
    """

    @staticmethod
    def _run(headers: dict[str, str]) -> str | None:
        from yadgar._shared.runtime.session_project import get_current_session_project
        from yadgar.core.server._app import SessionBindMiddleware

        seen: dict[str, str | None] = {}

        async def _app(scope, receive, send):
            seen["project"] = get_current_session_project()

        scope = {
            "type": "http",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        }
        asyncio.run(SessionBindMiddleware(_app)(scope, None, None))
        return seen["project"]

    def test_header_becomes_the_contextvar(self):
        assert self._run({"x-yadgar-project-id": "m-agahi/yadgar"}) == "m-agahi/yadgar"

    def test_absent_header_leaves_it_unbound(self):
        assert self._run({}) is None

    def test_blank_header_is_not_an_identity(self):
        assert self._run({"x-yadgar-project-id": "   "}) is None
