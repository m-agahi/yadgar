"""Unit tests for yadgar.paths — XDG path constant resolution.

Tests verify:
  - XDG fallbacks when XDG env vars are unset.
  - XDG env vars are respected when set.
  - Yadgar-specific env overrides take precedence over XDG defaults.
  - Constants are lazy (monkeypatch works without module reload).
  - PID_PATH lives in STATE_DIR (plan miss fixed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import yadgar.paths as paths

# ── CONFIG_DIR ────────────────────────────────────────────────────────────────


def test_config_dir_default(monkeypatch):
    """CONFIG_DIR falls back to ~/.config/yadgar when XDG_CONFIG_HOME unset."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    expected = Path.home() / ".config" / "yadgar"
    assert paths.CONFIG_DIR == expected


def test_config_dir_respects_xdg_config_home(monkeypatch, tmp_path):
    """CONFIG_DIR uses XDG_CONFIG_HOME when set."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgconf"))
    assert paths.CONFIG_DIR == tmp_path / "xdgconf" / "yadgar"


# ── DATA_DIR ──────────────────────────────────────────────────────────────────


def test_data_dir_default(monkeypatch):
    """DATA_DIR falls back to ~/.local/share/yadgar when XDG_DATA_HOME unset."""
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("YADGAR_DATA_DIR", raising=False)
    expected = Path.home() / ".local" / "share" / "yadgar"
    assert paths.DATA_DIR == expected


def test_data_dir_respects_xdg_data_home(monkeypatch, tmp_path):
    """DATA_DIR uses XDG_DATA_HOME when set (no YADGAR_DATA_DIR override)."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdgdata"))
    monkeypatch.delenv("YADGAR_DATA_DIR", raising=False)
    assert paths.DATA_DIR == tmp_path / "xdgdata" / "yadgar"


def test_yadgar_data_dir_override_wins(monkeypatch, tmp_path):
    """YADGAR_DATA_DIR takes precedence over XDG_DATA_HOME default."""
    override = tmp_path / "override_data"
    monkeypatch.setenv("YADGAR_DATA_DIR", str(override))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdgdata"))
    assert paths.DATA_DIR == override


# ── STATE_DIR ─────────────────────────────────────────────────────────────────


def test_state_dir_default(monkeypatch):
    """STATE_DIR falls back to ~/.local/state/yadgar when XDG_STATE_HOME unset."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    expected = Path.home() / ".local" / "state" / "yadgar"
    assert paths.STATE_DIR == expected


def test_state_dir_respects_xdg_state_home(monkeypatch, tmp_path):
    """STATE_DIR uses XDG_STATE_HOME when set."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdgstate"))
    assert paths.STATE_DIR == tmp_path / "xdgstate" / "yadgar"


# ── CONFIG_YAML_PATH ─────────────────────────────────────────────────────────


def test_config_yaml_path_default(monkeypatch):
    """CONFIG_YAML_PATH defaults to CONFIG_DIR/config.yaml."""
    monkeypatch.delenv("YADGAR_CONFIG_FILE", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    expected = Path.home() / ".config" / "yadgar" / "config.yaml"
    assert paths.CONFIG_YAML_PATH == expected


def test_yadgar_config_file_override(monkeypatch, tmp_path):
    """YADGAR_CONFIG_FILE takes precedence over CONFIG_YAML_PATH default."""
    cfg = tmp_path / "custom_config.yaml"
    monkeypatch.setenv("YADGAR_CONFIG_FILE", str(cfg))
    assert paths.CONFIG_YAML_PATH == cfg


# ── SECRETS_ENV_PATH ─────────────────────────────────────────────────────────


def test_secrets_env_path_default(monkeypatch):
    """SECRETS_ENV_PATH defaults to CONFIG_DIR/secrets.env."""
    monkeypatch.delenv("YADGAR_SECRETS_ENV_FILE", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    expected = Path.home() / ".config" / "yadgar" / "secrets.env"
    assert paths.SECRETS_ENV_PATH == expected


def test_yadgar_secrets_env_file_override(monkeypatch, tmp_path):
    """YADGAR_SECRETS_ENV_FILE override wins."""
    sec = tmp_path / "custom_secrets.env"
    monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(sec))
    assert paths.SECRETS_ENV_PATH == sec


# ── DB_PATH ───────────────────────────────────────────────────────────────────


def test_db_path_default(monkeypatch):
    """DB_PATH defaults to DATA_DIR/surreal_db."""
    monkeypatch.delenv("YADGAR_DB_PATH", raising=False)
    monkeypatch.delenv("YADGAR_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    expected = Path.home() / ".local" / "share" / "yadgar" / "surreal_db"
    assert paths.DB_PATH == expected


def test_yadgar_db_path_override(monkeypatch, tmp_path):
    """YADGAR_DB_PATH override wins over XDG default."""
    db = tmp_path / "custom_db"
    monkeypatch.setenv("YADGAR_DB_PATH", str(db))
    assert paths.DB_PATH == db


# ── LOG_DIR ───────────────────────────────────────────────────────────────────


def test_log_dir_default(monkeypatch):
    """LOG_DIR defaults to DATA_DIR/logs."""
    monkeypatch.delenv("YADGAR_LOG_DIR", raising=False)
    monkeypatch.delenv("YADGAR_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    expected = Path.home() / ".local" / "share" / "yadgar" / "logs"
    assert paths.LOG_DIR == expected


def test_yadgar_log_dir_override(monkeypatch, tmp_path):
    """YADGAR_LOG_DIR override wins."""
    log = tmp_path / "custom_logs"
    monkeypatch.setenv("YADGAR_LOG_DIR", str(log))
    assert paths.LOG_DIR == log


# ── STATE sub-paths ───────────────────────────────────────────────────────────


def test_triggers_dir_under_state(monkeypatch):
    """TRIGGERS_DIR is STATE_DIR/triggers."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    expected = Path.home() / ".local" / "state" / "yadgar" / "triggers"
    assert paths.TRIGGERS_DIR == expected


def test_stop_hook_state_path_under_state(monkeypatch):
    """STOP_HOOK_STATE_PATH is STATE_DIR/stop-hook-state.json."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    expected = Path.home() / ".local" / "state" / "yadgar" / "stop-hook-state.json"
    assert paths.STOP_HOOK_STATE_PATH == expected


def test_pid_path_under_state(monkeypatch):
    """PID_PATH is STATE_DIR/yadgar.pid (plan miss — not in §2.2 inventory)."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    expected = Path.home() / ".local" / "state" / "yadgar" / "yadgar.pid"
    assert paths.PID_PATH == expected


# ── Laziness / precedence ─────────────────────────────────────────────────────


def test_lazy_resolution_env_set_after_import(monkeypatch, tmp_path):
    """Constants are evaluated at access time — env changes after import take effect."""
    # Remove override, confirm default
    monkeypatch.delenv("YADGAR_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    default = paths.DATA_DIR
    assert "local" in str(default)

    # Now set override — no reload needed
    override = tmp_path / "late_override"
    monkeypatch.setenv("YADGAR_DATA_DIR", str(override))
    assert paths.DATA_DIR == override


def test_unknown_attr_raises_attribute_error():
    """Accessing a non-existent attribute raises AttributeError."""
    with pytest.raises(AttributeError):
        _ = paths.NONEXISTENT_CONSTANT


def test_dir_includes_all_constants():
    """dir(paths) includes all public path constants."""
    d = dir(paths)
    for name in [
        "CONFIG_DIR",
        "DATA_DIR",
        "STATE_DIR",
        "CACHE_DIR",
        "SECRETS_ENV_PATH",
        "CONFIG_YAML_PATH",
        "DB_PATH",
        "LOG_DIR",
        "TRIGGERS_DIR",
        "SESSION_ENDS_DIR",
        "QUARANTINE_DIR",
        "SECRET_GATE_ALLOWLIST_PATH",
        "SECRET_GATE_AUDIT_DIR",
        "STOP_HOOK_STATE_PATH",
        "ACTIVE_WORK_TRACKED_DIR",
        "PID_PATH",
    ]:
        assert name in d, f"{name} missing from dir(paths)"
