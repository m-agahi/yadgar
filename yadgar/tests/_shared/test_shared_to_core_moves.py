"""T2 Car A — `_shared`→core move seam tests (layer-boundary train).

Pins the two pure moves to their canonical core homes:

  ``yadgar.core.config_sync`` (package; impl in ``sync.py``) and
  ``yadgar.core.install.platform_paths``.

The PEP-562 back-compat shims at the old flat `_shared` paths
(``yadgar._shared.config_sync``, ``yadgar._shared.platform_paths``) were
removed in the forward-only shim-cleanup (I34) — every importer now uses the
canonical core paths, so the shim back-compat and layer-hygiene sections that
used to live here are gone with them.
"""

# ── Canonical core paths ──────────────────────────────────────────────────────


def test_config_sync_canonical_core_path():
    """cmd_config_sync + helpers live in yadgar.core.config_sync.sync."""
    from yadgar.core.config_sync.sync import (
        _apply_missing,
        _atomic_yaml_write,
        cmd_config_sync,
    )

    assert callable(cmd_config_sync)
    assert callable(_apply_missing)
    assert callable(_atomic_yaml_write)


def test_config_sync_package_reexports():
    """The config_sync package __init__ re-exports the command entrypoint."""
    from yadgar.core.config_sync import cmd_config_sync
    from yadgar.core.config_sync.sync import cmd_config_sync as canonical

    assert cmd_config_sync is canonical


def test_platform_paths_canonical_core_path():
    """platform_paths lives install-adjacent in yadgar.core.install."""
    from yadgar.core.install.platform_paths import (
        get_claude_agents_dir,
        get_claude_config_dir,
        get_claude_settings_path,
        is_nix_managed,
    )

    assert callable(get_claude_config_dir)
    assert callable(get_claude_agents_dir)
    assert callable(get_claude_settings_path)
    assert callable(is_nix_managed)
