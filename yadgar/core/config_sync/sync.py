"""config_sync — incremental YAML config sync (v5.44.0 X5).

Separate module to keep config_yaml.py under I13 file_loc limits.
Imported by the yadgar/core/cli/config.py dispatch table.

T2 Car A (layer-boundary train): moved from yadgar/_shared/config_sync.py —
core-only importers, no compute (dual-import law). A PEP-562 back-compat shim
remains at the old path.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from yadgar._shared.observability.observe import observe


def _compute_missing(data: CommentedMap, settings) -> list[str]:
    """Return list of Settings field names absent from yaml data."""
    from yadgar._shared.config import Settings  # noqa: PLC0415

    return [k.lower() for k in Settings.model_fields if k.lower() not in data]


@observe(tier="hot")
def _compute_unknown(data: CommentedMap, remove_unknown: bool) -> list[str]:
    """Return yaml keys not in Settings (only when remove_unknown=True)."""
    if not remove_unknown:
        return []
    from yadgar._shared.config import Settings  # noqa: PLC0415

    known = {k.lower() for k in Settings.model_fields}
    return [k for k in data if k.lower() not in known]


@observe(tier="hot")
def _handle_check(missing: list[str], settings) -> None:
    """Print check result and exit (helper for cmd_config_sync --check)."""
    if missing:
        print(f"Missing keys ({len(missing)}):")
        for k in missing:
            default = getattr(settings, k.upper(), "<unknown>")
            print(f"  {k}: {default!r}")
        sys.exit(1)
    print("Config is fully synced — no missing keys.")
    sys.exit(0)


@observe(tier="hot")
def _handle_dry_run(missing: list[str], unknown: list[str], settings, remove_unknown: bool) -> None:
    """Print dry-run diff (helper for cmd_config_sync --dry-run)."""
    from yadgar._shared.config.config_yaml import FIELD_META  # noqa: PLC0415

    if missing:
        print(f"Would add {len(missing)} keys:")
        for k in missing:
            default = getattr(settings, k.upper(), "<unknown>")
            desc = FIELD_META.get(k, {}).get("desc", "")
            print(f"  + {k}: {default!r}  # {desc}")
    else:
        print("Config fully synced — no changes needed.")
    if unknown and remove_unknown:
        print(f"Would remove {len(unknown)} unknown keys: {unknown}")


@observe(tier="stage")
def _apply_missing(data: CommentedMap, missing: list[str], settings) -> None:
    """Add missing keys with defaults + FIELD_META comments."""
    from yadgar._shared.config.config_yaml import FIELD_META  # noqa: PLC0415

    for field_lower in missing:
        value = getattr(settings, field_lower.upper())
        data[field_lower] = value
        desc = FIELD_META.get(field_lower, {}).get("desc", "")
        if desc:
            data.yaml_set_comment_before_after_key(field_lower, before=f" {desc}")


@observe(tier="stage")
def _atomic_yaml_write(path: Path, y: YAML, data: CommentedMap) -> None:
    """Write yaml data atomically (temp file → rename) with chmod 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=path.parent, prefix=".config_sync_tmp_", suffix=".yaml"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            y.dump(data, f)
        os.replace(tmp_path_str, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass
        raise


@observe(tier="boundary")
def cmd_config_sync(args) -> None:
    """Incrementally sync ~/.config/yadgar/config.yaml with current Settings model fields.

    Adds missing keys with defaults + FIELD_META comments. Preserves all existing
    user-set values. Idempotent — running twice is a no-op.

    v5.44.0 X5: addresses the recurring bug class where a release adds a new
    Settings field but the user's existing config.yaml doesn't contain it
    (config.yaml is one-shot written by cmd_config_init; never auto-updated).

    Flags (from args):
        check:          List keys that would be added without writing. Exit 1 if any.
        dry_run:        Print diff to stdout, no file change.
        remove_unknown: Delete yaml keys not in current Settings. Default False.
    """
    from yadgar._shared.config import Settings  # noqa: PLC0415
    from yadgar._shared.config.config_yaml import get_config_path  # noqa: PLC0415

    path = get_config_path()
    check = getattr(args, "check", False)
    dry_run = getattr(args, "dry_run", False)
    remove_unknown = getattr(args, "remove_unknown", False)

    if not path.exists():
        print(f"Config file not found: {path}", file=sys.stderr)
        print("Run 'yadgar config init' to create it.", file=sys.stderr)
        sys.exit(1)

    y = YAML()
    y.default_flow_style = False
    y.width = 4096

    try:
        with open(path) as f:
            data = y.load(f)
    except Exception as e:
        print(f"YAML parse error in {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, CommentedMap):
        data = CommentedMap(data or {})

    settings = Settings()
    missing = _compute_missing(data, settings)
    unknown = _compute_unknown(data, remove_unknown)

    if check:
        _handle_check(missing, settings)

    if dry_run:
        _handle_dry_run(missing, unknown, settings, remove_unknown)
        return

    if not missing and not unknown:
        print("Config already fully synced — no changes needed.")
        return

    _apply_missing(data, missing, settings)
    for k in unknown:
        del data[k]

    _atomic_yaml_write(path, y, data)

    if missing:
        print(f"Added {len(missing)} key(s): {', '.join(missing)}")
    if unknown and remove_unknown:
        print(f"Removed {len(unknown)} unknown key(s): {', '.join(unknown)}")
    print(f"Config: {path}")
