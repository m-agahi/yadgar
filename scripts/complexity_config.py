#!/usr/bin/env python3
"""Shared config + allowlist loader for I13 complexity governance.

Both check_complexity.py (pre-commit hook) and check_complexity_allowlist.py
(I30 invariant) import from here — a single source of truth for cap values
and allowlist lookup.

Config file: .complexity-config.json (repo root)
Allowlist file: .complexity-allowlist.json (repo root)

Canonical metric vocabulary (used in both config and allowlist):
  cyclomatic, fn_loc, params, nesting, file_loc, class_depth
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults — must match the constants previously hardcoded in complexity_audit.py
# ---------------------------------------------------------------------------

_DEFAULT_CAPS: dict[str, dict[str, int | None]] = {
    "cyclomatic": {"soft": 10, "hard": 15},
    "nesting": {"soft": None, "hard": 4},
    "params": {"soft": 5, "hard": 8},
    "fn_loc": {"soft": 80, "hard": 150},
    "file_loc": {"soft": 500, "hard": 1000},
    "class_depth": {"soft": None, "hard": 3},
}


@dataclass
class ComplexityCaps:
    cyclomatic_soft: int | None
    cyclomatic_hard: int
    nesting_hard: int
    params_soft: int | None
    params_hard: int
    fn_loc_soft: int | None
    fn_loc_hard: int
    file_loc_soft: int | None
    file_loc_hard: int
    class_depth_hard: int
    per_path_overrides: dict[str, dict[str, dict[str, int | None]]] = field(default_factory=dict)

    def file_overrides(self, rel_path: str) -> dict[str, dict[str, int | None]]:
        """Return per-path cap overrides for rel_path, or {} if none."""
        return self.per_path_overrides.get(rel_path, {})


def _defaults() -> ComplexityCaps:
    return ComplexityCaps(
        cyclomatic_soft=_DEFAULT_CAPS["cyclomatic"]["soft"],
        cyclomatic_hard=_DEFAULT_CAPS["cyclomatic"]["hard"],
        nesting_hard=_DEFAULT_CAPS["nesting"]["hard"],
        params_soft=_DEFAULT_CAPS["params"]["soft"],
        params_hard=_DEFAULT_CAPS["params"]["hard"],
        fn_loc_soft=_DEFAULT_CAPS["fn_loc"]["soft"],
        fn_loc_hard=_DEFAULT_CAPS["fn_loc"]["hard"],
        file_loc_soft=_DEFAULT_CAPS["file_loc"]["soft"],
        file_loc_hard=_DEFAULT_CAPS["file_loc"]["hard"],
        class_depth_hard=_DEFAULT_CAPS["class_depth"]["hard"],
    )


def load_caps(config_path: str | Path | None = None) -> ComplexityCaps:
    """Load ComplexityCaps from .complexity-config.json.

    Falls back to built-in defaults if the file is missing or unparseable.
    If config_path is None, searches upward from cwd.
    """
    if config_path is None:
        config_path = _find_config()
    p = Path(config_path)
    if not p.exists():
        return _defaults()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):  # fmt: skip
        return _defaults()

    caps_raw = raw.get("caps", {})

    def _get(metric: str, key: str, default: int | None) -> int | None:
        m = caps_raw.get(metric, {})
        v = m.get(key, default)
        if v is None:
            return None
        return int(v)

    per_path_overrides: dict[str, dict[str, dict[str, int | None]]] = {}
    for path_str, overrides in raw.get("per_path_overrides", {}).items():
        per_path_overrides[path_str] = {
            metric: {k: (None if v is None else int(v)) for k, v in bounds.items()}
            for metric, bounds in overrides.items()
        }

    return ComplexityCaps(
        cyclomatic_soft=_get("cyclomatic", "soft", _DEFAULT_CAPS["cyclomatic"]["soft"]),
        cyclomatic_hard=_get("cyclomatic", "hard", _DEFAULT_CAPS["cyclomatic"]["hard"]),
        nesting_hard=_get("nesting", "hard", _DEFAULT_CAPS["nesting"]["hard"]),
        params_soft=_get("params", "soft", _DEFAULT_CAPS["params"]["soft"]),
        params_hard=_get("params", "hard", _DEFAULT_CAPS["params"]["hard"]),
        fn_loc_soft=_get("fn_loc", "soft", _DEFAULT_CAPS["fn_loc"]["soft"]),
        fn_loc_hard=_get("fn_loc", "hard", _DEFAULT_CAPS["fn_loc"]["hard"]),
        file_loc_soft=_get("file_loc", "soft", _DEFAULT_CAPS["file_loc"]["soft"]),
        file_loc_hard=_get("file_loc", "hard", _DEFAULT_CAPS["file_loc"]["hard"]),
        class_depth_hard=_get("class_depth", "hard", _DEFAULT_CAPS["class_depth"]["hard"]),
        per_path_overrides=per_path_overrides,
    )


def _find_config(start: str = ".") -> Path:
    """Walk up from start to find .complexity-config.json."""
    p = Path(start).resolve()
    for parent in [p] + list(p.parents):
        candidate = parent / ".complexity-config.json"
        if candidate.exists():
            return candidate
    return Path(start) / ".complexity-config.json"


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


@dataclass
class AllowlistEntry:
    path: str
    function: str
    lineno: int
    metrics: dict[str, int]
    rationale: str
    added: str
    added_by: str


def load_allowlist(allowlist_path: str | Path | None = None) -> list[AllowlistEntry]:
    """Load .complexity-allowlist.json.

    Returns [] if missing or unparseable.
    If allowlist_path is None, searches upward from cwd.
    """
    if allowlist_path is None:
        allowlist_path = _find_allowlist()
    p = Path(allowlist_path)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):  # fmt: skip
        return []
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entries.append(
            AllowlistEntry(
                path=item.get("path", ""),
                function=item.get("function", ""),
                lineno=item.get("lineno", 0),
                metrics=item.get("metrics", {}),
                rationale=item.get("rationale", ""),
                added=item.get("added", ""),
                added_by=item.get("added_by", ""),
            )
        )
    return entries


def _find_allowlist(start: str = ".") -> Path:
    """Walk up from start to find .complexity-allowlist.json."""
    p = Path(start).resolve()
    for parent in [p] + list(p.parents):
        candidate = parent / ".complexity-allowlist.json"
        if candidate.exists():
            return candidate
    return Path(start) / ".complexity-allowlist.json"


# ---------------------------------------------------------------------------
# Allowlist lookup
# ---------------------------------------------------------------------------


def build_allowlist_index(
    entries: list[AllowlistEntry],
) -> dict[tuple[str, str, str], AllowlistEntry]:
    """Build a fast lookup index keyed by (path, function, metric).

    The metric key uses the canonical vocabulary:
      cyclomatic, fn_loc, params, nesting, file_loc, class_depth
    """
    idx: dict[tuple[str, str, str], AllowlistEntry] = {}
    for e in entries:
        for metric in e.metrics:
            key = (e.path, e.function, metric)
            idx[key] = e
    return idx


def is_allowlisted(
    rel_path: str,
    function: str,
    metric: str,
    index: dict[tuple[str, str, str], AllowlistEntry],
    min_rationale_len: int = 1,
) -> bool:
    """Return True iff (rel_path, function, metric) is in the allowlist with non-empty rationale."""
    entry = index.get((rel_path, function, metric))
    if entry is None:
        return False
    return len(entry.rationale.strip()) >= min_rationale_len
