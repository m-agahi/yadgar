"""Admin/maintenance MCP tool registrations and helpers.

Shim module: imports all per-domain admin submodules so @_tool decorators fire
at import time, and re-exports all public names for backward compatibility.

Submodules:
- admin_invariants: check_invariants tool (thin shell → backend /admin; the
                    _run_check_invariants compute lives in
                    yadgar.backend.admin_exec.invariants as of R3 Car 3d)
- admin_vacuum:     vacuum_now tool
- admin_dlq:        dlq_inspect + dlq_requeue tools
- admin_other:      forget, validate_memory, consolidate_now, reembed_all,
                    memory_stats, add_rule, get_rules, memory_get, wiki_get,
                    memory_update, wiki_update
- admin_archive:    archive_purge tool (v5.49.0 Phase 4)
"""

# ruff: noqa: F401, E402  — all imports are re-exports; order is load-order-significant

# Import submodules to trigger @_tool decorator registration
from yadgar.core.server.tools.admin_archive import archive_purge
from yadgar.core.server.tools.admin_dlq import dlq_dismiss, dlq_inspect, dlq_requeue
from yadgar.core.server.tools.admin_invariants import check_invariants
from yadgar.core.server.tools.admin_other import (
    _MEMORY_UPDATE_ALLOWED,
    _WIKI_UPDATE_ALLOWED,
    add_rule,
    consolidate_now,
    forget,
    get_rules,
    memory_get,
    memory_stats,
    memory_update,
    reembed_all,
    validate_memory,
    wiki_get,
    wiki_update,
)
from yadgar.core.server.tools.admin_vacuum import vacuum_now

__all__ = [
    "check_invariants",
    "vacuum_now",
    "dlq_inspect",
    "dlq_requeue",
    "dlq_dismiss",
    "forget",
    "validate_memory",
    "consolidate_now",
    "reembed_all",
    "memory_stats",
    "add_rule",
    "get_rules",
    "memory_get",
    "wiki_get",
    "memory_update",
    "wiki_update",
    "archive_purge",
]
