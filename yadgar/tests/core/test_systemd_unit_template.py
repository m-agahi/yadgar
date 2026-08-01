"""v5.49.0 Phase 7 TDD — systemd unit template + launchd plist rewrite.

Tests:
  37. test_unit_template_has_type_notify
  38. test_unit_template_uses_image_tag_env_var
  39. test_unit_template_has_timeoutstopsec
  40. test_unit_template_environmentfile_optional_prefix
  41. test_launchd_plist_has_exit_timeout  (optional 5th)

task:0105 added test_unit_template_docker_render_gates_readiness_without_notify —
readiness is now runtime-conditional, so T37 asserts on the RENDERED podman unit
and its docker counterpart asserts the Type=exec + health-gate shape. T38/T39/T40
still read the source text: those directives are runtime-invariant.
"""

from __future__ import annotations

import re

from yadgar.tests._paths import REPO_ROOT
from yadgar.tests._unit_render import render_systemd

SERVICE_IN = REPO_ROOT / "scripts" / "install" / "yadgar.service.in"
PLIST_IN = REPO_ROOT / "scripts" / "install" / "launchd" / "com.openfantasy.yadgar.plist.in"


def _render(tmp_path, runtime: str) -> str:
    """The RENDERED yadgar.service for *runtime* (task:0105 made Type= conditional)."""
    root = tmp_path / runtime
    render_systemd(root, {"YADGAR_RUNTIME": runtime})
    return (root / "units" / "yadgar.service").read_text()


# ── T37 ─────────────────────────────────────────────────────────────────────


def test_unit_template_has_type_notify(tmp_path):
    """T37: the RENDERED podman unit must use Type=notify (not Type=simple).

    task:0105 made readiness runtime-conditional, so the template now reads
    ``Type=@SERVICE_TYPE@`` and this assertion moved from the source text to the
    rendered output. Both original assertions are kept verbatim on the podman
    arm — the runtime this test has always been about. Left on the source text it
    would pass on prose alone (a comment naming the directive is enough to
    satisfy a substring check), which is a hollow green rather than a check.
    """
    content = _render(tmp_path, "podman")
    assert "Type=notify" in content, (
        "rendered podman yadgar.service missing Type=notify. "
        "Phase 7 requires sd_notify signals from host CLI to reach systemd."
    )
    assert "Type=simple" not in content, (
        "rendered podman yadgar.service still contains Type=simple — must be removed."
    )


def test_unit_template_docker_render_gates_readiness_without_notify(tmp_path):
    """task:0105: the docker arm — Type=notify there has no READY=1 source at all.

    Docker sets no NOTIFY_SOCKET in the container and has no sd_notify proxy, so
    a Type=notify unit sits until TimeoutStartSec. Type=exec plus a bounded
    ExecStartPost /health poll supplies the same After= ordering guarantee.
    """
    content = _render(tmp_path, "docker")
    assert "Type=exec" in content, "rendered docker yadgar.service is not Type=exec"
    assert "Type=notify" not in content, (
        "rendered docker yadgar.service is Type=notify — nothing can send READY=1 "
        "on docker, so the unit would sit until TimeoutStartSec kills it."
    )
    assert "Type=simple" not in content, (
        "rendered docker yadgar.service contains Type=simple — active would then "
        "mean 'the docker CLI forked', so After= ordering guarantees nothing."
    )
    assert re.search(r"^ExecStartPost=curl .*--retry .*/health", content, re.MULTILINE), (
        "rendered docker yadgar.service has no ExecStartPost= readiness gate"
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
