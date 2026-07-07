"""Tests for core_log_level and backend_log_level Settings fields (Change 3)."""

from yadgar._shared.config import Settings


def test_core_log_level_default():
    s = Settings()
    assert s.CORE_LOG_LEVEL == "warn"


def test_backend_log_level_default():
    s = Settings()
    assert s.BACKEND_LOG_LEVEL == "warn"


def test_core_log_level_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_CORE_LOG_LEVEL", "info")
    s = Settings()
    assert s.CORE_LOG_LEVEL == "info"


def test_backend_log_level_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_BACKEND_LOG_LEVEL", "debug")
    s = Settings()
    assert s.BACKEND_LOG_LEVEL == "debug"


def test_log_level_fields_are_strings():
    s = Settings()
    assert isinstance(s.CORE_LOG_LEVEL, str)
    assert isinstance(s.BACKEND_LOG_LEVEL, str)
