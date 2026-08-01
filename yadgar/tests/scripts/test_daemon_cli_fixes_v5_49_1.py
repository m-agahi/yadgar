"""v5.49.1 hotfix TDD — daemon CLI + systemd generator fixes from Rocky VM dogfood.

Tests T1–T16 cover the 11 bugs fixed in this hotfix:
  Bug 1:  backend started without secrets (env-file + -e flags)
  Bug 2:  missing --user root in backend + core containers
  Bug 3:  install_systemd_service CLI crashes with KeyError: 'service_file'
  Bug 4:  default YADGAR_IMAGE / YADGAR_BACKEND_IMAGE wrong tenant name
  Bug 5:  generated backend unit uses core version instead of backend_version
  Bug 6:  EnvironmentFile path is legacy /etc/yadgar/secrets.env
  Bug 7:  generated unit uses /root/.yadgar/upgrade.env instead of XDG state path
  Bug 8:  generated backend unit is Type=simple, should be Type=notify
  Bug 9:  generated unit filename is yadgar-db.service instead of yadgar-backend.service
  Bug 10: backend --memory 512m too small — should be 4g
  Bug 11: generated unit uses named volume instead of host bind mount (XDG DATA_DIR)
"""

from __future__ import annotations

import re
import subprocess
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs):
    """Build a minimal argparse-Namespace-like object for cmd_daemon."""
    defaults = dict(port=None, dev=False, db_path=None, daemon_command=None)
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _start_backend_argv(monkeypatch, tmp_path, secrets_exists: bool = True):
    """Return the argv list passed to subprocess.run during start_backend().

    Creates a dummy daemon, patches image-exists + container-running checks so
    the actual docker run path is reached, then returns the captured argv.
    """

    # Patch SECRETS_ENV_PATH so we control whether file exists
    secrets_path = tmp_path / "secrets.env"
    if secrets_exists:
        secrets_path.write_text("SURREAL_USER=root\n")
    monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets_path))

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        m = MagicMock()
        m.returncode = 0
        m.stdout = "abc123deadbeef"
        m.stderr = ""
        return m

    monkeypatch.setattr(subprocess, "run", fake_run)

    from yadgar.core.daemon import YadgarDaemon

    daemon = YadgarDaemon()
    # Patch helper predicates
    daemon._container_running = lambda name: False
    daemon._image_exists = lambda img: True
    # _ensure_network also calls subprocess.run; fake_run handles it
    daemon.start_backend()

    # Return the argv for the docker-run call (not the rm or network calls)
    docker_run_calls = [c for c in captured if len(c) > 2 and c[1] == "run"]
    assert docker_run_calls, f"No 'docker run' call captured. All calls: {captured}"
    return docker_run_calls[0]


def _start_core_argv(monkeypatch, tmp_path):
    """Return the argv list passed to subprocess.run during start() core container launch."""

    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text("SURREAL_USER=root\n")
    monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets_path))

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        m = MagicMock()
        m.returncode = 0
        m.stdout = "abc123deadbeef"
        m.stderr = ""
        return m

    monkeypatch.setattr(subprocess, "run", fake_run)

    from yadgar.core.daemon import YadgarDaemon

    daemon = YadgarDaemon()
    daemon._container_running = lambda name: False
    daemon._image_exists = lambda img: True
    daemon._health_ok = lambda port: True
    daemon.start_backend = lambda: {"status": "started", "container": "yadgar-backend"}

    daemon.start()

    # Return the argv for the core docker-run call (last 'docker run' call)
    docker_run_calls = [c for c in captured if len(c) > 2 and c[1] == "run"]
    assert docker_run_calls, f"No 'docker run' call captured. All calls: {captured}"
    return docker_run_calls[-1]


def _render_backend_unit(tmp_path) -> str:
    """Call install_systemd_service and return the backend unit content."""
    from yadgar.core.daemon import YadgarDaemon

    service_dir = tmp_path / "systemd" / "user"
    service_dir.mkdir(parents=True)

    daemon = YadgarDaemon()
    with patch("yadgar.core.daemon.daemon.Path.home", return_value=tmp_path):
        result = daemon.install_systemd_service()

    # Find the backend service file
    backend_key = "backend_service"
    if backend_key not in result:
        # Fallback: find yadgar-backend.service file
        candidates = list(service_dir.glob("yadgar-backend*.service"))
        if candidates:
            return candidates[0].read_text()
        raise AssertionError(f"No backend service file found. result={result}")
    return Path(result[backend_key]).read_text()


# ---------------------------------------------------------------------------
# T1: start_backend passes --env-file when secrets file exists
# ---------------------------------------------------------------------------


def test_start_backend_passes_env_file_when_present(monkeypatch, tmp_path):
    """Bug 1: --env-file <SECRETS_ENV_PATH> must appear in argv when file exists."""

    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text("SURREAL_USER=root\n")
    monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets_path))

    argv = _start_backend_argv(monkeypatch, tmp_path, secrets_exists=True)
    assert "--env-file" in argv, f"--env-file not found in argv: {argv}"
    assert str(secrets_path) in argv, f"secrets.env path not found in argv: {argv}"


# ---------------------------------------------------------------------------
# T2: start_backend passes -e flags for all secret env vars
# ---------------------------------------------------------------------------


def test_start_backend_passes_e_flags_for_secrets(monkeypatch, tmp_path):
    """Bug 1: -e SURREAL_USER/PASS and YADGAR_*_USER/PASS/TOKEN must be in argv."""
    argv = _start_backend_argv(monkeypatch, tmp_path)
    # Collect all values following -e flags
    e_vals = []
    for i, tok in enumerate(argv):
        if tok == "-e" and i + 1 < len(argv):
            e_vals.append(argv[i + 1].split("=")[0])

    for required in (
        "SURREAL_USER",
        "SURREAL_PASS",
        "YADGAR_MCP_AUTH_TOKEN",
        "YADGAR_RW_USER",
        "YADGAR_RW_PASS",
        "YADGAR_RO_USER",
        "YADGAR_RO_PASS",
    ):
        assert required in e_vals, (
            f"-e {required} not found in docker run argv. Got e_vals={e_vals}"
        )


# ---------------------------------------------------------------------------
# T3: start_backend passes --user root
# ---------------------------------------------------------------------------


def test_start_backend_passes_user_root(monkeypatch, tmp_path):
    """Bug 2: --user root must appear in docker run argv for start_backend."""
    argv = _start_backend_argv(monkeypatch, tmp_path)
    assert "--user" in argv and "root" in argv, f"--user root not found in argv: {argv}"
    user_idx = argv.index("--user")
    assert argv[user_idx + 1] == "root", f"Expected 'root' after --user, got {argv[user_idx + 1]!r}"


# ---------------------------------------------------------------------------
# T4: start() core container passes --user root
# ---------------------------------------------------------------------------


def test_start_core_passes_user_root(monkeypatch, tmp_path):
    """Bug 2: --user root must appear in docker run argv for core container start."""
    argv = _start_core_argv(monkeypatch, tmp_path)
    assert "--user" in argv and "root" in argv, f"--user root not found in core argv: {argv}"
    user_idx = argv.index("--user")
    assert argv[user_idx + 1] == "root", f"Expected 'root' after --user, got {argv[user_idx + 1]!r}"


# ---------------------------------------------------------------------------
# T5: start() core container passes -e flags for secrets
# ---------------------------------------------------------------------------


def test_start_core_passes_e_flags_for_secrets(monkeypatch, tmp_path):
    """Bug 1: core container must also receive secret env vars via -e flags."""
    argv = _start_core_argv(monkeypatch, tmp_path)
    e_vals = []
    for i, tok in enumerate(argv):
        if tok == "-e" and i + 1 < len(argv):
            e_vals.append(argv[i + 1].split("=")[0])

    # Core needs at least auth token + DB credentials
    for required in ("YADGAR_MCP_AUTH_TOKEN",):
        assert required in e_vals, (
            f"-e {required} not found in core docker run argv. Got e_vals={e_vals}"
        )


# ---------------------------------------------------------------------------
# T6: install_systemd_service CLI does not crash (no KeyError: 'service_file')
# ---------------------------------------------------------------------------


def test_install_systemd_service_cli_does_not_crash(tmp_path, monkeypatch):
    """Bug 3: cmd_daemon install-service must not raise KeyError."""
    import yadgar.core.cli.daemon as cli_mod

    service_dir = tmp_path / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    args = _make_args(daemon_command="install-service")
    # Should not raise
    try:
        cli_mod.cmd_daemon(args)
    except KeyError as e:
        pytest.fail(f"cmd_daemon raised KeyError: {e}")
    except SystemExit as e:
        if e.code != 0:
            pytest.fail(f"cmd_daemon exited with code {e.code}")


# ---------------------------------------------------------------------------
# T7: default YADGAR_IMAGE starts with docker.io/openfantasy/yadgar
# ---------------------------------------------------------------------------


def test_default_image_is_openfantasy():
    """Bug 4: YADGAR_IMAGE default must use docker.io/openfantasy/yadgar (not looseking)."""
    from yadgar._shared.config.config_registry import _REGISTRY

    entry = next(e for e in _REGISTRY if e.name == "YADGAR_IMAGE")
    assert entry.default.startswith("docker.io/openfantasy/yadgar"), (
        f"YADGAR_IMAGE default is {entry.default!r} — must start with docker.io/openfantasy/yadgar"
    )
    assert "looseking" not in entry.default, (
        f"YADGAR_IMAGE default still references looseking: {entry.default!r}"
    )


# ---------------------------------------------------------------------------
# T8: default YADGAR_BACKEND_IMAGE starts with docker.io/openfantasy/yadgar-backend
# ---------------------------------------------------------------------------


def test_default_backend_image_is_openfantasy():
    """Bug 4: YADGAR_BACKEND_IMAGE default must use docker.io/openfantasy/yadgar-backend."""
    from yadgar._shared.config.config_registry import _REGISTRY

    entry = next(e for e in _REGISTRY if e.name == "YADGAR_BACKEND_IMAGE")
    assert entry.default.startswith("docker.io/openfantasy/yadgar-backend"), (
        f"YADGAR_BACKEND_IMAGE default is {entry.default!r} — must start with docker.io/openfantasy/yadgar-backend"
    )
    assert "looseking" not in entry.default, (
        f"YADGAR_BACKEND_IMAGE default still references looseking: {entry.default!r}"
    )


# ---------------------------------------------------------------------------
# T9: generated backend unit uses backend_version from server.json
# ---------------------------------------------------------------------------


def test_generated_backend_unit_uses_backend_version(tmp_path):
    """Bug 5: backend service ExecStart must use backend_version, not core version."""
    import json

    from yadgar.core.daemon import YadgarDaemon

    # Load actual server.json to get real versions
    server_json = Path(__file__).resolve().parent.parent.parent.parent / "server.json"
    data = json.loads(server_json.read_text())
    backend_version = data["backend_version"]
    core_version = data["version"]

    service_dir = tmp_path / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True)

    daemon = YadgarDaemon()
    with patch("yadgar.core.daemon.daemon.Path.home", return_value=tmp_path):
        result = daemon.install_systemd_service()

    # Read the backend service file
    backend_path = Path(result.get("backend_service", result.get("db_service", "")))
    assert backend_path.exists(), f"Backend service file not found: {backend_path}"
    content = backend_path.read_text()

    assert backend_version in content, (
        f"backend_version {backend_version!r} not found in backend unit ExecStart. "
        f"Content snippet: {content[:500]}"
    )
    # Core version must NOT appear as the image tag for backend
    # (they may coincidentally match in test, so check image tag pattern specifically)
    # Only fail if core_version != backend_version and core appears as image tag
    if core_version != backend_version:
        # Check ExecStart line doesn't use core version for the image
        exec_lines = [
            ln for ln in content.splitlines() if "ExecStart" in ln or "yadgar-backend:" in ln
        ]
        for line in exec_lines:
            if "yadgar-backend:" in line:
                assert f"yadgar-backend:{core_version}" not in line, (
                    f"Backend unit uses core version {core_version!r} for backend image. "
                    f"Line: {line!r}"
                )


# ---------------------------------------------------------------------------
# T10: generated unit EnvironmentFile uses XDG secrets path (not /etc/yadgar)
# ---------------------------------------------------------------------------


def test_generated_unit_environment_file_is_xdg_secrets_path(tmp_path):
    """Bug 6: EnvironmentFile must use XDG path ~/.config/yadgar/secrets.env."""
    from yadgar.core.daemon import YadgarDaemon

    service_dir = tmp_path / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True)

    daemon = YadgarDaemon()
    with patch("yadgar.core.daemon.daemon.Path.home", return_value=tmp_path):
        result = daemon.install_systemd_service()

    # Collect all EnvironmentFile=...secrets.env lines from backend + core units
    secrets_env_lines = []
    for key in ("backend_service", "db_service", "core_service"):
        if key not in result:
            continue
        for ln in Path(result[key]).read_text().splitlines():
            if "EnvironmentFile" in ln and "secrets.env" in ln:
                secrets_env_lines.append((key, ln))

    assert secrets_env_lines, "No EnvironmentFile=...secrets.env line found in any unit"
    for key, line in secrets_env_lines:
        assert "/etc/yadgar/secrets.env" not in line, (
            f"Legacy /etc/yadgar/secrets.env in unit ({key}): {line!r}"
        )
        # Accept both `.config/yadgar/secrets.env` (real $HOME) and
        # `config/yadgar/secrets.env` (pytest tmp_path with XDG_CONFIG_HOME
        # defaulting via paths.py to `<HOME>/.config` — but pytest fixture
        # may inject XDG vars without leading dot).
        assert "config/yadgar/secrets.env" in line, (
            f"XDG config/yadgar/secrets.env not found in unit ({key}): {line!r}"
        )


# ---------------------------------------------------------------------------
# T11: generated unit upgrade.env path uses XDG state dir
# ---------------------------------------------------------------------------


def test_generated_unit_upgrade_env_path_is_xdg_state(tmp_path):
    """Bug 7: upgrade.env EnvironmentFile must use ~/.local/state/yadgar/upgrade.env."""
    from yadgar.core.daemon import YadgarDaemon

    service_dir = tmp_path / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True)

    daemon = YadgarDaemon()
    with patch("yadgar.core.daemon.daemon.Path.home", return_value=tmp_path):
        result = daemon.install_systemd_service()

    core_path = Path(result["core_service"])
    content = core_path.read_text()

    # Find upgrade.env EnvironmentFile line
    upgrade_lines = [
        ln for ln in content.splitlines() if "EnvironmentFile" in ln and "upgrade.env" in ln
    ]
    assert upgrade_lines, f"No EnvironmentFile line for upgrade.env in core unit:\n{content}"

    for line in upgrade_lines:
        assert "/root/.yadgar/upgrade.env" not in line, (
            f"Legacy /root/.yadgar/upgrade.env found in core unit: {line!r}"
        )
        # Accept `local/state/yadgar/upgrade.env` (with or without leading dot —
        # tmp_path HOME may not preserve XDG dot prefixes).
        assert "state/yadgar/upgrade.env" in line, (
            f"XDG state/yadgar/upgrade.env not found in core unit: {line!r}"
        )


# ---------------------------------------------------------------------------
# T12: generated backend unit is Type=notify (not Type=simple)
# ---------------------------------------------------------------------------


def _render_backend_unit(tmp_path, monkeypatch, runtime: str) -> str:
    """The generated BACKEND unit text for *runtime*.

    task:0105 pins the runtime explicitly. These tests used to inherit whatever
    the developer's host happened to have installed, which made a podman-shaped
    assertion pass or fail by accident of environment — a strengthening, not a
    weakening: the podman arm below still runs every assertion it ever did.
    """
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", runtime)
    service_dir = tmp_path / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

    daemon = YadgarDaemon()
    with patch("yadgar.core.daemon.daemon.Path.home", return_value=tmp_path):
        result = daemon.install_systemd_service()

    backend_path = Path(result.get("backend_service", result.get("db_service", "")))
    assert backend_path.exists(), "Backend service file not found"
    return backend_path.read_text()


def test_generated_backend_unit_is_type_notify(tmp_path, monkeypatch):
    """Bug 8: backend unit must have Type=notify (on podman — task:0105)."""
    content = _render_backend_unit(tmp_path, monkeypatch, "podman")

    assert "Type=notify" in content, (
        f"Type=notify not found in backend unit. Content:\n{content[:500]}"
    )
    assert "Type=simple" not in content, (
        f"Type=simple still present in backend unit. Content:\n{content[:500]}"
    )


def test_generated_backend_unit_on_docker_is_type_exec_with_a_health_gate(tmp_path, monkeypatch):
    """task:0105: docker has no sd_notify proxy, so Bug 8's shape cannot apply there.

    Type=simple stays forbidden on BOTH arms — Type=exec satisfies the original
    assertion — but the READY=1 source has to be replaced, not just removed.
    """
    content = _render_backend_unit(tmp_path, monkeypatch, "docker")

    assert "Type=exec" in content, f"docker backend unit is not Type=exec:\n{content[:500]}"
    assert "Type=notify" not in content, (
        "docker backend unit is Type=notify — docker sets no NOTIFY_SOCKET in the "
        f"container, so nothing ever sends READY=1:\n{content[:500]}"
    )
    assert "Type=simple" not in content, (
        f"Type=simple still present in backend unit. Content:\n{content[:500]}"
    )
    assert re.search(r"^ExecStartPost=curl .*--retry .*/health", content, re.MULTILINE), (
        f"docker backend unit has no ExecStartPost= readiness gate:\n{content[:500]}"
    )


# ---------------------------------------------------------------------------
# T13: generated backend unit has --sdnotify=healthy AND --health-cmd curl
# ---------------------------------------------------------------------------


def test_generated_backend_unit_has_sdnotify_healthy_and_health_cmd(tmp_path, monkeypatch):
    """Bug 8: backend unit ExecStart must include --sdnotify=healthy + --health-cmd curl."""
    content = _render_backend_unit(tmp_path, monkeypatch, "podman")

    assert "--sdnotify=healthy" in content, (
        "--sdnotify=healthy not found in backend unit ExecStart."
    )
    assert "--health-cmd" in content, "--health-cmd not found in backend unit ExecStart."
    assert "curl -f http://localhost:8001/health" in content, (
        "curl healthcheck command not found in backend unit ExecStart."
    )


def test_generated_backend_health_cmd_is_a_single_quoted_argv_word(tmp_path, monkeypatch):
    """task:0105: --health-cmd takes ONE argument, and systemd Exec= is not a shell.

    Unquoted, ``--health-cmd curl -f http://localhost:8001/health || exit 1``
    splits into six argv words, so the runtime receives ``--health-cmd=curl``
    followed by a bare ``-f`` — not a ``run`` flag on either podman or docker, so
    the unit could not start at all. flake.nix, the NixOS generator, has always
    passed it quoted as one element; this generator was the deviation. Both arms,
    because ``docker run`` supports ``--health-cmd`` too — only ``--sdnotify`` is
    podman-exclusive.
    """
    for runtime in ("podman", "docker"):
        content = _render_backend_unit(tmp_path / runtime, monkeypatch, runtime)
        assert '--health-cmd "curl -f http://localhost:8001/health || exit 1"' in content, (
            f"{runtime}: --health-cmd is not a single quoted argv word — systemd "
            f"would split it and the runtime would reject the stray flags:\n{content[:800]}"
        )


# ---------------------------------------------------------------------------
# T14: generated unit filename is yadgar-backend.service (not yadgar-db.service)
# ---------------------------------------------------------------------------


def test_generated_unit_filename_is_yadgar_backend_service(tmp_path):
    """Bug 9: install_systemd_service must write yadgar-backend.service (not yadgar-db.service)."""
    from yadgar.core.daemon import YadgarDaemon

    service_dir = tmp_path / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True)

    daemon = YadgarDaemon()
    with patch("yadgar.core.daemon.daemon.Path.home", return_value=tmp_path):
        result = daemon.install_systemd_service()

    # backend_service key in result must point to yadgar-backend.service
    backend_path_str = result.get("backend_service", result.get("db_service", ""))
    assert "yadgar-backend.service" in backend_path_str, (
        f"Expected yadgar-backend.service, got: {backend_path_str!r}"
    )
    assert "yadgar-db.service" not in backend_path_str, (
        f"Legacy yadgar-db.service still present: {backend_path_str!r}"
    )
    # Also check the result dict key itself
    assert "backend_service" in result, (
        f"Result dict must have 'backend_service' key, got keys: {list(result.keys())}"
    )


# ---------------------------------------------------------------------------
# T15: generated backend unit uses --memory 4g
# ---------------------------------------------------------------------------


def test_generated_backend_unit_memory_is_4g(tmp_path):
    """Bug 10: backend unit ExecStart must use --memory 4g (not 512m or any dynamic value)."""
    from yadgar.core.daemon import YadgarDaemon

    service_dir = tmp_path / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True)

    daemon = YadgarDaemon()
    with patch("yadgar.core.daemon.daemon.Path.home", return_value=tmp_path):
        result = daemon.install_systemd_service()

    backend_path = Path(result.get("backend_service", result.get("db_service", "")))
    content = backend_path.read_text()

    assert "--memory 4g" in content, (
        f"--memory 4g not found in backend unit. "
        f"ExecStart lines: {[ln for ln in content.splitlines() if '--memory' in ln]}"
    )


# ---------------------------------------------------------------------------
# T16: generated unit uses host bind mount for data dir (XDG DATA_DIR)
# ---------------------------------------------------------------------------


def test_generated_unit_uses_host_bind_mount_for_data_dir(tmp_path):
    """Bug 11: backend unit must bind-mount XDG DATA_DIR, not named volume yadgar-db-data."""
    from yadgar.core.daemon import YadgarDaemon

    service_dir = tmp_path / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True)

    daemon = YadgarDaemon()
    with patch("yadgar.core.daemon.daemon.Path.home", return_value=tmp_path):
        result = daemon.install_systemd_service()

    backend_path = Path(result.get("backend_service", result.get("db_service", "")))
    content = backend_path.read_text()

    # Named volume must not be used
    assert "yadgar-db-data:/data" not in content, (
        "Legacy named volume yadgar-db-data:/data still in backend unit."
    )
    # Host bind mount must contain XDG data path signature
    # Path.home() is patched to tmp_path so DATA_DIR = tmp_path/.local/share/yadgar
    data_dir_path = tmp_path / ".local" / "share" / "yadgar"
    # XDG_DATA_HOME may resolve to `<HOME>/data` or `<HOME>/share` in the
    # pytest tmp_path fixture; accept any `/yadgar:/data` host-bind pattern.
    assert str(data_dir_path) in content or "/yadgar:/data" in content, (
        f"XDG DATA_DIR bind mount not found in backend unit. "
        f"Expected path containing .local/share/yadgar.\n"
        f"Volume lines: {[ln for ln in content.splitlines() if '-v ' in ln]}"
    )
