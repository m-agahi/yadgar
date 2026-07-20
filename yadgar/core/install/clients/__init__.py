"""Multi-client integration framework — descriptor + registry foundation (Car 0).

One shared streamable-HTTP daemon serves every client; the per-client
"variants" are pure config/text (MCP-registration file + rules file). This
package holds the single client registry that all three install surfaces
(MCP-registration, rules, hooks) key off.

Car 0 ships the foundation:
  - ``descriptor``: the ``ClientDescriptor`` schema (design §4.3) + its enums.
  - ``registry``:   one descriptor per supported client (design §2.2 / §3.2).
  - ``merge``:      format-preserving, atomic JSON/TOML merge primitives.

Car 1 adds the MCP-registration generator:
  - ``mcp_register``: 5 entry-schema serializers + ``register_mcp`` + CLI entry
    ``yadgar install-mcp --client X``.  Absorbs ``configure_mcp`` (daemon.py).

The rules renderer (``rules_render``) is Car 2.

Car 3 adds the unified install orchestrator + auto-detect:
  - ``install``: ``install_client`` + ``install_auto_detect`` + ``--print``
    declarative mode for nix home-manager (#67).
  - ``detect``: ``detect_installed_clients`` + ``is_client_present`` probes.
  The ``yadgar install --client X`` CLI entry is in ``cli/install.py``.
"""

from __future__ import annotations

__all__ = ["descriptor", "detect", "install", "merge", "mcp_register", "registry"]
