"""Unified install orchestrator — Car 3.

Ties the MCP-registration generator (Car 1) and the rules-file generator
(Car 2) together behind a single call site.  The ``yadgar install`` CLI
(``cli/install.py``) is the thin command-line wrapper around these functions.

Public API
----------

``install_client(name, url, token, version, ...)``
    Resolve the ``ClientDescriptor`` from *registry*, then call
    ``register_mcp`` (Car 1) and/or ``write_rules`` (Car 2) according to the
    *mcp* / *rules* flags.  When *dry_run=True* no files are written; instead
    the rendered fragments are returned in the result dict under ``"mcp"`` and
    ``"rules"`` keys (path → content).

    The ``--print`` contract (#67 nix provisioning):

    * Same inputs → byte-identical fragment output regardless of on-disk state.
    * Fragments are rendered against an **empty base** (no merge into the
      user's existing config) so the output is machine-consumable by nix
      home-manager activation.
    * **No literal bearer token in dry_run output.** Even for BEARER_LITERAL
      clients (Claude Code) the MCP fragment emits the env-ref
      ``${YADGAR_MCP_AUTH_TOKEN}``.  The literal token stays on the write path
      only (where CC's config file already holds it today).  This prevents
      leaking secrets into the nix store or stdout.

``install_auto_detect(url, token, version, ...)``
    Run ``detect_installed_clients`` and call ``install_client`` for each
    detected client.  Returns a list of per-client result dicts.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from yadgar._shared.observability.observe import observe

# The env-ref literal emitted in dry_run mode for ALL clients (even BEARER_LITERAL).
_TOKEN_ENVREF = "${YADGAR_MCP_AUTH_TOKEN}"


@dataclasses.dataclass(frozen=True)
class InstallOptions:
    """Grouped install options — avoids PLR0913 (too many arguments) on public callsites.

    Passing options as a single ``InstallOptions`` value keeps the public
    ``install_client`` / ``install_auto_detect`` signatures under the 8-argument cap.

    Attributes:
        url:         daemon MCP endpoint URL (e.g. ``http://127.0.0.1:8765/mcp``).
        token:       raw bearer token.  Write-path only; dry_run always emits env-ref.
        version:     ``yadgar.__version__`` for rules template rendering.
        mcp:         install MCP registration surface.
        rules:       install rules file surface.
        scope:       ``"global"`` (default) or ``"project"``.
        project_dir: required when scope=``"project"``.
        dry_run:     when True render fragments but write no files (``--print`` mode).
    """

    url: str = "http://127.0.0.1:8765/mcp"
    token: str = ""
    version: str = ""
    mcp: bool = True
    rules: bool = True
    scope: str = "global"
    project_dir: Path | None = None
    dry_run: bool = False


# ── Dry-run fragment renderers (no writes) ────────────────────────────────────


@observe(tier="stage")
def _render_mcp_fragment(
    descriptor: Any,
    url: str,
    version: str,
    scope: str = "global",
    project_dir: Path | None = None,
) -> dict[str, Any]:
    """Render the MCP entry fragment for *descriptor* without writing.

    Always emits env-ref auth (``${YADGAR_MCP_AUTH_TOKEN}``) even for
    BEARER_LITERAL clients — no literal secret in dry_run output.

    Returns:
        ``{"path": str, "content": str}`` where *content* is the JSON fragment
        for the yadgar entry (not the full config file — just the entry dict,
        JSON-serialized for readability).
    """
    from yadgar.core.install.clients.descriptor import McpAuth  # noqa: PLC0415
    from yadgar.core.install.clients.mcp_register import (  # noqa: PLC0415
        _SERIALIZERS,
    )

    # Resolve the target path (for the "path" key in the fragment).
    if scope == "project" and project_dir is not None:
        config_path = descriptor.mcp_config_path.resolve_project(project_dir)
    else:
        config_path = descriptor.mcp_config_path.resolve_global()

    # Compute auth header — force env-ref for ALL clients in dry_run mode.
    if descriptor.mcp_auth is McpAuth.NONE:
        auth_header = None
    else:
        auth_header = f"Bearer {_TOKEN_ENVREF}"

    serializer = _SERIALIZERS[descriptor.mcp_entry_schema]
    entry = serializer(url, auth_header)

    return {
        "path": str(config_path) if config_path else None,
        "content": json.dumps(entry, indent=2),
    }


@observe(tier="stage")
def _render_rules_fragment(
    descriptor: Any,
    version: str,
    scope: str = "global",
    project_dir: Path | None = None,
) -> dict[str, Any]:
    """Render the rules body fragment for *descriptor* without writing.

    Returns:
        ``{"path": str, "content": str}`` where *content* is the rendered
        rules body (same text that ``write_rules`` would place in the section).
    """
    from yadgar.core.install.clients.rules_render import render_body  # noqa: PLC0415

    if scope == "project" and project_dir is not None:
        rules_path = descriptor.rules_path.resolve_project(project_dir)
    else:
        rules_path = descriptor.rules_path.resolve_global()

    body = render_body(descriptor, version)

    return {
        "path": str(rules_path) if rules_path else None,
        "content": body,
    }


# ── Public install entry point ────────────────────────────────────────────────


@observe(tier="boundary")
def install_client(
    name: str,
    opts: InstallOptions | None = None,
    *,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Install (or dry-run print) MCP registration + rules for a named client.

    Args:
        name:     client name (must be a key in *registry*).
        opts:     an ``InstallOptions`` instance grouping all install parameters.
                  When *opts* is None a default ``InstallOptions()`` is used.
        registry: client registry dict; defaults to ``CLIENT_REGISTRY``.

    Returns:
        Dict with keys:
          ``"client"``  — the resolved client name.
          ``"mcp"``     — ``{"path": str, "content": str}`` (or None if mcp=False).
          ``"rules"``   — ``{"path": str, "content": str}`` (or None if rules=False).
          ``"dry_run"`` — True when no files were written.

    Raises:
        ValueError: when *name* is not in *registry*.
    """
    if opts is None:
        opts = InstallOptions()

    if registry is None:
        from yadgar.core.install.clients.registry import CLIENT_REGISTRY  # noqa: PLC0415

        registry = CLIENT_REGISTRY

    if name not in registry:
        known = ", ".join(sorted(registry))
        raise ValueError(f"Unknown client {name!r}. Known: {known}")

    descriptor = registry[name]
    mcp_result: dict[str, Any] | None = None
    rules_result: dict[str, Any] | None = None

    if opts.mcp:
        if opts.dry_run:
            mcp_result = _render_mcp_fragment(
                descriptor, opts.url, opts.version, opts.scope, opts.project_dir
            )
        else:
            from yadgar.core.install.clients.mcp_register import register_mcp  # noqa: PLC0415

            reg = register_mcp(
                descriptor,
                url=opts.url,
                token=opts.token,
                scope=opts.scope,
                project_dir=opts.project_dir,
            )
            mcp_result = {"path": reg["updated"], "content": json.dumps(reg["new"], indent=2)}

    if opts.rules:
        if opts.dry_run:
            rules_result = _render_rules_fragment(
                descriptor, opts.version, opts.scope, opts.project_dir
            )
        else:
            from yadgar.core.install.clients.rules_render import write_rules  # noqa: PLC0415

            ver = opts.version
            if not ver:
                from yadgar import __version__  # noqa: PLC0415

                ver = __version__
            wr = write_rules(
                descriptor, version=ver, scope=opts.scope, project_dir=opts.project_dir
            )
            rules_result = {"path": wr["written"], "content": None}

    return {
        "client": name,
        "mcp": mcp_result,
        "rules": rules_result,
        "dry_run": opts.dry_run,
    }


@observe(tier="boundary")
def install_auto_detect(
    opts: InstallOptions | None = None,
    *,
    registry: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Detect installed clients and install for each one found.

    Uses ``detect_installed_clients`` to probe which clients are present, then
    calls ``install_client`` for each.

    Args:
        opts:     an ``InstallOptions`` instance.  When None a default is used.
        registry: client registry dict; defaults to ``CLIENT_REGISTRY``.

    Returns:
        List of per-client result dicts from ``install_client``.
    """
    if opts is None:
        opts = InstallOptions()

    if registry is None:
        from yadgar.core.install.clients.registry import CLIENT_REGISTRY  # noqa: PLC0415

        registry = CLIENT_REGISTRY

    from yadgar.core.install.clients.detect import detect_installed_clients  # noqa: PLC0415

    detected = detect_installed_clients(registry=registry)
    return [install_client(d.name, opts=opts, registry=registry) for d in detected]
