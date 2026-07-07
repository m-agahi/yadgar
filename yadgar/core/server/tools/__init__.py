"""yadgar.server.tools — MCP tool registrations.

Importing this package triggers all @_tool() decorators, registering
every tool with the FastMCP instance in yadgar.server._app.
"""
# ruff: noqa: I001  — import order is load-order-significant (project before memorize/recall/wiki)

# Import order matters: project.py must come before memorize/recall/wiki
# (branch helpers are used by those modules).
import yadgar.core.server.tools.project  # noqa: F401 — side-effects: tool registration
import yadgar.core.server.tools.memorize  # noqa: F401 — side-effects: tool registration
import yadgar.core.server.tools.recall  # noqa: F401 — side-effects: tool registration
import yadgar.core.server.tools.admin  # noqa: F401 — side-effects: tool registration
import yadgar.core.server.tools.wiki  # noqa: F401 — side-effects: tool registration
import yadgar.core.server.tools.misc  # noqa: F401 — side-effects: tool registration
import yadgar.core.server.tools.agent_prompts  # noqa: F401 — side-effects: tool registration (S8)
import yadgar.core.server.tools.wiki_coverage  # noqa: F401 — side-effects: tool registration
import yadgar.core.server.tools.dispatch_helper  # noqa: F401 — side-effects: tool registration
import yadgar.core.server.tools.audit  # noqa: F401 — side-effects: tool registration
import yadgar.core.server.tools.bookmarks  # noqa: F401 — side-effects: tool registration
import yadgar.core.server.tools.blocks  # noqa: F401 — side-effects: tool registration (v5.33.0)
import yadgar.core.server.tools.repo_wiki  # noqa: F401 — side-effects: tool registration (T8)
import yadgar.core.server.tools.adr  # noqa: F401 — side-effects: tool registration (car #12)

# Fix A (daemon-offload-A): import triggers register_test_tools() at its module
# bottom — registers _test_sleep/_test_thread_id only when YADGAR_TEST_TOOLS=1
# (no-op in prod). Used by the real-daemon offload e2e.
import yadgar.core.server.tools._test_tools  # noqa: F401 — side-effects: gated test-tool registration

# Re-export everything that tests or external code import directly
from yadgar.core.server.tools.memorize import memorize
from yadgar.core.server.tools.recall import recall
from yadgar.core.server.tools.project import (
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
from yadgar.core.server.tools.admin import (
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
    dlq_dismiss,
    _run_check_invariants,
    archive_purge,
)
from yadgar.core.server.tools.wiki import (
    wiki_add,
    wiki_query,
    wiki_read,
    wiki_delete,
    wiki_list,
    wiki_lint,
    wiki_autolink,
    wiki_drafts,
    wiki_approve,
    wiki_discard,
    wiki_check_duplicate,
    wiki_history,
    wiki_read_version,
    wiki_diff,
    wiki_restore,
    wiki_append_section,
    wiki_set_metadata,
    wiki_replace_text,
    wiki_delete_text,
    wiki_insert_after,
    wiki_insert_before,
    wiki_replace_at,  # noqa: F401
    wiki_delete_at,  # noqa: F401
    wiki_insert_at,  # noqa: F401
    wiki_replace_markdown_block,  # noqa: F401
)
from yadgar.core.server.tools.misc import (
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
from yadgar.core.server.tools.agent_prompts import (
    agent_prompt_save,
    seed_agent_prompts,
)
from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude
from yadgar.core.server.tools.wiki_coverage import wiki_coverage
from yadgar.core.server.tools.audit import audit_anchors
from yadgar.core.server.tools.bookmarks import (
    bookmark_add,
    bookmark_remove,
    bookmark_list,
    bookmark_reorder,
)
from yadgar.core.server.tools.blocks import (
    block_append,
    block_create,
    block_delete,
    block_get,
    block_list,
    block_replace,
    block_update,
)
from yadgar.core.server.tools.repo_wiki import repo_wiki_generate
from yadgar.core.server.tools.adr import adr_add

__all__ = [
    "memorize",
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
    "dlq_dismiss",
    "_run_check_invariants",
    "wiki_add",
    "wiki_query",
    "wiki_read",
    "wiki_delete",
    "wiki_list",
    "wiki_lint",
    "wiki_autolink",
    "wiki_drafts",
    "wiki_approve",
    "wiki_discard",
    "wiki_check_duplicate",
    "wiki_history",
    "wiki_read_version",
    "wiki_diff",
    "wiki_restore",
    "wiki_append_section",
    "wiki_set_metadata",
    "wiki_replace_text",
    "wiki_delete_text",
    "wiki_insert_after",
    "wiki_insert_before",
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
    "agent_prompt_save",
    "seed_agent_prompts",
    "agent_dispatch_prelude",
    "wiki_coverage",
    "audit_anchors",
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
    "archive_purge",
    "repo_wiki_generate",
    "adr_add",
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
