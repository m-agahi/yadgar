import logging as _logging
from importlib.metadata import PackageNotFoundError, version

# Car 5 bug-train item 2: standard library-logging practice — install a
# NullHandler on the package root logger at import time. Without this,
# Python's own `logging.lastResort` fallback (a bare `_StderrHandler` with
# NO formatter) fires for any WARNING+ record on a "yadgar.*" logger when
# nothing in the process has configured logging — which is the case for
# every `yadgar <subcommand>` on the host CLI except the bare MCP-server
# invocation (only `yadgar/__main__.py::cli()`'s no-subcommand branch calls
# `configure_logging()`). That left `@observe`'s error-log side-channel
# (`logger.error(spec.event, ...)`, where `spec.event` defaults to the bare
# `f"{fn.__module__}.{fn.__qualname__}"`) leaking undecorated fn qualnames
# straight to stderr, e.g. on `yadgar context` / `yadgar seed`:
#     yadgar._shared.storage.client._ClientMixin._q_embedded
# A NullHandler anywhere in a logger's ancestor chain is enough to satisfy
# `logging.Logger.callHandlers`'s `found` check and permanently suppress
# `lastResort` for the whole tree — it coexists harmlessly with any real
# handler `configure_logging()` adds later.
_logging.getLogger("yadgar").addHandler(_logging.NullHandler())

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
    # open() -> OSError (installed wheels have no pyproject.toml beside them);
    # tomllib.load() -> TOMLDecodeError, a ValueError; the two subscripts -> KeyError.
    except (OSError, ValueError, KeyError):  # fmt: skip
        __version__ = "unknown"

# Canonical source for runtime versions consumed by setup.sh and Makefile.
# __version__ = yadgar core (pip package version, set via importlib.metadata above).
# BACKEND_VERSION = independent backend image track (docker.io/openfantasy/yadgar-backend).
# Bumping either requires updating CHANGELOG + nix module sync (nix tracks both manually via release notes).
# v5.182 bug train: scoped restore reads, live hooks, honest write results.
# Keep this assignment on ONE line — scripts/sync_version.py matches
# `^BACKEND_VERSION\s*=\s*"` and a wrapped form silently breaks the sync hook.

BACKEND_VERSION = "5.86.0"
