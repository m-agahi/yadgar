"""``yadgar install`` CLI subcommand — Car 3.

Unified entry point that installs (or dry-run prints) MCP registration +
rules for one or more agentic clients.  Ties together the MCP-registration
generator (Car 1, ``install_mcp``) and the rules-file generator (Car 2,
``rules``) behind a single idempotent command.

Usage::

    yadgar install --client claude-code
    yadgar install --client opencode --mcp
    yadgar install --client cursor --rules --scope project
    yadgar install --client gemini --print
    yadgar install --client claude-code --print --mcp --rules
    yadgar install --auto-detect
    yadgar install --auto-detect --print

``--print`` / dry-run mode (contract for nix #67)
--------------------------------------------------
When ``--print`` is given, no files are written.  The command outputs a
JSON document to stdout mapping each surface to a ``{path, content}``
fragment::

    {
      "client": "claude-code",
      "mcp": {"path": "/home/…/.claude.json", "content": "…"},
      "rules": {"path": "/home/…/AGENTS.md", "content": "…"},
      "dry_run": true
    }

Key guarantees:
  * **Deterministic** — same inputs → byte-identical output regardless of
    local on-disk state.  The fragment is rendered from an empty base, not
    merged into any existing file.
  * **No secrets in stdout** — even for ``claude-code`` (which uses
    ``BEARER_LITERAL`` on the write path), ``--print`` emits the env-ref
    ``${YADGAR_MCP_AUTH_TOKEN}`` rather than the raw token.  Nix
    home-manager activation writes the file; the actual token is resolved
    from the environment at daemon start.

Back-compat aliases
-------------------
``yadgar daemon configure-mcp`` delegates to
``install --client claude-code --mcp`` via
``daemon.configure_mcp → mcp_register.register_mcp_for_claude_code``
(Car 1; that delegation is already in place — confirmed no changes needed here).

``yadgar-setup.sh`` step 9 (``_step_install_rules``) is rerouted in-script
to ``yadgar install --client claude-code --rules`` by Car 3 (see
``scripts/install/yadgar-setup.sh`` changes).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _get_version() -> str:
    """Return the running yadgar version string."""
    try:
        from yadgar import __version__  # noqa: PLC0415

        return __version__
    except Exception:
        return "unknown"


def cmd_install(args) -> None:
    """Dispatch to install_client or install_auto_detect based on args."""
    from yadgar.core.install.clients.install import (  # noqa: PLC0415
        install_auto_detect,
        install_client,
    )
    from yadgar.core.install.clients.registry import CLIENT_REGISTRY  # noqa: PLC0415

    version = _get_version()
    url_base = getattr(args, "port", None) or 8765
    url = f"http://127.0.0.1:{int(url_base)}/mcp"
    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "").strip()
    scope = getattr(args, "scope", "global") or "global"
    project_dir_raw = getattr(args, "project_directory", None)
    project_dir = Path(project_dir_raw).resolve() if project_dir_raw else None
    dry_run = bool(getattr(args, "print", False))

    # Determine which surfaces to install.
    want_mcp = bool(getattr(args, "mcp", False))
    want_rules = bool(getattr(args, "rules", False))
    # Default: all surfaces when neither flag given explicitly.
    if not want_mcp and not want_rules:
        want_mcp = True
        want_rules = True

    auto_detect = bool(getattr(args, "auto_detect", False))
    client_name = getattr(args, "client", None)

    if auto_detect:
        from yadgar.core.install.clients.install import InstallOptions  # noqa: PLC0415

        auto_opts = InstallOptions(
            url=url,
            token=token,
            version=version,
            mcp=want_mcp,
            rules=want_rules,
            scope=scope,
            project_dir=project_dir,
            dry_run=dry_run,
        )
        results = install_auto_detect(opts=auto_opts)
        if dry_run:
            print(json.dumps(results, indent=2))
        else:
            for r in results:
                _print_result(r)
        return

    if not client_name:
        known = ", ".join(sorted(CLIENT_REGISTRY))
        print(
            f"Error: --client NAME is required (or use --auto-detect).\nKnown clients: {known}",
            file=sys.stderr,
        )
        sys.exit(1)

    from yadgar.core.install.clients.install import InstallOptions  # noqa: PLC0415

    install_opts = InstallOptions(
        url=url,
        token=token,
        version=version,
        mcp=want_mcp,
        rules=want_rules,
        scope=scope,
        project_dir=project_dir,
        dry_run=dry_run,
    )

    try:
        result = install_client(client_name, opts=install_opts)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(json.dumps(result, indent=2))
    else:
        _print_result(result)


def _print_result(result: dict) -> None:
    """Print a human-readable install result to stdout."""
    client = result.get("client", "?")
    print(f"Installed for client: {client}")
    mcp = result.get("mcp")
    if mcp and isinstance(mcp, dict):
        path = mcp.get("path", "?")
        print(f"  MCP config:  {path}")
    rules = result.get("rules")
    if rules and isinstance(rules, dict):
        path = rules.get("path", "?")
        print(f"  Rules file:  {path}")


def register(subparsers) -> None:
    """Register ``yadgar install`` with the argparse root subparsers."""
    p = subparsers.add_parser(
        "install",
        help=(
            "Install yadgar MCP registration + rules for an agentic client "
            "(use --print for declarative/nix output)"
        ),
    )

    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--client",
        metavar="NAME",
        default=None,
        help=(
            "Client to install for (claude-code, codex, gemini, cursor, "
            "cline, windsurf, kiro, amp, opencode)"
        ),
    )
    group.add_argument(
        "--auto-detect",
        dest="auto_detect",
        action="store_true",
        default=False,
        help="Auto-detect installed clients and install for all found",
    )

    p.add_argument(
        "--mcp",
        action="store_true",
        default=False,
        help="Install MCP registration (writes client's config file)",
    )
    p.add_argument(
        "--rules",
        action="store_true",
        default=False,
        help="Install rules file (writes AGENTS.md / CLAUDE.md / client-native)",
    )
    p.add_argument(
        "--print",
        dest="print",
        action="store_true",
        default=False,
        help=(
            "Dry-run: emit JSON fragments to stdout without writing any files. "
            "Deterministic; no secrets in output. Designed for nix home-manager "
            "activation (#67)."
        ),
    )
    p.add_argument(
        "--port",
        type=int,
        default=8765,
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

    p.set_defaults(func=cmd_install)
