"""v5.95.0 config-integrity Phase 3 — the anti-recurrence ratchet (#78 for config).

THE PHANTOM-KNOB CLASS: a user-tunable Settings field surfaced in config.yaml /
the /admin UI (it has a `config_yaml.py` FIELD_META entry) but consumed in the
code ONLY via `os.environ`/`os.getenv` — never via `get_settings()`. Because
get_settings() is the ONLY yaml-aware read path, such a knob shows+writes a value
the code never reads. Proof it bites: `offload_tools: true` was ignored → offload
ran OFF → the --cpus 1 core froze (#72).

This test FAILS if any FIELD_META-backed Settings field is consumed env-ONLY.
Once green it permanently prevents a NEW phantom knob (the ratchet). A field on
the explicit INFRA/SECRET allowlist below is deployment-env by design (resolved
per deploy target, never persisted to yaml) and is exempt.

Detection is source-scan based: for each in-scope field we grep the yadgar/ tree
(excluding tests) for (a) an env consumer `os.environ`/`os.getenv` referencing
YADGAR_<FIELD> and (b) a settings consumer `get_settings().<FIELD>` /
`_settings().<FIELD>` / `settings.<FIELD>` / `.<FIELD>` via resolve_knob. A field
with an env consumer but NO settings consumer is a phantom knob → fail.
"""

from __future__ import annotations

import re
from pathlib import Path

# INFRA/SECRET allowlist — deployment-env by design (task-confirmed). These are
# resolved from the environment per deploy target and intentionally NOT
# config.yaml-authoritative, so an env-only consumer is correct for them.
_INFRA_SECRET_ALLOWLIST: set[str] = {
    "PORT",
    "HOST",
    "DB_URL",
    "EMBED_URL",
    "DATA_DIR",
    "DB_PATH",
    "MCP_AUTH_TOKEN",
    "DB_USER",
    "DB_PASS",
    "RW_USER",
    "RW_PASS",
    "RO_USER",
    "RO_PASS",
    "REQUIRE_AUTH",
    "ALLOW_ROOT",
}

_REPO = Path(__file__).resolve().parents[2]
_PKG = _REPO / "yadgar"


def _python_sources() -> list[Path]:
    """All yadgar/*.py excluding the tests package."""
    out: list[Path] = []
    for p in _PKG.rglob("*.py"):
        parts = p.relative_to(_PKG).parts
        if "tests" in parts:
            continue
        out.append(p)
    return out


def _in_scope_fields() -> list[str]:
    """FIELD_META-backed Settings fields, minus the INFRA/SECRET allowlist.

    A field is user-tunable (in scope) iff it is BOTH a Settings model field AND
    has a config_yaml.py FIELD_META entry (i.e. it is surfaced in the yaml/UI).
    """
    from yadgar._shared.config import Settings
    from yadgar._shared.config_yaml import FIELD_META

    yaml_keys = set(FIELD_META.keys())
    fields = []
    for field_upper in Settings.model_fields:
        if field_upper in _INFRA_SECRET_ALLOWLIST:
            continue
        if field_upper.lower() in yaml_keys:
            fields.append(field_upper)
    return sorted(fields)


def _scan(sources: list[Path]) -> str:
    return "\n".join(p.read_text(errors="ignore") for p in sources)


def test_no_field_meta_knob_is_env_only() -> None:
    """Every FIELD_META-backed, non-infra Settings field with an env consumer
    must ALSO have a get_settings()-based consumer (directly or via resolve_knob).

    RED before v5.95.0 wiring (offload + the 20); GREEN after. A new phantom knob
    added later turns this RED again — that is the point.
    """
    corpus = _scan(_python_sources())
    phantoms: list[str] = []

    for field in _in_scope_fields():
        env_ref = f"YADGAR_{field}"
        # (a) env consumer: os.environ / os.getenv referencing the env name.
        has_env = bool(
            re.search(
                rf"os\.(environ\.get|getenv|environ\[)\s*\(?\s*[\"']{re.escape(env_ref)}[\"']",
                corpus,
            )
        )
        if not has_env:
            continue
        # (b) settings consumer: an attribute access on a KNOWN settings-holder
        #     (get_settings()/_settings() call results, or the conventional
        #     Settings-instance variable names settings/_settings/cfg/_cfg used in
        #     this codebase), OR a resolve_knob(..., "<FIELD>", ...) call naming the
        #     field. Deliberately NOT a bare `.<FIELD>` match — that would accept ANY
        #     attribute anywhere ending in the field name and render the ratchet
        #     toothless (a future phantom with a coincidental `.FIELD` elsewhere
        #     would pass). The holder allowlist keeps the ratchet honest.
        #     Limitation: per-FIELD, not per-SITE — a field with BOTH a settings
        #     consumer and a SEPARATE env-only site (a "mixed" knob) is not caught.
        f = re.escape(field)
        has_settings = bool(
            re.search(rf"(get_settings|_settings)\(\)\.{f}\b", corpus)
            or re.search(rf"\b(settings|_settings|cfg|_cfg)\.{f}\b", corpus)
            or re.search(rf"resolve_knob\([^)]*[\"']{f}[\"']", corpus)
        )
        if not has_settings:
            phantoms.append(field)

    assert not phantoms, (
        "Phantom config knob(s) — surfaced in config.yaml/UI (FIELD_META) but consumed "
        "ONLY via os.environ, so the yaml value is silently ignored:\n  "
        + "\n  ".join(sorted(phantoms))
        + "\n\nFix each: read it via resolve_knob(env, FIELD, parse, default) or "
        "get_settings().<FIELD> so config.yaml becomes authoritative. "
        "If it is genuinely deployment-env, add it to _INFRA_SECRET_ALLOWLIST with a note."
    )
