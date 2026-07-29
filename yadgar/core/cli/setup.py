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


#: Runtime-config store key holding the code_graph enable flag (ADR-0163).
_CODE_GRAPH_KEY = "code_graph.enabled"


def _resolve_code_graph_action(args) -> str:
    """Decide the code_graph setup action. Pure — no TTY, no env, no prompt.

    Returns one of:
      * ``"install"`` — the DEFAULT: install the host binary AND persist
        ``code_graph.enabled=true``.
      * ``"opt_out"`` — ``--no-code-graph``: install nothing AND persist
        ``code_graph.enabled=false``.

    task:0082 — the decision tree used to branch on ``sys.stdin.isatty()``, an
    interactive ``[y/N]`` prompt, and a ``CODE_GRAPH_ENABLED`` env trigger. That
    made the ONLY scriptable path ``--no-code-graph``, which skipped the binary
    while ``code_graph.enabled`` still defaulted to True (ADR-0163) — an install
    whose runtime flag disagreed with its own filesystem. code_graph is on by
    default, so the DEFAULT install now provisions it; the single opt-out turns
    BOTH halves off together. No branch reads stdin, so setup can never block on
    a closed/absent stdin.
    """
    return "opt_out" if getattr(args, "no_code_graph", False) else "install"


def _do_install_code_graph() -> bool:
    """Install the codebase-memory-mcp binary host-side. Returns success.

    ``skip_if_exists=True``: a setup re-run with the binary already present must
    not need the network (offline re-provisioning, nix-provided binary). Never
    raises — a genuinely impossible install (offline, unsupported platform) is
    reported and the caller degrades by disabling the feature.
    """
    from yadgar.core.install.codebase_memory_mcp import (
        BINARY_NAME,
        VERSION,
        install_codebase_memory_mcp,
    )

    print(f"Installing codebase-memory-mcp {VERSION}...", end="  ", flush=True)
    try:
        binary_path = install_codebase_memory_mcp(skip_if_exists=True)
    except Exception as exc:  # noqa: BLE001 — a failed optional install never aborts setup
        print(f"✗ {exc}")
        print(
            "  The code_graph binary could not be installed (offline, or an "
            "unsupported platform). Setup CONTINUES — code_graph will be turned "
            "off so the runtime flag matches the missing binary.\n"
            "  Re-run `yadgar setup` once the problem is resolved to install it "
            "and turn code_graph back on."
        )
        return False

    print(f"✓ {binary_path}")
    print(f"  Binary: {binary_path}\n  Ensure ~/.local/bin is on PATH to use '{BINARY_NAME}'.")
    return True


def _persist_code_graph_enable() -> bool:
    """Persist ``code_graph.enabled=true`` in the runtime config store (ADR-0163).

    Uses the host WRITE client (``runtime_config_client.set``) which is NOT
    fail-open: daemon-down / non-2xx returns False. A failure here is BENIGN on a
    fresh machine — ``code_graph.enabled`` already defaults to True with no row —
    so the message says so rather than alarming the user; it only matters when a
    previous run (or a manual ``config_set``) left an explicit ``false`` behind.
    """
    from yadgar.core import runtime_config_client

    if runtime_config_client.set(_CODE_GRAPH_KEY, True, scope="global"):
        print("  code_graph enabled globally (runtime_config store).")
        return True
    print(
        "  code_graph binary installed — the daemon is not reachable, so the enable "
        "was NOT persisted. code_graph.enabled already defaults to true, so no action "
        "is needed unless you previously disabled it; in that case run the MCP tool "
        f'`config_set("{_CODE_GRAPH_KEY}", true, scope="global")` or re-run '
        "`yadgar setup` once `yadgar daemon start` is up."
    )
    return False


def _persist_code_graph_disable(why: str) -> bool:
    """Persist ``code_graph.enabled=false`` so the store matches an absent binary.

    This is the half the old flow was missing: ``--no-code-graph`` skipped the
    binary but left ``code_graph.enabled`` at its True default, producing a
    machine where the feature was ON with nothing to run. A failed write IS
    consequential here (the default is True), so the message spells out the one
    manual step.
    """
    from yadgar.core import runtime_config_client

    if runtime_config_client.set(_CODE_GRAPH_KEY, False, scope="global"):
        print(f"  code_graph disabled globally (runtime_config store) — {why}.")
        return True
    print(
        f"  code_graph was NOT disabled ({why}) — the daemon is not reachable and the "
        "flag defaults to true, so the feature would stay ON with no binary installed. "
        f'Run the MCP tool `config_set("{_CODE_GRAPH_KEY}", false, scope="global")` '
        "or re-run `yadgar setup --no-code-graph` once `yadgar daemon start` is up."
    )
    return False


def _maybe_install_code_graph(args) -> None:
    """Provision code_graph so the store flag and the host binary always AGREE.

    code_graph is ON by default (ADR-0162/0163: no row → ``is_enabled`` True), so
    setup installs the host binary by DEFAULT — unattended, with no prompt and no
    stdin read, which is what makes a scripted/QA install work without flags
    (task:0082).

    The two outcomes are coherent by construction:

    ==========================  ===============  =========================
    invocation                  host binary      ``code_graph.enabled``
    ==========================  ===============  =========================
    ``yadgar setup``            installed        ``true``
    ``… --no-code-graph``       not installed    ``false``
    install failed (offline)    not installed    ``false``
    ==========================  ===============  =========================

    Best-effort caveat: the persist needs a running daemon, and ``yadgar setup``
    normally runs BEFORE ``yadgar daemon start``. When the write cannot land, the
    printed remediation names the one manual step. On a virgin machine the True
    default already matches a successful install, so only the disable paths carry
    a real residual risk — and they say so.
    """
    action = _resolve_code_graph_action(args)

    if action == "opt_out":
        _persist_code_graph_disable("--no-code-graph")
        return

    if _do_install_code_graph():
        _persist_code_graph_enable()
    else:
        _persist_code_graph_disable("binary install failed")


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
