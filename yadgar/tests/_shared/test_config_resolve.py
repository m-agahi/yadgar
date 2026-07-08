"""v5.95.0 config-integrity: shared env>yaml>default knob resolver.

The `resolve_knob` helper is the DRY implementation of the hybrid read pattern
that makes config.yaml authoritative for tunable knobs while preserving the
live-env override that tests/containers rely on (no get_settings lru_cache lag).

Precedence asserted here: env (live os.environ) > get_settings().<FIELD> > default.
The get_settings() layer is itself yaml-aware (YamlConfigSource), so a config.yaml
value flows through the FIELD read.
"""

from __future__ import annotations

import os

from yadgar._shared.config import get_settings, resolve_knob
from yadgar._shared.config_registry import clear_config_caches


def _reset_env(name: str, old: str | None) -> None:
    if old is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old


def test_env_overrides_settings_and_default():
    """Live env value wins over both the Settings field and the literal default."""
    name = "YADGAR_METRICS_ENABLED"
    old = os.environ.get(name)
    os.environ[name] = "0"
    try:
        val = resolve_knob(
            name, "METRICS_ENABLED", lambda s: s.strip().lower() in ("1", "true"), True
        )
        assert val is False, f"env '0' should parse to False, got {val!r}"
    finally:
        _reset_env(name, old)


def test_settings_used_when_env_unset():
    """With env UNSET, the Settings field value is returned (yaml-aware layer)."""
    name = "YADGAR_METRICS_ENABLED"
    old = os.environ.pop(name, None)
    clear_config_caches()
    try:
        # Settings default for METRICS_ENABLED is True; no env, no yaml -> field value.
        val = resolve_knob(
            name, "METRICS_ENABLED", lambda s: s.strip().lower() in ("1", "true"), False
        )
        assert val == get_settings().METRICS_ENABLED, (
            f"with env unset resolve_knob must equal get_settings().METRICS_ENABLED, got {val!r}"
        )
    finally:
        _reset_env(name, old)
        clear_config_caches()


def test_default_when_field_missing():
    """A non-existent Settings field falls through to the literal default (no crash)."""
    name = "YADGAR_TOTALLY_MADE_UP_KNOB_XYZ"
    old = os.environ.pop(name, None)
    try:
        val = resolve_knob(name, "NO_SUCH_FIELD_ON_SETTINGS", int, 42)
        assert val == 42, f"missing field must yield the default 42, got {val!r}"
    finally:
        _reset_env(name, old)


def test_bad_env_value_never_crashes_consumer():
    """An unparseable env value is swallowed (falls through), never crashes.

    A malformed YADGAR_TOOL_POOL_WORKERS also breaks pydantic's env source, so
    get_settings() raises too — resolve_knob must then return the literal default
    rather than propagate. (This is the "never hard-fail on a broken config
    surface" guarantee.)
    """
    name = "YADGAR_TOOL_POOL_WORKERS"
    old = os.environ.get(name)
    os.environ[name] = "not-an-int"
    clear_config_caches()
    try:
        val = resolve_knob(name, "TOOL_POOL_WORKERS", int, 2)
        assert val == 2, f"bad env + broken Settings must yield the default 2, got {val!r}"
    finally:
        _reset_env(name, old)
        clear_config_caches()
