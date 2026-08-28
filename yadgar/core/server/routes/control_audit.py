"""Control-API audit log + destructive-knob classifier + restart rate-limit (Car D).

This module backs three config-panel safety features:

  1. ``is_destructive(name)`` — reads the additive ``"destructive"`` key on a
     FIELD_META entry so the POST /config handler can 428-gate destructive knobs
     (retention windows, cold-memory purge, DLQ pruning) unless the caller sends
     ``"armed": true``.
  2. ``audit_config_event(...)`` — appends ONE JSONL line per config write /
     restart / action to ``$XDG_STATE_HOME/yadgar/config-audit.jsonl`` (D2).
  3. In-memory restart rate-limit (``restart_rate_limited`` / ``stamp_restart``,
     D2) so a single-process daemon can't be restart-flooded.

ACTOR IDENTITY (ADR-0013): the control API uses Bearer-token auth, which carries
NO caller principal. The audit "actor" is therefore best-effort only — the
client remote address and User-Agent header — NOT an authenticated identity. Do
not treat the audit ``client`` / ``user_agent`` fields as a proven principal.

Dependency direction: this module imports NOTHING from ``control.py`` (control.py
imports ``is_destructive`` from here). The state-dir is re-derived locally to
avoid a circular import.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Restart rate-limit state (D2). Declared in D1 so the test-reset hook exists.
# ---------------------------------------------------------------------------
# In-memory, single-process daemon — no new infra. Keyed by service name,
# value = time.monotonic() of the last SUCCESSFUL restart sentinel write.
_last_restart: dict[str, float] = {}

# Default rate-limit window (seconds) for restart requests.
_RESTART_WINDOW_S: float = 30.0

# ---------------------------------------------------------------------------
# Audit-log JSONL sink.
# ---------------------------------------------------------------------------
# A dedicated named logger with propagate=False so its span-end log (emitted by
# @observe under a RECORDING tracer) can NEVER reach this handler — no log→span→
# log amplification (ADR-0041: the log-emission SUBSYSTEM is @observe-exempt, but
# a FEATURE fn that writes a log is decorated normally + isolated via propagate).
_AUDIT_LOGGER_NAME = "yadgar.control.config_audit"
# The RotatingJSONLFileHandler binds baseFilename at ctor, but XDG_STATE_HOME can
# change between calls (per-test isolation). Cache the handler keyed on its
# resolved path; rebuild + re-attach when the resolved path differs.
_audit_handler_path: str | None = None


@observe(tier="stage")
def _audit_state_dir() -> Path:
    """Return the XDG state dir (re-derived per call; never cached).

    Copies control._sentinel_dir's body so this module imports NOTHING from
    control.py (one-way dependency: control → control_audit only). STATE_DIR is a
    PEP-562 lazy attribute that reads XDG_STATE_HOME at access time.
    """
    import yadgar._shared.paths as _paths  # noqa: PLC0415

    return Path(str(_paths.STATE_DIR))


@observe(tier="stage")
def _audit_logger() -> logging.Logger:
    """Return the dedicated audit logger, (re)pointing its JSONL handler at the
    currently-resolved state dir. Rebuilds the handler when the path changes."""
    global _audit_handler_path

    audit_log = logging.getLogger(_AUDIT_LOGGER_NAME)
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False  # isolate from root → no span-end log feedback

    target_dir = _audit_state_dir()
    target_path = str(target_dir / "config-audit.jsonl")
    if _audit_handler_path == target_path and audit_log.handlers:
        return audit_log

    # Path changed (or first use) — drop stale handlers, attach a fresh one.
    for h in list(audit_log.handlers):
        audit_log.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001 — handler teardown: a logging handler's close() is third-party code and one failing handler must not abort the close of the rest
            pass
    target_dir.mkdir(parents=True, exist_ok=True)

    from yadgar._shared.observability.log_config import (
        RotatingJSONLFileHandler,  # noqa: PLC0415 — call-site import
    )

    handler = RotatingJSONLFileHandler(target_path, logger_name="config_audit")
    audit_log.addHandler(handler)
    _audit_handler_path = target_path
    return audit_log


@observe(tier="boundary")
def audit_config_event(
    kind: str,
    name: str | None,
    old: Any,
    new: Any,
    status: int,
    request: Any,
    *,
    armed: bool | None = None,
) -> None:
    """Append ONE JSONL audit line for a config write / restart / action.

    kind: one of "config_write" | "restart" | "action".
    old/new: the value before / the value attempted-or-persisted.
    status: the HTTP status the handler is about to return.
    request: the Starlette Request — only remote host + User-Agent are read.

    ACTOR IDENTITY (ADR-0013): Bearer auth carries no principal. ``client`` and
    ``user_agent`` are best-effort attribution, NOT a proven identity.
    """
    client_host = None
    user_agent = None
    try:
        client = getattr(request, "client", None)
        client_host = getattr(client, "host", None)
        headers = getattr(request, "headers", {}) or {}
        user_agent = headers.get("user-agent")
    except Exception:  # noqa: BLE001 — audit must never break the request path
        pass

    # Fields ride as extra= so the I14 JSONLogFormatter propagates them as
    # top-level JSON keys (it already emits ts/level/event). old/new are
    # str()-coerced so a bool/None value can't break JSON serialisation.
    # NB: the knob name is emitted as "knob" (NOT "name") — "name" is a reserved
    # LogRecord attribute and logging raises if extra= tries to overwrite it.
    extra = {
        "kind": kind,
        "knob": name,
        "old": None if old is None else str(old),
        "new": None if new is None else str(new),
        "status": int(status),
        "armed": armed,
        "client": client_host,
        "user_agent": user_agent,
    }
    try:
        _audit_logger().info("config_audit", extra=extra)
    except Exception as exc:  # noqa: BLE001 — never let audit failure 500 the write
        logger.warning("config-audit append failed: %s", exc)


@observe(tier="stage")
def restart_rate_limited(service: str, *, window: float = _RESTART_WINDOW_S) -> bool:
    """Return True if a successful restart for ``service`` happened < window ago.

    Uses time.monotonic() (immune to wall-clock jumps). In-memory only.
    """
    last = _last_restart.get(service)
    if last is None:
        return False
    return (time.monotonic() - last) < window


@observe(tier="stage")
def stamp_restart(service: str) -> None:
    """Record a successful restart for ``service`` (starts the rate-limit window)."""
    _last_restart[service] = time.monotonic()


@observe(tier="stage")
def is_destructive(name: str) -> bool:
    """Return True iff the knob's FIELD_META entry carries ``destructive: True``.

    ``name`` is the env-var form (e.g. ``YADGAR_MEMORY_ARCHIVE_RETENTION_DAYS``);
    it is normalised to the FIELD_META key (lowercase, no ``YADGAR_`` prefix),
    mirroring ``control._enrich_knob``'s lookup style. Unknown knobs → False.
    """
    from yadgar._shared.config.config_yaml import (
        FIELD_META,  # noqa: PLC0415 — keep import at call site (mirror _enrich_knob)
    )

    meta = FIELD_META.get(name.removeprefix("YADGAR_").lower(), {})
    return bool(meta.get("destructive", False))
