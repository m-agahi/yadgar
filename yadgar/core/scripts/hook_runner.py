#!/usr/bin/env python3
"""Yadgar hook runner — thin shim over ``yadgar hook <event>`` (Car 0).

This script is installed as a real file and referenced by ABSOLUTE PATH in
~/.claude/settings.json; the project directory (or other context) is passed as
argv[1], never shell-interpolated. By referencing this script by absolute path
and passing context as argv[1], we avoid all shell metacharacter injection risks
present in the previous inline `python3 -c "..."` approach.

Car 0 collapsed the two hook code paths into ONE implementation living in
``yadgar.core.cli.hook`` (the ``yadgar hook <event>`` CLI body). This module is
now a shim: it re-exports that module's full surface (so external importers and
the characterization test-suite that import ``hook_*`` / ``hook_db_lockdown_check``
from ``yadgar.core.scripts.hook_runner`` are byte-unaffected) and delegates
``main()`` to the shared dispatcher IN-PROCESS (no subprocess — same interpreter,
same stdin, identical behavior).

Usage:
    hook_runner.py <hook_type> [project_directory]

hook_type:
    post-tool-capture       — PostToolUse handler
    session-start-context   — SessionStart handler
    post-compact-rehydrate  — SessionStart (compact) handler
    pre-compact-drain       — PreCompact handler
    prompt-recall           — UserPromptSubmit handler
    block-reflect           — PostToolUse memory-block re-inject
"""

from __future__ import annotations

import sys

# Re-export the shared hook body. The characterization suite patches these names
# ON THIS MODULE (`monkeypatch.setattr(hr, "_http_post", ...)`) — but the suite
# was repointed at ``yadgar.core.cli.hook`` in Car 0, so the canonical patch
# target is that module. These re-exports keep import-only back-compat
# (`from yadgar.core.scripts.hook_runner import hook_db_lockdown_check`).
from yadgar.core.cli.hook import (  # noqa: F401
    _AUTH_TOKEN,
    _BLOCK_REFLECT_TOOLS,
    _BLOCKED_EXEC_PATTERNS,
    _HOOKS,
    _PORT,
    _auth_headers,
    _capture_in_flight_host,
    _detect_branch,
    _http_get,
    _http_post,
    _log_hook_error,
    dispatch,
    hook_block_reflect,
    hook_db_lockdown_check,
    hook_post_compact_rehydrate,
    hook_post_tool_capture,
    hook_pre_compact_drain,
    hook_prompt_recall,
    hook_session_start_context,
)


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <hook_type> [project_directory]", file=sys.stderr)
        print(f"Available hook types: {', '.join(_HOOKS)}", file=sys.stderr)
        sys.exit(1)

    sys.exit(dispatch(sys.argv[1]))


if __name__ == "__main__":
    main()
