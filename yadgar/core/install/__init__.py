"""yadgar.core.install — host install/bootstrap helpers package.

T2 Car A (layer-boundary train): created as the install-adjacent home for
``platform_paths`` (moved from the flat ``yadgar/_shared/platform_paths.py``;
dual-import law: its only prod importer is core's install_subagents flow).
Car D3 relocates the remaining ``install_*_lib`` lone files into this package.

  platform_paths.py — cross-platform Claude Code config-dir resolution
                      (Linux/macOS/Windows) + nix-managed detection.

A back-compat PEP-562 shim remains at the old
``yadgar._shared.platform_paths`` path.
"""
