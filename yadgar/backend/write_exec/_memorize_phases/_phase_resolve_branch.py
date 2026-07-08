"""Phase 2 — resolve_branch: resolve git branch for the drain-replay sync path.

R3 Car 1 (write-half): the enqueue fast-path moved OUT of this phase into the
core memorize shell (yadgar.core.server.tools.memorize). This phase now runs
ONLY inside the backend drainer's replay path, so it never enqueues — it only
resolves the branch that was captured at enqueue time (carried on
ctx.branch_hint) and lets the sync pipeline proceed.

Resolution order: ctx.branch_hint (enqueue-time branch) → YADGAR_CI_BRANCH env.
No git subprocess: the daemon cwd is not the caller's repo, so re-detecting the
branch here would be wrong. The enqueue-time value is authoritative.
"""

from __future__ import annotations

import logging
import os

from yadgar._shared.tracing import trace_span
from yadgar._shared.write_exec import MemorizeContext

logger = logging.getLogger(__name__)


@trace_span()
def phase_resolve_branch(ctx: MemorizeContext) -> dict | None:
    """Resolve branch context for the drain-replay sync path.

    Mutations on ctx:
    - resolved_branch set from branch_hint (enqueue-time branch) / env.

    Returns None always (sync pipeline continues to embed phase). Kept as a
    dict | None signature for call-site symmetry with the other phases.
    """
    branch = ctx.branch_hint or os.environ.get("YADGAR_CI_BRANCH") or None
    ctx.resolved_branch = branch
    return None
