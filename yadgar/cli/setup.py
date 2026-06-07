"""setup subcommand — first-run setup."""

import yadgar.paths as _paths


def cmd_setup(args):
    """First-run setup: check Docker, create config dirs, generate credentials,
    print MCP snippet + secrets.env template."""
    import json
    import secrets as _secrets

    from yadgar import __version__
    from yadgar.daemon import YadgarDaemon

    # ── Docker check ──
    print("Checking Docker...", end="  ", flush=True)
    check = YadgarDaemon.check_docker()
    if check["ok"]:
        print(f"✓ Docker {check.get('version', 'available')}")
    else:
        print(f"✗ {check['reason']}")
        print("  Install Docker Desktop or Docker Engine and re-run setup.")
        # Don't exit — let the rest of setup complete so config is written

    # Create XDG dirs
    _paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _paths.STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Write default config.yaml if not present
    from yadgar.config_yaml import cmd_config_init, get_config_path

    config_path = get_config_path()
    if not config_path.exists():
        import types

        _fake = types.SimpleNamespace(force=False)
        cmd_config_init(_fake)
        print(f"Config written: {config_path}")
    else:
        print(f"Config:         {config_path}")

    # ── Credential bootstrap (v5.0) ──
    # Generate strong defaults; operator copies template to secrets.env.
    secrets_path = _paths.SECRETS_ENV_PATH
    if secrets_path.exists():
        print(f"Secrets:        {secrets_path} (exists — keeping)")
    else:
        token = _secrets.token_urlsafe(32)
        db_pass = _secrets.token_urlsafe(24)
        rw_pass = _secrets.token_urlsafe(24)
        ro_pass = _secrets.token_urlsafe(24)
        secrets_path.write_text(
            "# Yadgar v5 secrets — do NOT commit. chmod 600 enforced below.\n"
            "# For HTTP/Docker mode the daemon reads these via EnvironmentFile.\n"
            "\n"
            "# MCP bearer token (required when YADGAR_REQUIRE_AUTH=1, the v5 default)\n"
            f"YADGAR_MCP_AUTH_TOKEN={token}\n"
            "\n"
            "# SurrealDB root (required by backend container)\n"
            "SURREAL_USER=root\n"
            f"SURREAL_PASS={db_pass}\n"
            "\n"
            "# Three-tier DB users (optional; backend provisions on first start)\n"
            "YADGAR_RW_USER=yadgar\n"
            f"YADGAR_RW_PASS={rw_pass}\n"
            "YADGAR_RO_USER=viewer\n"
            f"YADGAR_RO_PASS={ro_pass}\n"
        )
        try:
            secrets_path.chmod(0o600)
        except OSError:
            pass
        print(f"Secrets written: {secrets_path} (chmod 600)")

    print()
    print("=== Yadgar v" + __version__ + " — setup complete ===")
    print()

    if check["ok"]:
        # Docker-mode MCP config (streamable-http). Token resolved at
        # daemon-configure-mcp time so this snippet is illustrative.
        mcp_config = {
            "mcpServers": {
                "yadgar": {
                    "type": "streamable-http",
                    "url": "http://localhost:8765/mcp",
                    "headers": {"Authorization": "Bearer ${YADGAR_MCP_AUTH_TOKEN}"},
                }
            }
        }
        print("Next steps:")
        print(f"  1. set -a && . {secrets_path} && set +a")
        print("  2. yadgar daemon start")
        print("  3. yadgar daemon configure-mcp   # writes ~/.claude.json with bearer header")
        print("  4. Restart Claude Code.")
        print()
        print("Or merge manually into ~/.claude.json:")
        print()
        print(json.dumps(mcp_config, indent=2))
    else:
        # Fallback: stdio mode (no Docker). Auth middleware bypassed.
        mcp_config = {
            "mcpServers": {
                "yadgar": {
                    "command": "yadgar",
                    "args": [],
                    "env": {},
                }
            }
        }
        print("Docker unavailable — using stdio mode (one process per Claude session).")
        print("Stdio bypasses the HTTP auth middleware so no token is needed.")
        print()
        print("Add to ~/.claude.json (merge with existing mcpServers):")
        print()
        print(json.dumps(mcp_config, indent=2))
        print()
        print("Restart Claude Code — Yadgar tools appear automatically.")


def register(subparsers):
    p = subparsers.add_parser("setup", help="First-run setup: create config and print MCP snippet")
    p.set_defaults(func=cmd_setup)
