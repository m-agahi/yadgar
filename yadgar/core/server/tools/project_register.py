"""``project_register`` — register one project id in the registry (ADR-1131).

The explicit, single-project counterpart to the SessionStart hook's
auto-registration. ``project_seed`` stays the bulk migration path (a TSV map);
this tool takes one id and one optional display name.

Not a power tool: the ``UnknownProjectError`` message tells an agent to call
it, so it must exist in the minimal profile too.
"""

from __future__ import annotations

from typing import Any

from yadgar.core.server._app import _tool


@_tool(power=False)
def project_register(*, project_id: str, display_name: str | None = None) -> dict[str, Any]:
    """Register *project_id* in the project registry if it is absent.

    Use this when a write fails with ``unknown project_id`` and the id is
    right (a new repo, or a session started before the hook could register
    it). Do NOT use it to make a typo work: pass the correct ``project=``.

    Idempotent: an id already registered returns ``ok: True, created: False``.

    Args:
        project_id: ``owner/repo`` (any depth, e.g. ``group/sub/repo``) or
            ``local/<name>``. Lowercase, no whitespace, at most 256 characters,
            not ``global`` / ``unresolved`` / ``system``.
        display_name: optional human label, at most 64 characters.

    Returns:
        ``{"ok": bool, "project_id": str, "created": bool}``, plus ``"error"``
        when ``ok`` is False (shape-guard rejection or backend failure).
    """
    from yadgar.core import project_registration

    return project_registration.register_project(project_id, display_name)
