"""Back-compat shim — platform_paths moved to ``yadgar.core.install`` (T2 Car A).

Dual-import law (layer-boundary train): the only prod importer was core's
install_subagents flow, so the module left `_shared` for
``yadgar/core/install/platform_paths.py`` (install-adjacent per plan Car A).

PEP-562 shim (Car 0 #167 precedent): ``from yadgar._shared.platform_paths
import get_claude_config_dir`` keeps working. New code must import from
``yadgar.core.install.platform_paths`` instead. The forward is a lazy
string-based importlib call ON PURPOSE — a static ``from yadgar.core ...
import`` here would create a forbidden _shared→core edge (import-linter
contract 1).
"""

from typing import Final

_TARGET: Final = "yadgar.core.install.platform_paths"
_EXPORTS: Final = (
    "get_claude_config_dir",
    "get_claude_agents_dir",
    "get_claude_settings_path",
    "is_nix_managed",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
