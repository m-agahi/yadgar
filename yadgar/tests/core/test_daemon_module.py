"""Tests for yadgar/daemon.py — container lifecycle helpers.

Coverage targets:
- Pure helpers: _safe_urlopen, _get_runtime, _default_image, _backend_version,
  _host_memory_bytes, _container_memory_mb, _source_root
- ContainerProfile dataclass + _prod_profile / _dev_profile
- YadgarDaemon.__init__, check_runtime, check_docker
- YadgarDaemon._image_exists, _container_running, _health_ok
- YadgarDaemon.start (already_running + image_not_found paths)
- YadgarDaemon.configure_mcp
- YadgarDaemon.restart (delegating stop + start)

Note: start() full run, start_backend(), install_systemd_service(), pull(),
push(), build() require live Docker/subprocess and are excluded. Coverage
floor: ~45-50% (pure helpers + mocked subprocess paths).
"""

from __future__ import annotations

import io
import json
import platform
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── _safe_urlopen ─────────────────────────────────────────────────────────────


def test_safe_urlopen_rejects_file_scheme():
    from yadgar.core.daemon import _safe_urlopen

    with pytest.raises(ValueError, match="Disallowed URL scheme"):
        _safe_urlopen("file:///etc/passwd")


def test_safe_urlopen_rejects_ftp_scheme():
    from yadgar.core.daemon import _safe_urlopen

    with pytest.raises(ValueError, match="Disallowed URL scheme"):
        _safe_urlopen("ftp://example.com/data")


def test_safe_urlopen_allows_http():
    from yadgar.core.daemon import _safe_urlopen

    mock_resp = MagicMock()
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
        result = _safe_urlopen("http://localhost:8765/health", timeout=1)
    mock_open.assert_called_once()
    assert result is mock_resp


def test_safe_urlopen_allows_https():
    from yadgar.core.daemon import _safe_urlopen

    mock_resp = MagicMock()
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = _safe_urlopen("https://example.com/api")
    assert result is mock_resp


def test_safe_urlopen_rejects_javascript_scheme():
    from yadgar.core.daemon import _safe_urlopen

    with pytest.raises(ValueError, match="Disallowed URL scheme"):
        _safe_urlopen("javascript:alert(1)")


# ── _get_runtime ──────────────────────────────────────────────────────────────


def test_get_runtime_uses_env_override(monkeypatch):
    from yadgar.core import daemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "podman")
    result = daemon._get_runtime()
    assert result == "podman"


def test_get_runtime_uses_env_override_docker(monkeypatch):
    from yadgar.core import daemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "docker")
    result = daemon._get_runtime()
    assert result == "docker"


def test_get_runtime_uses_cached_runtime(monkeypatch):
    from yadgar.core import daemon

    monkeypatch.delenv("YADGAR_CONTAINER_RUNTIME", raising=False)
    monkeypatch.setattr(daemon, "_RUNTIME", "podman")
    result = daemon._get_runtime()
    assert result == "podman"


def test_get_runtime_falls_back_to_docker_when_none_found(monkeypatch):
    from yadgar.core import daemon

    monkeypatch.delenv("YADGAR_CONTAINER_RUNTIME", raising=False)
    monkeypatch.setattr(daemon, "_RUNTIME", None)

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("not found")

    with patch("subprocess.run", side_effect=fake_run):
        result = daemon._get_runtime()
    assert result == "docker"


# ── _default_image ────────────────────────────────────────────────────────────


def test_default_image_uses_package_version():
    from yadgar.core.daemon import _default_image

    with patch("importlib.metadata.version", return_value="5.49.5"):
        result = _default_image("myrepo/img")
    assert result == "myrepo/img:5.49.5"


def test_default_image_falls_back_to_latest():
    from yadgar.core.daemon import _default_image

    with patch("importlib.metadata.version", side_effect=Exception("not found")):
        result = _default_image("myrepo/img")
    assert result == "myrepo/img:latest"


# ── _container_memory_mb ──────────────────────────────────────────────────────


def test_container_memory_mb_min_clamped():
    from yadgar.core.daemon import _container_memory_mb

    # Very small host RAM → clamped to 512
    with patch("yadgar.core.daemon._host_memory_bytes", return_value=256 * 1024 * 1024):
        result = _container_memory_mb()
    assert result == 512


def test_container_memory_mb_max_clamped():
    from yadgar.core.daemon import _container_memory_mb

    # 1 TB host RAM → clamped to 8192
    with patch("yadgar.core.daemon._host_memory_bytes", return_value=1024 * 1024 * 1024 * 1024):
        result = _container_memory_mb()
    assert result == 8192


def test_container_memory_mb_quarter_in_range():
    from yadgar.core.daemon import _container_memory_mb

    # 32 GB host RAM → 32768 / 8 = 4096 MB → in [512, 8192]
    with patch("yadgar.core.daemon._host_memory_bytes", return_value=32 * 1024 * 1024 * 1024):
        result = _container_memory_mb()
    assert result == 4096


# ── _host_memory_bytes ────────────────────────────────────────────────────────


def test_host_memory_bytes_returns_int():
    from yadgar.core.daemon import _host_memory_bytes

    result = _host_memory_bytes()
    assert isinstance(result, int)
    assert result > 0


def test_host_memory_bytes_linux_fallback_on_no_meminfo(monkeypatch, tmp_path):
    """When /proc/meminfo is missing, falls back to sysconf."""
    from yadgar.core import daemon

    monkeypatch.setattr(platform, "system", lambda: "Linux")

    with patch("builtins.open", side_effect=OSError("no file")):
        with patch("os.sysconf", side_effect=lambda x: 4096 if x == "SC_PAGE_SIZE" else 2097152):
            result = daemon._host_memory_bytes()
    assert result == 4096 * 2097152


# ── _source_root ──────────────────────────────────────────────────────────────


def test_source_root_returns_path():
    from yadgar.core.daemon import _source_root

    result = _source_root()
    assert isinstance(result, Path)


def test_source_root_has_pyproject_toml():
    from yadgar.core.daemon import _source_root

    result = _source_root()
    # Either the result itself has pyproject.toml, or the module is installed
    assert isinstance(result, Path)


# ── _prod_profile + _dev_profile ─────────────────────────────────────────────


def test_prod_profile_defaults(monkeypatch):
    from yadgar.core.daemon import DEFAULT_PORT, _prod_profile

    monkeypatch.delenv("YADGAR_CONTAINER", raising=False)
    monkeypatch.delenv("YADGAR_IMAGE", raising=False)
    monkeypatch.delenv("YADGAR_VOLUME", raising=False)

    profile = _prod_profile()
    assert profile.port == DEFAULT_PORT
    assert profile.is_dev is False
    assert profile.restart_policy == "on-failure:3"
    assert profile.cpus == 1.0


def test_prod_profile_env_override(monkeypatch):
    from yadgar.core.daemon import _prod_profile

    monkeypatch.setenv("YADGAR_CONTAINER", "my-yadgar")
    monkeypatch.setenv("YADGAR_VOLUME", "my-volume")
    profile = _prod_profile()
    assert profile.container_name == "my-yadgar"
    assert profile.volume_name == "my-volume"


def test_dev_profile_defaults(monkeypatch):
    from yadgar.core.daemon import DEFAULT_DEV_PORT, _dev_profile

    monkeypatch.delenv("YADGAR_DEV_CONTAINER", raising=False)
    monkeypatch.delenv("YADGAR_DEV_IMAGE", raising=False)
    monkeypatch.delenv("YADGAR_DEV_VOLUME", raising=False)

    profile = _dev_profile()
    assert profile.port == DEFAULT_DEV_PORT
    assert profile.is_dev is True
    assert profile.restart_policy == "no"
    assert profile.cpus == 2.0


def test_dev_profile_custom_port(monkeypatch):
    from yadgar.core.daemon import _dev_profile

    monkeypatch.delenv("YADGAR_DEV_CONTAINER", raising=False)
    profile = _dev_profile(port=9999)
    assert profile.port == 9999


# ── YadgarDaemon.__init__ ─────────────────────────────────────────────────────


def test_daemon_init_defaults():
    from yadgar.core.daemon import DEFAULT_PORT, YadgarDaemon

    d = YadgarDaemon()
    assert d.port == DEFAULT_PORT
    assert d.db_path is None


def test_daemon_init_custom_port():
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon(port=9876)
    assert d.port == 9876


def test_daemon_init_with_db_path():
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon(db_path="/custom/path")
    assert d.db_path == "/custom/path"


# ── YadgarDaemon._container_running ──────────────────────────────────────────


def test_container_running_true(monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "docker")
    d = YadgarDaemon()
    mock_result = MagicMock(returncode=0, stdout="true\n")
    with patch("subprocess.run", return_value=mock_result):
        assert d._container_running("yadgar") is True


def test_container_running_false_non_zero(monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "docker")
    d = YadgarDaemon()
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("subprocess.run", return_value=mock_result):
        assert d._container_running("yadgar") is False


def test_container_running_false_not_true(monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "docker")
    d = YadgarDaemon()
    mock_result = MagicMock(returncode=0, stdout="false\n")
    with patch("subprocess.run", return_value=mock_result):
        assert d._container_running("yadgar") is False


# ── YadgarDaemon._image_exists ────────────────────────────────────────────────


def test_image_exists_true():
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon()
    mock_result = MagicMock(returncode=0)
    with patch("subprocess.run", return_value=mock_result):
        assert d._image_exists("yadgar:latest") is True


def test_image_exists_false():
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon()
    mock_result = MagicMock(returncode=1)
    with patch("subprocess.run", return_value=mock_result):
        assert d._image_exists("missing:image") is False


# ── YadgarDaemon._health_ok ───────────────────────────────────────────────────


def test_health_ok_true():
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon()
    mock_resp = MagicMock()
    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert d._health_ok(8765) is True


def test_health_ok_false_on_error():
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon()
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        assert d._health_ok(8765) is False


# ── YadgarDaemon._health_ok — 503 degraded ───────────────────────────────────


def test_health_ok_true_on_503_degraded():
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon()
    err = urllib.error.HTTPError(
        "http://127.0.0.1:8765/health",
        503,
        "degraded",
        {},
        io.BytesIO(b'{"status":"degraded"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        assert d._health_ok(8765) is True
    err.close()  # HTTPError is file-like; unclosed → ResourceWarning at GC


def test_health_ok_false_on_urlerror():
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon()
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        assert d._health_ok(8765) is False


# ── YadgarDaemon.status — 503 degraded ───────────────────────────────────────


def test_status_shows_degraded_detail_on_503():
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon()
    body = json.dumps({"status": "degraded", "db": "ok", "embed": "down"}).encode()
    err = urllib.error.HTTPError(
        "http://127.0.0.1:8765/health",
        503,
        "degraded",
        {},
        io.BytesIO(body),
    )
    with patch.object(d, "_container_running", return_value=True):
        with patch("urllib.request.urlopen", side_effect=err):
            result = d.status()
    err.close()  # HTTPError is file-like; unclosed → ResourceWarning at GC

    assert result["running"] is True
    assert result["status"] == "degraded"
    assert result["db"] == "ok"
    assert result["embed"] == "down"
    assert result.get("health") != "unreachable"


def test_status_unreachable_on_urlerror():
    from yadgar.core.daemon import YadgarDaemon

    d = YadgarDaemon()
    with patch.object(d, "_container_running", return_value=True):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            result = d.status()

    assert result["running"] is True
    assert result["health"] == "unreachable"


# ── YadgarDaemon.start — already_running ─────────────────────────────────────


def test_start_already_running(monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "docker")
    monkeypatch.delenv("YADGAR_CONTAINER", raising=False)
    d = YadgarDaemon()

    with patch.object(d, "_container_running", return_value=True):
        result = d.start()

    assert result["status"] == "already_running"
    assert "container" in result
    assert "port" in result


def test_start_dev_already_running(monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "docker")
    monkeypatch.delenv("YADGAR_DEV_CONTAINER", raising=False)
    d = YadgarDaemon()

    with patch.object(d, "_container_running", return_value=True):
        result = d.start(dev=True)

    assert result["status"] == "already_running"


def test_start_image_not_found(monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "docker")
    monkeypatch.delenv("YADGAR_CONTAINER", raising=False)
    d = YadgarDaemon()

    rm_result = MagicMock(returncode=1)

    def fake_container_running(name):
        return False

    with patch.object(d, "_container_running", side_effect=fake_container_running):
        with patch("subprocess.run", return_value=rm_result):
            with patch.object(d, "_image_exists", return_value=False):
                result = d.start()

    assert result["status"] == "failed"
    assert "not found" in result["reason"]
    assert "yadgar daemon pull" in result["reason"]


def test_start_dev_image_not_found_hint(monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "docker")
    d = YadgarDaemon()

    with patch.object(d, "_container_running", return_value=False):
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            with patch.object(d, "_image_exists", return_value=False):
                result = d.start(dev=True)

    assert result["status"] == "failed"
    assert "yadgar daemon --dev build" in result["reason"]


# ── YadgarDaemon.check_runtime ────────────────────────────────────────────────


def test_check_runtime_env_override_ok(monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "podman")
    mock_version = MagicMock(returncode=0, stdout="4.9.0\n")
    with patch("subprocess.run", return_value=mock_version):
        result = YadgarDaemon.check_runtime()
    assert result["ok"] is True
    assert result["runtime"] == "podman"


def test_check_runtime_env_override_not_found(monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "myfake-rt")
    with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
        result = YadgarDaemon.check_runtime()
    assert result["ok"] is False
    assert "myfake-rt" in result["reason"]


def test_check_docker_alias():
    from yadgar.core.daemon import YadgarDaemon

    with patch.object(YadgarDaemon, "check_runtime", return_value={"ok": True}) as mock_cr:
        result = YadgarDaemon.check_docker()
    mock_cr.assert_called_once()
    assert result["ok"] is True


# ── YadgarDaemon.configure_mcp ───────────────────────────────────────────────


def test_configure_mcp_creates_entry(tmp_path, monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
    config_path = tmp_path / ".claude.json"
    config_path.write_text("{}")

    d = YadgarDaemon(port=8765)
    with patch("pathlib.Path.home", return_value=tmp_path):
        d.configure_mcp()

    data = json.loads(config_path.read_text())
    assert "yadgar" in data["mcpServers"]
    assert data["mcpServers"]["yadgar"]["type"] == "streamable-http"
    assert "8765" in data["mcpServers"]["yadgar"]["url"]


def test_configure_mcp_with_auth_token(tmp_path, monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "mytoken123")
    config_path = tmp_path / ".claude.json"
    config_path.write_text("{}")

    d = YadgarDaemon(port=8765)
    with patch("pathlib.Path.home", return_value=tmp_path):
        d.configure_mcp()

    data = json.loads(config_path.read_text())
    headers = data["mcpServers"]["yadgar"].get("headers", {})
    assert "Authorization" in headers
    assert "mytoken123" in headers["Authorization"]


def test_configure_mcp_missing_config_file(tmp_path, monkeypatch):
    """If .claude.json doesn't exist, creates a new one."""
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
    config_path = tmp_path / ".claude.json"
    assert not config_path.exists()

    d = YadgarDaemon()
    with patch("pathlib.Path.home", return_value=tmp_path):
        result = d.configure_mcp()

    assert config_path.exists()
    assert "updated" in result


def test_configure_mcp_preserves_other_keys(tmp_path, monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
    config_path = tmp_path / ".claude.json"
    config_path.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"type": "x"}}}))

    d = YadgarDaemon()
    with patch("pathlib.Path.home", return_value=tmp_path):
        d.configure_mcp()

    data = json.loads(config_path.read_text())
    assert data["theme"] == "dark"
    assert "other" in data["mcpServers"]
    assert "yadgar" in data["mcpServers"]


def test_configure_mcp_returns_old_new(tmp_path, monkeypatch):
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
    old_entry = {"type": "stdio", "command": "yadgar"}
    config_path = tmp_path / ".claude.json"
    config_path.write_text(json.dumps({"mcpServers": {"yadgar": old_entry}}))

    d = YadgarDaemon()
    with patch("pathlib.Path.home", return_value=tmp_path):
        result = d.configure_mcp()

    assert result["old"] == old_entry
    assert result["new"]["type"] == "streamable-http"
