"""Host probes ported out of ``generate_systemd.sh`` (task:0110 Stage C, ADR-0190).

``yadgar/core/daemon/unit_install.py`` carries the four things the shell renderer
does that are NOT rendering: resolving the two host entry points, the DP5
nix-symlink guard, pre-creating the trigger dir, and seeding ``upgrade.env``.
Each has a failure mode that is invisible at render time and expensive later, so
each is pinned here rather than left to the Stage-D wrapper flip.

Every test that touches ``$HOME`` or a state dir points them at ``tmp_path`` via
``monkeypatch.setenv``. ``Path.home()`` reads ``$HOME`` on POSIX; patching the
paths module instead leaks, because its PEP-562 ``__getattr__`` means an undone
``setattr`` writes the resolved value back as a real attribute.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from yadgar.core.daemon.unit_install import (
    NIX_GUARDED_UNITS,
    UNIT_SCHEMA_VERSION,
    HostCliUnresolved,
    InstallAborted,
    NixManagedUnit,
    UnitValidationFailed,
    ensure_trigger_dir,
    fail_no_host_cli_message,
    guard_nix_symlinks,
    resolve_host_exec,
    seed_upgrade_env,
    stamp_unit,
    write_units,
)

ISO_MODULE = "yadgar_iso_probe_0110c"


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def _bin_dir_with_python3(root: Path) -> str:
    """A controlled ``PATH`` that still carries ``python3``.

    Every PATH override here must keep python3 reachable: ``resolve_host_exec``
    resolves the interpreter for its last-resort probe through PATH, so a PATH
    with no python3 makes the probe unreachable and any assertion about it pass
    for the wrong reason.
    """
    python3 = shutil.which("python3")
    assert python3, "python3 must be on PATH"
    root.mkdir(parents=True, exist_ok=True)
    link = root / "python3"
    if not link.exists():
        link.symlink_to(python3)
    return str(root)


# ── resolve_host_exec: the four branches, in order ───────────────────────────


def test_override_wins_over_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _executable(tmp_path / ".local" / "bin" / "yadgar")
    assert resolve_host_exec("yadgar", "yadgar", "/opt/custom/yadgar") == "/opt/custom/yadgar"


def test_pipx_shape_is_preferred_over_path(monkeypatch, tmp_path):
    """``~/.local/bin/<script>`` is what ``pipx`` (and the flake) install."""
    monkeypatch.setenv("HOME", str(tmp_path))
    local = _executable(tmp_path / ".local" / "bin" / "yadgar")
    elsewhere = tmp_path / "usr-local-bin"
    _executable(elsewhere / "yadgar")
    monkeypatch.setenv("PATH", _bin_dir_with_python3(elsewhere))
    assert resolve_host_exec("yadgar", "yadgar", None) == str(local)


def test_falls_back_to_path_lookup(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    elsewhere = tmp_path / "brew-bin"
    found = _executable(elsewhere / "yadgar-nightly-cycle")
    monkeypatch.setenv("PATH", _bin_dir_with_python3(elsewhere))
    assert resolve_host_exec("yadgar-nightly-cycle", "nonexistent_module_0110c", None) == str(found)


def test_unresolvable_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("PATH", _bin_dir_with_python3(tmp_path / "only-python"))
    assert resolve_host_exec("yadgar", "nonexistent_module_0110c", None) is None


def test_non_executable_file_does_not_resolve(monkeypatch, tmp_path):
    """A ``~/.local/bin/yadgar`` left non-executable must not be baked into a unit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    stub = tmp_path / ".local" / "bin" / "yadgar"
    stub.parent.mkdir(parents=True)
    stub.write_text("not executable")
    monkeypatch.setenv("PATH", _bin_dir_with_python3(tmp_path / "only-python"))
    assert resolve_host_exec("yadgar", "nonexistent_module_0110c", None) is None


# ── The `-I` isolation probe ─────────────────────────────────────────────────


def _probe(module: str, cwd: Path, isolated: bool) -> int:
    python3 = shutil.which("python3")
    assert python3, "python3 must be on PATH for the isolation probe test"
    argv = [python3, *(["-I"] if isolated else []), "-c", f"import {module}"]
    return subprocess.run(  # noqa: S603 — argv list, resolved binary, no shell
        argv, cwd=str(cwd), capture_output=True, check=False
    ).returncode


def test_module_probe_is_isolated_from_the_cwd(monkeypatch, tmp_path):
    """``python3 -I`` drops cwd from ``sys.path`` — and that is the whole point.

    ``generate_systemd.sh:147-150``: without ``-I`` the probe succeeds from
    inside a repo checkout even with nothing installed, and the unit — which
    runs from a different working directory — then fails at 4am.

    Both halves are asserted against a SYNTHETIC package, so the test proves the
    isolation property itself rather than accidentally passing because this
    machine happens to have (or lack) yadgar installed.
    """
    (tmp_path / ISO_MODULE).mkdir()
    (tmp_path / ISO_MODULE / "__init__.py").write_text("")

    assert _probe(ISO_MODULE, tmp_path, isolated=False) == 0, (
        "control: without -I the cwd package IS importable — if this fails the "
        "test below proves nothing"
    )
    assert _probe(ISO_MODULE, tmp_path, isolated=True) != 0, (
        "with -I the cwd package must NOT be importable"
    )

    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("PATH", _bin_dir_with_python3(tmp_path / "only-python"))
    monkeypatch.chdir(tmp_path)
    assert resolve_host_exec("yadgar-absent-0110c", ISO_MODULE, None) is None, (
        "resolve_host_exec must use the isolated probe, not a bare import — the "
        "control above proves this module IS importable from this cwd"
    )


def test_installed_module_still_resolves_via_the_probe(monkeypatch, tmp_path):
    """The isolated probe is not a blanket refusal — a real install resolves.

    ``sys`` stands in for any genuinely importable module: ``-I`` drops the cwd,
    not site-packages, so the last-resort branch keeps working for a pipx or
    system install that ships no console script.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("PATH", _bin_dir_with_python3(tmp_path / "only-python"))
    assert resolve_host_exec("yadgar-absent-0110c", "sys", None) == "python3 -m sys"


def test_fail_message_names_the_override_and_the_fix():
    msg = fail_no_host_cli_message(
        "nightly-cycle",
        "YADGAR_HOST_NIGHTLY_CLI",
        "yadgar-nightly-cycle",
        "yadgar.core.scripts.nightly_cycle",
    )
    assert "$YADGAR_HOST_NIGHTLY_CLI" in msg
    assert "~/.local/bin/yadgar-nightly-cycle" in msg
    assert "python3 -m yadgar.core.scripts.nightly_cycle" in msg
    assert "pipx install yadgar" in msg


def test_host_cli_unresolved_is_an_install_abort():
    assert issubclass(HostCliUnresolved, InstallAborted)
    assert issubclass(NixManagedUnit, InstallAborted)


# ── DP5 nix-symlink guard ────────────────────────────────────────────────────


def test_nix_symlink_aborts(tmp_path):
    (tmp_path / "yadgar.service").symlink_to("/nix/store/abc-yadgar/yadgar.service")
    with pytest.raises(NixManagedUnit, match="managed by Nix"):
        guard_nix_symlinks(tmp_path)


def test_regular_units_and_ordinary_symlinks_pass(tmp_path):
    (tmp_path / "yadgar.service").write_text("[Unit]\n")
    (tmp_path / "yadgar-backend.service").symlink_to(tmp_path / "yadgar.service")
    guard_nix_symlinks(tmp_path)  # must not raise


def test_missing_output_dir_is_not_an_error(tmp_path):
    guard_nix_symlinks(tmp_path / "does-not-exist")


def test_guard_scope_is_the_two_flake_managed_units(tmp_path):
    """Ported at its current scope: ``generate_systemd.sh:100`` loops these two.

    Widening it to all nine would be a behaviour change, not a port — a nix user
    with a hand-placed timer symlink would start failing an install that
    succeeds today.
    """
    assert NIX_GUARDED_UNITS == ("yadgar.service", "yadgar-backend.service")
    (tmp_path / "yadgar-vacuum.timer").symlink_to("/nix/store/abc/yadgar-vacuum.timer")
    guard_nix_symlinks(tmp_path)


# ── Render-time side effects ─────────────────────────────────────────────────


def test_trigger_dir_is_created_and_idempotent(tmp_path):
    # NOT tmp_path/"state": conftest's isolate_yadgar_paths already points
    # XDG_STATE_HOME there and creates it, which would mask the absent-file arm.
    state = tmp_path / "install-state"
    triggers = ensure_trigger_dir(state)
    assert triggers == state / "triggers"
    assert triggers.is_dir()
    assert ensure_trigger_dir(state) == triggers  # second call must not raise


def test_upgrade_env_is_seeded_when_absent(tmp_path):
    path, seeded = seed_upgrade_env(
        tmp_path / "install-state", "docker.io/openfantasy/yadgar:1.2.3"
    )
    assert seeded is True
    assert path.read_text() == "YADGAR_IMAGE_TAG=docker.io/openfantasy/yadgar:1.2.3\n"


def test_upgrade_env_is_never_overwritten(tmp_path):
    """The upgrade orchestrator owns this file after the first install.

    Clobbering it on reinstall rolls the host back to the tag it was originally
    installed with — a silent downgrade on the next restart.
    """
    # NOT tmp_path/"state": conftest's isolate_yadgar_paths already points
    # XDG_STATE_HOME there and creates it, which would mask the absent-file arm.
    state = tmp_path / "install-state"
    state.mkdir()
    existing = state / "upgrade.env"
    existing.write_text("YADGAR_IMAGE_TAG=docker.io/openfantasy/yadgar:9.9.9\n")
    path, seeded = seed_upgrade_env(state, "docker.io/openfantasy/yadgar:1.2.3")
    assert seeded is False
    assert path.read_text() == "YADGAR_IMAGE_TAG=docker.io/openfantasy/yadgar:9.9.9\n"


# ── write_units: stamp, stage, validate, move (task:0110 Stage D) ─────────────


def test_write_units_stamps_and_installs_every_unit(tmp_path):
    out = tmp_path / "user"
    written = write_units({"a.service": "[Unit]\n", "b.timer": "[Timer]\n"}, out, "1.2.3")
    assert {p.name for p in written} == {"a.service", "b.timer"}
    assert (out / "a.service").read_text() == (
        f"# yadgar-unit-schema: {UNIT_SCHEMA_VERSION}\n# rendered-by: yadgar 1.2.3\n[Unit]\n"
    )


def test_write_units_leaves_no_staging_dir_behind(tmp_path):
    out = tmp_path / "user"
    write_units({"a.service": "[Unit]\n"}, out, "1.2.3")
    assert [p.name for p in out.iterdir()] == ["a.service"], (
        "the staging dir must be gone — systemd reads this directory, and a "
        "leftover .yadgar-render-<pid>/ is at best noise and at worst a partial set"
    )


def test_a_failed_unit_installs_nothing_at_all(tmp_path):
    """Validation runs on the STAGED copies, so a bad unit blocks the whole set.

    Plan §9.3: the shell renderer wrote each unit straight into the output dir
    one at a time, so an abort halfway left a mixed-generation set with no clean
    recovery. Here the units that DID render must not reach the output dir
    either — a coherent old set beats a partial new one.
    """
    out = tmp_path / "user"
    out.mkdir()
    (out / "a.service").write_text("[Unit]\nDescription=previous\n")
    with pytest.raises(UnitValidationFailed) as exc:
        write_units({"a.service": "[Unit]\n", "b.timer": "   "}, out, "1.2.3")
    assert "b.timer" in str(exc.value)
    assert (out / "a.service").read_text() == "[Unit]\nDescription=previous\n"
    assert [p.name for p in out.iterdir()] == ["a.service"]


def test_unit_validation_failure_is_an_install_abort():
    assert issubclass(UnitValidationFailed, InstallAborted)


def test_write_units_rejects_a_unit_with_no_section_header(tmp_path):
    """A body that is all comments renders, installs, and does nothing at all."""
    with pytest.raises(UnitValidationFailed, match="no section header"):
        write_units({"a.service": "# only a comment\n"}, tmp_path / "user", "1.2.3")


def test_stamp_is_prepended_not_interleaved():
    """systemd tolerates a comment anywhere, but the wrapper parses the FIRST line."""
    assert stamp_unit("[Unit]\n", "9.9.9").startswith(
        f"# yadgar-unit-schema: {UNIT_SCHEMA_VERSION}\n"
    )
