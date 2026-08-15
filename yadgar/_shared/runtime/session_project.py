"""Per-request ContextVar carrying the session's resolved project_id (Car B).

Car B (0047 §3.4): the MCP tool wrapper stamps ``Connection.state[sid]``
with the caller's project_id after a successful ``/session_bind`` nonce
consume. The project_id needs to reach ``resolve_effective_project`` tier 2
WITHOUT crossing the function's signature (which is the wrong layer — every
~46 tools would have to thread a new parameter through the wrapper).

A per-request ``ContextVar`` is the standard Python answer: middleware sets
it from ``Connection.state`` on the way IN, ``resolve_effective_project``
reads it, and on the way OUT the middleware resets it. Async-safe by
construction (contextvars are). Reset semantics: a missed reset would leak
a project_id into the NEXT call landing on the same worker — so the ASGI
middleware that stamps the ContextVar MUST pair every set with a
``reset(token)`` in a ``finally`` (or use ``Context.run``).

Wire-up (B3): the tool wrapper reads the ContextVar AFTER popping ``ctx``,
consumes the nonce, and stores ``sid -> project_id`` on the underlying
``StreamableHTTPServerTransport`` (proxy for Connection.state — see
``_extract_session_id`` for the actual SDK 2.0.0 path).

Wire-up (B4): ``resolve_effective_project`` reads the ContextVar as its
NEW tier 2. The legacy ``session_project`` keyword remains the FALLBACK
when the ContextVar is unbound (plan §3.4: "If unbound, fall back to
explicit ``project=``" — re-read as: if the ContextVar did not resolve
the identity, do NOT add a fourth tier; honour the existing
``project > session_project > raise`` chain as before).

NO imports from ``yadgar.core`` or ``yadgar.backend`` here — this is a
shared runtime primitive and must stay importable from anywhere without
dragging the layer graph.
"""

from __future__ import annotations

from contextvars import ContextVar

from yadgar._shared.observability.tracing import trace_span

#: Per-request project_id, populated by the ASGI middleware from
#: ``StreamableHTTPServerTransport.mcp_session_id`` → nonce-pool → project_id,
#: read by ``resolve_effective_project`` tier 2. ``None`` is the unbound
#: state — do NOT use a sentinel string; a missing value IS the answer.
_current_session_project: ContextVar[str | None] = ContextVar(
    "yadgar_session_project", default=None
)


@trace_span()
def get_current_session_project() -> str | None:
    """Return the current request's bound project_id, or ``None`` if unbound.

    Read-only: callers in tool bodies MUST NOT mutate the ContextVar directly
    (the ASGI middleware is the sole writer). The ``None`` return is a
    SEMANTIC "this request has no bound identity" — the caller decides what
    to do next (tier 3 in ``resolve_effective_project`` is to fall back to
    the legacy ``session_project`` keyword).
    """
    return _current_session_project.get()


def set_current_session_project(project_id: str | None) -> object:
    """Bind the current request's project_id; return a token the caller MUST
    pass to ``reset_current_session_project``.

    Intended caller: the ASGI middleware that knows the Mcp-Session-Id and
    has just looked up the corresponding project_id from the nonce pool.
    Tools themselves must NOT call this — the contract is "write once on
    request entry, reset on request exit".
    """
    return _current_session_project.set(project_id)


def reset_current_session_project(token: object) -> None:
    """Restore the ContextVar to its pre-``set`` state.

    MUST be called in a ``finally`` block by the same middleware that called
    ``set_current_session_project``, otherwise the project_id leaks into the
    NEXT request that lands on the same worker. Idempotent: calling on a
    stale token raises ``ValueError`` from the contextvars module, which is
    the CORRECT signal (means the middleware forgot a ``finally``).
    """
    _current_session_project.reset(token)  # type: ignore[arg-type]


__all__ = [
    "get_current_session_project",
    "set_current_session_project",
    "reset_current_session_project",
]
