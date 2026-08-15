"""Car F (task #61) — version-handshake rejection test.

The handshake is a single field on the existing ``/health`` payload; this
test pins the contract: mismatched versions are refused. The
in-range / unverifiable / range-boundary cases live next to the module
they exercise (``yadgar._shared.version_compat``) in
``test_version_compat_module.py`` — the in-line payload test here is
narrow: the helper the route calls returns the right shape.
"""

from __future__ import annotations

from unittest.mock import patch

from yadgar._shared.version_compat import (
    _BOUNDS,
    backend_compatible,
    core_compatible,
    handshake_status,
    versions_compatible,
)


def test_versions_compatible_in_range_passes() -> None:
    # Both well inside the sidecar's supported window.
    assert versions_compatible("5.182.0", "5.73.0") is True
    assert core_compatible("5.182.0") is True
    assert backend_compatible("5.73.0") is True


def test_versions_compatible_mismatch_refused() -> None:
    # Core too old — outside the supported window. The handshake must
    # refuse regardless of the backend side.
    assert versions_compatible("5.0.0", "5.73.0") is False
    # Backend too old — same class.
    assert versions_compatible("5.182.0", "5.0.0") is False
    # Both too new — a wire-incompatible future version.
    assert versions_compatible("99.0.0", "99.0.0") is False


def test_handshake_status_shape() -> None:
    # The field the /health payload adds has a stable shape so a single
    # parser can consume either side of the wire.
    block = handshake_status("5.182.0", "5.73.0", side="core")
    assert set(block) == {"compatible", "self_version", "peer_version", "bounds"}
    assert block["self_version"] == "5.182.0"
    assert block["peer_version"] == "5.73.0"
    assert block["compatible"] is True
    # Read the bound from the sidecar rather than pinning a literal: this
    # test is about the SHAPE of the block, and a hardcoded version turns
    # every legitimate compat-window bump into a spurious CI failure (it
    # did, on the 5.183.0 -> 5.183.1 bump).
    assert block["bounds"] == _BOUNDS


def test_unverifiable_peer_passes() -> None:
    # A peer we cannot read must NOT cause a refusal — a fresh install
    # without a version header yet would loop itself. "unverifiable"
    # is permissive, not "incompatible".
    assert versions_compatible("5.182.0", "unknown") is True
    assert versions_compatible("5.182.0", "") is True
    assert versions_compatible("5.182.0", "latest") is True


def test_sidecar_missing_falls_back_permissively() -> None:
    # If the sidecar is unreadable the module must NOT loop the daemon
    # on its own config — the fallback returns True for any non-``unknown``
    # input. Pins the contract: probe failure ≠ incompatibility.
    with patch(
        "yadgar._shared.version_compat._BOUNDS",
        {"core": {"min": "", "max": ""}, "backend": {"min": "", "max": ""}},
    ):
        assert versions_compatible("5.182.0", "5.73.0") is True
        # A literal ``"unknown"`` self-version still returns True
        # (``_parse`` short-circuits) so a broken install never refuses
        # itself; the handshake is "unverifiable" and surfaces in the
        # block, not in the bool.
        assert versions_compatible("unknown", "unknown") is True


def test_handshake_includes_peer_version_field() -> None:
    # Belt-and-braces: the field name is the contract with the runtime
    # probe (``yadgar.core.server.http._handshake_block``) — both ends
    # must agree on the field name to be backward-compatible.
    block = handshake_status("5.182.0", "5.99.0", side="core")
    assert block["peer_version"] == "5.99.0"
    assert block["compatible"] is False
