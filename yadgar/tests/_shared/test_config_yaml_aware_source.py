"""Bug A (v5.89) — ConfigEntry yaml-aware source() + _raw_value() (TDD RED→GREEN).

3-way precedence (mirrors pydantic settings_customise_sources: env > yaml > default):
  source()      → "env"  if name in os.environ
                  "yaml" if the key is present in the loaded config.yaml
                  "default" otherwise
  _raw_value()  → env value  if set
                  yaml value (lowercase-rendered for bools) if present
                  default     otherwise

Also covers:
  - value() agrees with source() (no self-contradicting dict in as_dict()).
  - clear_config_caches() clears BOTH the yaml-present cache and get_settings cache.
"""

from __future__ import annotations

import pytest

from yadgar._shared.config.config_registry import ConfigEntry, clear_config_caches


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    """Point config.yaml at a temp file and clear caches before/after each test.

    The conftest autouse fixtures set YADGAR_CONFIG_FILE; we override it here so
    this test owns the file, then write through get_config_path() so we always
    target whatever path the loader actually resolves.
    """
    cfg = tmp_path / "yadgar-source-test.yaml"
    monkeypatch.setenv("YADGAR_CONFIG_FILE", str(cfg))
    clear_config_caches()
    yield
    clear_config_caches()


def _write_yaml(monkeypatch, tmp_path, body: str) -> None:
    from yadgar._shared.config.config_yaml import get_config_path

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    clear_config_caches()


# ---------------------------------------------------------------------------
# source() three-way
# ---------------------------------------------------------------------------


def test_source_default_when_unset(monkeypatch):
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)
    entry = ConfigEntry("YADGAR_VIZ_NODE_SIZE_3D", "8.0", "float")
    assert entry.source() == "default"
    assert entry._raw_value() == "8.0"


def test_source_env_when_env_set(monkeypatch):
    monkeypatch.setenv("YADGAR_VIZ_NODE_SIZE_3D", "12.0")
    entry = ConfigEntry("YADGAR_VIZ_NODE_SIZE_3D", "8.0", "float")
    assert entry.source() == "env"
    assert entry._raw_value() == "12.0"


def test_source_yaml_when_only_in_yaml(monkeypatch, tmp_path):
    """Key present in config.yaml (not env) → source='yaml', value from yaml."""
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)
    _write_yaml(monkeypatch, tmp_path, "viz_node_size_3d: 16.0\n")
    entry = ConfigEntry("YADGAR_VIZ_NODE_SIZE_3D", "8.0", "float")
    assert entry.source() == "yaml"
    assert entry._raw_value() == "16.0"


def test_env_wins_over_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("YADGAR_VIZ_NODE_SIZE_3D", "99.0")
    _write_yaml(monkeypatch, tmp_path, "viz_node_size_3d: 16.0\n")
    entry = ConfigEntry("YADGAR_VIZ_NODE_SIZE_3D", "8.0", "float")
    assert entry.source() == "env"
    assert entry._raw_value() == "99.0"


def test_yaml_bool_renders_lowercase(monkeypatch, tmp_path):
    """yaml bool true must stringify as 'true' (not Python 'True') — ADR-0013."""
    monkeypatch.delenv("YADGAR_UPDATE_CHECK_ON_START", raising=False)
    _write_yaml(monkeypatch, tmp_path, "update_check_on_start: true\n")
    entry = ConfigEntry("YADGAR_UPDATE_CHECK_ON_START", "false", "bool")
    assert entry.source() == "yaml"
    assert entry._raw_value() == "true"


def test_value_agrees_with_source_for_yaml(monkeypatch, tmp_path):
    """value()/as_dict() must agree with source() — no self-contradiction."""
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)
    _write_yaml(monkeypatch, tmp_path, "viz_node_size_3d: 16.0\n")
    entry = ConfigEntry("YADGAR_VIZ_NODE_SIZE_3D", "8.0", "float")
    d = entry.as_dict()
    assert d["source"] == "yaml"
    assert d["value"] == "16.0"


def test_surreal_alias_not_in_settings_resolves_env_or_default(monkeypatch, tmp_path):
    """Non-Settings aliases (SURREAL_*) never match the yaml set → env-or-default."""
    monkeypatch.delenv("SURREAL_USER", raising=False)
    # Even with a stray 'surreal_user' yaml key, it is not a Settings field → ignored.
    _write_yaml(monkeypatch, tmp_path, "surreal_user: someone\n")
    entry = ConfigEntry("SURREAL_USER", "root", "string")
    assert entry.source() == "default"
    assert entry._raw_value() == "root"


# ---------------------------------------------------------------------------
# cache-clear contract
# ---------------------------------------------------------------------------


def test_clear_config_caches_refreshes_yaml_present(monkeypatch, tmp_path):
    """Writing yaml then clearing caches must make source() flip default→yaml."""
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)
    entry = ConfigEntry("YADGAR_VIZ_NODE_SIZE_3D", "8.0", "float")
    assert entry.source() == "default"  # populates cache

    from yadgar._shared.config.config_yaml import get_config_path

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("viz_node_size_3d: 16.0\n")
    # Without clearing, stale cache may still say default — clearing is required.
    clear_config_caches()
    assert entry.source() == "yaml"


def test_clear_config_caches_clears_get_settings(monkeypatch):
    """clear_config_caches() must also clear the get_settings lru_cache."""
    from yadgar._shared import config as config_mod

    config_mod.get_settings()  # populate
    assert config_mod.get_settings.cache_info().currsize == 1
    clear_config_caches()
    assert config_mod.get_settings.cache_info().currsize == 0
