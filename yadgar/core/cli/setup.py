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


#: Env values that count as the CODE_GRAPH_ENABLED install trigger being on.
_CODE_GRAPH_ENV_TRUE = ("1", "true", "yes")

#: Prompt shown when neither flag nor env is set and stdin is a TTY.
#: code_graph.enabled now defaults to True in the runtime-config store
#: (ADR-0163) — this prompt is about installing the HOST BINARY, not "enabling"
#: an off-by-default feature.
_CODE_GRAPH_PROMPT = "Install code_graph multi-language code indexing? [y/N]: "

#: Answers counted as "yes" to the interactive prompt.
_CODE_GRAPH_YES = ("y", "yes")

# Module constant — a bare inline ``except (EOFError, KeyboardInterrupt):`` gets its
# parens stripped by ruff-format, and on py3.14 the paren-less form silently changes
# semantics (PORTABILITY TRAP). Bind the tuple to a name so the except clause never
# carries an inline tuple literal.
_PROMPT_ABORT_ERRORS = (EOFError, KeyboardInterrupt)


def _prompt_code_graph() -> bool:
    """Ask the interactive yes/no question; default No. Never raises (EOF → No)."""
    try:
        return input(_CODE_GRAPH_PROMPT).strip().lower() in _CODE_GRAPH_YES
    except _PROMPT_ABORT_ERRORS:
        return False


def _resolve_code_graph_action(args, *, isatty: bool, env_enabled: bool, prompt_fn) -> str:
    """Decide the code_graph setup action (pure — injectable TTY / env / prompt).

    Returns one of:
      * ``"skip"``            — do nothing (no install, no persist).
      * ``"install_only"``    — install the host binary, do NOT persist the enable.
      * ``"install_persist"`` — install AND persist ``code_graph.enabled=true``.

    Decision tree (ADR-0163, Car G5):
      1. ``--no-code-graph`` → skip (wins over everything).
      2. ``--code-graph`` OR an interactive yes → install_persist.
      3. ``CODE_GRAPH_ENABLED`` env → install_only (INSTALL trigger, NOT a runtime
         enable — the store row is the runtime flag).
      4. No flag, no env, TTY → prompt; yes → install_persist, no → skip.
      5. No flag, no env, NO TTY → skip WITHOUT prompting (the CI/headless no-hang
         guarantee).
    """
    if getattr(args, "no_code_graph", False):
        return "skip"
    if getattr(args, "code_graph", False):
        return "install_persist"
    if env_enabled:
        return "install_only"
    if isatty and prompt_fn():
        return "install_persist"
    return "skip"


def _do_install_code_graph() -> bool:
    """Install the codebase-memory-mcp binary host-side. Returns success."""
    from yadgar.core.install.codebase_memory_mcp import (
        BINARY_NAME,
        VERSION,
        install_codebase_memory_mcp,
    )

    print(f"Installing codebase-memory-mcp {VERSION}...", end="  ", flush=True)
    try:
        binary_path = install_codebase_memory_mcp(skip_if_exists=False)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ {exc}")
        print(
            "  codebase-memory-mcp install failed.  "
            "Retry: CODE_GRAPH_ENABLED=1 yadgar setup --code-graph"
        )
        return False

    print(f"✓ {binary_path}")
    print(f"  Binary: {binary_path}\n  Ensure ~/.local/bin is on PATH to use '{BINARY_NAME}'.")
    return True


def _persist_code_graph_enable() -> None:
    """Persist ``code_graph.enabled=true`` in the runtime config store (ADR-0163).

    Uses the host WRITE client (``runtime_config_client.set``, Car G5) which is NOT
    fail-open: a daemon-down / non-2xx returns False. On success we confirm the
    enable; on failure we tell the user the ONE manual step to enable once the
    daemon is up (install already happened — only the persist is deferred).
    """
    from yadgar.core import runtime_config_client

    if runtime_config_client.set("code_graph.enabled", True, scope="global"):
        print("  code_graph enabled globally (runtime_config store).")
    else:
        print(
            "  installed — the daemon is not reachable, so the enable was NOT persisted. "
            "Once `yadgar daemon start` is up, enable it via the MCP tool "
            '`config_set("code_graph.enabled", true, scope="global")` '
            "or re-run `yadgar setup --code-graph`."
        )


def _maybe_install_code_graph(args) -> None:
    """Install codebase-memory-mcp + optionally persist the enable (ADR-0163, Car G5).

    Controlled by (see :func:`_resolve_code_graph_action` for the full tree):
      - ``--no-code-graph`` → skip entirely.
      - ``--code-graph`` flag OR an interactive ``[y/N]`` yes → install + persist
        ``code_graph.enabled=true`` in the runtime config store.
      - ``CODE_GRAPH_ENABLED`` env → install ONLY (an INSTALL-time trigger for the
        host binary — NOT the runtime enable, which is the store row).
      - non-interactive with no flag / no env → skip, no prompt, no hang (CI-safe).

    Pilot-gate satisfied 2026-07-27 (ADR-0162/0163): ``code_graph.enabled`` now
    defaults to True (opt-out) in the runtime-config store. This flow controls
    ONLY the HOST BINARY install step (never auto-installed, never enters the
    docker image) + an optional explicit persisted ``true`` row (redundant with
    the default, but explicit). Disable per-repo or globally any time via
    ``config_set("code_graph.enabled", false, scope=...)`` or ``--no-code-graph``.
    """
    import os as _os
    import sys as _sys

    env_enabled = _os.environ.get("CODE_GRAPH_ENABLED", "0").lower() in _CODE_GRAPH_ENV_TRUE
    isatty = bool(getattr(_sys.stdin, "isatty", lambda: False)())
    action = _resolve_code_graph_action(
        args, isatty=isatty, env_enabled=env_enabled, prompt_fn=_prompt_code_graph
    )
    if action == "skip":
        return

    if not _do_install_code_graph():
        return  # install failed — nothing to persist

    if action == "install_persist":
        _persist_code_graph_enable()


def _existing_secrets_token(secrets_path) -> str:
    """Best-effort parse of ``YADGAR_MCP_AUTH_TOKEN=`` from an existing secrets.env.

    Returns ``""`` if the file is missing, unreadable, or predates the token
    line (legacy secrets.env) — never raises. Setup must not crash over a
    malformed/legacy secrets file; an empty return just means MCP
    registration is skipped (see :func:`_register_claude_code_mcp`).
    """
    try:
        text = secrets_path.read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith(_MCP_TOKEN_ENV_LINE_PREFIX):
            return line[len(_MCP_TOKEN_ENV_LINE_PREFIX) :].strip()
    return ""


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

    # code_graph HOST BINARY install (opt-in step); runtime code_graph.enabled
    # now defaults to True — ADR-0162 Car A / ADR-0163 Car G6 flip.
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
    # --code-graph and --no-code-graph are mutually exclusive; with NEITHER set (and
    # no CODE_GRAPH_ENABLED env) an interactive TTY is prompted, non-TTY skips.
    _cg = p.add_mutually_exclusive_group()
    _cg.add_argument(
        "--code-graph",
        action="store_true",
        dest="code_graph",
        default=False,
        help=(
            "Install codebase-memory-mcp binary HOST-SIDE + persist code_graph.enabled=true "
            "in the runtime-config store when the daemon is reachable (code_graph feature, "
            "ADR-0162/0163). CODE_GRAPH_ENABLED=1 env installs the binary WITHOUT persisting "
            "the runtime enable (INSTALL trigger only). code_graph.enabled now defaults to "
            "True (opt-out) regardless of this flag — this only controls the binary install."
        ),
    )
    _cg.add_argument(
        "--no-code-graph",
        action="store_true",
        dest="no_code_graph",
        default=False,
        help=(
            "Skip the code_graph install step entirely (suppresses the interactive prompt). "
            "code_graph.enabled still defaults to True in the store — to fully disable, also "
            'run `config_set("code_graph.enabled", false, scope="global")` (or per-repo).'
        ),
    )
    p.set_defaults(func=cmd_setup)
