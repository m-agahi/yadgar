from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("yadgar")
except PackageNotFoundError:
    # Dev / uninstalled fallback: read version from pyproject.toml in repo root.
    import tomllib as _tomllib
    from pathlib import Path as _Path

    _pyproject = _Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        with _pyproject.open("rb") as _f:
            __version__ = _tomllib.load(_f)["project"]["version"]
    except Exception:
        __version__ = "unknown"

# Canonical source for runtime versions consumed by setup.sh and Makefile.
# __version__ = yadgar core (pip package version, set via importlib.metadata above).
# BACKEND_VERSION = independent backend image track (docker.io/openfantasy/yadgar-backend).
# Bumping either requires updating CHANGELOG + nix module sync (nix tracks both manually via release notes).
BACKEND_VERSION = "5.72.23"  # C10 (0047 PR#40) — the judgement sites + the admin-dispatch fix.
