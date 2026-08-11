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
``yadgar.core.server.*`` module. Its only first-party dependency is
``yadgar._shared.observability.observe``; ``httpx`` is imported lazily per call.

NOTE: the ``@observe(metric=...)`` labels below deliberately keep their historic
``tools._forward.*`` names — those are Prometheus metric labels, not module
paths, and renaming them would break dashboard/alert continuity. Span names
derive from ``__module__`` and follow the move automatically.
"""

from __future__ import annotations

import os

from yadgar._shared.observability.observe import observe

# backend 5.30.1: per-op timeout FLOORS for slow admin ops. check_invariants
# walks every memory + wiki row backend-side (observed 33-34s on the production
# dataset) — the flat 30s default guaranteed a client timeout while the backend
# kept burning CPU on a response nobody would read. Applied as
# max(caller_timeout, floor) so explicit larger caller timeouts still win and
# every other op keeps the fast 30s default.
_SLOW_OP_TIMEOUTS_S: dict[str, float] = {
    "check_invariants": 120.0,
}


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

    Raises:
        RuntimeError: if ``YADGAR_EMBED_URL`` is not configured.
        httpx.HTTPError: if the backend request fails (incl. 400 unknown op).
    """
    import httpx  # noqa: PLC0415

    backend_base = os.environ.get("YADGAR_EMBED_URL", "").rstrip("/")
    if not backend_base:
        raise RuntimeError(
            "YADGAR_EMBED_URL is not set; cannot forward admin op to backend. "
            "R3 Car 3a: CRUD writes are forward-only — core touches zero DB directly."
        )

    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    timeout_s = max(timeout_s, _SLOW_OP_TIMEOUTS_S.get(op, 0.0))

    resp = httpx.post(
        f"{backend_base}/admin",
        json={"op": op, "payload": payload},
        headers=headers,
        timeout=timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", {})


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

    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    resp = httpx.post(
        f"{backend_base}/viz",
        json={"op": op, "payload": payload},
        headers=headers,
        timeout=timeout_s,
    )
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

    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # httpx timeout = DB timeout + a forward margin so the backend's own timeout
    # surfaces as a 400 rather than a client-side read timeout.
    _client_timeout_s = (timeout_ms / 1000.0) + 5.0

    resp = httpx.post(
        f"{backend_base}/read_query",
        json={"query": query, "params": params or {}, "timeout_ms": timeout_ms},
        headers=headers,
        timeout=_client_timeout_s,
    )
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

    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    resp = httpx.post(
        f"{backend_base}/restore",
        json={"directory": directory, "project_id": project_id},
        headers=headers,
        timeout=timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", {})
