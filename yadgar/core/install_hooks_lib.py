"""Back-compat shim — install_hooks_lib moved into ``yadgar.core.install`` (T2 Car D3, no-lone-files law ADR-0084).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working. Lazy importlib forward — the target only loads on first attribute
access. New code must import from ``yadgar.core.install.install_hooks_lib`` instead.
"""

from typing import Final

_TARGET: Final = "yadgar.core.install.install_hooks_lib"
_EXPORTS: Final = (
    "Path",
    "_WORKTREE_MARKER",
    "_append_if_absent",
    "_atomic_write",
    "_build_core_hooks",
    "_canonical_repo_python",
    "_copy_hook",
    "_copy_scope_scripts",
    "_entry_interpreter",
    "_existing_registration_ok",
    "_install_append_hooks",
    "_install_global_scripts",
    "_is_durable_interpreter",
    "_is_git_worktree_path",
    "_load_settings",
    "_main_repo_root",
    "_make_hook_entry",
    "_pipx_python",
    "_registered_python",
    "_resolve_python_shebang",
    "_resolve_scope_paths",
    "_stable_python",
    "_write_global_stop_hooks",
    "annotations",
    "install_hooks_impl",
    "is_running_in_container",
    "json",
    "logger",
    "logging",
    "observe",
    "os",
    "shlex",
    "shutil",
    "subprocess",
    "sys",
    "tempfile",
)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(_TARGET), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
