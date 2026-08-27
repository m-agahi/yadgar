"""Field-filter helper for memory_update — shared gate (Car C7b, task #275).

Extracted from ``admin_other.py`` so ``memory_update`` (user-facing) and
``de_anchor`` (internal anchor-retire) route their ``fields`` dict through
the SAME allowlist check before forwarding to backend.

Before this helper, ``de_anchor`` called ``_forward_admin("memory_update", ...)``
directly and skipped the gate at admin_other.py:544. The backend handler
(``yadgar/backend/admin_exec/memory.py``) trusts core's contract and does
NOT re-validate, so the gate MUST run in core.

``project_id`` value is shape-validated (not registry-checked) by
``project_id_value_error``; see its docstring for why. Raises
``ValueError`` on a disallowed key or a malformed ``project_id``.
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe
from yadgar.core.server.tools._project_param import project_id_value_error


@observe(tier="stage", metric="tools.memory_update.filter_fields")
def _filter_memory_update_fields(fields: dict) -> dict:
    # Lazy import: admin_other imports this module at top-level for the
    # re-export. Importing admin_other here at module-load time creates a
    # circular import — admin_other._MEMORY_UPDATE_ALLOWED is a frozenset
    # literal that is always set before either of these tools is invoked.
    from yadgar.core.server.tools.admin_other import _MEMORY_UPDATE_ALLOWED

    unknown = set(fields) - _MEMORY_UPDATE_ALLOWED
    if unknown:
        raise ValueError(
            f"Disallowed field(s) for memory_update: {sorted(unknown)}. "
            f"Allowed: {sorted(_MEMORY_UPDATE_ALLOWED)}"
        )
    if "project_id" in fields and (err := project_id_value_error(fields["project_id"])):
        raise ValueError(err)
    return fields
