"""check_invariants MCP tool (thin core shell).

R3 Car 3d: check_invariants runs consistency checks AND auto-repairs (DB DELETEs),
so the whole compute (``_run_check_invariants`` + every ``_check_*`` helper) moved
to ``yadgar.backend.admin_exec.invariants``. The core shell takes no args and
runs no secret-gate — it just forwards to the backend /admin op.
"""

from __future__ import annotations

import logging

from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool

logger = logging.getLogger(__name__)


@_tool(power=True)
def check_invariants() -> dict:
    """Run consistency checks on the memory store, auto-repairing fixable issues.

    Returns {"ok": bool, "violations": [...], "fixed": [...], "counts": {...}}.
    - violations: unfixable structural problems (ceiling breaches, slot anomalies, etc.)
    - fixed: descriptions of auto-repaired issues (dangling FK rows deleted)
    - ok: True only when fixed items don't affect ok (violations is empty)
    Logs INFO for each auto-repair, CRITICAL for each remaining violation.

    R3 Car 3d: the checks + auto-repair DELETEs run backend-side (owns the DB);
    forward-only via /admin.
    """
    return _forward_admin("check_invariants", {})
