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

    Returns {"ok", "violations", "fixed", "counts", "cross_engine", ...}.
    - violations: unfixable structural problems (ceiling breaches, slot anomalies,
      etc.), PLUS any cross-engine violation
    - fixed: descriptions of auto-repaired issues (dangling FK rows deleted)
    - cross_engine: the engine-#2 arm (ADR-0195), ALWAYS present. Carries
      {"status", "checks", "violations", "unavailable"}, where every check is
      "ok" | "violation" | "unavailable" and reports the values it compared.
    - ok: True only when violations is empty. READ THIS CAREFULLY — ok=True is
      compatible with cross-engine checks that COULD NOT RUN (engine #2 absent,
      the `sql` extra missing, the spine unshipped). Those report "unavailable",
      never "ok", and are surfaced via cross_engine.status + a WARNING log rather
      than by flipping ok, because engine #2 is optional today and a permanently
      red check would be special-cased away. A cross-engine VIOLATION does flip ok.
    Logs INFO for each auto-repair, CRITICAL for each remaining violation.

    R3 Car 3d: the checks + auto-repair DELETEs run backend-side (owns the DB);
    forward-only via /admin. Engine-#2 car H: the backend op is async (asyncmy is
    async-only); this shell is unchanged, the forward is the same.
    """
    return _forward_admin("check_invariants", {})
