"""Core-side app startup: ``main()`` — the MCP-server entry point.

Car 3 (folder-split #17): extracted from ``yadgar._shared.runtime.lifecycle`` so
that ``_shared`` no longer imports ``yadgar.server`` (a core namespace). The pure
engine lifecycle (``init_engines`` / ``shutdown`` / signal handler / startup
diagnostics) stays in ``_shared.runtime.lifecycle`` — none of it creates a server
edge. Only ``main()`` does (it registers the FastMCP ``mcp_server`` app and runs
the ``sync_instructions`` / ``install_hooks`` core startup calls), so only
``main()`` lives here on the core side.

``main()`` calls back into ``_shared.runtime.lifecycle`` for ``init_engines`` and
the startup helpers — a core→_shared edge, which the layered import contract
allows.
"""

from __future__ import annotations

import logging
import os
import signal
import time

import yadgar._shared.paths as _paths
import yadgar._shared.runtime.state as _st
from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _emit_startup_diagnostics

# R2a Car B: main() must build the FULL 24-engine set — route through the CORE
# composition root (shared engines + 9 core-only), not the shared-only lifecycle
# entry.
from yadgar.core.bootstrap import core_init_engines as init_engines
from yadgar.core.daemon.daemons import _maybe_auto_check_for_update  # R2a Car D1

# R2a Car D2: the signal handler + graceful-shutdown wrapper moved to
# yadgar.core.lifecycle (they import yadgar.core.sensitive_lock / sd_notify / drain
# — formerly the last _shared → core edges). main() binds the CORE handler and the
# finally-block calls the CORE shutdown wrapper (which injects the sd_notify/drain
# callbacks into the shared teardown at their exact original positions).
from yadgar.core.lifecycle import _signal_handler, shutdown

logger = logging.getLogger(__name__)


@observe(tier="boundary")
def main(
    port: int | None = None,
    db_path: str | None = None,
    transport: str = "streamable-http",
):
    from yadgar.core.server._app import mcp_server

    _st._active_transport = transport
    _st._start_time = time.time()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Self-register PID file so `yadgar daemon stop/restart/status` can find us
    # regardless of how the process was started (systemd, direct CLI, etc.).
    _pid_path = _paths.PID_PATH
    try:
        _pid_path.parent.mkdir(parents=True, exist_ok=True)
        _pid_path.write_text(str(os.getpid()))
    except Exception:
        pass

    # H-7: Fail fast if REQUIRE_AUTH=True but no token configured.
    # A server that requires auth but has no token is silently broken — every
    # request would get 503 "Admin token not configured" rather than a useful error.
    # Use Settings() directly (bypass lru_cache) so the check always reflects the
    # current environment — important for tests that reload yadgar.config.
    from yadgar._shared.config import Settings as _Settings  # noqa: PLC0415

    _auth_settings = _Settings()
    if _auth_settings.REQUIRE_AUTH and not _auth_settings.MCP_AUTH_TOKEN:
        raise RuntimeError(
            "REQUIRE_AUTH=1 requires YADGAR_MCP_AUTH_TOKEN to be set. "
            "Source /etc/yadgar/secrets.env or run `yadgar setup`."
        )

    # Don't auto-watch cwd — in daemon/systemd mode cwd is $HOME, which would
    # recursively watch everything including the DB files, causing a watchdog storm.
    # Staleness watching is triggered per-project via MCP tools instead.
    init_engines(
        db_path=db_path,
        start_daemons=True,
        watch_directory=None,
    )

    # v5.6.7 PR-J: emit startup config-dump log + seed config gauges.
    # BC-EN2b: warn_comet_dormant gets its OWN try/except so a failure in
    # emit_startup_config_log / _set_config_gauges can never silently swallow
    # the dormant warning (see _emit_startup_diagnostics).
    _emit_startup_diagnostics(get_settings())

    # Auto-sync CLAUDE.md on every startup so rules stay current
    try:
        from yadgar.core.server.tools.misc import sync_instructions

        sync_instructions()
        from yadgar import __version__

        logger.info("CLAUDE.md synced with Yadgar v%s", __version__)
    except Exception:
        logger.debug("Auto-sync of CLAUDE.md failed (non-fatal)")

    # Auto-install hooks for the current project if not already present.
    # Skipped under pytest: install_hooks(os.getcwd()) writes to the project's
    # .claude/settings.json (project scope resolves from cwd, NOT HOME — the
    # _guard_home fixture only redirects HOME), so a test whose cwd is a real repo
    # would poison that repo's settings with a PreToolUse hook pointing at a
    # torn-down pytest tmp dir, blocking every subsequent Bash call in the repo.
    # Tests that exercise install_hooks call it directly with an isolated
    # project_directory, so nothing here needs the startup auto-install.
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            from yadgar.core.server.tools.misc import install_hooks

            install_hooks(os.getcwd())
            logger.info("Hippocampal Replay hooks installed for %s", os.getcwd())
        except Exception:
            logger.debug("Auto-install of hooks failed (non-fatal)")

    # v5.48.0: opt-in auto-check for updates on daemon start (default OFF)
    _maybe_auto_check_for_update()

    if port is not None:
        mcp_server.settings.port = port

    if transport == "streamable-http":
        # Enable stateless mode: each POST /mcp is handled independently with no
        # session ID required. This makes daemon restarts transparent — Claude Code
        # reconnects and tool calls work immediately without a stale-session failure.
        # Must be set on settings BEFORE streamable_http_app() is first called (lazy
        # init reads this flag to construct the StreamableHTTPSessionManager).
        mcp_server.settings.stateless_http = True

    try:
        mcp_server.run(transport=transport)
    finally:
        shutdown()
