"""v5.46.19 TDD — Rocky Linux SELinux bind-mount fix + restart-on-regen.

Bugs fixed
----------
1. Rocky Linux SELinux enforcing: bind-mount ``-v /root/.yadgar:/data`` caused
   "Permission denied" inside container on RHEL/Rocky with SELinux Enforcing.

   v5.46.19 initial fix: append ``:Z`` private-relabel flag.
   v5.46.20 superseded: ``:Z`` insufficient on Rocky 9 with admin_home_t context
   on /root/.yadgar.  Fix updated to use ``--security-opt label=disable`` instead
   (Option A: simplest for personal-mode root install; SELinux MAC adds no
   isolation when container and host user are both root).  See MIGRATION_NOTES.

2. Setup re-runs regenerate the unit file but don't reload/restart systemd →
   backend container stays on the stale unit (stale image tag, old flags) until
   manual restart.  Fix: after ``daemon-reload``, if yadgar.target is already
   active, restart it.

3. Backend container's entrypoint tries ``mkdir /data/logs`` which fails on
   hostile filesystems before SELinux relabel.  Fix: pre-create
   ``${YADGAR_DIR}/logs`` (chmod 700) in setup.sh before service start.

Test structure
--------------
T1  DELETED (task:0110 Stage D) — was the .in template's text; T6 covers the render.
T2  DELETED (task:0110 Stage D) — same.
T3  No bare ``-v <path>:/data:Z`` (old :Z form) in either RENDERED unit (v5.46.20).
T4  setup.sh ``_step_enable_units`` (or named helper) contains restart-if-active
    block: ``is-active --quiet yadgar.target`` AND ``restart yadgar.target``.
T5  setup.sh contains ``mkdir -p`` for a ``logs`` path under YADGAR_DIR.
T6  Rendered unit (generate_systemd.sh with test fixtures) contains
    ``--security-opt label=disable`` in both yadgar.service and
    yadgar-backend.service (v5.46.20).
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
from yadgar.tests._paths import REPO_ROOT
from yadgar.tests._unit_render import RENDERER_CLI

SCRIPTS_DIR = REPO_ROOT / "scripts" / "install"
SETUP_SH = SCRIPTS_DIR / "yadgar-setup.sh"
GENERATE_SYSTEMD = SCRIPTS_DIR / "generate_systemd.sh"


def _render_units() -> dict[str, str]:
    """The two container units as ``generate_systemd.sh`` actually installs them.

    task:0110 Stage D: the wrapper renders nothing, so this goes through it end to
    end — a delegation that broke would fail here rather than pass on template
    text nobody reads any more.
    """
    assert GENERATE_SYSTEMD.exists(), f"generate_systemd.sh not found: {GENERATE_SYSTEMD}"
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {
            **os.environ,
            "YADGAR_RUNTIME": "podman",
            "YADGAR_INSTALL_PREFIX": tmpdir,
            "YADGAR_SYSTEMD_OUTPUT_DIR": tmpdir,
            "YADGAR_SECRETS_ENV_FILE": f"{tmpdir}/secrets.env",
            "YADGAR_BACKEND_IMAGE": "test-registry/yadgar-backend:test",
            "YADGAR_CORE_IMAGE": "test-registry/yadgar:test",
            "YADGAR_RENDERER_CLI": RENDERER_CLI,
            # Suppress nix-symlink guard (no existing units to check)
            "HOME": tmpdir,
        }
        result = subprocess.run(
            ["bash", str(GENERATE_SYSTEMD)], capture_output=True, text=True, timeout=60, env=env
        )
        assert result.returncode == 0, (
            f"generate_systemd.sh exited {result.returncode}.\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        units = {}
        for unit_name in ("yadgar.service", "yadgar-backend.service"):
            unit_path = Path(tmpdir) / unit_name
            assert unit_path.exists(), (
                f"{unit_name} was not generated in {tmpdir}.\n"
                f"generate_systemd.sh stdout: {result.stdout!r}"
            )
            units[unit_name] = unit_path.read_text(encoding="utf-8")
        return units


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# v5.46.20: SELinux fix changed from :Z to --security-opt label=disable.
# Matches --security-opt label=disable in ExecStart line
_LABEL_DISABLE_RE = re.compile(r"--security-opt\s+label=disable", re.MULTILINE)

# Matches old :Z form on /data mount — must NOT appear in v5.46.20+ templates
_Z_MOUNT_RE = re.compile(r"-v\s+\S+:/data:Z(\s|$)", re.MULTILINE)


# DELETED task:0110 Stage D — T1 (test_t1_backend_template_has_selinux_z_flag) and
# T2 (test_t2_core_template_has_selinux_z_flag). Both asserted
# `--security-opt label=disable` on the SOURCE TEXT of a `.in` template; ADR-0190
# deleted the templates. Not retargeted, because T6 below already makes the exact
# same assertion on both RENDERED units, which is where it matters and is what a
# template assertion was only ever a proxy for. Retargeting would have produced
# two tests differing from T6 by nothing.


# ---------------------------------------------------------------------------
# T3 — No old :Z mount form in either rendered unit (v5.46.20)
# ---------------------------------------------------------------------------


def test_t3_no_bare_data_mount_in_rendered_units() -> None:
    """Neither unit may use the old ``:Z`` form on the ``/data`` mount.

    v5.46.20: :Z removed in favour of --security-opt label=disable. Retargeted in
    task:0110 Stage D from the two `.in` templates to the rendered output — the
    negative property has no counterpart in T6, so unlike T1/T2 it had to move
    rather than be dropped.
    """
    for unit_name, content in _render_units().items():
        match = _Z_MOUNT_RE.search(content)
        assert match is None, (
            f"Old ':Z' mount form still present in rendered {unit_name} "
            f"(removed in v5.46.20).\nMatched: {match.group()!r}\nContent:\n{content}"
        )


# ---------------------------------------------------------------------------
# T4 — setup.sh has restart-if-active block for yadgar.target
# ---------------------------------------------------------------------------


def test_t4_setup_has_restart_if_active_block() -> None:
    """setup.sh must contain a restart-if-active block for yadgar.target.

    Required patterns (both must be present in the file):
    - ``is-active --quiet yadgar.target``
    - ``restart yadgar.target``
    """
    assert SETUP_SH.exists(), f"setup.sh not found: {SETUP_SH}"
    content = SETUP_SH.read_text(encoding="utf-8")

    assert "is-active --quiet yadgar.target" in content, (
        "setup.sh does not contain 'is-active --quiet yadgar.target'.\n"
        "Expected restart-if-active guard for reinstall scenario."
    )
    assert "restart yadgar.target" in content, (
        "setup.sh does not contain 'restart yadgar.target'.\n"
        "Expected restart command after daemon-reload on reinstall."
    )


# ---------------------------------------------------------------------------
# T5 — setup.sh pre-creates logs dir under YADGAR_DIR
# ---------------------------------------------------------------------------


def test_t5_setup_precreates_logs_dir() -> None:
    """setup.sh must contain ``mkdir -p`` for a logs path under YADGAR_DIR."""
    assert SETUP_SH.exists(), f"setup.sh not found: {SETUP_SH}"
    content = SETUP_SH.read_text(encoding="utf-8")

    # Must have mkdir -p involving "logs" somewhere below YADGAR_DIR
    assert re.search(r"mkdir\s+-p\s+.*logs", content), (
        "setup.sh does not contain 'mkdir -p ... logs'.\n"
        "Expected pre-creation of ${YADGAR_DIR}/logs before service start."
    )


# ---------------------------------------------------------------------------
# T6 — Rendered units (via generate_systemd.sh) contain :Z on /data mount
# ---------------------------------------------------------------------------


def test_t6_rendered_units_contain_label_disable() -> None:
    """generate_systemd.sh must install units carrying ``--security-opt label=disable``."""
    for unit_name, unit_content in _render_units().items():
        assert _LABEL_DISABLE_RE.search(unit_content), (
            f"No '--security-opt label=disable' found in rendered {unit_name}.\n"
            f"Content:\n{unit_content}"
        )
