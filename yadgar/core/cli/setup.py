"""setup subcommand — first-run setup."""

import yadgar._shared.paths as _paths

#: Line prefix used to locate the bearer token inside an existing secrets.env
#: (v5.49.3 format — see ``_render_secrets_env``).
_MCP_TOKEN_ENV_LINE_PREFIX = "YADGAR_MCP_AUTH_TOKEN="


def _render_secrets_env(token: str, db_pass: str, rw_pass: str, ro_pass: str) -> str:
    """Return the secrets.env file content as a string.

    v5.49.3: emits YADGAR_DB_USER / YADGAR_DB_PASS alias keys (hardcoded
    duplicate of YADGAR_RW_USER / YADGAR_RW_PASS) so that the storage layer
    does not KeyError when YADGAR_ALLOW_ROOT is not set.  systemd EnvironmentFile
    does not evaluate shell expressions, so we duplicate the value directly.
    """
    return (
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
        "\n"
        "# v5.49.3: DB credential aliases — fall back to RW user\n"
        "# Duplicated literally (not shell expansion) for systemd EnvironmentFile compat.\n"
        f"YADGAR_DB_USER=yadgar\n"
        f"YADGAR_DB_PASS={rw_pass}\n"
    )


# ── code_graph provisioning ──────────────────────────────────────────────────
#
# 2026-07-29: the implementation MOVED to ``core.install.code_graph_provision``.
# `yadgar setup` is not the installer most users run — the pipx/brew/nix path
# (``scripts/install/yadgar-setup.sh``) and the repo path (``make setup``) never
# invoke it, so they shipped machines with code_graph ON and no binary. Both now
# call ``yadgar code-graph install``, which needs the logic outside this module.
# These re-exports keep every existing importer (and its tests) working; the move
# itself is behavior-preserving.
from yadgar.core.install.code_graph_provision import (  # noqa: E402
    _CODE_GRAPH_KEY,
    _do_install_code_graph,
    _persist_code_graph_disable,
    _persist_code_graph_enable,
    _resolve_code_graph_action,
    provision_code_graph,
)

__all__ = [
    "_CODE_GRAPH_KEY",
    "_do_install_code_graph",
    "_persist_code_graph_disable",
    "_persist_code_graph_enable",
    "_resolve_code_graph_action",
    "cmd_setup",
    "register",
]


def _maybe_install_code_graph(args) -> None:
    """Provision code_graph so the store flag and the host binary always AGREE.

    Thin arg-shaped adapter over
    :func:`~yadgar.core.install.code_graph_provision.provision_code_graph` —
    ``cmd_setup`` holds an argparse namespace, the shared function takes a bool
    so the ``yadgar code-graph install`` entry point can call it directly.

    Best-effort caveat that is specific to THIS caller: the persist needs a
    running daemon, and ``yadgar setup`` normally runs BEFORE
    ``yadgar daemon start``, so the write frequently cannot land here. The shell
    installer places its equivalent step after unit-enablement precisely so the
    persist can succeed there.
    """
    provision_code_graph(opt_out=_resolve_code_graph_action(args) == "opt_out")


def _existing_secrets_token(secrets_path) -> str:
    """Best-effort parse of ``YADGAR_MCP_AUTH_TOKEN=`` from an existing secrets.env.

    Returns ``""`` if the file is missing, unreadable, or predates the token
    line (legacy secrets.env) — never raises. Setup must not crash over a
    malformed/legacy secrets file; an empty return just means MCP
    registration is skipped (see :func:`_register_claude_code_mcp`).

    Delegates the actual file parse to
    ``mcp_register._parse_secrets_env_token`` — the same routine
    ``resolve_mcp_auth_token()`` uses for the ``yadgar install`` /
    ``yadgar daemon configure-mcp`` write paths (2026-07-28 fresh-VM QA fix),
    so setup and install can't drift on the ``YADGAR_MCP_AUTH_TOKEN=`` line
    format. Behavior-preserving: this function is file-only (no env check) —
    callers only reach it once they've confirmed *secrets_path* exists.
    """
    from yadgar.core.install.clients.mcp_register import (  # noqa: PLC0415
        _parse_secrets_env_token,
    )

    return _parse_secrets_env_token(secrets_path)


def _register_claude_code_mcp(token: str, *, port: int = 8765) -> dict | None:
    """Write yadgar's MCP entry into Claude Code's ``~/.claude.json`` (ADR-0161, #37).

    Calls the SAME registration primitive as ``yadgar daemon configure-mcp`` /
    ``yadgar install --client claude-code --mcp``
    (``mcp_register.register_mcp``, for which ``register_mcp_for_claude_code``
    is a thin default-port/env-token wrapper) so a fresh ``yadgar setup`` run
    leaves Claude Code configured without a separate manual step.

    This is a pure local config-file merge — no daemon HTTP call — so it is
    safe to run here even before the daemon has ever been started. The only
    real dependency is the auth *token value*, which ``cmd_setup`` already
    holds (freshly generated, or read back from an existing secrets.env) and
    passes explicitly here rather than round-tripping through
    ``YADGAR_MCP_AUTH_TOKEN`` in the process environment.

    If *token* is empty (e.g. a legacy/hand-edited secrets.env with no token
    line) registration is SKIPPED rather than writing a headerless MCP entry
    — against the v5 default (``YADGAR_REQUIRE_AUTH=1``) a headerless entry
    would silently 401, which is worse than not writing at all.

    Returns the ``register_mcp`` result dict on success, ``None`` when
    skipped or failed — either way the caller's own printed JSON snippet
    remains the manual fallback, and setup itself never aborts over this.
    """
    if not token:
        print(
            "  MCP registration skipped — no bearer token available "
            f"(secrets.env has no {_MCP_TOKEN_ENV_LINE_PREFIX}line). "
            "Merge the JSON snippet below into ~/.claude.json manually once you "
            "have a token, or re-run `yadgar daemon configure-mcp`."
        )
        return None

    from yadgar.core.install.clients.mcp_register import register_mcp
    from yadgar.core.install.clients.registry import CLIENT_REGISTRY

    try:
        result = register_mcp(
            CLIENT_REGISTRY["claude-code"],
            url=f"http://127.0.0.1:{port}/mcp",
            token=token,
        )
    except Exception as exc:  # noqa: BLE001 — registration must never abort setup
        print(f"  ✗ MCP registration failed: {exc}")
        print(
            "  Merge the JSON snippet below into ~/.claude.json manually, or re-run "
            "`yadgar daemon configure-mcp` once resolved."
        )
        return None

    print(f"  ✓ MCP registered for Claude Code: {result['updated']}")
    return result


def cmd_setup(args):
    """First-run setup: check Docker, create config dirs, generate credentials,
    print MCP snippet + secrets.env template."""
    import json
    import secrets as _secrets

    from yadgar import __version__
    from yadgar.core.daemon import YadgarDaemon

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
    from yadgar._shared.config.config_yaml import cmd_config_init, get_config_path

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
        mcp_token = _existing_secrets_token(secrets_path)
    else:
        token = _secrets.token_urlsafe(32)
        db_pass = _secrets.token_urlsafe(24)
        rw_pass = _secrets.token_urlsafe(24)
        ro_pass = _secrets.token_urlsafe(24)
        secrets_path.write_text(_render_secrets_env(token, db_pass, rw_pass, ro_pass))
        try:
            secrets_path.chmod(0o600)
        except OSError:
            pass
        print(f"Secrets written: {secrets_path} (chmod 600)")
        mcp_token = token

    print()
    print("=== Yadgar v" + __version__ + " — setup complete ===")
    print()

    # code_graph provisioning — DEFAULT-ON and unattended (task:0082): installs the
    # host binary and enables the feature, or (with --no-code-graph / on a failed
    # install) turns BOTH off together. Never prompts, never reads stdin, never
    # aborts setup.
    _maybe_install_code_graph(args)

    # Streamable-HTTP MCP config (the only supported transport — stdio dropped in Phase 2b).
    # Token resolved at daemon-configure-mcp time; this snippet is illustrative.
    mcp_config = {
        "mcpServers": {
            "yadgar": {
                "type": "streamable-http",
                "url": "http://localhost:8765/mcp",
                "headers": {"Authorization": "Bearer ${YADGAR_MCP_AUTH_TOKEN}"},
            }
        }
    }

    # Actually WRITE the Claude Code MCP registration (ADR-0161, #37) instead of
    # only printing instructions — same registration primitive `yadgar daemon
    # configure-mcp` / `yadgar install --client claude-code --mcp` use. Pure
    # local file merge, so it runs here regardless of Docker/daemon state.
    mcp_registered = _register_claude_code_mcp(mcp_token) is not None

    if check["ok"]:
        print("Next steps:")
        print(f"  1. set -a && . {secrets_path} && set +a")
        print("  2. yadgar daemon start")
        if mcp_registered:
            print("  3. Restart Claude Code.")
        else:
            print("  3. yadgar daemon configure-mcp   # writes ~/.claude.json with bearer header")
            print("  4. Restart Claude Code.")
        print()
        print("Or merge manually into ~/.claude.json:")
        print()
        print(json.dumps(mcp_config, indent=2))
    else:
        # Docker unavailable — streamable-HTTP is still required (stdio is no longer supported).
        # See MIGRATION_NOTES.md for the client config change.
        print("Docker unavailable — Yadgar requires Docker for the streamable-HTTP deployment.")
        print("Install Docker Desktop or Docker Engine, then re-run `yadgar setup`.")
        print()
        if mcp_registered:
            print("Claude Code's MCP client config has already been written (see above).")
            print("Once Docker is available and the daemon is started, it will connect.")
            print("Other MCP clients can use the same config:")
        else:
            print("Once Docker is available, configure your MCP client with:")
        print()
        print(json.dumps(mcp_config, indent=2))
        print()
        print("See MIGRATION_NOTES.md for migration from stdio-based configs.")


def register(subparsers):
    p = subparsers.add_parser("setup", help="First-run setup: create config and print MCP snippet")
    # task:0082 — the surface is a single OPT-OUT. `--code-graph` was REMOVED: once
    # code_graph.enabled defaulted to True (ADR-0163) an opt-IN flag for a default-on
    # feature was a no-op, and its existence pushed scripted installs onto
    # `--no-code-graph` purely to dodge the (now deleted) interactive prompt.
    p.add_argument(
        "--no-code-graph",
        action="store_true",
        dest="no_code_graph",
        default=False,
        help=(
            "Opt out of code_graph entirely: skip the codebase-memory-mcp host-binary "
            "install AND persist code_graph.enabled=false in the runtime-config store "
            "(ADR-0162/0163), so the flag and the binary stay coherent. Without this flag "
            "setup installs the binary and enables the feature — unattended, no prompt. "
            "Opt a single repo out instead with "
            '`config_set("code_graph.enabled", false, scope="project", directory=<repo>)`.'
        ),
    )
    p.set_defaults(func=cmd_setup)
