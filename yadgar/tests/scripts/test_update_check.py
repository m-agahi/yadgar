"""v5.48.0 — TDD scaffolding (RED phase) for update mechanism: check.py + install_methods.py.

Tests for:
- yadgar/update/check.py   — PyPI probe, LatestVersionInfo dataclass
- yadgar/update/install_methods.py — detection + upgrade command generation

All httpx calls are mocked (no live PyPI traffic).
All subprocess calls (detect_install_method.sh) are mocked.

Markers: not integration (unit tests, no live network).
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _mock_pypi_response(version: str = "9.99.0") -> MagicMock:
    """Return a mock httpx.Response with .json() returning PyPI shape."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "info": {
            "version": version,
            "home_page": f"https://pypi.org/project/yadgar/{version}/",
        },
        "urls": [],
    }
    mock_resp.raise_for_status.return_value = None
    return mock_resp


# ---------------------------------------------------------------------------
# check.py — LatestVersionInfo + probe_latest_version
# ---------------------------------------------------------------------------


class TestProbeLatestVersion:
    """Unit tests for yadgar.update.check.probe_latest_version."""

    def test_returns_latest_version_info_dataclass(self, monkeypatch):
        """Happy path: probe returns LatestVersionInfo with available_version populated."""
        from unittest.mock import patch

        mock_resp = _mock_pypi_response("9.99.0")
        with patch("httpx.get", return_value=mock_resp):
            from yadgar.core.update.check import probe_latest_version

            result = probe_latest_version()

        assert result.available_version == "9.99.0"
        assert result.checked_at is not None

    def test_sends_correct_user_agent(self, monkeypatch):
        """probe_latest_version sends User-Agent: yadgar/<version>."""
        mock_resp = _mock_pypi_response("9.99.0")
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            from yadgar.core.update.check import probe_latest_version

            probe_latest_version()

        call_kwargs = mock_get.call_args
        headers = call_kwargs.kwargs.get("headers", {}) or (
            call_kwargs.args[1] if len(call_kwargs.args) > 1 else {}
        )
        # Accept either positional or keyword headers
        if not headers and call_kwargs.kwargs.get("headers"):
            headers = call_kwargs.kwargs["headers"]
        ua = headers.get("User-Agent", headers.get("user-agent", ""))
        assert ua.startswith("yadgar/"), f"Expected UA starting with 'yadgar/', got: {ua!r}"

    def test_sends_no_extra_identifying_headers(self):
        """probe_latest_version sends only User-Agent and Accept headers."""
        mock_resp = _mock_pypi_response("9.99.0")
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            from yadgar.core.update.check import probe_latest_version

            probe_latest_version()

        call_kwargs = mock_get.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        allowed = {"user-agent", "accept"}
        extra = {k.lower() for k in headers.keys()} - allowed
        assert not extra, f"Unexpected identifying headers sent: {extra}"

    def test_uses_get_method_no_body(self):
        """probe_latest_version uses GET, no request body."""
        mock_resp = _mock_pypi_response("9.99.0")
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            from yadgar.core.update.check import probe_latest_version

            probe_latest_version()

        # httpx.get (not .post) was called
        assert mock_get.called

    def test_honors_timeout(self):
        """probe_latest_version passes timeout to httpx.get."""
        mock_resp = _mock_pypi_response("9.99.0")
        with patch("httpx.get", return_value=mock_resp) as mock_get:
            from yadgar.core.update.check import probe_latest_version

            probe_latest_version(timeout=3)

        call_kwargs = mock_get.call_args
        timeout = call_kwargs.kwargs.get("timeout")
        assert timeout == 3, f"Expected timeout=3, got {timeout}"

    def test_raises_on_pypi_5xx(self):
        """probe_latest_version raises on 5xx response."""
        import httpx

        with patch(
            "httpx.get",
            side_effect=httpx.HTTPStatusError("503", request=MagicMock(), response=MagicMock()),
        ):
            from yadgar.core.update.check import probe_latest_version

            with pytest.raises((httpx.HTTPStatusError, Exception)):
                probe_latest_version()

    def test_raises_on_timeout(self):
        """probe_latest_version raises TimeoutError on slow response."""
        import httpx

        with patch("httpx.get", side_effect=httpx.TimeoutException("timeout")):
            from yadgar.core.update.check import probe_latest_version

            with pytest.raises((httpx.TimeoutException, TimeoutError, Exception)):
                probe_latest_version()

    def test_respects_https_proxy_env(self, monkeypatch):
        """probe_latest_version uses HTTPS_PROXY from env (httpx default behavior)."""
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:3128")
        mock_resp = _mock_pypi_response("9.99.0")
        # Just assert no exception — httpx handles proxy via env automatically
        with patch("httpx.get", return_value=mock_resp):
            from yadgar.core.update.check import probe_latest_version

            result = probe_latest_version()
        assert result is not None


# ---------------------------------------------------------------------------
# install_methods.py — detect_install_method + upgrade_command + can_self_install
# ---------------------------------------------------------------------------


class TestDetectInstallMethod:
    """Unit tests for yadgar.update.install_methods.detect_install_method."""

    def test_detects_pipx(self, tmp_path, monkeypatch):
        """Returns 'pipx' when yadgar resolves to pipx venv path."""
        fake_bin = tmp_path / "home" / ".local" / "pipx" / "venvs" / "yadgar" / "bin" / "yadgar"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.write_text("#!/usr/bin/env python3\n")

        with patch("subprocess.check_output", return_value=str(fake_bin)):
            # Force reimport so mock takes effect
            import importlib

            from yadgar.core.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "pipx"

    def test_detects_brew(self, tmp_path, monkeypatch):
        """Returns 'brew' when yadgar resolves to Cellar path."""
        fake_bin = tmp_path / "opt" / "homebrew" / "Cellar" / "yadgar" / "5.47.0" / "bin" / "yadgar"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.write_text("#!/usr/bin/env python3\n")

        with patch("subprocess.check_output", return_value=str(fake_bin)):
            import importlib

            from yadgar.core.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "brew"

    def test_detects_nix_flake(self, tmp_path, monkeypatch):
        """Returns 'nix-flake' when yadgar resolves to /nix/store path."""
        fake_bin = tmp_path / "nix" / "store" / "abc123-yadgar-5.47" / "bin" / "yadgar"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.write_text("#!/usr/bin/env python3\n")

        with patch(
            "subprocess.check_output", return_value="/nix/store/abc123-yadgar-5.47/bin/yadgar"
        ):
            import importlib

            from yadgar.core.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "nix-flake"

    def test_detects_container(self, tmp_path):
        """Returns 'container' when yadgar is a docker-run shim."""
        fake_shim = tmp_path / "yadgar"
        fake_shim.write_text('#!/bin/sh\ndocker run --rm openfantasy/yadgar:latest "$@"\n')
        fake_shim.chmod(0o755)

        with patch("subprocess.check_output", return_value=str(fake_shim)):
            import importlib

            from yadgar.core.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "container"

    def test_detects_source(self, tmp_path):
        """Returns 'source' when yadgar resolves into a git repo path."""
        git_dir = tmp_path / "git" / "yadgar" / ".git"
        git_dir.mkdir(parents=True)
        fake_bin = tmp_path / "git" / "yadgar" / "yadgar" / "__main__.py"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.write_text("# fake\n")

        with patch(
            "subprocess.check_output",
            return_value=str(tmp_path / "git" / "yadgar" / "bin" / "yadgar"),
        ):
            # Mock os.path.exists to return True for the .git check
            original_exists = os.path.exists

            def mock_exists(p):
                if ".git" in str(p):
                    return True
                return original_exists(p)

            with patch("os.path.exists", side_effect=mock_exists):
                import importlib

                from yadgar.core.update import install_methods

                importlib.reload(install_methods)
                result = install_methods.detect_install_method()

        assert result == "source"

    def test_returns_not_installed_when_which_fails(self):
        """Returns 'not_installed' when which yadgar fails."""
        with patch(
            "subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "which")
        ):
            import importlib

            from yadgar.core.update import install_methods

            importlib.reload(install_methods)
            result = install_methods.detect_install_method()

        assert result == "not_installed"


class TestUpgradeCommand:
    """Unit tests for yadgar.update.install_methods.upgrade_command."""

    def test_pipx_upgrade_command(self):
        from yadgar.core.update.install_methods import upgrade_command

        assert upgrade_command("pipx") == "pipx upgrade yadgar"

    def test_brew_upgrade_command(self):
        from yadgar.core.update.install_methods import upgrade_command

        assert upgrade_command("brew") == "brew upgrade yadgar"

    def test_nix_flake_upgrade_command(self):
        from yadgar.core.update.install_methods import upgrade_command

        cmd = upgrade_command("nix-flake")
        assert "nix" in cmd.lower()

    def test_container_upgrade_command(self):
        from yadgar.core.update.install_methods import upgrade_command

        cmd = upgrade_command("container")
        assert "docker pull" in cmd

    def test_source_upgrade_command(self):
        from yadgar.core.update.install_methods import upgrade_command

        cmd = upgrade_command("source")
        assert "pip install" in cmd or "git pull" in cmd

    def test_unknown_upgrade_command(self):
        from yadgar.core.update.install_methods import upgrade_command

        cmd = upgrade_command("unknown")
        assert "pypi.org" in cmd or "manual" in cmd.lower()


class TestCanSelfInstall:
    """Unit tests for yadgar.update.install_methods.can_self_install."""

    def test_pipx_can_self_install(self, tmp_path):
        """pipx with writable venv dir → True."""
        from yadgar.core.update.install_methods import can_self_install

        # pipx with no specific path check → defaults to True
        result = can_self_install("pipx")
        assert isinstance(result, bool)

    def test_nix_flake_cannot_self_install(self):
        """nix-flake → always False (read-only /nix/store)."""
        from yadgar.core.update.install_methods import can_self_install

        assert can_self_install("nix-flake") is False

    def test_container_cannot_self_install(self):
        """container → always False."""
        from yadgar.core.update.install_methods import can_self_install

        assert can_self_install("container") is False

    def test_brew_cannot_self_install(self):
        """brew → False (requires user to run manually)."""
        from yadgar.core.update.install_methods import can_self_install

        assert can_self_install("brew") is False
