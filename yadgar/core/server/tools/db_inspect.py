"""db_inspect MCP tool — sanctioned read-only DB inspection (ADR-0078).

One tool:
  db_inspect(query, params, limit) — run a read-only SurrealQL query against the DB

Safety model: the query executes backend-side on the VIEWER-role RO DB
connection (``_q_ro``) — the DB rejects any write regardless of query text
(ADR-0078). Core touches zero DB; this tool forwards to the backend
``POST /read_query`` via ``_forward_read_query`` (same forward path the
``/api/debug/read_query`` route uses).

Debug-gated: the tool refuses unless ``YADGAR_DEBUG_APIS_ENABLED=on`` — the MCP
tool call does NOT pass through the HTTP auth middleware (which gates the
``/api/debug/*`` prefix), so the tool re-checks the flag itself (ADR-0013). This
keeps the surface off in prod by default.

Row-capped (hard ceiling 500) + timeout-bounded so the model cannot pull large
or sensitive result sets into context.
"""

from __future__ import annotations

import logging

import httpx

from yadgar.core.forward import _forward_read_query
from yadgar.core.server._app import _tool

logger = logging.getLogger(__name__)

# Hard row-cap ceiling — mirrors the backend _RO_QUERY_ROW_CAP module constant.
# A caller's ``limit`` may clamp LOWER but never raises it. Module constant, not
# a knob (I25).
_DB_INSPECT_ROW_CAP: int = 500
_DB_INSPECT_TIMEOUT_MS: int = 5000


def _is_debug_apis_enabled() -> bool:
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob(
        "YADGAR_DEBUG_APIS_ENABLED",
        "DEBUG_APIS_ENABLED",
        lambda s: s.strip().lower() in ("1", "true", "yes", "on"),
        False,
    )


@_tool()
def db_inspect(query: str, params: dict | None = None, limit: int = 500) -> dict:
    """Run a read-only SurrealQL query against the DB for debugging/introspection.

    READ-ONLY (VIEWER role): the query executes on a read-only DB connection —
    the DB rejects any write regardless of query text (ADR-0078). This is for DB
    introspection only (e.g. "what edges does entity:4539 have", "the row for
    memory N"), not a general query surface.

    DEBUG-GATED: refuses unless ``YADGAR_DEBUG_APIS_ENABLED`` is on (off in prod
    by default).

    ROW-CAPPED: results are capped at 500 rows (hard ceiling); ``limit`` may
    clamp lower but never higher. ``truncated: true`` signals the cap was hit.

    Args:
        query: A SurrealQL SELECT/INFO statement. Bind params via ``$name``.
        params: Bind params referenced as ``$name`` in the query.
        limit: Max rows to return (clamped to <= 500).

    Returns:
        ``{rows, row_count, truncated}`` on success.
        ``{error: str}`` when debug APIs are disabled or the query is rejected
        (write keyword / malformed / write attempted over the VIEWER role).
    """
    if not _is_debug_apis_enabled():
        return {
            "error": (
                "db_inspect is debug-gated: set YADGAR_DEBUG_APIS_ENABLED=on to "
                "enable read-only DB inspection (off in prod by default)."
            )
        }

    effective_limit = min(int(limit), _DB_INSPECT_ROW_CAP)

    try:
        result = _forward_read_query(query, params or {}, timeout_ms=_DB_INSPECT_TIMEOUT_MS)
    except httpx.HTTPStatusError as exc:
        detail: object = exc.response.text
        try:
            detail = exc.response.json().get("detail", detail)
        except Exception:  # noqa: BLE001
            pass
        return {"error": detail}
    except RuntimeError as exc:
        return {"error": str(exc)}

    # Apply the caller's (already-clamped) limit on top of the backend's 500 cap.
    rows = result.get("rows", [])
    truncated = bool(result.get("truncated", False))
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        truncated = True
    return {"rows": rows, "row_count": len(rows), "truncated": truncated}
