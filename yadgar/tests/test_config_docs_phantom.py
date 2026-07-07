"""Phantom-doc guard: docs/configuration.md documented keys ⊆ Settings fields.

I25 sibling ratchet. The three-way-sync test (test_config_three_way_sync.py)
proves every *real* Settings field is covered by FIELD_META + _REGISTRY (or
allowlisted). This test proves the *reverse direction*: every config knob the
docs claim exists actually exists as a Settings field.

Why this matters: docs/configuration.md drifted to document phantom knobs that
were never real Settings fields (e.g. ``fractal_levels`` →
``YADGAR_FRACTAL_LEVELS``, ``wrrf_k``, the whole ``confidence_*`` family). A
user who sets ``YADGAR_FRACTAL_LEVELS=5`` silently gets nothing — the env var is
ignored because no field reads it. This guard turns that drift RED.

Scope: only the canonical config-knob table rows are checked — rows of the shape
``| `key` | `YADGAR_KEY` | type | default | desc |``. Schema-field tables
(``memory.branch`` etc.), the DB-credentials table (env-only, no config key),
and prose are intentionally excluded: they have no ``| `key` | `YADGAR_*` |``
shape so the row regex never matches them.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOCS_PATH = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"

# Match canonical config-knob rows:  | `key` | `YADGAR_KEY` | ...
# The two backticked, pipe-delimited leading cells (config key + YADGAR_ env)
# uniquely identify a knob row. Dotted keys (``update.install_enabled``) are
# normalised to underscores to match Settings field names.
_ROW_RE = re.compile(r"^\|\s*`([a-z0-9_.]+)`\s*\|\s*`(YADGAR_[A-Z0-9_]+)`", re.MULTILINE)


def _documented_keys() -> set[str]:
    """Return the set of config keys documented as knob-table rows in the doc."""
    text = _DOCS_PATH.read_text()
    return {m.group(1).replace(".", "_") for m in _ROW_RE.finditer(text)}


def test_docs_path_exists() -> None:
    assert _DOCS_PATH.exists(), f"Missing config doc: {_DOCS_PATH}"


def test_documented_keys_subset_of_settings() -> None:
    """Every documented config knob must be a real Settings field.

    RED when docs/configuration.md documents a knob with no backing Settings
    field (phantom doc). Fix by deleting the phantom row from the doc — never by
    inventing a field to satisfy it.
    """
    from yadgar._shared.config import Settings

    fields = {f.lower() for f in Settings.model_fields}
    documented = _documented_keys()

    assert documented, "No documented config knobs parsed — row regex likely broke."

    phantom = sorted(documented - fields)
    assert not phantom, (
        f"{len(phantom)} phantom config knob(s) documented in docs/configuration.md "
        f"with NO backing Settings field:\n  " + "\n  ".join(phantom) + "\n\n"
        "Delete the phantom row(s) from docs/configuration.md — a documented "
        "YADGAR_* env var that no field reads is silently ignored at runtime."
    )
