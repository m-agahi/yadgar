"""Core-side forwarder to the backend /admin endpoint (R3 Car 3a / R5).

The pure-CRUD write tools (bookmarks, blocks, …) keep their ``@_tool`` shell +
validation + secret-gate in core and forward the storage write to the backend
over HTTP via ``_forward_admin``. This mirrors ``recall._forward_to_backend``
and the consolidation orchestrator's forwarder: HTTP only, NO Python import of
``yadgar.backend`` (which would break the core→backend import-linter contract).

Forward-only: if ``YADGAR_EMBED_URL`` is unset, raises RuntimeError (no in-core
storage fallback — core touches zero DB directly).

**LEAF MODULE — keep it that way (Car 0031).** This used to live at
``yadgar/core/server/tools/_forward.py``. Importing anything under
``yadgar.core.server`` runs ``yadgar/core/server/__init__.py``, which eagerly
imports ``_app``, which calls ``setup_tracing("yadgar-core")`` at module scope.
So the two live Claude Code hook CLIs (``yadgar restore`` / ``yadgar drain``,
via ``yadgar/core/cli/_shared.py``) dragged in the entire MCP server *plus* a
live OTLP exporter to make a single HTTP POST — measured 8.2s vs 1.2s per
invocation on the host. Moving it here breaks that edge; the daemon is
unaffected (it imports the server anyway). ``yadgar seed``, the consolidation
orchestrator and the staleness scanner ride along for free.

Guarded by ``yadgar/tests/scripts/test_cli_import_isolation.py`` — do NOT move
this back under ``yadgar.core.server`` and do NOT add imports here from any
``yadgar.core.server.*`` module. Its only first-party dependencies are
``yadgar._shared.observability.observe`` and ``yadgar._shared.refusal`` (which
itself imports only the former, so the leaf property is preserved); ``httpx`` is
imported lazily per call.

NOTE: the ``@observe(metric=...)`` labels below deliberately keep their historic
``tools._forward.*`` names — those are Prometheus metric labels, not module
paths, and renaming them would break dashboard/alert continuity. Span names
derive from ``__module__`` and follow the move automatically.
"""

from __future__ import annotations

import os

from yadgar._shared.observability.observe import observe
from yadgar._shared.refusal import parse_refusal

# backend 5.30.1: per-op timeout FLOORS for slow admin ops. check_invariants
# walks every memory + wiki row backend-side (observed 33-34s on the production
# dataset) — the flat 30s default guaranteed a client timeout while the backend
# kept burning CPU on a response nobody would read. Applied as
# max(caller_timeout, floor) so explicit larger caller timeouts still win and
# every other op keeps the fast 30s default.
_SLOW_OP_TIMEOUTS_S: dict[str, float] = {
    "check_invariants": 120.0,
}


def _unreachable_backend_error(backend_base: str, what: str) -> RuntimeError:
    """Build the actionable error for "the URL is set but nothing answers".

    v5.182 bug train, Car 5 follow-on. Car 5 made ``yadgar/__main__.py``
    ``setdefault`` ``YADGAR_EMBED_URL`` to the published host port, so the host
    CLI works out of the box instead of dying with "not set" on every
    forwarding subcommand. Correct — but it moved the failure: with the variable
    always populated, an absent daemon stopped producing the actionable
    "YADGAR_EMBED_URL is not set" message and started producing a raw
    ``httpx.ConnectError: [Errno 111] Connection refused`` traceback.

    ``test_cli_{drain,restore}_fails_loud_without_backend_url`` caught exactly
    that, and the fix is NOT to relax them to accept a traceback: their contract
    is "this CLI path fails LOUD and names the variable", which is still the
    right contract — only the reachable failure mode changed. So the connect
    failure now names the variable, the URL actually tried, and the override,
    and those tests pass unmodified. Weakening a guard to match new code is the
    defect task #0139 exists to prevent; this is the opposite move.
    """
    return RuntimeError(
        f"cannot reach the yadgar backend at {backend_base!r} to {what}. "
        "That address comes from YADGAR_EMBED_URL (defaulted to the published "
        "host port when unset). Is the daemon running? Check "
        "`systemctl --user status yadgar-backend` / `curl "
        f"{backend_base}/health`, or set YADGAR_EMBED_URL to the right address."
    )


@observe(tier="boundary", metric="tools._forward._forward_admin")
def _forward_admin(op: str, payload: dict, timeout_s: float = 30.0) -> dict:
    """Forward a single admin (CRUD write) op to the backend /admin endpoint.

    Args:
        op: Op name — must match a key registered in
            ``yadgar.backend.admin_exec._ADMIN_OPS`` (mirrors the core tool name).
        payload: The op's arguments (already validated + secret-gated core-side).
        timeout_s: httpx request timeout. CRUD writes are fast; default 30s.
            Slow ops listed in ``_SLOW_OP_TIMEOUTS_S`` get a per-op floor via
            ``max(timeout_s, floor)``.

    Backend URL: derived from ``YADGAR_EMBED_URL`` (the same base URL recall +
    consolidation forward to). Bearer auth via ``YADGAR_MCP_AUTH_TOKEN``.

    Returns:
        The backend impl's result dict (the /admin route wraps it as
        ``{"result": ...}``; this helper unwraps and returns the inner dict).

        OR, when the op REFUSED by design (``AdminRefusal`` backend-side), the
        structured refusal envelope — ``{"ok": False, "refused": True, "reason":
        ..., "error": ..., **report}``. A refusal is an EXPECTED outcome, not a
        transport failure, so it is returned rather than raised; callers
        discriminate on ``ok`` / ``refused`` / ``reason``.

    Raises:
        RuntimeError: if ``YADGAR_EMBED_URL`` is not configured.
        httpx.HTTPError: if the backend request fails (incl. 400 unknown op and
            any 5xx — a genuine fault still raises, which is what keeps it
            distinguishable from a refusal).
    """
    import httpx  # noqa: PLC0415

    backend_base = os.environ.get("YADGAR_EMBED_URL", "").rstrip("/")
    if not backend_base:
        raise RuntimeError(
            "YADGAR_EMBED_URL is not set; cannot forward admin op to backend. "
            "R3 Car 3a: CRUD writes are forward-only — core touches zero DB directly."
        )

    # Car 5 item 4 (follow-on): route through the ONE sanctioned bearer-token
    # resolver (env var, else secrets.env) rather than a bare os.environ.get —
    # that pattern is exactly the "fourth hand-rolled copy" its own docstring
    # forbids, and on a bare host CLI nothing exports the env var, so a bare
    # lookup here silently sent every forwarded admin/viz/read_query/restore
    # op unauthenticated (401 from the backend's fail-secure gate).
    from yadgar.core.install.auth_token import resolve_auth_token  # noqa: PLC0415

    token = resolve_auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    timeout_s = max(timeout_s, _SLOW_OP_TIMEOUTS_S.get(op, 0.0))

    try:
        resp = httpx.post(
            f"{backend_base}/admin",
            json={"op": op, "payload": payload},
            headers=headers,
            timeout=timeout_s,
        )
    except httpx.ConnectError as exc:
        raise _unreachable_backend_error(backend_base, "forward an admin op") from exc

    # Ledger tasks 80 + 294: a DELIBERATE refusal comes back as a structured
    # envelope, and the caller gets it — it is not a fault, so it is not raised.
    # This is also what makes quiesce.py's pre-existing
    # ``if verification.get("status") != "ok"`` reachable at last: the old
    # unconditional raise_for_status() fired before the body was ever read, so
    # the one place that inspected the tri-state report was dead code.
    # Everything else — 500s, the 400 unknown-op, a 409 that is not ours —
    # still raises exactly as before.
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is simply not a refusal
        body = None
    refusal = parse_refusal(resp.status_code, body)
    if refusal is not None:
        return refusal

    resp.raise_for_status()
    return body.get("result", {}) if isinstance(body, dict) else {}


@observe(tier="boundary", metric="tools._forward._forward_viz")
def _forward_viz(op: str, payload: dict, timeout_s: float = 60.0) -> dict:
    """Forward a single viz (graph data-assembly) op to the backend /viz endpoint.

    T2 Car E3 (census verdict #11): the core /api/graph* handlers keep their
    route shells (param parsing, CORS, hook metrics) and forward the DB-heavy
    assembly here. Mirrors ``_forward_admin`` exactly; 60s default matches the
    old viz proxy budget (large graphs with 2k+ nodes).

    Raises:
        RuntimeError: if ``YADGAR_EMBED_URL`` is not configured.
        httpx.HTTPError: if the backend request fails (incl. 400 unknown op).
    """
    import httpx  # noqa: PLC0415

    backend_base = os.environ.get("YADGAR_EMBED_URL", "").rstrip("/")
    if not backend_base:
        raise RuntimeError(
            "YADGAR_EMBED_URL is not set; cannot forward viz op to backend. "
            "T2 Car E3: graph data assembly is forward-only — core assembles zero graph data."
        )

    # Car 5 item 4 (follow-on): route through the ONE sanctioned bearer-token
    # resolver (env var, else secrets.env) rather than a bare os.environ.get —
    # that pattern is exactly the "fourth hand-rolled copy" its own docstring
    # forbids, and on a bare host CLI nothing exports the env var, so a bare
    # lookup here silently sent every forwarded admin/viz/read_query/restore
    # op unauthenticated (401 from the backend's fail-secure gate).
    from yadgar.core.install.auth_token import resolve_auth_token  # noqa: PLC0415

    token = resolve_auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        resp = httpx.post(
            f"{backend_base}/viz",
            json={"op": op, "payload": payload},
            headers=headers,
            timeout=timeout_s,
        )
    except httpx.ConnectError as exc:
        raise _unreachable_backend_error(backend_base, "forward a viz op") from exc
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", {})


@observe(tier="boundary", metric="tools._forward._forward_read_query")
def _forward_read_query(query: str, params: dict | None = None, timeout_ms: int = 5000) -> dict:
    """Forward a read-only DB query to the backend POST /read_query endpoint.

    ADR-0078 sanctioned read path: the query executes backend-side on the
    VIEWER-role RO DB connection (a write over that connection does not persist,
    regardless of query text). Core touches zero DB — it forwards HTTP only.
    Called by the core ``/api/debug/read_query`` route AND the ``db_inspect``
    MCP tool.

    Args:
        query: The SurrealQL SELECT/INFO statement to run.
        params: Bind params for the query (``$name`` in the statement).
        timeout_ms: Per-call DB timeout (backend applies it to ``_q_ro``). Also
            used to size the httpx request timeout (+ a small forward margin).

    Returns:
        The backend's ``ReadQueryResponse`` dict: ``{rows, row_count, truncated}``.

    Raises:
        RuntimeError: if ``YADGAR_EMBED_URL`` is not configured.
        httpx.HTTPError: if the backend request fails (400 on write-keyword /
            malformed query).
    """
    import httpx  # noqa: PLC0415

    backend_base = os.environ.get("YADGAR_EMBED_URL", "").rstrip("/")
    if not backend_base:
        raise RuntimeError(
            "YADGAR_EMBED_URL is not set; cannot forward read_query to backend. "
            "ADR-0078: the read-only DB inspection surface is forward-only — "
            "core touches zero DB directly."
        )

    # Car 5 item 4 (follow-on): route through the ONE sanctioned bearer-token
    # resolver (env var, else secrets.env) rather than a bare os.environ.get —
    # that pattern is exactly the "fourth hand-rolled copy" its own docstring
    # forbids, and on a bare host CLI nothing exports the env var, so a bare
    # lookup here silently sent every forwarded admin/viz/read_query/restore
    # op unauthenticated (401 from the backend's fail-secure gate).
    from yadgar.core.install.auth_token import resolve_auth_token  # noqa: PLC0415

    token = resolve_auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # httpx timeout = DB timeout + a forward margin so the backend's own timeout
    # surfaces as a 400 rather than a client-side read timeout.
    _client_timeout_s = (timeout_ms / 1000.0) + 5.0

    try:
        resp = httpx.post(
            f"{backend_base}/read_query",
            json={"query": query, "params": params or {}, "timeout_ms": timeout_ms},
            headers=headers,
            timeout=_client_timeout_s,
        )
    except httpx.ConnectError as exc:
        raise _unreachable_backend_error(backend_base, "run a read query") from exc
    resp.raise_for_status()
    return resp.json()


@observe(tier="boundary", metric="tools._forward._forward_restore")
def _forward_restore(
    directory: str = "", project_id: str | None = None, timeout_s: float = 120.0
) -> dict:
    """Forward restore to the backend POST /restore endpoint (T2 Car B).

    The restore compute (CheckpointRestore + CognitiveMap SR navigation, census
    verdict #7) runs backend-side next to the DB; core is a thin forwarder.
    Callers: the restore MCP tool, the /hooks/post-compact HTTP hook, and the
    ``yadgar restore`` CLI subcommand.

    Args:
        directory: Host-side project path. C10g: still the key for restore's
            checkpoint + memory-block sinks (neither table carries a
            ``project_id`` column yet), and no longer a scope key for anything
            else.
        project_id: Resolved ``owner/repo``. C10g: keys restore's memory-backed
            sinks — the anchor and hot buckets and gap detection — because
            C10f/C10g moved both writers' stamp onto it. ``None`` means the
            caller named no project; those buckets come back EMPTY rather than
            widened to the corpus.
        timeout_s: httpx request timeout. Restore builds + inverts the SR
            matrix — allow the same generous budget as the MCP recall forward.

    Returns:
        The restore payload dict (the /restore route wraps it as
        ``{"result": ...}``; this helper unwraps and returns the inner dict).

    Raises:
        RuntimeError: if ``YADGAR_EMBED_URL`` is not configured (forward-only —
            no in-core fallback; the impl no longer exists in the core process).
        httpx.HTTPError: if the backend request fails.
    """
    import httpx  # noqa: PLC0415

    backend_base = os.environ.get("YADGAR_EMBED_URL", "").rstrip("/")
    if not backend_base:
        raise RuntimeError(
            "YADGAR_EMBED_URL is not set; cannot forward restore to backend. "
            "T2 Car B: restore is forward-only — the compute runs backend-side."
        )

    # Car 5 item 4 (follow-on): route through the ONE sanctioned bearer-token
    # resolver (env var, else secrets.env) rather than a bare os.environ.get —
    # that pattern is exactly the "fourth hand-rolled copy" its own docstring
    # forbids, and on a bare host CLI nothing exports the env var, so a bare
    # lookup here silently sent every forwarded admin/viz/read_query/restore
    # op unauthenticated (401 from the backend's fail-secure gate).
    from yadgar.core.install.auth_token import resolve_auth_token  # noqa: PLC0415

    token = resolve_auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        resp = httpx.post(
            f"{backend_base}/restore",
            json={"directory": directory, "project_id": project_id},
            headers=headers,
            timeout=timeout_s,
        )
    except httpx.ConnectError as exc:
        raise _unreachable_backend_error(backend_base, "restore context") from exc
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", {})
