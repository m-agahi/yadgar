"""TDD tests for codebase-memory-mcp host-side install (Car A, ADR-0162).

Coverage
--------
1. Asset name selection — all 4 os×arch combos produce correct filenames.
2. Asset name rejects unsupported OS/arch inputs.
3. verify_sha256 — accepts matching hash; rejects mismatched hash.
4. verify_sha256 — raises KeyError for unknown asset name.
5. get_download_url — produces pinned v0.9.0 URL for all 4 targets.
6. install_codebase_memory_mcp — extracts binary, sets chmod 755, returns path.
7. install_codebase_memory_mcp — aborts on checksum mismatch (no file written).
8. install_codebase_memory_mcp — skip_if_exists short-circuits download.
9. CODE_GRAPH_ENABLED opt-out: setup --code-graph gated on env flag.

Network is fully mocked in all tests — no real downloads.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from yadgar.core.install.codebase_memory_mcp import (
    _ASSET_SHA256,
    BINARY_NAME,
    VERSION,
    _detect_arch,
    _detect_os,
    get_asset_name,
    get_download_url,
    install_codebase_memory_mcp,
    verify_sha256,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_tarball(binary_content: bytes = b"fake-binary") -> bytes:
    """Build an in-memory tarball containing a 'codebase-memory-mcp' entry."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=BINARY_NAME)
        info.size = len(binary_content)
        tf.addfile(info, io.BytesIO(binary_content))
    return buf.getvalue()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── 1. Asset name selection ────────────────────────────────────────────────────


class TestGetAssetName:
    """All 4 os×arch combos produce the correct asset filename."""

    def test_linux_amd64(self) -> None:
        assert get_asset_name("linux", "amd64") == (
            "codebase-memory-mcp-linux-amd64-portable.tar.gz"
        )

    def test_linux_arm64(self) -> None:
        assert get_asset_name("linux", "arm64") == (
            "codebase-memory-mcp-linux-arm64-portable.tar.gz"
        )

    def test_darwin_amd64(self) -> None:
        assert get_asset_name("darwin", "amd64") == ("codebase-memory-mcp-darwin-amd64.tar.gz")

    def test_darwin_arm64(self) -> None:
        assert get_asset_name("darwin", "arm64") == ("codebase-memory-mcp-darwin-arm64.tar.gz")

    def test_linux_portable_suffix_not_on_darwin(self) -> None:
        """Darwin assets do not carry '-portable' suffix."""
        name = get_asset_name("darwin", "amd64")
        assert "portable" not in name

    def test_linux_portable_suffix_present(self) -> None:
        """Linux assets always carry '-portable' suffix."""
        assert "portable" in get_asset_name("linux", "amd64")
        assert "portable" in get_asset_name("linux", "arm64")


# ── 2. Unsupported OS/arch ─────────────────────────────────────────────────────


class TestGetAssetNameUnsupported:
    """Unsupported OS or arch raises RuntimeError, not a silent fallthrough."""

    def test_unsupported_os_raises(self) -> None:
        with pytest.raises(RuntimeError, match="Unsupported OS"):
            get_asset_name("windows", "amd64")

    def test_unsupported_arch_raises(self) -> None:
        with pytest.raises(RuntimeError, match="Unsupported"):
            get_asset_name("linux", "riscv64")

    @patch("platform.system", return_value="Windows")
    def test_detect_os_windows_raises(self, _mock) -> None:
        with pytest.raises(RuntimeError, match="Unsupported OS"):
            _detect_os()

    @patch("platform.machine", return_value="s390x")
    def test_detect_arch_unknown_raises(self, _mock) -> None:
        with pytest.raises(RuntimeError, match="Unsupported architecture"):
            _detect_arch()


# ── 3 & 4. verify_sha256 ──────────────────────────────────────────────────────


class TestVerifySha256:
    """SHA-256 verification accepts correct hash and rejects mismatches."""

    _ASSET = "codebase-memory-mcp-linux-amd64-portable.tar.gz"

    def test_accepts_correct_hash(self) -> None:
        """verify_sha256 does not raise when hash matches the pinned constant."""
        # Construct fake data whose sha256 matches the pinned value.
        # We patch _ASSET_SHA256 to use a hash we can compute ourselves.
        fake_data = b"test-payload-linux-amd64"
        fake_hash = _sha256_hex(fake_data)
        patched = {self._ASSET: fake_hash}
        with patch("yadgar.core.install.codebase_memory_mcp._ASSET_SHA256", patched):
            verify_sha256(fake_data, self._ASSET)  # must not raise

    def test_rejects_mismatched_hash(self) -> None:
        """verify_sha256 raises ValueError when download hash doesn't match."""
        fake_data = b"tampered-payload"
        wrong_hash = "a" * 64  # valid hex format but wrong value
        patched = {self._ASSET: wrong_hash}
        with patch("yadgar.core.install.codebase_memory_mcp._ASSET_SHA256", patched):
            with pytest.raises(ValueError, match="SHA-256 mismatch"):
                verify_sha256(fake_data, self._ASSET)

    def test_unknown_asset_raises_key_error(self) -> None:
        """verify_sha256 raises KeyError for an unrecognised asset name."""
        with pytest.raises(KeyError):
            verify_sha256(b"data", "codebase-memory-mcp-unknown-platform.tar.gz")


# ── 5. get_download_url ────────────────────────────────────────────────────────


class TestGetDownloadUrl:
    """Download URLs are pinned to VERSION and the correct asset name."""

    def test_version_in_url(self) -> None:
        url = get_download_url("codebase-memory-mcp-linux-amd64-portable.tar.gz")
        assert VERSION in url

    def test_asset_name_in_url(self) -> None:
        asset = "codebase-memory-mcp-darwin-arm64.tar.gz"
        url = get_download_url(asset)
        assert asset in url

    def test_all_four_assets_have_github_url(self) -> None:
        for asset in _ASSET_SHA256:
            url = get_download_url(asset)
            assert "github.com/DeusData/codebase-memory-mcp" in url


# ── 6. install_codebase_memory_mcp — happy path ────────────────────────────────


class TestInstallCodebaseMemoryMcp:
    """install_codebase_memory_mcp extracts binary, chmod 755, returns path."""

    def _mock_download(self, tarball_data: bytes):
        """Return a context manager mock that serves tarball_data."""
        cm = MagicMock()
        cm.__enter__ = lambda s: MagicMock(read=lambda: tarball_data)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    def test_installs_binary_to_dest_dir(self, tmp_path: Path) -> None:
        """Binary appears in dest_dir after install."""
        tarball = _make_tarball(b"fake-cbm-binary")
        fake_hash = _sha256_hex(tarball)
        asset = get_asset_name("linux", "amd64")

        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="x86_64"),
            patch("yadgar.core.install.codebase_memory_mcp._ASSET_SHA256", {asset: fake_hash}),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__ = lambda s: MagicMock(read=lambda: tarball)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            result = install_codebase_memory_mcp(dest_dir=tmp_path)

        assert result == tmp_path / BINARY_NAME
        assert result.exists()

    def test_binary_is_executable(self, tmp_path: Path) -> None:
        """Installed binary has executable bit set (chmod 755)."""
        tarball = _make_tarball(b"fake-cbm-binary")
        fake_hash = _sha256_hex(tarball)
        asset = get_asset_name("linux", "amd64")

        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="x86_64"),
            patch("yadgar.core.install.codebase_memory_mcp._ASSET_SHA256", {asset: fake_hash}),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__ = lambda s: MagicMock(read=lambda: tarball)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            result = install_codebase_memory_mcp(dest_dir=tmp_path)

        mode = stat.S_IMODE(result.stat().st_mode)
        assert mode & 0o111, f"Binary not executable: {oct(mode)}"

    def test_returns_absolute_path(self, tmp_path: Path) -> None:
        """Returned path is absolute."""
        tarball = _make_tarball(b"fake-cbm-binary")
        fake_hash = _sha256_hex(tarball)
        asset = get_asset_name("linux", "amd64")

        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="x86_64"),
            patch("yadgar.core.install.codebase_memory_mcp._ASSET_SHA256", {asset: fake_hash}),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__ = lambda s: MagicMock(read=lambda: tarball)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            result = install_codebase_memory_mcp(dest_dir=tmp_path)

        assert result.is_absolute()


# ── 7. Checksum mismatch aborts install ───────────────────────────────────────


class TestInstallChecksumRejection:
    """install_codebase_memory_mcp aborts on checksum mismatch; no file written."""

    def test_mismatch_raises_value_error(self, tmp_path: Path) -> None:
        tarball = _make_tarball(b"tampered-binary")
        asset = get_asset_name("linux", "amd64")
        bad_hash = "0" * 64  # wrong hash

        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="x86_64"),
            patch("yadgar.core.install.codebase_memory_mcp._ASSET_SHA256", {asset: bad_hash}),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__ = lambda s: MagicMock(read=lambda: tarball)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ValueError, match="SHA-256 mismatch"):
                install_codebase_memory_mcp(dest_dir=tmp_path)

    def test_no_binary_written_on_mismatch(self, tmp_path: Path) -> None:
        """No binary is written to dest_dir when checksum fails."""
        tarball = _make_tarball(b"tampered-binary")
        asset = get_asset_name("linux", "amd64")
        bad_hash = "0" * 64

        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="x86_64"),
            patch("yadgar.core.install.codebase_memory_mcp._ASSET_SHA256", {asset: bad_hash}),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__ = lambda s: MagicMock(read=lambda: tarball)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(ValueError):
                install_codebase_memory_mcp(dest_dir=tmp_path)

        assert not (tmp_path / BINARY_NAME).exists()


# ── 8. skip_if_exists ─────────────────────────────────────────────────────────


class TestInstallSkipIfExists:
    """skip_if_exists=True skips download when binary already present."""

    def test_skip_if_exists_no_download(self, tmp_path: Path) -> None:
        """No network call when binary already exists and skip_if_exists=True."""
        binary = tmp_path / BINARY_NAME
        binary.write_bytes(b"existing-binary")
        binary.chmod(0o755)

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = install_codebase_memory_mcp(dest_dir=tmp_path, skip_if_exists=True)
            mock_urlopen.assert_not_called()

        assert result == binary

    def test_no_skip_when_missing(self, tmp_path: Path) -> None:
        """skip_if_exists=True still downloads when binary is absent."""
        tarball = _make_tarball(b"fresh-binary")
        fake_hash = _sha256_hex(tarball)
        asset = get_asset_name("linux", "amd64")

        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="x86_64"),
            patch("yadgar.core.install.codebase_memory_mcp._ASSET_SHA256", {asset: fake_hash}),
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__ = lambda s: MagicMock(read=lambda: tarball)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            result = install_codebase_memory_mcp(dest_dir=tmp_path, skip_if_exists=True)
            mock_urlopen.assert_called_once()

        assert result.exists()


# ── 9. code_graph install dispatch (task:0082 — default-on, single opt-out) ───


class TestCodeGraphInstallDispatch:
    """``_maybe_install_code_graph`` dispatch: the binary install is DEFAULT-ON and
    the single ``--no-code-graph`` opt-out turns the store flag off with it.

    Rewritten by task:0082. The old shape gated the install on a
    ``CODE_GRAPH_ENABLED`` env trigger + a ``--code-graph`` opt-IN flag; both are
    gone (the env var no longer influences setup, and the opt-in flag was removed
    once ``code_graph.enabled`` defaulted to True). ``runtime_config_client.set``
    is stubbed in every case — an unstubbed call POSTs to a developer's LIVE
    daemon and would mutate its real runtime-config store.
    """

    def _make_args(self, *, no_code_graph: bool = False):
        import types

        return types.SimpleNamespace(no_code_graph=no_code_graph)

    def test_default_triggers_install_and_enables(self, tmp_path: Path) -> None:
        from yadgar.core.cli.setup import _maybe_install_code_graph

        sets: list = []
        with (
            patch(
                "yadgar.core.install.codebase_memory_mcp.install_codebase_memory_mcp"
            ) as mock_install,
            patch(
                "yadgar.core.runtime_config_client.set",
                side_effect=lambda k, v, **kw: sets.append((k, v)) or True,
            ),
        ):
            mock_install.return_value = tmp_path / BINARY_NAME
            _maybe_install_code_graph(self._make_args())
            mock_install.assert_called_once()
        assert sets == [("code_graph.enabled", True)]

    def test_opt_out_skips_install_and_disables(self, tmp_path: Path) -> None:
        from yadgar.core.cli.setup import _maybe_install_code_graph

        sets: list = []
        with (
            patch(
                "yadgar.core.install.codebase_memory_mcp.install_codebase_memory_mcp"
            ) as mock_install,
            patch(
                "yadgar.core.runtime_config_client.set",
                side_effect=lambda k, v, **kw: sets.append((k, v)) or True,
            ),
        ):
            _maybe_install_code_graph(self._make_args(no_code_graph=True))
            mock_install.assert_not_called()
        assert sets == [("code_graph.enabled", False)]

    def test_legacy_env_trigger_no_longer_gates_the_install(self, tmp_path: Path) -> None:
        """``CODE_GRAPH_ENABLED=0`` used to suppress the install; it is now inert."""
        from yadgar.core.cli.setup import _maybe_install_code_graph

        with (
            patch.dict(os.environ, {"CODE_GRAPH_ENABLED": "0"}),
            patch(
                "yadgar.core.install.codebase_memory_mcp.install_codebase_memory_mcp"
            ) as mock_install,
            patch("yadgar.core.runtime_config_client.set", return_value=True),
        ):
            mock_install.return_value = tmp_path / BINARY_NAME
            _maybe_install_code_graph(self._make_args())
            mock_install.assert_called_once()
