from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("yadgar")
except PackageNotFoundError:
    __version__ = "unknown"

# Canonical source for runtime versions consumed by setup.sh and Makefile.
# __version__ = yadgar core (pip package version, set via importlib.metadata above).
# BACKEND_VERSION = independent backend image track (docker.io/openfantasy/yadgar-backend).
# Bumping either requires updating CHANGELOG + nix module sync (nix tracks both manually via release notes).
BACKEND_VERSION = "5.4.0"
