"""XDG-compliant path constants for yadgar.

Single source of truth for all file-system paths the daemon reads or writes.
All constants are resolved lazily via PEP 562 module ``__getattr__`` so that
``os.environ`` is read at *access time*, not at import time.  This means
``monkeypatch.setenv(...)`` in pytest fixtures works without needing to reload
the module.

Resolution hierarchy (highest priority first):
  1. Yadgar-specific env override (``YADGAR_DATA_DIR``, ``YADGAR_CONFIG_FILE``,
     ``YADGAR_DB_PATH``, ``YADGAR_LOG_DIR``, ``YADGAR_SECRETS_ENV_FILE``,
     ``YADGAR_SESSION_END_DIR``).
  2. XDG base-dir env var (``XDG_CONFIG_HOME``, ``XDG_DATA_HOME``,
     ``XDG_STATE_HOME``, ``XDG_CACHE_HOME``).
  3. XDG-spec absolute fallback (``~/.config``, ``~/.local/share``,
     ``~/.local/state``, ``~/.cache``).

Container installs set ``YADGAR_DATA_DIR=/data`` explicitly — that override
sits at tier 1 and wins over all XDG values.  Container-internal paths
(``/data``, ``/data/surreal_db``, ``/data/logs``) are unchanged by this
module.

Thread-safety: ``os.environ`` reads are atomic on CPython; ``Path``
construction is cheap and stateless.  No module-level mutable state is kept.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from yadgar._shared.observability.observe import observe

# ── XDG base-dir helpers ─────────────────────────────────────────────────────


@observe(tier="hot")
def _xdg_config_home() -> Path:
    """Return ``$XDG_CONFIG_HOME`` or ``~/.config``."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()


@observe(tier="hot")
def _xdg_data_home() -> Path:
    """Return ``$XDG_DATA_HOME`` or ``~/.local/share``."""
    return Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()


@observe(tier="hot")
def _xdg_state_home() -> Path:
    """Return ``$XDG_STATE_HOME`` or ``~/.local/state``."""
    return Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()


@observe(tier="hot")
def _xdg_cache_home() -> Path:
    """Return ``$XDG_CACHE_HOME`` or ``~/.cache``."""
    return Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser()


# ── Derived constant resolvers ────────────────────────────────────────────────


def _config_dir() -> Path:
    """``~/.config/yadgar/`` (respects ``XDG_CONFIG_HOME``)."""
    return _xdg_config_home() / "yadgar"


@observe(tier="stage")
def _data_dir() -> Path:
    """``~/.local/share/yadgar/`` or ``$YADGAR_DATA_DIR``."""
    override = os.environ.get("YADGAR_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return _xdg_data_home() / "yadgar"


def _state_dir() -> Path:
    """``~/.local/state/yadgar/`` (respects ``XDG_STATE_HOME``)."""
    return _xdg_state_home() / "yadgar"


def _cache_dir() -> Path:
    """``~/.cache/yadgar/`` (respects ``XDG_CACHE_HOME``)."""
    return _xdg_cache_home() / "yadgar"


@observe(tier="stage")
def _secrets_env_path() -> Path:
    """``~/.config/yadgar/secrets.env`` or ``$YADGAR_SECRETS_ENV_FILE``."""
    override = os.environ.get("YADGAR_SECRETS_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return _config_dir() / "secrets.env"


@observe(tier="stage")
def _config_yaml_path() -> Path:
    """``~/.config/yadgar/config.yaml`` or ``$YADGAR_CONFIG_FILE``."""
    override = os.environ.get("YADGAR_CONFIG_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return _config_dir() / "config.yaml"


@observe(tier="stage")
def _db_path() -> Path:
    """``~/.local/share/yadgar/surreal_db/`` or ``$YADGAR_DB_PATH``."""
    override = os.environ.get("YADGAR_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return _data_dir() / "surreal_db"


@observe(tier="stage")
def _log_dir() -> Path:
    """``~/.local/share/yadgar/logs/`` or ``$YADGAR_LOG_DIR``."""
    override = os.environ.get("YADGAR_LOG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return _data_dir() / "logs"


def _triggers_dir() -> Path:
    """``~/.local/state/yadgar/triggers/`` — written by vacuum_now(), watched by launchd."""
    return _state_dir() / "triggers"


@observe(tier="stage")
def _session_ends_dir() -> Path:
    """``~/.local/state/yadgar/session-ends/`` or ``$YADGAR_SESSION_END_DIR``."""
    override = os.environ.get("YADGAR_SESSION_END_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return _state_dir() / "session-ends"


def _quarantine_dir() -> Path:
    """``~/.local/state/yadgar/quarantine/``."""
    return _state_dir() / "quarantine"


def _secret_gate_allowlist_path() -> Path:
    """``~/.config/yadgar/secret-gate-allowlist.yaml``."""
    return _config_dir() / "secret-gate-allowlist.yaml"


def _secret_gate_audit_dir() -> Path:
    """``~/.local/state/yadgar/secret-gate-audit/``."""
    return _state_dir() / "secret-gate-audit"


def _stop_hook_state_path() -> Path:
    """``~/.local/state/yadgar/stop-hook-state.json``."""
    return _state_dir() / "stop-hook-state.json"


def _active_work_tracked_dir() -> Path:
    """``~/.local/state/yadgar/active-work-tracked/``."""
    return _state_dir() / "active-work-tracked"


def _pid_path() -> Path:
    """``~/.local/state/yadgar/yadgar.pid`` — runtime PID file."""
    return _state_dir() / "yadgar.pid"


# ── PEP 562 module __getattr__ ────────────────────────────────────────────────
# Exposes lazy constants as module attributes without module-level assignment.
# Evaluated on every access so monkeypatch.setenv() is honoured in tests.

_RESOLVERS: Final = {
    "CONFIG_DIR": _config_dir,
    "DATA_DIR": _data_dir,
    "STATE_DIR": _state_dir,
    "CACHE_DIR": _cache_dir,
    "SECRETS_ENV_PATH": _secrets_env_path,
    "CONFIG_YAML_PATH": _config_yaml_path,
    "DB_PATH": _db_path,
    "LOG_DIR": _log_dir,
    "TRIGGERS_DIR": _triggers_dir,
    "SESSION_ENDS_DIR": _session_ends_dir,
    "QUARANTINE_DIR": _quarantine_dir,
    "SECRET_GATE_ALLOWLIST_PATH": _secret_gate_allowlist_path,
    "SECRET_GATE_AUDIT_DIR": _secret_gate_audit_dir,
    "STOP_HOOK_STATE_PATH": _stop_hook_state_path,
    "ACTIVE_WORK_TRACKED_DIR": _active_work_tracked_dir,
    "PID_PATH": _pid_path,
}


def __getattr__(name: str) -> Path:
    resolver = _RESOLVERS.get(name)
    if resolver is not None:
        return resolver()
    raise AttributeError(f"module 'yadgar.paths' has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(globals()) + list(_RESOLVERS)
