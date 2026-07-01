"""v5.95.0 config-integrity Phase 4: hot-path literals promoted to config knobs.

Five operational literals in the daemon hot path had no Settings field — they
were rebuild-only. Promoted to config.yaml-authoritative knobs (full I25/I32
5-place) so ops can tune them without a rebuild:

  RERANKER_IDLE_UNLOAD_SEC          (was 600.0, lifecycle.py)
  RERANKER_IDLE_CHECK_INTERVAL_SEC  (was 60,    lifecycle.py)
  HEALTH_HANDLER_TIMEOUT_SEC        (was 3.0,   server/http.py _HEALTH_TIMEOUT_SEC)
  HEALTH_PROBE_TIMEOUT_SEC          (was 2.0,   server/http.py probe httpx client)
  VACUUM_AUTO_COOLDOWN_HOURS        (was 6.0,   consolidation auto-vacuum cooldown)

Each is resolved via get_settings() (config.yaml-authoritative) so the phantom-knob
ratchet (test_no_phantom_knobs) covers them too.
"""

from __future__ import annotations

import pytest

from yadgar.config_registry import clear_config_caches


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    cfg = tmp_path / "yadgar-hardcoded-knobs.yaml"
    monkeypatch.setenv("YADGAR_CONFIG_FILE", str(cfg))
    clear_config_caches()
    yield
    clear_config_caches()


def _write_yaml(body: str) -> None:
    from yadgar.config_yaml import get_config_path

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    clear_config_caches()


@pytest.mark.parametrize(
    ("field", "yaml_key", "yaml_val", "expected", "default"),
    [
        ("RERANKER_IDLE_UNLOAD_SEC", "reranker_idle_unload_sec", "900", 900.0, 600.0),
        ("RERANKER_IDLE_CHECK_INTERVAL_SEC", "reranker_idle_check_interval_sec", "30", 30, 60),
        ("HEALTH_HANDLER_TIMEOUT_SEC", "health_handler_timeout_sec", "4.5", 4.5, 3.0),
        ("HEALTH_PROBE_TIMEOUT_SEC", "health_probe_timeout_sec", "1.5", 1.5, 2.0),
        ("VACUUM_AUTO_COOLDOWN_HOURS", "vacuum_auto_cooldown_hours", "12", 12.0, 6.0),
    ],
)
def test_field_exists_and_yaml_respected(monkeypatch, field, yaml_key, yaml_val, expected, default):
    """Each new Settings field exists, has the right default, and honors config.yaml."""
    from yadgar.config import Settings, get_settings

    # Field exists on the model with the documented default.
    assert field in Settings.model_fields, f"{field} missing from Settings"
    monkeypatch.delenv(f"YADGAR_{field}", raising=False)
    _write_yaml("")  # empty config → default
    assert getattr(get_settings(), field) == default, f"{field} default should be {default}"

    # config.yaml value is respected.
    _write_yaml(f"{yaml_key}: {yaml_val}\n")
    assert getattr(get_settings(), field) == expected, (
        f"config.yaml {yaml_key}={yaml_val} must be respected for {field}"
    )


@pytest.mark.parametrize(
    ("field", "env_val", "expected"),
    [
        ("RERANKER_IDLE_UNLOAD_SEC", "111", 111.0),
        ("VACUUM_AUTO_COOLDOWN_HOURS", "2", 2.0),
    ],
)
def test_env_overrides_yaml(monkeypatch, field, env_val, expected):
    """Env still overrides config.yaml for the converted knobs (pydantic env source)."""
    from yadgar.config import get_settings

    monkeypatch.setenv(f"YADGAR_{field}", env_val)
    _write_yaml(f"{field.lower()}: 999\n")
    assert float(getattr(get_settings(), field)) == expected


def test_new_knobs_registered_in_registry():
    """Each converted knob has a _REGISTRY ConfigEntry (I25 three-way)."""
    from yadgar.config_registry import list_config

    names = {e.name for e in list_config()}
    for field in (
        "RERANKER_IDLE_UNLOAD_SEC",
        "RERANKER_IDLE_CHECK_INTERVAL_SEC",
        "HEALTH_HANDLER_TIMEOUT_SEC",
        "HEALTH_PROBE_TIMEOUT_SEC",
        "VACUUM_AUTO_COOLDOWN_HOURS",
    ):
        assert f"YADGAR_{field}" in names, f"YADGAR_{field} missing from _REGISTRY"
