"""v5.49.0 Phase 7 TDD — systemd unit template + launchd plist rewrite.

Tests:
  37. test_unit_template_has_type_notify
  38. test_unit_template_uses_image_tag_env_var
  39. test_unit_template_has_timeoutstopsec
  40. test_unit_template_environmentfile_optional_prefix
  41. test_launchd_plist_has_exit_timeout  (optional 5th)
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVICE_IN = REPO_ROOT / "scripts" / "install" / "yadgar.service.in"
PLIST_IN = REPO_ROOT / "scripts" / "install" / "launchd" / "com.openfantasy.yadgar.plist.in"


# ── T37 ─────────────────────────────────────────────────────────────────────


def test_unit_template_has_type_notify():
    """T37: yadgar.service.in must use Type=notify (not Type=simple)."""
    content = SERVICE_IN.read_text()
    assert "Type=notify" in content, (
        "yadgar.service.in missing Type=notify. "
        "Phase 7 requires sd_notify signals from host CLI to reach systemd."
    )
    assert "Type=simple" not in content, (
        "yadgar.service.in still contains Type=simple — must be removed."
    )


# ── T38 ─────────────────────────────────────────────────────────────────────


def test_unit_template_uses_image_tag_env_var():
    """T38: ExecStart must reference ${YADGAR_IMAGE_TAG}, not a literal version tag."""
    content = SERVICE_IN.read_text()
    assert "${YADGAR_IMAGE_TAG}" in content, (
        "yadgar.service.in ExecStart missing ${YADGAR_IMAGE_TAG}. "
        "Image tag must come from EnvironmentFile (~/.local/state/yadgar/upgrade.env) "
        "so routine upgrades rewrite only the env-file, not the unit."
    )
    # Must NOT contain a literal versioned image tag like docker.io/openfantasy/yadgar:5.x
    assert not re.search(r"docker\.io/openfantasy/yadgar:\d+\.", content), (
        "yadgar.service.in ExecStart still contains a literal versioned image tag "
        "(e.g. docker.io/openfantasy/yadgar:5.x). Replace with ${YADGAR_IMAGE_TAG}."
    )


# ── T39 ─────────────────────────────────────────────────────────────────────


def test_unit_template_has_timeoutstopsec():
    """T39: yadgar.service.in must have TimeoutStopSec=45."""
    content = SERVICE_IN.read_text()
    assert "TimeoutStopSec=45" in content, (
        "yadgar.service.in missing TimeoutStopSec=45. "
        "45s graceful-stop window needed for queue flush + STOPPING=1 signal."
    )


# ── T40 ─────────────────────────────────────────────────────────────────────


def test_unit_template_environmentfile_optional_prefix():
    """T40: EnvironmentFile must use leading '-' so missing file is non-fatal."""
    content = SERVICE_IN.read_text()
    assert "EnvironmentFile=-%h/.local/state/yadgar/upgrade.env" in content, (
        "yadgar.service.in missing EnvironmentFile=-%h/.local/state/yadgar/upgrade.env. "
        "Leading '-' required — first install has no env-file yet."
    )


# ── T41 (optional 5th) ───────────────────────────────────────────────────────


def test_launchd_plist_has_exit_timeout():
    """T41: com.openfantasy.yadgar.plist.in must have ExitTimeOut key with value 30."""
    content = PLIST_IN.read_text()
    assert "<key>ExitTimeOut</key>" in content, (
        "com.openfantasy.yadgar.plist.in missing <key>ExitTimeOut</key>. "
        "macOS launchd needs explicit ExitTimeOut for graceful-stop window."
    )
    # The integer value 30 must follow the key
    assert "<integer>30</integer>" in content, (
        "com.openfantasy.yadgar.plist.in missing <integer>30</integer> for ExitTimeOut."
    )
