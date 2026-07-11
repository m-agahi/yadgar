"""v5.95.0 config-integrity: core cluster env-only knobs wired to resolve_knob.

Per knob: (a) env override wins; (b) config.yaml respected when env unset.

Knobs covered:
  1. LOG_FORMAT          — log_config.configure_logging / server._app
  2. METRICS_ENABLED     — metrics._is_metrics_enabled
  3. DEBUG_APIS_ENABLED  — auth_middleware._is_debug_apis_enabled
                        — server.routes.logs._is_debug_apis_enabled
  4. UPDATE_DEBUG_APIS_ENABLED — server.routes.control_update._is_debug_apis_enabled
  5. AUTO_CAPTURE_RATE_LIMIT   — server._state._get_auto_capture_rate_limit
  6. SENSITIVE_LOCK_TTL_SEC    — sensitive_lock._ttl_seconds
  7. HEALTH_READINESS_FAIL_THRESHOLD — server.http._readiness_fail_threshold
  8. ALLOWED_ORIGINS     — server._app._get_allowed_origins
  9. UPDATE_CHECK_ON_START — server.lifecycle._maybe_auto_check_for_update (via resolve_knob)
"""

from __future__ import annotations

import pytest

from yadgar._shared.config.config_registry import clear_config_caches

# ---------------------------------------------------------------------------
# Shared fixture (mirrors test_config_yaml_aware_source.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    """Isolate config.yaml to a temp file; clear caches before/after each test."""
    cfg = tmp_path / "yadgar-core-integrity-test.yaml"
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
# 1. LOG_FORMAT — log_config.configure_logging
# ---------------------------------------------------------------------------


def test_log_format_env_override(monkeypatch):
    """YADGAR_LOG_FORMAT env wins — configure_logging uses resolve_knob."""
    monkeypatch.setenv("YADGAR_LOG_FORMAT", "text")
    # Access the knob indirectly through the private resolve path.
    # configure_logging stores the result in log_format local; test via resolve_knob directly.
    from yadgar._shared.config import resolve_knob

    val = resolve_knob("YADGAR_LOG_FORMAT", "LOG_FORMAT", str, "json").lower()
    assert val == "text"


def test_log_format_yaml_respected(monkeypatch, tmp_path):
    """When YADGAR_LOG_FORMAT unset, config.yaml value is used."""
    monkeypatch.delenv("YADGAR_LOG_FORMAT", raising=False)
    _write_yaml(monkeypatch, tmp_path, "log_format: text\n")
    from yadgar._shared.config import resolve_knob

    val = resolve_knob("YADGAR_LOG_FORMAT", "LOG_FORMAT", str, "json").lower()
    assert val == "text"


# ---------------------------------------------------------------------------
# 2. METRICS_ENABLED — metrics._is_metrics_enabled
# ---------------------------------------------------------------------------


def test_metrics_enabled_env_override_false(monkeypatch):
    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "0")
    from yadgar._shared.observability.metrics import _is_metrics_enabled

    assert _is_metrics_enabled() is False


def test_metrics_enabled_env_override_true(monkeypatch):
    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "true")
    from yadgar._shared.observability.metrics import _is_metrics_enabled

    assert _is_metrics_enabled() is True


def test_metrics_enabled_yaml_respected(monkeypatch, tmp_path):
    """config.yaml metrics_enabled: false respected when env unset."""
    monkeypatch.delenv("YADGAR_METRICS_ENABLED", raising=False)
    _write_yaml(monkeypatch, tmp_path, "metrics_enabled: false\n")
    from yadgar._shared.observability.metrics import _is_metrics_enabled

    assert _is_metrics_enabled() is False


# ---------------------------------------------------------------------------
# 3. DEBUG_APIS_ENABLED — auth_middleware._is_debug_apis_enabled
# ---------------------------------------------------------------------------


def test_debug_apis_enabled_env_override_true(monkeypatch):
    monkeypatch.setenv("YADGAR_DEBUG_APIS_ENABLED", "1")
    from yadgar.core.auth_middleware import _is_debug_apis_enabled

    assert _is_debug_apis_enabled() is True


def test_debug_apis_enabled_env_override_false(monkeypatch):
    monkeypatch.setenv("YADGAR_DEBUG_APIS_ENABLED", "false")
    from yadgar.core.auth_middleware import _is_debug_apis_enabled

    assert _is_debug_apis_enabled() is False


def test_debug_apis_enabled_yaml_respected(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_DEBUG_APIS_ENABLED", raising=False)
    _write_yaml(monkeypatch, tmp_path, "debug_apis_enabled: true\n")
    from yadgar.core.auth_middleware import _is_debug_apis_enabled

    assert _is_debug_apis_enabled() is True


# ---------------------------------------------------------------------------
# 3b. DEBUG_APIS_ENABLED — server.routes.logs._is_debug_apis_enabled (same field)
# ---------------------------------------------------------------------------


def test_logs_debug_apis_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_DEBUG_APIS_ENABLED", "on")
    from yadgar.core.server.routes.logs import _is_debug_apis_enabled

    assert _is_debug_apis_enabled() is True


def test_logs_debug_apis_yaml_respected(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_DEBUG_APIS_ENABLED", raising=False)
    _write_yaml(monkeypatch, tmp_path, "debug_apis_enabled: true\n")
    from yadgar.core.server.routes.logs import _is_debug_apis_enabled

    assert _is_debug_apis_enabled() is True


# ---------------------------------------------------------------------------
# 4. UPDATE_DEBUG_APIS_ENABLED — server.routes.control_update._is_debug_apis_enabled
# ---------------------------------------------------------------------------


def test_update_debug_apis_env_override_on(monkeypatch):
    monkeypatch.setenv("YADGAR_UPDATE_DEBUG_APIS_ENABLED", "on")
    from yadgar.core.server.routes.control_update import _is_debug_apis_enabled

    assert _is_debug_apis_enabled() is True


def test_update_debug_apis_env_override_off(monkeypatch):
    monkeypatch.setenv("YADGAR_UPDATE_DEBUG_APIS_ENABLED", "off")
    from yadgar.core.server.routes.control_update import _is_debug_apis_enabled

    assert _is_debug_apis_enabled() is False


def test_update_debug_apis_yaml_respected(monkeypatch, tmp_path):
    """config.yaml update_debug_apis_enabled: on respected when env unset."""
    monkeypatch.delenv("YADGAR_UPDATE_DEBUG_APIS_ENABLED", raising=False)
    _write_yaml(monkeypatch, tmp_path, "update_debug_apis_enabled: on\n")
    from yadgar.core.server.routes.control_update import _is_debug_apis_enabled

    assert _is_debug_apis_enabled() is True


# ---------------------------------------------------------------------------
# 5. AUTO_CAPTURE_RATE_LIMIT — server._state._get_auto_capture_rate_limit
# ---------------------------------------------------------------------------


def test_auto_capture_rate_limit_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_AUTO_CAPTURE_RATE_LIMIT", "99")
    from yadgar._shared.runtime.state import _get_auto_capture_rate_limit

    assert _get_auto_capture_rate_limit() == 99


def test_auto_capture_rate_limit_yaml_respected(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_AUTO_CAPTURE_RATE_LIMIT", raising=False)
    _write_yaml(monkeypatch, tmp_path, "auto_capture_rate_limit: 60\n")
    from yadgar._shared.runtime.state import _get_auto_capture_rate_limit

    assert _get_auto_capture_rate_limit() == 60


# ---------------------------------------------------------------------------
# 6. SENSITIVE_LOCK_TTL_SEC — sensitive_lock._ttl_seconds
# ---------------------------------------------------------------------------


def test_sensitive_lock_ttl_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_SENSITIVE_LOCK_TTL_SEC", "3600")
    from yadgar.core.sensitive_lock import _ttl_seconds

    assert _ttl_seconds() == 3600.0


def test_sensitive_lock_ttl_yaml_respected(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_SENSITIVE_LOCK_TTL_SEC", raising=False)
    _write_yaml(monkeypatch, tmp_path, "sensitive_lock_ttl_sec: 1800\n")
    from yadgar.core.sensitive_lock import _ttl_seconds

    assert _ttl_seconds() == 1800.0


def test_sensitive_lock_ttl_default(monkeypatch):
    monkeypatch.delenv("YADGAR_SENSITIVE_LOCK_TTL_SEC", raising=False)
    from yadgar.core.sensitive_lock import _ttl_seconds

    # Default is 7200 when no env and no yaml
    assert _ttl_seconds() == 7200.0


# ---------------------------------------------------------------------------
# 7. HEALTH_READINESS_FAIL_THRESHOLD — server.http._readiness_fail_threshold
# ---------------------------------------------------------------------------


def test_readiness_fail_threshold_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "7")
    from yadgar.core.server.http import _readiness_fail_threshold

    assert _readiness_fail_threshold() == 7


def test_readiness_fail_threshold_yaml_respected(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", raising=False)
    _write_yaml(monkeypatch, tmp_path, "health_readiness_fail_threshold: 5\n")
    from yadgar.core.server.http import _readiness_fail_threshold

    assert _readiness_fail_threshold() == 5


# ---------------------------------------------------------------------------
# 8. ALLOWED_ORIGINS — server._app._get_allowed_origins
# ---------------------------------------------------------------------------


def test_allowed_origins_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_ALLOWED_ORIGINS", "https://example.com,https://other.com")
    from yadgar.core.server._app import _get_allowed_origins

    result = _get_allowed_origins()
    assert "https://example.com" in result
    assert "https://other.com" in result


def test_allowed_origins_yaml_respected(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_ALLOWED_ORIGINS", raising=False)
    _write_yaml(monkeypatch, tmp_path, "allowed_origins: https://yaml-origin.com\n")
    from yadgar.core.server._app import _get_allowed_origins

    result = _get_allowed_origins()
    assert "https://yaml-origin.com" in result


# ---------------------------------------------------------------------------
# 9. UPDATE_CHECK_ON_START — test via resolve_knob directly (lifecycle fn spawns threads)
# ---------------------------------------------------------------------------


def test_update_check_on_start_env_override_true(monkeypatch):
    monkeypatch.setenv("YADGAR_UPDATE_CHECK_ON_START", "true")
    from yadgar._shared.config import resolve_knob

    val = resolve_knob(
        "YADGAR_UPDATE_CHECK_ON_START",
        "UPDATE_CHECK_ON_START",
        lambda s: s.strip().lower() in ("1", "true", "yes", "on"),
        False,
    )
    assert val is True


def test_update_check_on_start_env_override_false(monkeypatch):
    monkeypatch.setenv("YADGAR_UPDATE_CHECK_ON_START", "false")
    from yadgar._shared.config import resolve_knob

    val = resolve_knob(
        "YADGAR_UPDATE_CHECK_ON_START",
        "UPDATE_CHECK_ON_START",
        lambda s: s.strip().lower() in ("1", "true", "yes", "on"),
        False,
    )
    assert val is False


def test_update_check_on_start_yaml_respected(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_UPDATE_CHECK_ON_START", raising=False)
    _write_yaml(monkeypatch, tmp_path, "update_check_on_start: true\n")
    from yadgar._shared.config import resolve_knob

    val = resolve_knob(
        "YADGAR_UPDATE_CHECK_ON_START",
        "UPDATE_CHECK_ON_START",
        lambda s: s.strip().lower() in ("1", "true", "yes", "on"),
        False,
    )
    assert val is True
