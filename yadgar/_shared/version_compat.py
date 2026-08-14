"""Car F (task #61) — version-compat bounds + handshake check.

The core and the backend live on independent version tracks
(``yadgar/__init__.py::BACKEND_VERSION`` is read at install time from
``server.json``) but their wire protocol evolves together. Without a handshake,
a fresh-core-talking-to-old-backend (or vice versa) misbehaves silently — the
~101 s deploy window task #61 measured before the systemd ``BindsTo=`` and the
restart-order fix landed.

This module owns the SINGLE place the bounds live: the JSON sidecar
``yadgar/_shared/version_compat.json``. Both core and backend import it
(the backend imports via a relative path because the file ships in the
``yadgar._shared`` package both images build from), so a single edit widens
the accepted window for both sides.

The check is intentionally loose: major.minor only, never patch. A patch
release never changes the wire format, so pinning patch would force an
unrelated ``server.json`` bump on every bug fix. The interpreter deliberately
accepts ``"unknown"`` and the empty string as "unverifiable" rather than
"incompatible" — a fresh install that has not yet read its own version
must not refuse itself.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

# Anchored to the JSON sidecar so both core and backend build from the same
# source. Resolved at import time — the path is fixed by package layout.
_SIDECAR = Path(__file__).parent / "version_compat.json"

# major.minor[.patch] — patch is optional, suffix like "a1"/"rc1" tolerated
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")

# Anything that does not parse as a semver-ish tuple is unverifiable. The
# handshake must NOT refuse a peer it cannot read; it must surface the
# unverifiable status to the operator instead. Hard-failing here would loop
# a fresh install against itself.
_UNVERIFIABLE = frozenset({"", "unknown", "latest", "0.0.0"})


@observe(tier="stage")
def _load_bounds() -> dict[str, dict[str, str]]:
    """Read the sidecar; on any I/O / parse error, return a permissive fallback.

    The permissive fallback (empty strings → matches any non-``unknown``)
    means a missing or malformed sidecar DEGRADES the handshake to
    "unverifiable" rather than refusing the wire — never loop the daemon
    on its own config file.

    ``$schema`` and ``_comment`` keys (sidecar metadata, not bounds) are
    stripped so the in-process copy is a clean ``{core, backend}`` shape.
    """
    try:
        with _SIDECAR.open() as f:
            _raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("version_compat sidecar unreadable (%s); handshake unverifiable", exc)
        return {
            "core": {"min": "", "max": ""},
            "backend": {"min": "", "max": ""},
        }
    return {k: v for k, v in _raw.items() if k in {"core", "backend"}}


_BOUNDS: dict[str, dict[str, str]] = _load_bounds()


@observe(tier="stage")
def _parse(version: str) -> tuple[int, int, int] | None:
    """Return ``(major, minor, patch)`` or ``None`` if unverifiable.

    A patch of ``None`` is treated as 0 — semver's "absent patch = .0" rule,
    so ``5.65`` matches ``5.65.0`` exactly without widening to ``5.65.99``.
    """
    if not version or version in _UNVERIFIABLE:
        return None
    m = _VERSION_RE.match(version.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


@observe(tier="stage")
def _in_range(version: str, bounds: dict[str, str]) -> bool:
    """``version`` satisfies ``bounds = {"min": ..., "max": ...}``.

    Returns ``True`` for unverifiable inputs (matches the handshake contract:
    a peer we cannot read is not refused). Returns ``True`` when the bounds
    are empty (sidecar missing — see ``_load_bounds``).
    """
    parsed = _parse(version)
    if parsed is None:
        return True
    lo = _parse(bounds.get("min", ""))
    hi = _parse(bounds.get("max", ""))
    if lo is not None and parsed < lo:
        return False
    if hi is not None and parsed > hi:
        return False
    return True


@observe(tier="boundary")
def core_compatible(core_version: str) -> bool:
    """Is ``core_version`` within the supported core window?"""
    return _in_range(core_version, _BOUNDS.get("core", {}))


@observe(tier="boundary")
def backend_compatible(backend_version: str) -> bool:
    """Is ``backend_version`` within the supported backend window?"""
    return _in_range(backend_version, _BOUNDS.get("backend", {}))


def versions_compatible(core_version: str, backend_version: str) -> bool:
    """Top-level check the /health handshake calls.

    Both sides must be within their own bounds — a stale core cannot talk
    to a fresh backend, AND a fresh core cannot talk to a stale backend
    (the inverse pairing the orchestrator fix exists to prevent).
    """
    return core_compatible(core_version) and backend_compatible(backend_version)


@observe(tier="boundary")
def handshake_status(self_version: str, peer_version: str, *, side: str) -> dict:
    """Build the ``versions_compatible`` block the /health payload carries.

    ``side`` is ``"core"`` or ``"backend"`` — the SIDE PRODUCING this
    payload. ``peer_version`` is the remote peer's version as observed at
    handshake time (core reads it from the backend's /health response;
    backend reads it from the core's first MCP request header).

    The shape is intentionally identical on both sides so a single
    handshake can be parsed in either direction. The ``bounds`` field
    strips the sidecar's ``$schema`` and ``_comment`` keys so the
    payload is wire-clean (no JSON-schema or doc leak in /health).
    """
    if side == "core":
        self_ok = core_compatible(self_version)
        peer_ok = backend_compatible(peer_version)
    else:
        self_ok = backend_compatible(self_version)
        peer_ok = core_compatible(peer_version)
    return {
        "compatible": bool(self_ok and peer_ok),
        "self_version": self_version,
        "peer_version": peer_version,
        "bounds": {track: dict(b) for track, b in _BOUNDS.items()},
    }
