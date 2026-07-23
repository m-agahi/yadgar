"""codebase-memory-mcp host-side install helpers.

Car A of the code_graph train (ADR-0162).

Downloads, verifies, and installs the codebase-memory-mcp static binary
HOST-SIDE only — the binary NEVER enters the docker image (the MCP core
daemon is a read-only container that cannot reach host repos).

Pinned to v0.9.0.  Hashes taken from the upstream checksums.txt at release
time and baked in as module constants — runtime verification checks the
download against these, not against a re-fetched checksums.txt (same origin
as the tarball = no additional trust).

Asset selection
---------------
Linux  amd64  → codebase-memory-mcp-linux-amd64-portable.tar.gz
Linux  arm64  → codebase-memory-mcp-linux-arm64-portable.tar.gz
Darwin amd64  → codebase-memory-mcp-darwin-amd64.tar.gz
Darwin arm64  → codebase-memory-mcp-darwin-arm64.tar.gz

Only the -portable variants are used on Linux (static ELF, no glibc dep).
Darwin tarballs are the plain ones (macOS links dynamically against system libs).

Usage
-----
    from yadgar.core.install.codebase_memory_mcp import install_codebase_memory_mcp
    install_codebase_memory_mcp()   # installs to ~/.local/bin/codebase-memory-mcp

Or from the CLI:
    yadgar setup --code-graph   # enables CODE_GRAPH_ENABLED download step
"""

from __future__ import annotations

import hashlib
import platform
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from yadgar._shared.observability.observe import observe

# ── Pin constants ─────────────────────────────────────────────────────────────

VERSION = "v0.9.0"
_BASE_URL = f"https://github.com/DeusData/codebase-memory-mcp/releases/download/{VERSION}"

# Hex SHA-256 hashes taken from checksums.txt published at the release URL.
# Do NOT replace with runtime-fetched checksums (same origin as the binary).
_ASSET_SHA256: dict[str, str] = {
    "codebase-memory-mcp-linux-amd64-portable.tar.gz": (
        "8459d5c9d1457f2c82de3de307ffc7641ecbba2dde893427be1e62eca8ef9b25"
    ),
    "codebase-memory-mcp-linux-arm64-portable.tar.gz": (
        "b0a43fdaf534073c16707d72726b73b149d4c1212034b281ee8b7b2dac755107"
    ),
    "codebase-memory-mcp-darwin-amd64.tar.gz": (
        "6af3d02a27f589901fa763d3971089337bc8c9838bbed5d0cf543ca9f1a9e543"
    ),
    "codebase-memory-mcp-darwin-arm64.tar.gz": (
        "faa02f0404230c451a9812230394481948f80183801fa5bf67044b41c2f25ed4"
    ),
}

BINARY_NAME = "codebase-memory-mcp"


# ── Asset selection ───────────────────────────────────────────────────────────


@observe(tier="stage")
def _detect_os() -> str:
    """Return normalised OS string: 'linux' or 'darwin'.

    Raises ``RuntimeError`` for unsupported OS.
    """
    sysname = platform.system().lower()
    if sysname == "linux":
        return "linux"
    if sysname == "darwin":
        return "darwin"
    raise RuntimeError(
        f"Unsupported OS '{platform.system()}'. codebase-memory-mcp supports Linux and macOS only."
    )


@observe(tier="stage")
def _detect_arch() -> str:
    """Return normalised arch string: 'amd64' or 'arm64'.

    Raises ``RuntimeError`` for unsupported arch.
    """
    machine = platform.machine().lower()
    if machine == "x86_64":
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise RuntimeError(
        f"Unsupported architecture '{platform.machine()}'. "
        "codebase-memory-mcp supports x86_64 and aarch64/arm64 only."
    )


@observe(tier="stage")
def get_asset_name(os_name: str | None = None, arch: str | None = None) -> str:
    """Return the tarball asset name for the given os+arch combination.

    Parameters are derived from the current system if omitted.

    Linux uses -portable suffixed tarballs (static ELF, no glibc dep).
    Darwin uses the plain tarballs (system-linked against macOS libs).

    Raises ``RuntimeError`` for unsupported os/arch combinations.
    """
    if os_name is None:
        os_name = _detect_os()
    if arch is None:
        arch = _detect_arch()

    _SUPPORTED_ARCHS = ("amd64", "arm64")
    if arch not in _SUPPORTED_ARCHS:
        raise RuntimeError(
            f"Unsupported architecture: {arch!r}. codebase-memory-mcp supports {_SUPPORTED_ARCHS}."
        )

    if os_name == "linux":
        return f"codebase-memory-mcp-linux-{arch}-portable.tar.gz"
    if os_name == "darwin":
        return f"codebase-memory-mcp-darwin-{arch}.tar.gz"
    raise RuntimeError(f"Unsupported OS: {os_name!r}")


@observe(tier="stage")
def get_download_url(asset_name: str | None = None) -> str:
    """Return the full download URL for the given asset (or current system)."""
    if asset_name is None:
        asset_name = get_asset_name()
    return f"{_BASE_URL}/{asset_name}"


# ── Verification ──────────────────────────────────────────────────────────────


@observe(tier="stage")
def verify_sha256(data: bytes, asset_name: str) -> None:
    """Verify ``data`` against the pinned hex SHA-256 for ``asset_name``.

    Raises ``ValueError`` if the hash does not match.
    Raises ``KeyError`` if ``asset_name`` is not in the pinned table.
    """
    expected = _ASSET_SHA256[asset_name]
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(
            f"SHA-256 mismatch for {asset_name}.\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}\n"
            "Download may be corrupt or tampered; aborting install."
        )


# ── Install logic ─────────────────────────────────────────────────────────────


@observe(tier="stage")
def _default_bin_dir() -> Path:
    """Return the XDG-conventional user bin dir (``~/.local/bin``)."""
    return Path.home() / ".local" / "bin"


@observe(tier="stage")
def install_codebase_memory_mcp(
    dest_dir: Path | None = None,
    *,
    skip_if_exists: bool = False,
) -> Path:
    """Download, verify, extract, and install the codebase-memory-mcp binary.

    Parameters
    ----------
    dest_dir:
        Directory to install the binary into.  Defaults to ``~/.local/bin``.
    skip_if_exists:
        If True and the binary already exists at the destination, return early
        without downloading.  Useful for idempotent setup re-runs.

    Returns
    -------
    Path
        Absolute path to the installed binary.

    Raises
    ------
    RuntimeError
        Unsupported OS or architecture.
    ValueError
        SHA-256 checksum mismatch (corrupt download or tampering).
    urllib.error.URLError
        Network error during download.
    """
    if dest_dir is None:
        dest_dir = _default_bin_dir()

    dest_dir.mkdir(parents=True, exist_ok=True)
    binary_path = dest_dir / BINARY_NAME

    if skip_if_exists and binary_path.exists():
        return binary_path

    asset_name = get_asset_name()
    url = get_download_url(asset_name)

    # Download
    with urllib.request.urlopen(url) as resp:  # noqa: S310 — URL is pinned constant
        data = resp.read()

    # Verify before extraction
    verify_sha256(data, asset_name)

    # Extract the binary from the tarball
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tarball_path = tmp_path / asset_name
        tarball_path.write_bytes(data)

        with tarfile.open(tarball_path) as tf:
            # The tarball contains: codebase-memory-mcp, LICENSE,
            # install.sh, THIRD_PARTY_NOTICES.md
            member = tf.getmember(BINARY_NAME)
            member.name = BINARY_NAME  # strip any path prefix
            tf.extract(member, path=tmp_path)

        extracted = tmp_path / BINARY_NAME
        extracted.chmod(0o755)

        import shutil

        shutil.copy2(str(extracted), str(binary_path))

    binary_path.chmod(0o755)
    return binary_path
