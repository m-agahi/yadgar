"""Characterization fixtures for ``install_systemd_service`` (task:0110 Stage A).

Stage A re-expresses the two Python-generated units in the ordered unit model
(``yadgar/core/daemon/unit_model.py``). "No behaviour change" is only an
assertion until something compares bytes, and the units this function writes are
what ``yadgar daemon install-service`` installs — so the claim is pinned here,
against committed fixtures, for both runtime arms.

The fixture is deliberately NOT the same artifact as the parity snapshots under
``yadgar/tests/scripts/snapshots/systemd/``: those capture the ``sed`` render of
``scripts/install/*.in`` (the ``yadgar-setup`` arm). This one captures the
profile-driven arm. Plan §9.5 keeps the two arms on different mounts, so they
are expected to differ — conflating them is what makes Stage B look impossible.

Regenerating (Stage B changes this arm on purpose — the diff IS the review
artifact for "what changed about ``daemon install-service``'s units"):
``YADGAR_UPDATE_UNIT_FIXTURES=1 pytest <this file>``.

Every host probe is pinned: runtime, host RAM, backend version, ``Path.home()``
and the XDG path constants. A fixture that moved with the developer's RAM would
be noise, not a check.
"""

from __future__ import annotations

import os
import unittest.mock as mock
from pathlib import Path

import pytest

from yadgar.tests._paths import REPO_ROOT

FIXTURES = REPO_ROOT / "yadgar" / "tests" / "core" / "snapshots" / "install_systemd_service"

# A FIXED home — the hf-cache mount and the secrets/upgrade paths are rendered
# into the unit text, so a tmp_path home would make the fixture unstable.
FIXTURE_HOME = Path("/tmp/yadgar-fixture-home")  # noqa: S108 — test sandbox, not real state
FIXTURE_DATA = Path("/home/testuser/.local/share/yadgar")
FIXTURE_STATE = Path("/home/testuser/.local/state/yadgar")
FIXTURE_SECRETS = Path("/home/testuser/.config/yadgar/secrets.env")
FIXTURE_MEMORY_MB = 2048
FIXTURE_BACKEND_VERSION = "9.9.9"


def _written_units(monkeypatch, runtime: str) -> dict[str, str]:
    """Every unit ``install_systemd_service`` writes, with all host probes pinned."""
    import yadgar.core.daemon.systemd as systemd_mod
    from yadgar.core.daemon.profiles import _prod_profile

    for var in ("YADGAR_CONTAINER", "YADGAR_VOLUME", "YADGAR_BACKEND_CONTAINER"):
        monkeypatch.delenv(var, raising=False)
    # Pinned, not deleted: the default core image embeds the CORE version, which
    # the release cascade bumps — an unpinned fixture would break on every bump.
    monkeypatch.setenv("YADGAR_IMAGE", "docker.io/openfantasy/yadgar:9.9.9")
    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", runtime)
    monkeypatch.setenv("YADGAR_BACKEND_IMAGE", "docker.io/openfantasy/yadgar-backend:ignored")
    monkeypatch.setattr(systemd_mod, "_container_memory_mb", lambda: FIXTURE_MEMORY_MB)
    monkeypatch.setattr(systemd_mod, "_backend_version", lambda: FIXTURE_BACKEND_VERSION)

    # Pin the XDG constants through the ENVIRONMENT, never by setattr on
    # yadgar._shared.paths.paths: that module resolves its constants through a
    # PEP-562 __getattr__, so monkeypatch records the RESOLVED value as the "old"
    # one and undo() writes it back as a real attribute — permanently shadowing
    # the resolver for every later test in the session. (Observed: it made
    # test_daemon_cli_fixes_v5_49_1's --env-file assertion fail only when this
    # file ran first.)
    monkeypatch.setenv("YADGAR_DATA_DIR", str(FIXTURE_DATA))
    monkeypatch.setenv("XDG_STATE_HOME", str(FIXTURE_STATE.parent))
    monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(FIXTURE_SECRETS))

    # The hf-cache mount is conditional on the directory existing; create it so the
    # fixture always covers the mounted arm. The absent arm is covered by
    # test_no_hf_cache_mount_when_absent below.
    (FIXTURE_HOME / ".cache" / "huggingface").mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {}
    orig_write = Path.write_text

    def capturing_write(self: Path, text: str, *args, **kwargs):  # type: ignore[override]
        written[self.name] = text
        return orig_write(self, text, *args, **kwargs)

    with (
        mock.patch.object(Path, "write_text", capturing_write),
        mock.patch("pathlib.Path.home", return_value=FIXTURE_HOME),
    ):
        systemd_mod.install_systemd_service(_prod_profile(8765), dev=False)
    return written


@pytest.mark.parametrize("runtime", ["podman", "docker"])
def test_install_systemd_service_matches_characterization_fixture(monkeypatch, runtime):
    """The two profile-arm units are byte-identical to their committed fixture."""
    written = _written_units(monkeypatch, runtime)
    assert set(written) == {"yadgar-backend.service", "yadgar.service"}, (
        f"install_systemd_service wrote an unexpected unit set: {sorted(written)}"
    )
    arm = FIXTURES / runtime
    if os.environ.get("YADGAR_UPDATE_UNIT_FIXTURES"):
        arm.mkdir(parents=True, exist_ok=True)
        for name, text in written.items():
            (arm / name).write_text(text)
        pytest.fail("fixtures regenerated — re-run without YADGAR_UPDATE_UNIT_FIXTURES")
    for name, text in sorted(written.items()):
        expected = (arm / name).read_text()
        assert text == expected, (
            f"{runtime}/{name} drifted from its characterization fixture. If the change is "
            f"intentional, regenerate with YADGAR_UPDATE_UNIT_FIXTURES=1 and review the diff."
        )


def test_no_hf_cache_mount_when_absent():
    """``hf_cache_dir=None`` must drop the mount line entirely, not render an empty one."""
    from yadgar.core.daemon.unit_model import render_unit
    from yadgar.core.daemon.units import build_backend_unit
    from yadgar.tests.core.test_unit_model import minimal_spec

    text = render_unit(build_backend_unit(minimal_spec(hf_cache_dir=None)))
    assert "huggingface" not in text
    assert "\\\n\n" not in text, "a dropped optional line must not leave a blank continuation"
