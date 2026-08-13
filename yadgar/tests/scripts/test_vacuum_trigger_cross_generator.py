"""Cross-generator regression: the vacuum trigger a generator tells the daemon
to write MUST land in the dir that same generator's watcher watches.

fix-vacuum-trigger-path-and-watcher-2026-07-29 (task:0044). ``vacuum_now()``
writes a trigger file at ``YADGAR_VACUUM_TRIGGER_PATH``
(``yadgar/core/ops/ops.py::_fire_vacuum_service``) and a host-side watcher unit
starts the vacuum. The write always succeeded, so ``vacuum_now()`` reported
``started: True`` even on surfaces where nothing was watching — a silent no-op.

Same failure class (and the same structural template) as
``test_backend_unit_queue_base_cross_generator.py`` (task:0076, the
``/data`` vs ``/queue-data`` split): the correct value differs per surface, so
the anti-recurrence mechanism is a cross-generator test, not a unified constant.

Two shapes are asserted, per surface:

* **watcher-bearing** — the generator sets ``-e YADGAR_VACUUM_TRIGGER_PATH``,
  the value is under a ``-v <host>:<target>`` bind in the *same* rendered unit,
  the host projection's dir equals the watched dir of the watcher unit rendered
  by the *same* generator, and the watcher is actually ACTIVATED (not merely
  rendered — a watcher nothing pulls in passes every render assertion and still
  never fires).
* **declared-no-watcher** — the generator appears in ``_NO_WATCHER_SURFACES``
  with a cited reason, and the test asserts it renders NO watcher unit AND sets
  NO trigger env. The absence is asserted, not silently tolerated: a surface
  that grows half a fix (watcher without env, or env without a mount) fails.

Since ``_fire_vacuum_service`` now fails loud when the env is unset (D1), a
no-watcher surface is honest by construction: ``vacuum_now()`` returns
``started: False, skipped_reason="no_trigger_path_configured"`` there.

The private nix module (``modules/home/yadgar.nix``) renders the same contract
but lives in the dotfiles repo, out-of-repo and unreachable from this suite.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from yadgar.core.daemon import systemd as systemd_mod
from yadgar.core.daemon.profiles import _prod_profile
from yadgar.tests._mount_projection import extract_env, parse_mounts, project_to_host
from yadgar.tests._paths import REPO_ROOT
from yadgar.tests._unit_render import render_systemd

BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"
INSTALL_DIR = REPO_ROOT / "scripts" / "install"
GENERATE_LAUNCHD_SH = INSTALL_DIR / "generate_launchd.sh"
FLAKE_NIX = REPO_ROOT / "flake.nix"

TRIGGER_ENV = "YADGAR_VACUUM_TRIGGER_PATH"

# Surfaces that deliberately ship NO trigger watcher. The reason is cited so a
# future reader knows the absence is a decision, not an oversight.
_NO_WATCHER_SURFACES: dict[str, str] = {
    "docker-compose.yml": (
        "task:0077 F3 — compose is the dev/CI surface. It ships no maintenance "
        "units at all (no vacuum runner, no timer, no watcher) and deliberately "
        "does NOT publish the SurrealDB port, so there is nothing host-side to "
        "trigger. vacuum_now() refuses there, which is the honest answer."
    ),
    "daemon.py docker-run": (
        "task:0077 F3 — the `yadgar daemon start` dev path (DaemonManager in "
        "yadgar/core/daemon/daemon.py) runs plain `docker run` with "
        "`-v {volume_name}:/data`, the same NAMED volume as the Python systemd "
        "generator (yadgar/core/daemon/profiles.py). No host path exists for a "
        "watcher to watch, and it installs no units to watch with."
    ),
    "install_systemd_service (Python)": (
        "task:0044 D3 — the core container mounts `-v {profile.volume_name}:/data` "
        "and volume_name is os.environ.get('YADGAR_VOLUME', 'yadgar-data') "
        "(yadgar/core/daemon/profiles.py) — a NAMED volume, never a host path, and "
        "the same volume the backend mounts at /queue-data. No host path exists "
        "for a watcher to watch, so a host-side watcher is structurally impossible "
        "on this surface."
    ),
}


# ── Renderers ─────────────────────────────────────────────────────────────────


def _render_launchd(tmp_path: Path) -> tuple[str, str]:
    """Return (core run command, watched dir) for the launchd surface."""
    env = dict(os.environ)
    env.update(
        {
            "YADGAR_LAUNCHD_OUTPUT_DIR": str(tmp_path),
            "YADGAR_RUNTIME": "podman",
            "YADGAR_INSTALL_PREFIX": "/home/testuser/.yadgar",
            "YADGAR_SECRETS_ENV_FILE": "/home/testuser/.yadgar/secrets.env",
            "YADGAR_BACKEND_IMAGE": "openfantasy/yadgar-backend:test",
            "YADGAR_CORE_IMAGE": "openfantasy/yadgar:test",
            "YADGAR_HOME": "/home/testuser",
        }
    )
    result = subprocess.run(
        [BASH, str(GENERATE_LAUNCHD_SH)], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, f"generate_launchd.sh failed\n{result.stderr}"

    core = plistlib.loads((tmp_path / "com.openfantasy.yadgar.plist").read_bytes())
    run_cmd = next(a for a in core["ProgramArguments"] if " run " in a and "-v " in a)

    watcher = plistlib.loads(
        (tmp_path / "com.openfantasy.yadgar-vacuum-trigger.plist").read_bytes()
    )
    return run_cmd, watcher["WatchPaths"][0]


# flake.nix declares each unit as a top-level `systemd.user.<kind>.<name> = {`
# attribute at a fixed indent. Anchoring the block slicer on newline+indent (not
# the bare substring) keeps prose in a comment from truncating a block.
_ATTR_ANCHOR = "\n            systemd.user."


def _flake_attr_block(attr: str) -> str | None:
    """Return the flake.nix source of `systemd.user.<attr> = { ... }`, or None."""
    text = FLAKE_NIX.read_text()
    marker = f"{_ATTR_ANCHOR}{attr} = {{"
    if marker not in text:
        return None
    start = text.index(marker) + len(_ATTR_ANCHOR)
    rest = text[start:]
    end = rest.find(_ATTR_ANCHOR)
    return rest[:end] if end != -1 else rest


def _flake_core_block() -> str:
    """Return the `systemd.user.services.yadgar` block of flake.nix.

    Sliced so backend mounts (`systemd.user.services.yadgar-backend`) cannot
    contaminate the mount table of the core unit under test.
    """
    block = _flake_attr_block("services.yadgar")
    assert block is not None, "flake.nix has no systemd.user.services.yadgar unit"
    return block


def _flake_watcher_block() -> str:
    block = _flake_attr_block("paths.yadgar-vacuum-trigger")
    assert block is not None, (
        "flake.nix ships a vacuum runner + weekly timer but NO trigger watcher — "
        "vacuum_now() writes a trigger file nothing reads. Add "
        "systemd.user.paths.yadgar-vacuum-trigger watching the host projection "
        f"of its own -e {TRIGGER_ENV}."
    )
    return block


def _render_flake() -> tuple[str, str]:
    """Return (core run command, watched dir) for the repo flake.nix surface.

    flake.nix is not rendered by a script — it is asserted as text, with Nix
    interpolation tokens (`${stateDir}`) compared literally on both sides. The
    watcher deliberately uses the SAME `${stateDir}` token that appears on the
    left of the `-v` bind so this comparison is exact rather than a
    substring/heuristic match after Nix evaluation.
    """
    watcher_block = _flake_watcher_block()
    # Car 10: PathExists= edge-triggers only on a non-existent -> existent
    # transition, so a second request while a stale trigger file exists
    # (the .path unit not yet re-armed, or a previous vacuum's leftover)
    # never fires. PathChanged= (close-after-write / atomic rename) re-fires
    # on every fresh write, matching the nix module's RESTART-watcher fix.
    m = re.search(r'PathChanged\s*=\s*"([^"]+)"', watcher_block)
    assert m, "flake.nix vacuum-trigger .path unit has no PathChanged"
    watched_dir = m.group(1).rsplit("/", 1)[0]
    # The core ExecStart is a Nix list of quoted strings; blank the quotes so
    # `-v`/`-e` tokens parse the same way a rendered shell command line does.
    return _flake_core_block().replace('"', " "), watched_dir


def _render_systemd_sh_watcher(tmp_path: Path) -> tuple[str, str]:
    """Return (core run command, watched dir) for the non-nix systemd surface.

    Deliberately NOT the concatenate-every-rendered-file shape used by the
    declared-no-watcher parametrization: the invariant compares the CORE unit's
    own mount table against the `.path` unit's watched dir, and concatenating
    the backend unit's mounts into the same string would let a bind that only
    the backend has satisfy the projection.
    """
    render_systemd(tmp_path)
    run_cmd = (tmp_path / "units" / "yadgar.service").read_text()

    watcher = (tmp_path / "units" / "yadgar-vacuum-trigger.path").read_text()
    # Car 10: PathChanged=, not PathExists= — see _render_flake's comment.
    m = re.search(r"^PathChanged=(.+)$", watcher, re.MULTILINE)
    assert m, "yadgar-vacuum-trigger.path has no PathChanged"
    return run_cmd, m.group(1).strip().rsplit("/", 1)[0]


def _read_compose(_tmp_path: Path) -> tuple[str, list[Path]]:
    """docker-compose.yml is static YAML — read it, render nothing (F3)."""
    return (REPO_ROOT / "docker-compose.yml").read_text(), []


def _read_daemon_docker_run(_tmp_path: Path) -> tuple[str, list[Path]]:
    """The `yadgar daemon start` docker-run dev path (F3).

    Asserted as source text rather than by executing DaemonManager.start():
    the `docker run` argv is built inline inside the start methods, not by an
    extractable command-builder function, and driving the real method needs an
    image-exists probe plus a live backend. Source text is the honest cheap
    check here — it fails if this surface grows either half of the pair.
    """
    return (REPO_ROOT / "yadgar" / "core" / "daemon" / "daemon.py").read_text(), []


def _render_python_systemd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, list[Path]]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_VOLUME", "yadgar-data")
    profile = _prod_profile(8765)
    systemd_mod.install_systemd_service(profile, dev=False)
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    written = sorted(p for p in unit_dir.iterdir() if p.is_file())
    return "\n".join(p.read_text() for p in written), written


# ── The invariant ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label", ["generate_launchd.sh", "generate_systemd.sh", "flake.nix"])
def test_watcher_bearing_generator_trigger_dir_equals_watched_dir(label, tmp_path):
    """THE INVARIANT: host dir the daemon writes the trigger to == watched dir."""
    if label == "generate_launchd.sh":
        run_cmd, watched_dir = _render_launchd(tmp_path)
    elif label == "generate_systemd.sh":
        run_cmd, watched_dir = _render_systemd_sh_watcher(tmp_path)
    else:
        run_cmd, watched_dir = _render_flake()

    container_trigger = extract_env(run_cmd, TRIGGER_ENV)
    assert container_trigger, (
        f"{label}: no -e {TRIGGER_ENV} — the daemon would fall back to the "
        f"fail-loud default and vacuum_now() would report started=False"
    )

    mounts = parse_mounts(run_cmd)
    host_trigger = project_to_host(container_trigger, mounts)
    assert host_trigger is not None, (
        f"{label}: {TRIGGER_ENV}={container_trigger!r} is under no `-v host:target` "
        f"bind ({sorted(mounts)}) — the write would stay inside the container"
    )

    host_trigger_dir = host_trigger.rsplit("/", 1)[0]
    assert host_trigger_dir == watched_dir, (
        f"{label}: vacuum-trigger MISMATCH — daemon writes to host "
        f"{host_trigger_dir!r} but the watcher watches {watched_dir!r}; "
        f"vacuum_now() would be a silent no-op"
    )


def test_flake_watcher_is_activated_not_merely_rendered():
    """A watcher nothing pulls in renders fine and never fires.

    home-manager creates the wants symlink only from `Install.WantedBy`; without
    it the .path unit exists on disk, every render assertion above passes, and
    vacuum_now() is still a no-op.
    """
    block = _flake_watcher_block()
    assert re.search(r'Install\.WantedBy\s*=\s*\[\s*"paths\.target"', block), (
        'flake.nix vacuum-trigger .path unit has no Install.WantedBy = [ "paths.target" ] — '
        "it would render but never activate"
    )


def test_flake_watcher_handler_removes_trigger_before_starting_vacuum():
    """Handler must remove the trigger file BEFORE starting the runner, so a
    failed vacuum does not pin the .path unit active (R4)."""
    block = _flake_attr_block("services.yadgar-vacuum-trigger")
    assert block is not None, (
        "flake.nix has no systemd.user.services.yadgar-vacuum-trigger handler service"
    )
    exec_at = block.find("ExecStart")
    assert exec_at != -1, "handler service has no ExecStart"
    block = block[exec_at:]  # skip Unit.Description prose
    rm_at = block.find("rm -f")
    start_at = block.find("start yadgar-vacuum")
    assert rm_at != -1, "handler does not remove the trigger file"
    assert start_at != -1, "handler does not start yadgar-vacuum.service"
    assert rm_at < start_at, (
        "handler starts the vacuum before removing the trigger file — a failed "
        "vacuum would pin the .path unit active and never re-fire"
    )


def test_systemd_watcher_is_activated_not_merely_rendered(tmp_path):
    """A watcher nothing pulls in renders fine and never fires.

    `[Install] WantedBy=paths.target` alone does NOT activate the unit here:
    every install entry point only ever runs `systemctl --user enable yadgar.target`,
    which installs yadgar.target's own symlink and nothing else. The ACTUAL
    activation mechanism is yadgar.target's `Wants=` (task:0077 D2), so that is
    what this asserts — asserting the `[Install]` stanza would be a false green.
    """
    render_systemd(tmp_path)
    target = (tmp_path / "units" / "yadgar.target").read_text()
    wants = " ".join(
        line.split("=", 1)[1] for line in target.splitlines() if line.startswith("Wants=")
    ).split()

    for unit in (
        "yadgar-vacuum-trigger.path",
        "yadgar-vacuum.timer",
        "yadgar-nightly-cycle.timer",
    ):
        assert unit in wants, (
            f"yadgar.target does not Wants={unit} — setup only ever enables "
            f"yadgar.target, so this unit would render on disk, pass every render "
            f"assertion, and never activate.\nTarget Wants: {wants}"
        )


def test_systemd_trigger_handler_removes_trigger_before_starting_vacuum(tmp_path):
    """Systemd twin of the flake assertion: remove the trigger BEFORE starting the
    runner, so a failed vacuum does not pin the .path unit active."""
    render_systemd(tmp_path)
    handler = (tmp_path / "units" / "yadgar-vacuum-trigger.service").read_text()
    exec_at = handler.find("ExecStart")
    assert exec_at != -1, "handler service has no ExecStart"
    handler = handler[exec_at:]
    rm_at = handler.find("rm -f")
    start_at = handler.find("start yadgar-vacuum")
    assert rm_at != -1, "handler does not remove the trigger file"
    assert start_at != -1, "handler does not start yadgar-vacuum.service"
    assert rm_at < start_at, (
        "handler starts the vacuum before removing the trigger file — a failed "
        "vacuum would pin the .path unit active and never re-fire"
    )


# Explicit label → renderer map. An if/else chain with a bare `else` silently
# routes every NEW label to the last renderer (three labels, one surface actually
# rendered, all green) — the exact false-green this suite exists to prevent.
_NO_WATCHER_RENDERERS = {
    "docker-compose.yml": lambda tmp, _mp: _read_compose(tmp),
    "daemon.py docker-run": lambda tmp, _mp: _read_daemon_docker_run(tmp),
    "install_systemd_service (Python)": _render_python_systemd,
}


def test_no_watcher_renderer_map_covers_every_declared_surface():
    """Guard the guard: a surface declared in _NO_WATCHER_SURFACES with no
    renderer would be silently untested."""
    assert set(_NO_WATCHER_RENDERERS) == set(_NO_WATCHER_SURFACES), (
        f"renderer map and declared-surface map disagree: "
        f"{set(_NO_WATCHER_RENDERERS) ^ set(_NO_WATCHER_SURFACES)}"
    )


@pytest.mark.parametrize("label", sorted(_NO_WATCHER_SURFACES))
def test_declared_no_watcher_surface_ships_neither_watcher_nor_env(label, tmp_path, monkeypatch):
    """Declared-no-watcher surfaces must ship NEITHER half of the pair.

    Asserting the absence (rather than skipping) is the point: a future car that
    adds a watcher without the env, or the env without a host mount, fails here
    instead of shipping another silent no-op.
    """
    rendered, written = _NO_WATCHER_RENDERERS[label](tmp_path, monkeypatch)

    vacuum_units = [p.name for p in written if "vacuum" in p.name]
    assert not vacuum_units, (
        f"{label} is in _NO_WATCHER_SURFACES but rendered vacuum units "
        f"{vacuum_units}. Either remove them, or move this surface to the "
        f"watcher-bearing parametrization above (which asserts the trigger dir "
        f"equals the watched dir).\nDeclared reason: {_NO_WATCHER_SURFACES[label]}"
    )
    assert TRIGGER_ENV not in rendered, (
        f"{label} sets {TRIGGER_ENV} but ships no watcher — that is the silent "
        f"no-op this test exists to prevent. Leave it unset so "
        f"_fire_vacuum_service() fails loud.\n"
        f"Declared reason: {_NO_WATCHER_SURFACES[label]}"
    )


def test_python_systemd_data_mount_is_a_named_volume_not_a_host_path(tmp_path, monkeypatch):
    """D3's cited reason, asserted rather than trusted.

    If /data ever becomes a real host bind on this surface, the reason in
    _NO_WATCHER_SURFACES stops being true and the deferral must be revisited.
    """
    rendered, _ = _render_python_systemd(tmp_path, monkeypatch)
    mounts = parse_mounts(rendered)
    data_host = mounts.get("/data")
    assert data_host is not None, "core unit no longer mounts /data"
    assert not data_host.startswith("/"), (
        f"/data is now a host bind ({data_host!r}), not a named volume — the "
        f"D3 deferral reason in _NO_WATCHER_SURFACES no longer holds; a host-side "
        f"watcher is now possible and this surface should grow one"
    )
