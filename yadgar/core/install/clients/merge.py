"""Format-preserving, atomic merge primitives (Car 0 — format-generic).

Given an existing config file, a root-key path, and a value dict, merge the
value under that path preserving every sibling key/comment, then write
atomically. These helpers are schema-agnostic: the per-client entry SHAPES
(Gemini ``httpUrl``, Cline ``streamableHttp``, …) are Car 1's concern — Car 0
only guarantees the merge never clobbers the user's other servers/settings.

Two format paths:
  - ``merge_json``: parse (empty on error) → set one nested key → atomic write.
    Preserves every sibling.
  - ``merge_toml``: ``tomlkit.parse`` → set one nested table → atomic write.
    Preserves comments + key order + other tables.

Atomicity uses a text-level primitive (``_atomic_write_text``): a crash
mid-write must not truncate the user's config, and no temp file may leak. The
JSON writer in ``install/_settings.py`` JSON-encodes its argument, so it cannot
serve the TOML path — hence the local text primitive here.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import tomlkit

from yadgar._shared.observability.observe import observe


@observe(tier="stage")
def _atomic_write_text(target: Path, text: str) -> None:
    """Write *text* to *target* atomically via a temp file in the same dir.

    Same-directory temp + ``os.replace`` gives an atomic rename on POSIX and
    Windows. On any failure the temp file is unlinked and the original target is
    left untouched.
    """
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path_str = tempfile.mkstemp(dir=directory, prefix=".yadgar_cfg_tmp_")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(text)
        os.replace(tmp_path_str, target)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass
        raise


@observe(tier="stage")
def _load_json(path: Path) -> dict[str, Any]:
    """Parse *path* as JSON, returning an empty dict on missing/malformed input."""
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):  # fmt: skip
        return {}
    return loaded if isinstance(loaded, dict) else {}


@observe(tier="stage")
def _descend(container: Any, keys: tuple[str, ...]) -> Any:
    """Walk/create nested dict-like tables for *keys*, returning the innermost.

    Works for plain ``dict`` (JSON) and ``tomlkit`` tables alike — both support
    ``in`` / item get / item set. A non-mapping value at an intermediate key is
    overwritten with a fresh mapping of the container's own type.
    """
    node = container
    for key in keys:
        child = node.get(key) if hasattr(node, "get") else None
        if not _is_mapping(child):
            child = tomlkit.table() if isinstance(container, tomlkit.TOMLDocument) else {}
            node[key] = child
        node = node[key]
    return node


def _is_mapping(value: Any) -> bool:
    """True when *value* is a writable mapping (dict or tomlkit table).

    tomlkit tables/documents register as ``MutableMapping``, as do plain dicts;
    scalars and lists do not — so a scalar sitting where a nested table must go
    is correctly rejected and replaced.
    """
    return isinstance(value, MutableMapping)


@observe(tier="stage")
def merge_json(
    path: Path,
    root_key: tuple[str, ...],
    entry_key: str,
    value: dict[str, Any],
) -> None:
    """Merge *value* under ``root_key → entry_key`` in the JSON file at *path*.

    Every sibling key (other servers, unrelated top-level keys) is preserved.
    The file is created (with parents) when absent; a malformed existing file is
    replaced rather than merged into.
    """
    config = _load_json(path)
    parent = _descend(config, root_key)
    parent[entry_key] = value
    _atomic_write_text(path, json.dumps(config, indent=2) + "\n")


@observe(tier="stage")
def merge_toml(
    path: Path,
    root_key: tuple[str, ...],
    value: dict[str, Any],
) -> None:
    """Merge *value* into the TOML table addressed by *root_key* at *path*.

    Uses ``tomlkit`` so comments, key order, and other tables round-trip
    unchanged. The final key in *root_key* is the target table (e.g.
    ``("mcp_servers", "yadgar")``); it is replaced wholesale with *value*.
    """
    if path.exists():
        try:
            doc = tomlkit.parse(path.read_text())
        except (OSError, tomlkit.exceptions.TOMLKitError):  # fmt: skip
            doc = tomlkit.document()
    else:
        doc = tomlkit.document()

    *parents, leaf = root_key
    parent = _descend(doc, tuple(parents)) if parents else doc
    table = tomlkit.table()
    for k, v in value.items():
        table[k] = v
    parent[leaf] = table
    _atomic_write_text(path, tomlkit.dumps(doc))
