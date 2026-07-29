"""MCP-registration generator — Car 1.

Serializes a per-client MCP server entry and merges it atomically into the
client's config file, preserving all foreign MCP servers and unrelated keys.

Five entry schemas cover every client in the registry (design §2.2):

  STREAMABLE_HTTP_TYPE  Claude Code, Cursor, Windsurf, Kiro, Amp
  OPENCODE_REMOTE       OpenCode
  GEMINI_HTTPURL        Gemini (``httpUrl`` key — highest precedence)
  CLINE_STREAMABLEHTTP  Cline (explicit ``streamableHttp`` type spelling)
  CODEX_TOML            Codex (TOML ``[mcp_servers.yadgar]`` table)

Auth (D5): emit ``${YADGAR_MCP_AUTH_TOKEN}`` env-ref where the descriptor says
``BEARER_ENVREF``; literal token where it says ``BEARER_LITERAL``.

Claude Code env-ref probe (D5 TODO):  CC's ``~/.claude.json`` currently stores a
**literal** bearer token (observed state: ``"Bearer <token>"``). Whether CC
expands ``${YADGAR_MCP_AUTH_TOKEN}`` in ``headers.Authorization`` at session
load is not clearly documented; primary-source verification was attempted but
inconclusive. Decision: keep CC on ``BEARER_LITERAL`` (matching registry.py +
observed ~/.claude.json) and leave this note. Flip to ``BEARER_ENVREF`` once
expansion behavior is confirmed from primary docs.

Absorbs ``configure_mcp``'s body (daemon.py:402) — that method now delegates
here. Return shape preserved: ``{"updated": str, "old": dict, "new": dict}``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from yadgar._shared import paths as _paths
from yadgar._shared.observability.observe import observe
from yadgar.core.install.auth_token import parse_secrets_env_token, resolve_auth_token
from yadgar.core.install.clients.descriptor import (
    ClientDescriptor,
    McpAuth,
    McpEntrySchema,
    McpFormat,
)
from yadgar.core.install.clients.merge import _load_json, merge_json, merge_toml

# Public constant — the server-key written under every client's root.
SERVER_KEY = "yadgar"

# The env-var name that carries the bearer token (D5).
_TOKEN_ENV_VAR = "YADGAR_MCP_AUTH_TOKEN"

# The env-ref literal emitted for BEARER_ENVREF clients.
_TOKEN_ENVREF = f"${{{_TOKEN_ENV_VAR}}}"

# (The secrets.env line prefix moved with the parser to core.install.auth_token,
# where it is exported as TOKEN_ENV_LINE_PREFIX.)


# ── Token resolution (2026-07-28 fresh-VM QA fix) ────────────────────────────
#
# 2026-07-29: the implementation MOVED to ``core.install.auth_token`` — it was
# being re-typed in three places (here, ``cli/seed.py``, and
# ``runtime_config_client``, whose env-only copy was the bug that made
# ``--no-code-graph`` a silent no-op). These are aliases, not wrappers, so
# ``mcp_register.resolve_mcp_auth_token is auth_token.resolve_auth_token``
# holds and there is exactly ONE resolver to audit. The names stay exported
# here for the existing importers (``cli/install.py``, ``cli/setup.py``,
# ``yadgar/tests/clients/test_mcp_register.py``).

#: Alias — see :func:`yadgar.core.install.auth_token.parse_secrets_env_token`.
_parse_secrets_env_token = parse_secrets_env_token

#: Alias — see :func:`yadgar.core.install.auth_token.resolve_auth_token`.
resolve_mcp_auth_token = resolve_auth_token


# ── Serializers (one per McpEntrySchema variant) ─────────────────────────────


@observe(tier="stage")
def _serialize_streamable_http_type(url: str, auth_header: str | None) -> dict[str, Any]:
    """Claude Code / Cursor / Windsurf / Kiro / Amp — ``type:"streamable-http"``."""
    entry: dict[str, Any] = {"type": "streamable-http", "url": url}
    if auth_header:
        entry["headers"] = {"Authorization": auth_header}
    return entry


@observe(tier="stage")
def _serialize_opencode_remote(url: str, auth_header: str | None) -> dict[str, Any]:
    """OpenCode — ``type:"remote"``."""
    entry: dict[str, Any] = {"type": "remote", "url": url}
    if auth_header:
        entry["headers"] = {"Authorization": auth_header}
    return entry


@observe(tier="stage")
def _serialize_gemini_httpurl(url: str, auth_header: str | None) -> dict[str, Any]:
    """Gemini — ``httpUrl`` key (highest precedence over ``url``/``command``).

    Gemini resolves: httpUrl > url > command. Emitting ``httpUrl`` ensures
    the remote entry is preferred over any locally installed server with the
    same key name.
    """
    entry: dict[str, Any] = {"httpUrl": url}
    if auth_header:
        entry["headers"] = {"Authorization": auth_header}
    return entry


@observe(tier="stage")
def _serialize_cline_streamablehttp(url: str, auth_header: str | None) -> dict[str, Any]:
    """Cline — explicit ``streamableHttp`` type (camelCase, not auto-detected).

    Cline requires the ``type`` field to be ``"streamableHttp"`` (exact
    camelCase); it does not auto-infer transport from the URL.
    """
    entry: dict[str, Any] = {"type": "streamableHttp", "url": url}
    if auth_header:
        entry["headers"] = {"Authorization": auth_header}
    return entry


@observe(tier="stage")
def _serialize_codex_toml(url: str, auth_header: str | None) -> dict[str, Any]:
    """Codex — TOML ``[mcp_servers.yadgar]`` table.

    The value dict is passed to ``merge_toml`` which writes it as a TOML
    table, preserving comments and other tables in ``~/.codex/config.toml``.
    Nested values (``headers`` dict) are rendered as inline TOML tables by
    tomlkit.

    NOTE: Codex's exact MCP config schema is derived from the #57/#59 survey;
    field names have not been verified against primary Codex CLI docs.
    """
    entry: dict[str, Any] = {"url": url}
    if auth_header:
        entry["headers"] = {"Authorization": auth_header}
    return entry


# ── Schema dispatch ──────────────────────────────────────────────────────────

_SERIALIZERS = {
    McpEntrySchema.STREAMABLE_HTTP_TYPE: _serialize_streamable_http_type,
    McpEntrySchema.OPENCODE_REMOTE: _serialize_opencode_remote,
    McpEntrySchema.GEMINI_HTTPURL: _serialize_gemini_httpurl,
    McpEntrySchema.CLINE_STREAMABLEHTTP: _serialize_cline_streamablehttp,
    McpEntrySchema.CODEX_TOML: _serialize_codex_toml,
}


@observe(tier="stage")
def _resolve_auth_header(descriptor: ClientDescriptor, token: str) -> str | None:
    """Return the ``Authorization`` header value for this client + token, or ``None``.

    D5: emit env-ref where the descriptor says BEARER_ENVREF; literal token
    where it says BEARER_LITERAL. If auth is NONE or the token is empty for
    a literal client, no header is emitted (the daemon allows no-auth mode).
    """
    if descriptor.mcp_auth is McpAuth.NONE:
        return None
    if descriptor.mcp_auth is McpAuth.BEARER_ENVREF:
        return f"Bearer {_TOKEN_ENVREF}"
    if descriptor.mcp_auth is McpAuth.BEARER_LITERAL:
        return f"Bearer {token}" if token else None
    # McpAuth.OAUTH — not yet implemented; fall through to no header.
    return None


@observe(tier="stage")
def build_entry(
    descriptor: ClientDescriptor,
    url: str,
    token: str = "",
) -> dict[str, Any]:
    """Serialize the yadgar MCP entry for *descriptor* as a plain Python dict.

    Args:
        descriptor: the client descriptor (drives schema + auth strategy).
        url: the daemon's MCP endpoint (e.g. ``http://127.0.0.1:8765/mcp``).
        token: the raw bearer token. Required when ``descriptor.mcp_auth`` is
            ``BEARER_LITERAL`` and non-empty; ignored for ``BEARER_ENVREF``
            (the env-ref literal is emitted regardless of *token*).

    Returns:
        A dict that can be passed directly to ``merge_json`` / ``merge_toml``.
    """
    auth_header = _resolve_auth_header(descriptor, token)
    serializer = _SERIALIZERS[descriptor.mcp_entry_schema]
    return serializer(url, auth_header)


@observe(tier="boundary")
def register_mcp(
    descriptor: ClientDescriptor,
    url: str,
    token: str = "",
    scope: str = "global",
    project_dir: Path | None = None,
) -> dict[str, Any]:
    """Merge the yadgar MCP entry into *descriptor*'s config file.

    Atomic, format-preserving, idempotent — re-running with the same inputs
    produces byte-identical output. Foreign MCP servers and unrelated keys
    are never touched.

    Args:
        descriptor: the client descriptor (config path, format, schema, auth).
        url: the daemon MCP endpoint URL.
        token: bearer token (raw). Used only when ``descriptor.mcp_auth`` is
            ``BEARER_LITERAL``. For ``BEARER_ENVREF`` clients the env-ref
            ``${YADGAR_MCP_AUTH_TOKEN}`` is written regardless.
        scope: ``"global"`` (default) or ``"project"``.
        project_dir: required when *scope* is ``"project"``.

    Returns:
        ``{"updated": str, "old": dict, "new": dict}`` — mirrors the legacy
        ``configure_mcp`` return contract so the CLI handler needs no changes.

    Raises:
        ValueError: if *scope* is ``"project"`` and *project_dir* is None, or
            if the resolved config path is None (client has no file at that
            scope).
    """
    if scope == "project":
        if project_dir is None:
            raise ValueError("project_dir required when scope='project'")
        config_path = descriptor.mcp_config_path.resolve_project(project_dir)
    else:
        config_path = descriptor.mcp_config_path.resolve_global()

    if config_path is None:
        raise ValueError(f"Client {descriptor.name!r} has no config path for scope={scope!r}")

    entry = build_entry(descriptor, url, token)

    # Capture the prior yadgar entry for the return value (mirrors configure_mcp).
    old: dict[str, Any] = {}
    if config_path.exists() and descriptor.mcp_format is McpFormat.JSON:
        existing = _load_json(config_path)
        # Walk the root_key path to find the existing entry.
        node: Any = existing
        for k in descriptor.mcp_root_key:
            if isinstance(node, dict):
                node = node.get(k, {})
            else:
                node = {}
                break
        old = node.get(SERVER_KEY, {}) if isinstance(node, dict) else {}

    if descriptor.mcp_format is McpFormat.JSON:
        merge_json(
            config_path,
            root_key=descriptor.mcp_root_key,
            entry_key=SERVER_KEY,
            value=entry,
        )
    else:  # McpFormat.TOML
        merge_toml(
            config_path,
            root_key=(*descriptor.mcp_root_key, SERVER_KEY),
            value=entry,
        )

    return {
        "updated": str(config_path),
        "old": old,
        "new": entry,
    }


@observe(tier="boundary")
def register_mcp_for_claude_code(port: int = 8765, dev: bool = False) -> dict[str, Any]:
    """Convenience wrapper — write the yadgar MCP entry for Claude Code.

    Replaces the ``configure_mcp`` body in ``daemon.py``; that method now
    delegates here. Token is resolved via :func:`resolve_mcp_auth_token`
    (env first, then ``secrets.env`` — 2026-07-28 fresh-VM QA fix; literal,
    per the CC descriptor's ``BEARER_LITERAL`` auth — D5 TODO: flip to envref
    once CC's ${...} expansion is confirmed).

    Args:
        port: daemon port (default 8765).
        dev: if True, uses the dev-profile port (9765).

    Returns:
        ``{"updated": str, "old": dict, "new": dict}``.
    """
    from yadgar.core.install.clients.registry import CLIENT_REGISTRY

    if dev:
        from yadgar.core.daemon import DEFAULT_DEV_PORT

        port = DEFAULT_DEV_PORT

    token = resolve_mcp_auth_token()
    if not token:
        # OD-1: loud-warn, non-fatal — matches setup.py's
        # _register_claude_code_mcp skip-with-message pattern. A headerless
        # entry will 401 against a daemon running with YADGAR_REQUIRE_AUTH=1.
        print(
            "Warning: no YADGAR_MCP_AUTH_TOKEN resolved (checked the "
            f"environment and {_paths.SECRETS_ENV_PATH}) — the yadgar MCP "
            "entry for Claude Code will be written WITHOUT an Authorization "
            "header and may 401 against a daemon running with "
            "YADGAR_REQUIRE_AUTH=1. Run `yadgar setup` to mint a token, or "
            "set YADGAR_MCP_AUTH_TOKEN and re-run.",
            file=sys.stderr,
        )
    url = f"http://127.0.0.1:{port}/mcp"
    descriptor = CLIENT_REGISTRY["claude-code"]
    return register_mcp(descriptor, url=url, token=token)
