"""export subcommand — DuckDB analytics snapshot.

Registers: yadgar export duckdb [flags]

Lazy imports duckdb only at command runtime, so the CLI remains importable
without the analytics optional dependency installed.

Not a backup: export is analytics-only and lossy. Canonical backups are
owned by yadgar vacuum. Re-run export to get fresh data — no incremental
mode (plan §Non-goals).

Exit codes: 0 success, 1 generic failure, 2 duckdb not installed.
"""

from __future__ import annotations

import sys


def cmd_export_duckdb(args) -> None:
    """Export SurrealDB tables to a DuckDB analytics snapshot."""
    try:
        import duckdb as _duckdb_check  # noqa: F401
    except ImportError:
        print(
            "duckdb is not installed. Install the analytics extra:\n"
            "    pip install yadgar[analytics]",
            file=sys.stderr,
        )
        sys.exit(2)

    from yadgar._shared.config import Settings
    from yadgar.core.export.duckdb_exporter import DuckDBExporter, ExportConfig

    settings = Settings()
    db_path = str(args.db_path or settings.DB_PATH)
    embedding_dim = getattr(settings, "EMBEDDING_DIM", 384)

    cfg = ExportConfig(
        include_secrets=args.include_secrets,
        action_log_since=args.action_log_since,
        action_log_limit=args.action_log_limit,
        create_views=not args.no_views,
        tables=_parse_tables(args.tables),
        embedding_dim=embedding_dim,
        force=args.force,
    )
    exporter = DuckDBExporter(db_path=db_path, output_path=args.output, config=cfg)

    try:
        exporter.run()
        print(f"Export complete: {args.output}", file=sys.stderr)
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — CLI top-level error reporting: every fault prints a one-line reason and exits 1 rather than a traceback
        print(f"Export failed: {exc}", file=sys.stderr)
        sys.exit(1)


def _parse_tables(tables_arg: str | None) -> list[str] | None:
    """Parse comma-separated tables arg → list or None (all)."""
    if not tables_arg:
        return None
    return [t.strip() for t in tables_arg.split(",") if t.strip()]


def register(subparsers) -> None:
    """Register 'export' parser with 'duckdb' sub-subcommand."""
    export_parser = subparsers.add_parser(
        "export",
        help="Export Yadgar data to external formats",
    )
    export_sub = export_parser.add_subparsers(dest="export_format")

    duckdb_parser = export_sub.add_parser(
        "duckdb",
        help=(
            "Export SurrealDB tables → DuckDB analytics snapshot. "
            "NOT a backup — analytics-only, lossy, snapshot semantics. "
            "Re-run to get fresh data. "
            "Requires: pip install yadgar[analytics]"
        ),
    )
    duckdb_parser.add_argument(
        "--output",
        required=True,
        metavar="FILE",
        help="Path to write .duckdb output file",
    )
    duckdb_parser.add_argument(
        "--include-secrets",
        action="store_true",
        default=False,
        help=(
            "Reserved for future row-level secret tagging. "
            "v5.10.2 secret-gate operates at write-time so no rows are "
            "stored with secrets; this flag is a forward-compat no-op today."
        ),
    )
    duckdb_parser.add_argument(
        "--action-log-since",
        default="30d",
        metavar="DURATION",
        help=(
            "Export action_log rows newer than this window. "
            "Accepts Nd/Nh/Nm (e.g. 30d, 12h) or 'all' (no time filter). "
            "Default: 30d"
        ),
    )
    duckdb_parser.add_argument(
        "--action-log-limit",
        type=int,
        default=100_000,
        metavar="N",
        help="Hard cap on action_log rows exported, sorted newest-first. Default: 100000",
    )
    duckdb_parser.add_argument(
        "--no-views",
        action="store_true",
        default=False,
        help="Skip creating the 10 analytics views. Plain table dump only.",
    )
    duckdb_parser.add_argument(
        "--tables",
        default=None,
        metavar="TABLE,...",
        help="Comma-separated subset of tables to export (advanced). Default: all",
    )
    duckdb_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Overwrite existing output file without prompting.",
    )
    duckdb_parser.add_argument(
        "--db-path",
        default=None,
        help="SurrealDB database path (overrides config)",
    )
    duckdb_parser.set_defaults(func=cmd_export_duckdb)

    # When 'export' is invoked without 'duckdb', show help
    export_parser.set_defaults(func=lambda _args: export_parser.print_help())
