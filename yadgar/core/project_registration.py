"""Register one project in the ``project`` registry — ADR-1131.

The registry accepts a new row from exactly two sources:

1. the SessionStart hook, for the id it minted from ``.yadgar/project-id`` or
   the git ``origin`` remote (it reaches this module through the daemon route
   ``POST /hooks/project-register``);
2. an explicit ``project_register`` MCP call or ``yadgar project register``
   CLI call that names one id.

A ``project=`` on any other tool call never creates a row: the create gate
(``core/server/tools/_project_registry.assert_project_registered_for_create``)
still raises ``UnknownProjectError`` for it (ADR-0231).

Both sources pass the same shape guard, ``registrable_project_id_error``. It
is stricter than the WRITE-side ``project_id_value_error`` on purpose: that
one only refuses sentinels, because the registry decides membership; this one
decides what may ENTER the registry, so a free-text value such as
``"debugging opsecrets nixos-quinyx"`` (seen as a stored ``directory_context``)
can never become a namespace.

The module is light at import time (stdlib + one ``_shared`` frozenset) so the
host-side hook can import the guard without pulling in the server graph.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage._project_id_writer import _NON_IDENTIFYING_PROJECT_IDS

logger = logging.getLogger(__name__)

#: Width of ``project.key`` (alembic ``003_project_registry``: ``String(256)``).
PROJECT_KEY_MAX_CHARS = 256

#: Width of ``project.display_name`` (``String(64)``). A wider value raises
#: MariaDB ``DataError 1406`` at INSERT time, so it is refused here first.
DISPLAY_NAME_MAX_CHARS = 64

# One path segment: starts with a letter or digit, then letters, digits,
# ``.``, ``_`` or ``-``. Two or more segments: ``owner/repo``,
# ``group/sub/repo`` or ``local/<name>``.
_SEGMENT = r"[a-z0-9][a-z0-9._-]*"
_REGISTRABLE_RE = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})+")


@observe(exempt="pure single-value shape predicate on one string; no I/O, no storage access")
def registrable_project_id_error(value: object) -> str | None:
    """Return why *value* may not enter the registry, or ``None`` when it may.

    Accepted: ``owner/repo``, ``group/sub/repo`` (any depth), ``local/<name>``.
    All lowercase, no whitespace, at most 256 characters, not a sentinel
    (``global`` / ``unresolved`` / ``system``).
    """
    if not isinstance(value, str) or not value:
        return f"project_id must be a non-empty string; got {value!r}"
    if value in _NON_IDENTIFYING_PROJECT_IDS:
        return f"project_id {value!r} names no project (a sentinel, ADR-0227)"
    if any(ch.isspace() for ch in value):
        return f"project_id {value!r} contains whitespace"
    if len(value) > PROJECT_KEY_MAX_CHARS:
        return f"project_id is {len(value)} characters; the limit is {PROJECT_KEY_MAX_CHARS}"
    if value != value.lower():
        return f"project_id {value!r} must be lowercase"
    if not _REGISTRABLE_RE.fullmatch(value):
        return (
            f"project_id {value!r} is not owner/repo[/...] or local/<name> "
            "(segments start with a letter or digit; only . _ - inside)"
        )
    return None


@observe(exempt="pure dict builder for the result envelope; no I/O, no storage access")
def _result(project_id: str, *, ok: bool, created: bool, error: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"ok": ok, "project_id": project_id, "created": created}
    if error:
        out["error"] = error
    return out


@observe(tier="boundary", metric="core.project_registration.register_project")
def register_project(project_id: str, display_name: str | None = None) -> dict[str, Any]:
    """Create the registry row for *project_id* if it is absent. Idempotent.

    Returns ``{"ok", "project_id", "created"}`` and, when ``ok`` is False,
    ``"error"``. An id already registered is ``ok`` with ``created: False``;
    so is a ``DuplicateProjectError`` from a concurrent registration. Never
    raises: both callers (a hook route and a tool) want an envelope.
    """
    shape_error = registrable_project_id_error(project_id)
    if shape_error is not None:
        return _result(str(project_id), ok=False, created=False, error=shape_error)
    if display_name is not None and len(display_name) > DISPLAY_NAME_MAX_CHARS:
        return _result(
            project_id,
            ok=False,
            created=False,
            error=f"display_name is {len(display_name)} characters; "
            f"the limit is {DISPLAY_NAME_MAX_CHARS}",
        )

    from yadgar.core.cli.project import infer_kind, is_duplicate_project_error
    from yadgar.core.server.tools import _project_registry

    # Steady state: the hook calls this on every session start, so a cached
    # hit answers without an INSERT that would only end in a 409.
    keys = _project_registry.known_project_ids()
    if keys is not None and project_id in keys:
        return _result(project_id, ok=True, created=False)

    payload: dict[str, Any] = {"key": project_id, "kind": infer_kind(project_id)}
    if display_name:
        payload["display_name"] = display_name

    from yadgar.core import forward

    try:
        result = forward._forward_admin("create_project_row", payload, timeout_s=10.0)
    except Exception as exc:  # noqa: BLE001 — transport faults become the envelope; a hook must never crash on them
        logger.warning("project_register %s: backend call raised: %s", project_id, exc)
        return _result(project_id, ok=False, created=False, error=f"backend call failed: {exc}")

    if not isinstance(result, dict):
        return _result(project_id, ok=False, created=False, error=f"bad backend answer: {result!r}")
    if result.get("ok") is True:
        _project_registry.invalidate_project_registry_cache()
        logger.info("project_register: created registry row %s", project_id)
        return _result(project_id, ok=True, created=True)
    error = str(result.get("error", ""))
    if result.get("reason") == "duplicate_project" or is_duplicate_project_error(error):
        _project_registry.invalidate_project_registry_cache()
        return _result(project_id, ok=True, created=False)
    return _result(project_id, ok=False, created=False, error=error or "unknown backend error")


__all__ = [
    "DISPLAY_NAME_MAX_CHARS",
    "PROJECT_KEY_MAX_CHARS",
    "register_project",
    "registrable_project_id_error",
]
