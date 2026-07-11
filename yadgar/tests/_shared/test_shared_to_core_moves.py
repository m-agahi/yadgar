"""T2 Car A — `_shared`→core move seam tests (layer-boundary train).

Pins the two pure moves and their PEP-562 shims:

1. Canonical new homes import cleanly:
   ``yadgar.core.config_sync`` (package; impl in ``sync.py``) and
   ``yadgar.core.install.platform_paths``.
2. PEP-562 shim back-compat: the old flat `_shared` paths
   (``yadgar._shared.config_sync``, ``yadgar._shared.platform_paths``) keep
   resolving the public names, and resolve them to the SAME objects as the
   canonical core paths (Car 0 #167 shim precedent).
3. Layer hygiene: the `_shared` shims must not create a STATIC _shared→core
   import edge (the forward is a lazy string-based importlib call, invisible
   to import-linter by design — but also verified here so a future edit can't
   silently regress it into a real edge).
"""

import subprocess
import sys

# ── 1. Canonical core paths ───────────────────────────────────────────────────


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


# ── 2. PEP-562 shim back-compat ───────────────────────────────────────────────


def test_config_sync_old_path_resolves_same_objects():
    """Old flat _shared path keeps working and resolves the canonical objects."""
    import yadgar._shared.config_sync as shim
    from yadgar.core.config_sync.sync import cmd_config_sync

    assert shim.cmd_config_sync is cmd_config_sync
    assert "cmd_config_sync" in dir(shim)


def test_platform_paths_old_path_resolves_same_objects():
    """Old flat _shared path keeps working and resolves the canonical objects."""
    import yadgar._shared.platform_paths as shim
    from yadgar.core.install.platform_paths import (
        get_claude_config_dir,
        is_nix_managed,
    )

    assert shim.get_claude_config_dir is get_claude_config_dir
    assert shim.is_nix_managed is is_nix_managed
    assert "get_claude_settings_path" in dir(shim)


def test_shim_unknown_attribute_raises():
    """Shims raise AttributeError for names that never lived in the modules."""
    import pytest

    import yadgar._shared.config_sync as cs_shim
    import yadgar._shared.platform_paths as pp_shim

    with pytest.raises(AttributeError):
        _ = cs_shim.does_not_exist
    with pytest.raises(AttributeError):
        _ = pp_shim.does_not_exist


# ── 3. Layer hygiene (no static _shared→core edge) ───────────────────────────


def test_shims_do_not_statically_import_core():
    """Importing the _shared shims alone must not load yadgar.core.

    The forward must stay lazy (PEP-562 __getattr__): merely importing the
    shim modules must not pull yadgar.core into sys.modules — only attribute
    access does.
    """
    code = (
        "import sys\n"
        "import yadgar._shared.config_sync\n"
        "import yadgar._shared.platform_paths\n"
        "core_mods = [m for m in sys.modules if m.startswith('yadgar.core')]\n"
        "assert not core_mods, f'shim import loaded core modules: {core_mods}'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"isolated shim import check failed:\n{proc.stderr}"
