"""yadgar.server — MCP server subpackage.

Split from a single 4353-LoC module into:
  _app.py        — FastMCP instance + _tool decorator + middleware
  _state.py      — Module-level singleton state (all globals)
  _helpers.py    — Shared utility functions
  lifecycle.py   — Singleton getters, init_engines, shutdown, main
  http.py        — Custom HTTP routes (@mcp_server.custom_route)
  tools/         — MCP tool registrations (memorize, recall, wiki, …)

Public API preserved: 'from yadgar.server import X' works for all X
that existed in the original monolithic server.py.
"""
# ruff: noqa: I001  — import order is load-order-significant

from __future__ import annotations

import sys as _sys
import types as _types

# ── 1. Core singletons (leaf — no internal imports) ──────────────────
from yadgar.server._app import (  # noqa: F401
    mcp_server,
    _tool,
    _PROFILE,
    _get_allowed_origins,
    _cors_wrapped_http_app,
    _auth_wrapped_sse_app,
    _orig_streamable_http_app,
    _orig_sse_app,
    settings,
)

# ── 2. State constants (locks, frozen sets — never reassigned after init) ─
# Mutable singletons (_storage, _wiki, etc.) are NOT statically re-exported
# because `from module import x` snapshots the value at import time (None).
# Instead, __getattr__ below delegates to _state for dynamic lookups.
import yadgar.server._state as _state_mod  # noqa: F401

from yadgar.server._state import (  # noqa: F401
    _queue_lock,
    _event_lock,
    _metrics_lock,
    _action_batch_lock,
    _CAPTURE_TOOLS,
    _SKIP_TOOL_PREFIXES,
    _PER_TABLE_FIELDS,
    _DICT_MAX_SIZE,
    _auto_capture_limiter,
    # These are mutable containers (dict/deque/set/OrderedDict) but are
    # mutated in-place — the object identity doesn't change, only contents.
    # So static re-export is safe: tests get the live container object.
    _action_batch,
    _project_roots,
    _last_session_context,
    _last_prompt_recall,
    _event_queue,
    _system_metrics_cache,
    _last_recalled_ids,
)

# ── 3. Shared helpers ─────────────────────────────────────────────────
from yadgar.server._helpers import (  # noqa: F401
    _q_with_timeout,
    _has_unpaired_surrogate,
    _push_event,
    _bounded_set,
    _is_episodic_query,
    _file_hash,
    _build_dlq_alert_text,
    _DECISION_STRONG_RE,
)

# ── 4. Lifecycle (getters, init, shutdown, main) ──────────────────────
from yadgar.server.lifecycle import (  # noqa: F401
    _get_storage,
    _get_embeddings,
    _get_buffer,
    _get_consolidation,
    _get_staleness,
    _get_thermo,
    _get_retriever,
    _get_write_gate,
    _get_engram,
    _get_replay,
    _get_file_queue,
    _load_default_rules,
    init_engines,
    shutdown,
    _signal_handler,
    main,
)

# ── 5. HTTP routes (side-effects: registers @mcp_server.custom_route) ─
import yadgar.server.http  # noqa: F401
import yadgar.server.admin_config  # noqa: F401 — v5.6.7 PR-J: GET /admin/config route
import yadgar.server.http_bookmarks  # noqa: F401 — v5.23.0: bookmark + wiki routes
import yadgar.server.http_wiki_versioning  # noqa: F401 — v5.50.1: wiki query/history/diff/restore
import yadgar.server.routes.control_update  # noqa: F401 — v5.48.0: POST /api/control/update
import yadgar.server.routes.control  # noqa: F401 — v5.50.2: GET+POST /api/control/config, POST /api/control/action/*, POST /api/control/restart/*

# Re-export HTTP route functions so 'import yadgar.server as srv; srv.hook_auto_capture'
# resolves correctly (test_async_handlers_no_block.py, test_sse.py)
from yadgar.server.http import (  # noqa: F401
    health_check,
    metrics_endpoint,
    hook_pre_compact,
    hook_post_compact,
    hook_auto_capture,
    hook_subagent_stop,
    hook_file_changed,
    hook_session_context,
    hook_prompt_recall,
    api_graph,
    api_stats,
    api_graph_stats,
    api_graph_neighborhood,
    api_system,
    api_heat_histogram,
    api_consolidation_log,
    _make_event_stream,
    api_graph_events,
    graph_view,
    _build_dlq_alert_text,  # noqa: F811 — already re-exported above, both point to same func
)

# ── 6. MCP tools (side-effects: registers @_tool decorators) ─────────
import yadgar.server.tools  # noqa: F401

from yadgar.server.tools import (  # noqa: F401
    memorize,
    remember,
    recall,
    project_brief,
    bootstrap_project,
    update_active_work,
    wiki_refresh_stale,
    wiki_cleanup_merged_branches,
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
    wiki_add,
    wiki_query,
    wiki_read,
    wiki_delete,
    wiki_list,
    wiki_lint,
    wiki_drafts,
    wiki_approve,
    block_create,
    block_get,
    block_update,
    block_delete,
    block_list,
    wiki_discard,
    wiki_check_duplicate,
    checkpoint,
    restore,
    anchor,
    install_hooks,
    sync_instructions,
    seed_project,
    resource_stats,
    resource_hot,
    resource_stale,
    agent_prompt_get,
    agent_prompt_save,
    wiki_coverage,
    agent_dispatch_prelude,
)

# ── 7. Project helpers (tests import _detect_branch etc. indirectly) ──
from yadgar.server.tools.project import (  # noqa: F401
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


def __getattr__(name: str):
    """Dynamic attribute lookup delegates to _state for singleton vars.

    PEP 562: called only when name is not found in module __dict__.
    This ensures tests that do `server._wiki` or `server._storage` after
    init_engines() get the live object, not the None snapshot from import time.
    """
    import yadgar.server._state as _st  # noqa: PLC0415

    try:
        return getattr(_st, name)
    except AttributeError:
        raise AttributeError(f"module 'yadgar.server' has no attribute {name!r}") from None


# ── Module-class override: intercept attribute writes ────────────────────────
# PEP 562 only covers __getattr__ (reads). For writes (e.g. tests that do
# `server._event_seq = 0`), we replace the module class with one that has a
# real __setattr__ that delegates to _state so tool code using `_st._X`
# sees the same value.


class _ServerModule(_types.ModuleType):
    """ModuleType subclass that forwards attribute writes to _state.

    Single source of truth = _state. We do NOT cache in server.__dict__
    because lifecycle.init_engines / shutdown assign directly to _state
    (e.g. `_st._storage = StorageEngine(...)`), bypassing this forwarder.
    Caching here would let server.__dict__ drift stale, and PEP 562
    __getattr__ only fires on a miss in __dict__ — so a stale cached
    entry would shadow the live _state value.

    Writes mirror to _state when it owns the attribute. Reads go through
    __getattr__ → _state (skip __dict__ entirely). monkeypatch.setattr
    on yadgar.server works because pytest reads via __getattr__ first
    and then writes via this forwarder, both consistent with _state.
    """

    def __setattr__(self, name: str, value) -> None:
        import yadgar.server._state as _st  # noqa: PLC0415

        if _st is not self and name in _st.__dict__:
            # Forward to _state — bypass _state's own __setattr__ (if any)
            # to avoid the recursion bug from the original v5.1.0 fix.
            _st.__dict__[name] = value
        else:
            # Symbol not owned by _state — fall back to plain module behavior
            # so things like `_sys.modules[__name__].__class__ = _ServerModule`
            # at import time, or test attrs not in _state, still work.
            super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _ServerModule
del _types  # keep namespace clean
