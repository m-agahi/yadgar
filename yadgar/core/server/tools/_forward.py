"""Core-side forwarder to the backend /admin endpoint (R3 Car 3a / R5).

The pure-CRUD write tools (bookmarks, blocks, …) keep their ``@_tool`` shell +
validation + secret-gate in core and forward the storage write to the backend
over HTTP via ``_forward_admin``. This mirrors ``recall._forward_to_backend``
and the consolidation orchestrator's forwarder: HTTP only, NO Python import of
``yadgar.backend`` (which would break the core→backend import-linter contract).

Forward-only: if ``YADGAR_EMBED_URL`` is unset, raises RuntimeError (no in-core
storage fallback — core touches zero DB directly).
"""

from __future__ import annotations

import os

from yadgar._shared.observability.observe import observe


@observe(tier="boundary", metric="tools._forward._forward_admin")
def _forward_admin(op: str, payload: dict, timeout_s: float = 30.0) -> dict:
    """Forward a single admin (CRUD write) op to the backend /admin endpoint.

    Args:
        op: Op name — must match a key registered in
            ``yadgar.backend.admin_exec._ADMIN_OPS`` (mirrors the core tool name).
        payload: The op's arguments (already validated + secret-gated core-side).
        timeout_s: httpx request timeout. CRUD writes are fast; default 30s.

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

    resp = httpx.post(
        f"{backend_base}/admin",
        json={"op": op, "payload": payload},
        headers=headers,
        timeout=timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", {})
