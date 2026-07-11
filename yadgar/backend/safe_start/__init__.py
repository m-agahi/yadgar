"""yadgar.backend.safe_start — safe-start / split-brain guard package.

T2 Car D (D2, layer-boundary train): the flat ``safe_start.py`` packaged per
the no-lone-files law (ADR-0084). ``yadgar.backend.safe_start`` IS the old dotted
path — imports keep working through this PEP-562 re-export ``__init__``
(Car 0 #167 precedent). New code may import ``yadgar.backend.safe_start.safe_start``
directly.

  safe_start.py — startup restore-source selection + torn-manifest detection (P0 #37)
"""

from typing import Final

_EXPORTS: Final = {
    "CANONICAL_NAME": "yadgar.backend.safe_start.safe_start",
    "EXIT_NOT_TORN": "yadgar.backend.safe_start.safe_start",
    "EXIT_NO_RESTORE_SOURCE": "yadgar.backend.safe_start.safe_start",
    "EXIT_OK": "yadgar.backend.safe_start.safe_start",
    "EXIT_SPLIT_BRAIN": "yadgar.backend.safe_start.safe_start",
    "Path": "yadgar.backend.safe_start.safe_start",
    "RUNBOOK_POINTER": "yadgar.backend.safe_start.safe_start",
    "TORN_MANIFEST_PATTERNS": "yadgar.backend.safe_start.safe_start",
    "UTC": "yadgar.backend.safe_start.safe_start",
    "_CANDIDATE_GLOBS": "yadgar.backend.safe_start.safe_start",
    "_cmd_preflight": "yadgar.backend.safe_start.safe_start",
    "_cmd_recover": "yadgar.backend.safe_start.safe_start",
    "annotations": "yadgar.backend.safe_start.safe_start",
    "argparse": "yadgar.backend.safe_start.safe_start",
    "choose_restore_source": "yadgar.backend.safe_start.safe_start",
    "datetime": "yadgar.backend.safe_start.safe_start",
    "detect_split_brain": "yadgar.backend.safe_start.safe_start",
    "is_structurally_complete": "yadgar.backend.safe_start.safe_start",
    "is_torn_manifest_failure": "yadgar.backend.safe_start.safe_start",
    "list_restore_candidates": "yadgar.backend.safe_start.safe_start",
    "main": "yadgar.backend.safe_start.safe_start",
    "newest_inner_mtime": "yadgar.backend.safe_start.safe_start",
    "observe": "yadgar.backend.safe_start.safe_start",
    "perform_auto_restore": "yadgar.backend.safe_start.safe_start",
    "shutil": "yadgar.backend.safe_start.safe_start",
    "sys": "yadgar.backend.safe_start.safe_start",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
