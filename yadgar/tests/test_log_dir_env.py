"""Tests for YADGAR_LOG_DIR env-configurable log directory (v5.6.7 PR-M).

TDD: these tests are written before implementation. They exercise the
_resolve_log_dir() helper and its interaction with _resolve_log_file_path().
"""

from __future__ import annotations

import os
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helper imports — these will NameError until implementation exists.
# ---------------------------------------------------------------------------


def _import_helpers():
    """Lazy import so individual tests can show the specific error."""
    from yadgar.log_config import _resolve_log_dir, _resolve_log_file_path  # noqa: PLC0415

    return _resolve_log_dir, _resolve_log_file_path


# ---------------------------------------------------------------------------
# 1. Default (env unset) returns expanded ~/.yadgar/logs
# ---------------------------------------------------------------------------


def test_resolve_log_dir_default_is_home_dir(monkeypatch, tmp_path):
    """Without YADGAR_LOG_DIR, returns ~/.local/share/yadgar/logs (XDG default)."""
    _resolve_log_dir, _ = _import_helpers()
    monkeypatch.delenv("YADGAR_LOG_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("YADGAR_DATA_DIR", raising=False)
    # Patch makedirs so no real dir is created during test
    with patch("os.makedirs"):
        result = _resolve_log_dir()
    expected = os.path.expanduser("~/.local/share/yadgar/logs")
    assert result == expected


# ---------------------------------------------------------------------------
# 2. Env set → returns that path
# ---------------------------------------------------------------------------


def test_resolve_log_dir_env_override(monkeypatch):
    """YADGAR_LOG_DIR=/tmp/test-yadgar-logs → result is that path."""
    _resolve_log_dir, _ = _import_helpers()
    monkeypatch.setenv("YADGAR_LOG_DIR", "/tmp/test-yadgar-logs")
    with patch("os.makedirs"):
        result = _resolve_log_dir()
    assert result == "/tmp/test-yadgar-logs"


# ---------------------------------------------------------------------------
# 3. PermissionError on makedirs → fallback + stderr warning
# ---------------------------------------------------------------------------


def test_resolve_log_dir_permission_error_falls_back(monkeypatch, capsys):
    """When makedirs raises PermissionError, falls back to /tmp/yadgar-logs/."""
    _resolve_log_dir, _ = _import_helpers()
    monkeypatch.setenv("YADGAR_LOG_DIR", "/no-permission-path")

    fallback = "/tmp/yadgar-logs"

    def selective_makedirs(path, **kwargs):
        if path != fallback:
            raise PermissionError(f"Permission denied: {path}")
        # fallback path creation succeeds silently

    with patch("os.makedirs", side_effect=selective_makedirs):
        result = _resolve_log_dir()

    assert result == fallback
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower() or "YADGAR_LOG_DIR" in captured.err


# ---------------------------------------------------------------------------
# 4. Idempotency — calling twice with dir already existing does not error
# ---------------------------------------------------------------------------


def test_resolve_log_dir_idempotent(monkeypatch, tmp_path):
    """Calling _resolve_log_dir() twice for an existing dir does not error."""
    _resolve_log_dir, _ = _import_helpers()
    log_dir = str(tmp_path / "yadgar-logs")
    monkeypatch.setenv("YADGAR_LOG_DIR", log_dir)

    # First call creates the dir
    _resolve_log_dir()
    assert os.path.isdir(log_dir)

    # Second call must not raise (exist_ok=True required internally)
    _resolve_log_dir()


# ---------------------------------------------------------------------------
# 5. Single env var affects both core and backend log file paths
# ---------------------------------------------------------------------------


def test_single_env_affects_both_processes(monkeypatch):
    """YADGAR_LOG_DIR change propagates to both core and backend log paths."""
    _, _resolve_log_file_path = _import_helpers()
    monkeypatch.setenv("YADGAR_LOG_DIR", "/tmp/shared-log-dir")
    # Ensure per-file overrides are not set so YADGAR_LOG_DIR takes effect
    monkeypatch.delenv("YADGAR_LOG_FILE_PATH", raising=False)
    monkeypatch.delenv("YADGAR_BACKEND_LOG_FILE_PATH", raising=False)

    with patch("os.makedirs"):
        core_path = _resolve_log_file_path("core")
        backend_path = _resolve_log_file_path("backend")

    assert core_path.startswith("/tmp/shared-log-dir")
    assert backend_path.startswith("/tmp/shared-log-dir")
    assert core_path != backend_path  # different filenames (yadgar.log vs backend.log)


# ---------------------------------------------------------------------------
# 6. File handler is opt-in — no env var → no handler installed
# ---------------------------------------------------------------------------


def test_file_handler_not_installed_when_env_unset(monkeypatch):
    """Without any YADGAR_LOG_DIR / YADGAR_LOG_FILE_PATH, _install_file_handler returns None."""
    import logging

    from yadgar.log_config import RotatingJSONLFileHandler, _install_file_handler  # noqa: PLC0415

    # Clear all file-handler gates
    monkeypatch.delenv("YADGAR_LOG_DIR", raising=False)
    monkeypatch.delenv("YADGAR_LOG_FILE_PATH", raising=False)
    monkeypatch.delenv("YADGAR_BACKEND_LOG_FILE_PATH", raising=False)

    # Remove any pre-existing file handlers so the idempotency guard doesn't skew result
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, RotatingJSONLFileHandler):
            h.close()
            root.removeHandler(h)

    # Call _install_file_handler directly — avoids configure_logging side-effects
    # (request logger mutations, root handler resets) that would leak into other tests.
    dummy_formatter = logging.Formatter()
    result = _install_file_handler(dummy_formatter, "core")

    assert result is None, (
        "_install_file_handler must return None when no YADGAR_LOG_DIR/YADGAR_LOG_FILE_PATH is set"
    )
    file_handlers = [h for h in root.handlers if isinstance(h, RotatingJSONLFileHandler)]
    assert len(file_handlers) == 0, (
        "No file handler should be installed when YADGAR_LOG_DIR/YADGAR_LOG_FILE_PATH are unset"
    )
