"""yadgar._shared.server_helpers — server helper utilities package.

T2 Car D (D1, layer-boundary train): the flat ``server_helpers.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar._shared.server_helpers`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar._shared.server_helpers.server_helpers``
directly.

  server_helpers.py — normalize_write_context + assorted server-side helper functions
"""

from typing import Final

_EXPORTS: Final = {
    "Path": "yadgar._shared.server_helpers.server_helpers",
    "UTC": "yadgar._shared.server_helpers.server_helpers",
    "_ANCHOR_PROMOTE_TAGS": "yadgar._shared.server_helpers.server_helpers",
    "_CLAUDE_WORKTREES_MARKER": "yadgar._shared.server_helpers.server_helpers",
    "_DECISION_STRONG_RE": "yadgar._shared.server_helpers.server_helpers",
    "_FENCED_CODE_BLOCK_RE": "yadgar._shared.server_helpers.server_helpers",
    "_GIT_SAFE_ARGS": "yadgar._shared.server_helpers.server_helpers",
    "_GIT_WORKTREES_MARKER": "yadgar._shared.server_helpers.server_helpers",
    "_MD_HEADER_RE": "yadgar._shared.server_helpers.server_helpers",
    "_bump_epoch_for_context": "yadgar._shared.server_helpers.server_helpers",
    "_compute_valid_until": "yadgar._shared.server_helpers.server_helpers",
    "_cosine_similarity": "yadgar._shared.server_helpers.server_helpers",
    "_count_markdown_headers": "yadgar._shared.server_helpers.server_helpers",
    "_default_branch_for_root": "yadgar._shared.server_helpers.server_helpers",
    "_file_hash": "yadgar._shared.server_helpers.server_helpers",
    "_git_safe_env": "yadgar._shared.server_helpers.server_helpers",
    "_has_unpaired_surrogate": "yadgar._shared.server_helpers.server_helpers",
    "_is_throwaway_context": "yadgar._shared.server_helpers.server_helpers",
    "_parse_worktree_gitdir_file": "yadgar._shared.server_helpers.server_helpers",
    "_push_event": "yadgar._shared.server_helpers.server_helpers",
    "_q_with_timeout": "yadgar._shared.server_helpers.server_helpers",
    "_resolve_project_root": "yadgar._shared.server_helpers.server_helpers",
    "_st": "yadgar._shared.server_helpers.server_helpers",
    "_worktree_canonical_root": "yadgar._shared.server_helpers.server_helpers",
    "_worktree_root_from_path_heuristics": "yadgar._shared.server_helpers.server_helpers",
    "annotations": "yadgar._shared.server_helpers.server_helpers",
    "datetime": "yadgar._shared.server_helpers.server_helpers",
    "functools": "yadgar._shared.server_helpers.server_helpers",
    "get_settings": "yadgar._shared.server_helpers.server_helpers",
    "hashlib": "yadgar._shared.server_helpers.server_helpers",
    "logger": "yadgar._shared.server_helpers.server_helpers",
    "logging": "yadgar._shared.server_helpers.server_helpers",
    "math": "yadgar._shared.server_helpers.server_helpers",
    "normalize_write_context": "yadgar._shared.server_helpers.server_helpers",
    "observe": "yadgar._shared.server_helpers.server_helpers",
    "os": "yadgar._shared.server_helpers.server_helpers",
    "re": "yadgar._shared.server_helpers.server_helpers",
    "subprocess": "yadgar._shared.server_helpers.server_helpers",
    "timedelta": "yadgar._shared.server_helpers.server_helpers",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
