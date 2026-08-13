"""install-mcp CLI subcommand — Car 1 minimal entry point.

Writes the yadgar MCP entry into the target client's config file.  The full
unified ``yadgar install --client X`` command (covering MCP + rules + hooks in
one shot) is Car 3.  This module wires just the MCP surface so Car 1 is
independently callable without waiting for Cars 2+3.

Usage:
  yadgar install-mcp --client claude-code
  yadgar install-mcp --client opencode --port 8765
  yadgar install-mcp --client codex --scope global
  yadgar install-mcp --client cursor --scope project --project-directory .
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def cmd_install_mcp(args) -> None:
    from yadgar.core.install.auth_token import resolve_auth_token
    from yadgar.core.install.clients.mcp_register import register_mcp
    from yadgar.core.install.clients.registry import CLIENT_REGISTRY

    client_name = args.client
    if client_name not in CLIENT_REGISTRY:
        known = ", ".join(sorted(CLIENT_REGISTRY))
        print(f"Unknown client {client_name!r}. Known: {known}", file=sys.stderr)
        sys.exit(1)

    descriptor = CLIENT_REGISTRY[client_name]
    port = args.port or 8765
    url = f"http://127.0.0.1:{port}/mcp"
    # Car 9: route through the ONE sanctioned bearer-token resolver (env var,
    # else secrets.env) — `yadgar install-mcp` is a host CLI invocation where
    # the env var may not be exported even though secrets.env holds it.
    token = resolve_auth_token().strip()
    scope = args.scope or "global"
    project_dir = Path(args.project_directory).resolve() if args.project_directory else None

    try:
        result = register_mcp(
            descriptor,
            url=url,
            token=token,
            scope=scope,
            project_dir=project_dir,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"MCP config updated: {result['updated']}")
    print(f"  Client:    {client_name}")
    print(f"  Endpoint:  {url}")
    if result.get("old"):
        print(f"  Previous:  {json.dumps(result['old'])}")
    print(f"  New entry: {json.dumps(result['new'])}")


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "install-mcp",
        help="Write the yadgar MCP entry into a client's config file (Car 1; full install is Car 3)",
    )
    p.add_argument(
        "--client",
        required=True,
        metavar="NAME",
        help=(
            "Client to register (claude-code, codex, gemini, cursor, cline, "
            "windsurf, kiro, amp, opencode)"
        ),
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Daemon port (default: 8765)",
    )
    p.add_argument(
        "--scope",
        choices=["global", "project"],
        default="global",
        help="Write to global or project-scope config (default: global)",
    )
    p.add_argument(
        "--project-directory",
        metavar="PATH",
        default=None,
        help="Project directory (required when --scope project)",
    )
    p.set_defaults(func=cmd_install_mcp)
