"""v5.46.19 TDD — Rocky Linux SELinux bind-mount fix + restart-on-regen.

Bugs fixed
----------
1. Rocky Linux SELinux enforcing: bind-mount ``-v /root/.yadgar:/data`` lacks
   ``:Z`` private-relabel flag → container's ``container_file_t`` context can't
   write to default-labeled host dir.  Fix: append ``:Z`` to all volume mount
   lines in service templates.

2. Setup re-runs regenerate the unit file but don't reload/restart systemd →
   backend container stays on the stale unit (stale image tag, old flags) until
   manual restart.  Fix: after ``daemon-reload``, if yadgar.target is already
   active, restart it.

3. Backend container's entrypoint tries ``mkdir /data/logs`` which fails on
   hostile filesystems before SELinux relabel.  Fix: pre-create
   ``${YADGAR_DIR}/logs`` (chmod 700) in setup.sh before service start.

Test structure
--------------
T1  yadgar-backend.service.in volume line matches ``-v @DATA_DIR@:/data:Z``.
T2  yadgar.service.in volume line matches ``-v @DATA_DIR@:/data:Z``.
T3  No bare ``-v <path>:/data`` (without ``:Z``) in either template.
T4  setup.sh ``_step_enable_units`` (or named helper) contains restart-if-active
    block: ``is-active --quiet yadgar.target`` AND ``restart yadgar.target``.
T5  setup.sh contains ``mkdir -p`` for a ``logs`` path under YADGAR_DIR.
T6  Rendered unit (generate_systemd.sh with test fixtures) contains ``:Z`` on
    the volume mount line in both yadgar.service and yadgar-backend.service.
"""

import re
import subprocess
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts" / "install"
BACKEND_TEMPLATE = SCRIPTS_DIR / "yadgar-backend.service.in"
CORE_TEMPLATE = SCRIPTS_DIR / "yadgar.service.in"
SETUP_SH = SCRIPTS_DIR / "yadgar-setup.sh"
GENERATE_SYSTEMD = SCRIPTS_DIR / "generate_systemd.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Matches the :Z form on the /data mount (trailing whitespace or end-of-line)
_Z_MOUNT_RE = re.compile(r"-v\s+\S+:/data:Z(\s|$)", re.MULTILINE)

# Matches a bare /data mount WITHOUT :Z — any trailing char except ':'
# Must NOT match when only :Z form is present
_BARE_MOUNT_RE = re.compile(r"-v\s+\S+:/data(?!:Z)(?!\S)", re.MULTILINE)


# ---------------------------------------------------------------------------
# T1 — yadgar-backend.service.in has :Z on /data mount
# ---------------------------------------------------------------------------


def test_t1_backend_template_has_selinux_z_flag() -> None:
    """`yadgar-backend.service.in` must have ``:Z`` on the ``/data`` volume mount."""
    assert BACKEND_TEMPLATE.exists(), f"Template not found: {BACKEND_TEMPLATE}"
    content = BACKEND_TEMPLATE.read_text(encoding="utf-8")
    assert _Z_MOUNT_RE.search(content), (
        f"No ':Z' SELinux flag found on /data mount in {BACKEND_TEMPLATE.name}.\n"
        f"Expected pattern: {_Z_MOUNT_RE.pattern!r}\n"
        f"Content:\n{content}"
    )


# ---------------------------------------------------------------------------
# T2 — yadgar.service.in has :Z on /data mount
# ---------------------------------------------------------------------------


def test_t2_core_template_has_selinux_z_flag() -> None:
    """`yadgar.service.in` must have ``:Z`` on the ``/data`` volume mount."""
    assert CORE_TEMPLATE.exists(), f"Template not found: {CORE_TEMPLATE}"
    content = CORE_TEMPLATE.read_text(encoding="utf-8")
    assert _Z_MOUNT_RE.search(content), (
        f"No ':Z' SELinux flag found on /data mount in {CORE_TEMPLATE.name}.\n"
        f"Expected pattern: {_Z_MOUNT_RE.pattern!r}\n"
        f"Content:\n{content}"
    )


# ---------------------------------------------------------------------------
# T3 — No bare /data mount (without :Z) in either template
# ---------------------------------------------------------------------------


def test_t3_no_bare_data_mount_in_templates() -> None:
    """Neither template may contain a bare ``-v X:/data`` without ``:Z``."""
    for template in (BACKEND_TEMPLATE, CORE_TEMPLATE):
        assert template.exists(), f"Template not found: {template}"
        content = template.read_text(encoding="utf-8")
        match = _BARE_MOUNT_RE.search(content)
        assert match is None, (
            f"Bare /data mount without ':Z' found in {template.name}.\n"
            f"Matched: {match.group()!r}\n"
            f"Content:\n{content}"
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


def test_t6_rendered_units_contain_z_flag() -> None:
    """generate_systemd.sh must render units with ``:Z`` on the ``/data`` volume mount.

    Runs the script with test fixtures (no real runtime, no real sockets).
    """
    assert GENERATE_SYSTEMD.exists(), f"generate_systemd.sh not found: {GENERATE_SYSTEMD}"

    with tempfile.TemporaryDirectory() as tmpdir:
        env_overrides = {
            "YADGAR_RUNTIME": "podman",
            "YADGAR_INSTALL_PREFIX": tmpdir,
            "YADGAR_SYSTEMD_OUTPUT_DIR": tmpdir,
            "YADGAR_SECRETS_ENV_FILE": f"{tmpdir}/secrets.env",
            "YADGAR_BACKEND_IMAGE": "test-registry/yadgar-backend:test",
            "YADGAR_CORE_IMAGE": "test-registry/yadgar:test",
            # Suppress nix-symlink guard (no existing units to check)
            "HOME": tmpdir,
        }
        import os

        full_env = {**os.environ, **env_overrides}

        result = subprocess.run(
            ["bash", str(GENERATE_SYSTEMD)],
            capture_output=True,
            text=True,
            timeout=15,
            env=full_env,
        )
        assert result.returncode == 0, (
            f"generate_systemd.sh exited {result.returncode}.\n"
            f"stdout: {result.stdout!r}\n"
            f"stderr: {result.stderr!r}"
        )

        # Check both rendered units contain :Z
        for unit_name in ("yadgar.service", "yadgar-backend.service"):
            unit_path = Path(tmpdir) / unit_name
            assert unit_path.exists(), (
                f"{unit_name} was not generated in {tmpdir}.\n"
                f"generate_systemd.sh stdout: {result.stdout!r}"
            )
            unit_content = unit_path.read_text(encoding="utf-8")
            assert _Z_MOUNT_RE.search(unit_content), (
                f"No ':Z' SELinux flag found on /data mount in rendered {unit_name}.\n"
                f"Content:\n{unit_content}"
            )
