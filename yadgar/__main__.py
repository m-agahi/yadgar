"""Entry point for python -m yadgar."""

import argparse
import sys

from yadgar import __version__

VALID_TRANSPORTS = ("sse", "streamable-http")

STARTUP_BANNER = f"""\
=== Yadgar v{__version__} ===
Persistent memory engine for Claude Code — heat decay, sleep consolidation, and surprise-gated storage

Active modules:
  * StorageEngine          (SurrealDB with KV + FTS + vector search)
  * EmbeddingEngine        (sentence-transformers)
  * ActionLogger           (episode capture)
  * MemoryThermodynamics   (surprise, importance, valence, decay)
  * KnowledgeGraph         (typed relationships)
  * Retriever              (PPR + vector + FTS + spreading activation)
  * MemoryCurator          (merge/link/create, contradiction detection)
  * ConsolidationScheduler (background consolidation + sleep cycle)
  * ProspectiveMemory      (future-oriented triggers)
  * StalenessDetector      (file-change watchdog)

Core tools: memorize, recall, forget, project_brief, checkpoint, restore,
            anchor, wiki_query, wiki_add, memory_stats
Power tools: add_rule, get_rules, wiki_read, wiki_list, wiki_delete,
             wiki_approve, wiki_discard, wiki_drafts, consolidate_now,
             reembed_all, validate_memory, seed_project, install_hooks,
             sync_instructions
v5 tools:   memory_get, wiki_get, memory_update, wiki_update,
             bootstrap_project, update_active_work, wiki_refresh_stale,
             wiki_cleanup_merged_branches

MCP Resources: memory://stats, memory://hot, memory://stale,
               memory://processes
"""


def cli():
    parser = argparse.ArgumentParser(description="Yadgar memory engine MCP server")
    subparsers = parser.add_subparsers(dest="command")

    # Default server mode (no subcommand)
    parser.add_argument("--host", type=str, default=None, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Server port (default: 8765)")
    parser.add_argument("--db-path", type=str, default=None, help="Database path")
    parser.add_argument(
        "--transport",
        type=str,
        default="streamable-http",
        choices=VALID_TRANSPORTS,
        help="MCP transport protocol (default: streamable-http)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress startup banner",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        default=False,
        help="Show yadgar core, backend, and daemon versions, then exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="With --version: emit JSON instead of plain text.",
    )

    # Register all subcommands in original order
    from yadgar.core.cli import (  # noqa: E402
        capture,
        config,
        context,
        daemon,
        drain,
        export,
        install_hooks,
        install_subagents,
        repo_wiki,
        restore,
        rules,
        seed,
        setup,
        stats,
        update,
        vacuum,
        viz,
    )

    drain.register(subparsers)
    export.register(subparsers)
    restore.register(subparsers)
    capture.register(subparsers)
    context.register(subparsers)
    stats.register(subparsers)
    vacuum.register(subparsers)
    seed.register(subparsers)
    config.register(subparsers)
    rules.register(subparsers)
    repo_wiki.register(subparsers)
    viz.register(subparsers)
    setup.register(subparsers)
    daemon.register(subparsers)
    install_hooks.register(subparsers)
    install_subagents.register(subparsers)
    update.register(subparsers)

    args = parser.parse_args()

    if args.version:
        from yadgar.core.cli.version import print_version_summary

        print_version_summary(json_mode=args.json)
        sys.exit(0)

    if args.command is None:
        # Default: run MCP server
        import os as _os

        if args.host:
            _os.environ["YADGAR_HOST"] = args.host
        if args.port:
            _os.environ["YADGAR_PORT"] = str(args.port)

        from yadgar._shared.config import get_settings as _get_settings

        _cfg = _get_settings()
        _log_level = _cfg.CORE_LOG_LEVEL
        _log_format = _cfg.LOG_FORMAT
        if _log_level and _log_level.upper() != "WARN" and _log_level.upper() != "WARNING":
            from yadgar._shared.observability.log_config import (
                configure_logging as _configure_logging,
            )

            _configure_logging(log_format=_log_format, level=_log_level, process="core")
        elif _log_format and _log_format.lower() == "json":
            from yadgar._shared.observability.log_config import (
                configure_logging as _configure_logging,
            )

            _configure_logging(log_format="json", level="WARNING", process="core")

        if not args.quiet:
            print(STARTUP_BANNER, file=sys.stderr)
            print(f"Transport: {args.transport}", file=sys.stderr)
            if args.host:
                print(f"Host: {args.host}", file=sys.stderr)
            if args.port:
                print(f"Port: {args.port}", file=sys.stderr)
            if args.db_path:
                print(f"Database: {args.db_path}", file=sys.stderr)
            print(file=sys.stderr)

        from yadgar.core.server import main

        main(port=args.port, db_path=args.db_path, transport=args.transport)
    else:
        args.func(args)


if __name__ == "__main__":
    cli()


# Re-export cmd_vacuum so tests that import yadgar.__main__ and call
# main_mod.cmd_vacuum(args) still work after the CLI was split into yadgar.cli.vacuum.
def cmd_vacuum(args):
    """Delegate to yadgar.cli.vacuum.cmd_vacuum (v4.x public API preserved)."""
    from yadgar.core.cli.vacuum import cmd_vacuum as _cmd_vacuum

    return _cmd_vacuum(args)
