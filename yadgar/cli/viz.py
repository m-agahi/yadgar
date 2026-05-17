"""viz subcommand — knowledge graph visualization server."""


def cmd_viz(args):
    """Start the knowledge graph visualization server."""
    from yadgar.viz_server import run_viz_server

    run_viz_server(
        port=args.port,
        daemon_url=args.daemon_url,
        open_browser=args.open,
    )


def register(subparsers):
    p = subparsers.add_parser(
        "viz", help="Start knowledge graph visualization server (http://localhost:42069)"
    )
    p.add_argument("--port", type=int, default=42069, help="Viz server port (default: 42069)")
    p.add_argument("--open", action="store_true", help="Open browser automatically")
    p.add_argument(
        "--daemon-url",
        type=str,
        default="http://127.0.0.1:8765",
        help="Yadgar daemon URL (default: http://127.0.0.1:8765)",
    )
    p.set_defaults(func=cmd_viz)
