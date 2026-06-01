"""yadgar.server.tools — MCP tool registrations.

Importing this package triggers all @_tool() decorators, registering
every tool with the FastMCP instance in yadgar.server._app.
"""
# ruff: noqa: I001  — import order is load-order-significant (project before memorize/recall/wiki)

# Import order matters: project.py must come before memorize/recall/wiki
# (branch helpers are used by those modules).
import yadgar.server.tools.project  # noqa: F401 — side-effects: tool registration
import yadgar.server.tools.memorize  # noqa: F401 — side-effects: tool registration
import yadgar.server.tools.recall  # noqa: F401 — side-effects: tool registration
import yadgar.server.tools.admin  # noqa: F401 — side-effects: tool registration
import yadgar.server.tools.wiki  # noqa: F401 — side-effects: tool registration
import yadgar.server.tools.misc  # noqa: F401 — side-effects: tool registration
import yadgar.server.tools.agent_prompts  # noqa: F401 — side-effects: none (pure functions)
import yadgar.server.tools.wiki_coverage  # noqa: F401 — side-effects: tool registration
import yadgar.server.tools.dispatch_helper  # noqa: F401 — side-effects: tool registration
import yadgar.server.tools.audit  # noqa: F401 — side-effects: tool registration
import yadgar.server.tools.bookmarks  # noqa: F401 — side-effects: tool registration
import yadgar.server.tools.blocks  # noqa: F401 — side-effects: tool registration (v5.33.0)

# Re-export everything that tests or external code import directly
from yadgar.server.tools.memorize import memorize, remember
from yadgar.server.tools.recall import recall
from yadgar.server.tools.project import (
    project_brief,
    bootstrap_project,
    update_active_work,
    wiki_refresh_stale,
    wiki_cleanup_merged_branches,
    _detect_branch,
    _detect_branch_cached,
    _get_default_branch,
    _get_default_branch_cached,
    _get_current_branch,
    _resolve_project_root,
    _git_safe_env,
    _GIT_SAFE_ARGS,
    _render_project_brief,
    _wiki_refresh_stale_impl,
    _parse_frontmatter,
    _compute_source_hash,
)
from yadgar.server.tools.admin import (
    forget,
    validate_memory,
    check_invariants,
    vacuum_now,
    consolidate_now,
    reembed_all,
    memory_stats,
    add_rule,
    get_rules,
    memory_get,
    wiki_get,
    memory_update,
    wiki_update,
    dlq_inspect,
    dlq_requeue,
    _run_check_invariants,
)
from yadgar.server.tools.wiki import (
    wiki_add,
    wiki_query,
    wiki_read,
    wiki_delete,
    wiki_list,
    wiki_lint,
    wiki_drafts,
    wiki_approve,
    wiki_discard,
    wiki_check_duplicate,
)
from yadgar.server.tools.misc import (
    checkpoint,
    restore,
    anchor,
    install_hooks,
    sync_instructions,
    seed_project,
    resource_stats,
    resource_hot,
    resource_stale,
    resource_processes,
)
from yadgar.server.tools.agent_prompts import (
    agent_prompt_get,
    agent_prompt_save,
)
from yadgar.server.tools.dispatch_helper import agent_dispatch_prelude
from yadgar.server.tools.wiki_coverage import wiki_coverage
from yadgar.server.tools.audit import audit_anchors
from yadgar.server.tools.bookmarks import (
    bookmark_add,
    bookmark_remove,
    bookmark_list,
    bookmark_reorder,
)
from yadgar.server.tools.blocks import (
    block_append,
    block_create,
    block_delete,
    block_get,
    block_list,
    block_replace,
    block_update,
)

__all__ = [
    "memorize",
    "remember",
    "recall",
    "project_brief",
    "bootstrap_project",
    "update_active_work",
    "wiki_refresh_stale",
    "wiki_cleanup_merged_branches",
    "forget",
    "validate_memory",
    "check_invariants",
    "vacuum_now",
    "consolidate_now",
    "reembed_all",
    "memory_stats",
    "add_rule",
    "get_rules",
    "memory_get",
    "wiki_get",
    "memory_update",
    "wiki_update",
    "dlq_inspect",
    "dlq_requeue",
    "_run_check_invariants",
    "wiki_add",
    "wiki_query",
    "wiki_read",
    "wiki_delete",
    "wiki_list",
    "wiki_lint",
    "wiki_drafts",
    "wiki_approve",
    "wiki_discard",
    "wiki_check_duplicate",
    "checkpoint",
    "restore",
    "anchor",
    "install_hooks",
    "sync_instructions",
    "seed_project",
    "resource_stats",
    "resource_hot",
    "resource_stale",
    "resource_processes",
    "agent_prompt_get",
    "agent_prompt_save",
    "agent_dispatch_prelude",
    "wiki_coverage",
    "audit_anchors",
    "agent_dispatch_prelude",
    "bookmark_add",
    "bookmark_remove",
    "bookmark_list",
    "bookmark_reorder",
    "block_append",
    "block_create",
    "block_delete",
    "block_get",
    "block_list",
    "block_replace",
    "block_update",
    # Private helpers re-exported for test access
    "_detect_branch",
    "_detect_branch_cached",
    "_get_default_branch",
    "_get_default_branch_cached",
    "_get_current_branch",
    "_resolve_project_root",
    "_git_safe_env",
    "_GIT_SAFE_ARGS",
    "_render_project_brief",
    "_wiki_refresh_stale_impl",
    "_parse_frontmatter",
    "_compute_source_hash",
]
