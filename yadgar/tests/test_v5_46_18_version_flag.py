"""v5.46.18 TDD — yadgar --version flag (core + backend + daemon probe).

Feature
-------
``yadgar --version`` (new global flag) prints a three-line version summary:

    yadgar core       5.46.18
    yadgar backend    5.4.0
    yadgar daemon     5.46.18 (uptime 142s, db ok, embed ok)   # running
    yadgar daemon     not running (start with ...)              # not running

JSON mode (``yadgar --version --json``) emits parseable JSON with keys
``core``, ``backend``, ``daemon``.

setup.sh ``_resolve_yadgar_version`` / ``_resolve_backend_version`` helpers
collapse from shim-shebang workaround to ``yadgar --version | awk ...`` with
the shim-shebang as fallback.

Test structure
--------------
T1  ``yadgar --version`` exits 0.
T2  Output contains ``yadgar core  <semver>`` and ``yadgar backend <semver>`` lines.
T3  Daemon section present — running or not-running form accepted.
T4  JSON mode (``--json``) produces parseable JSON with keys core/backend/daemon.
T5  ``--help`` output mentions ``--version``.
T6  setup.sh ``_resolve_yadgar_version`` uses ``yadgar --version`` awk approach.
T7  setup.sh ``_resolve_backend_version`` uses ``yadgar --version`` awk approach.
T8  yadgar/cli/version.py module exists with ``print_version_summary`` callable.

All subprocess tests invoke via ``sys.executable -m yadgar`` to avoid PATH issues.
T6/T7 are static checks (grep setup.sh source).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"
VERSION_MODULE = REPO_ROOT / "yadgar" / "core" / "cli" / "version.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_version(*extra_args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run ``python -m yadgar --version [extra_args]`` and return result."""
    cmd = [sys.executable, "-m", "yadgar", "--version"] + list(extra_args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# T1 — yadgar --version exits 0
# ---------------------------------------------------------------------------


def test_t1_version_exits_zero() -> None:
    """``yadgar --version`` must exit with code 0."""
    result = _run_version()
    assert result.returncode == 0, (
        f"yadgar --version exited with {result.returncode}.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# T2 — output contains core + backend lines
# ---------------------------------------------------------------------------


_SEMVER_RE = r"\d+\.\d+\.\d+"
_CORE_LINE_RE = re.compile(r"^yadgar\s+core\s+" + _SEMVER_RE, re.MULTILINE)
_BACKEND_LINE_RE = re.compile(r"^yadgar\s+backend\s+" + _SEMVER_RE, re.MULTILINE)


def test_t2_core_and_backend_lines_present() -> None:
    """Output must contain 'yadgar core <semver>' and 'yadgar backend <semver>' lines."""
    result = _run_version()
    output = result.stdout + result.stderr  # accept either stream
    assert _CORE_LINE_RE.search(output), f"'yadgar core <semver>' not found in output:\n{output!r}"
    assert _BACKEND_LINE_RE.search(output), (
        f"'yadgar backend <semver>' not found in output:\n{output!r}"
    )


# ---------------------------------------------------------------------------
# T3 — daemon section present (running or not-running form)
# ---------------------------------------------------------------------------

# Matches either:
#   yadgar daemon     5.46.18 (uptime ...
#   yadgar daemon     not running (start ...
_DAEMON_LINE_RE = re.compile(
    r"^yadgar\s+daemon\s+(?:" + _SEMVER_RE + r"|not running)",
    re.MULTILINE,
)


def test_t3_daemon_section_present() -> None:
    """Output must contain a daemon line — either running (semver) or not-running form."""
    result = _run_version()
    output = result.stdout + result.stderr
    assert _DAEMON_LINE_RE.search(output), (
        f"Daemon line not found in output.\n"
        f"Expected pattern: {_DAEMON_LINE_RE.pattern!r}\n"
        f"Actual output: {output!r}"
    )


# ---------------------------------------------------------------------------
# T4 — JSON mode produces parseable JSON with correct keys
# ---------------------------------------------------------------------------


def test_t4_json_mode_parseable() -> None:
    """``yadgar --version --json`` must emit parseable JSON with keys core/backend/daemon."""
    result = _run_version("--json")
    assert result.returncode == 0, (
        f"yadgar --version --json exited {result.returncode}.\nstderr: {result.stderr!r}"
    )
    output = result.stdout.strip()
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"JSON parse failed: {exc}\nOutput was: {output!r}") from exc

    assert "core" in data, f"'core' key missing from JSON: {data}"
    assert "backend" in data, f"'backend' key missing from JSON: {data}"
    assert "daemon" in data, f"'daemon' key missing from JSON: {data}"

    # core and backend must look like semver strings
    assert re.fullmatch(_SEMVER_RE, str(data["core"])), (
        f"'core' is not a semver string: {data['core']!r}"
    )
    assert re.fullmatch(_SEMVER_RE, str(data["backend"])), (
        f"'backend' is not a semver string: {data['backend']!r}"
    )

    # daemon must be a dict with at least 'running' key
    assert isinstance(data["daemon"], dict), (
        f"'daemon' must be a dict, got {type(data['daemon'])}: {data['daemon']!r}"
    )
    assert "running" in data["daemon"], f"'daemon' dict missing 'running' key: {data['daemon']}"


# ---------------------------------------------------------------------------
# T5 — --help mentions --version
# ---------------------------------------------------------------------------


def test_t5_help_mentions_version() -> None:
    """``yadgar --help`` output must mention --version as a global flag."""
    result = subprocess.run(
        [sys.executable, "-m", "yadgar", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
    )
    # --help may exit 0 or print to stdout/stderr
    output = result.stdout + result.stderr
    assert "--version" in output, f"'--version' not found in yadgar --help output:\n{output!r}"


# ---------------------------------------------------------------------------
# T6 — setup.sh _resolve_yadgar_version uses yadgar --version awk approach
# ---------------------------------------------------------------------------


def test_t6_setup_resolve_yadgar_version_uses_awk() -> None:
    """setup.sh _resolve_yadgar_version must use 'yadgar --version' + awk as primary
    extraction method."""
    assert SETUP_SH.exists(), f"setup.sh not found at {SETUP_SH}"
    content = SETUP_SH.read_text(encoding="utf-8")

    # Find _resolve_yadgar_version function body
    func_start = content.find("_resolve_yadgar_version()")
    assert func_start != -1, "_resolve_yadgar_version() not found in setup.sh"

    # Find end of function (next function definition or end)
    next_func = content.find("\n_resolve_", func_start + 1)
    func_body = content[func_start:next_func] if next_func != -1 else content[func_start:]

    # Must call yadgar --version
    assert "yadgar --version" in func_body, (
        f"_resolve_yadgar_version does not call 'yadgar --version'.\nFunction body:\n{func_body}"
    )
    # Must use awk to extract core version
    assert "awk" in func_body, (
        f"_resolve_yadgar_version does not use awk for extraction.\nFunction body:\n{func_body}"
    )


# ---------------------------------------------------------------------------
# T7 — setup.sh _resolve_backend_version uses yadgar --version awk approach
# ---------------------------------------------------------------------------


def test_t7_setup_resolve_backend_version_uses_awk() -> None:
    """setup.sh _resolve_backend_version must use 'yadgar --version' + awk as primary
    extraction method."""
    assert SETUP_SH.exists(), f"setup.sh not found at {SETUP_SH}"
    content = SETUP_SH.read_text(encoding="utf-8")

    # Find _resolve_backend_version function body
    func_start = content.find("_resolve_backend_version()")
    assert func_start != -1, "_resolve_backend_version() not found in setup.sh"

    # Find end of function (next function definition at root indent)
    next_func = content.find("\n_", func_start + 1)
    func_body = content[func_start:next_func] if next_func != -1 else content[func_start:]

    # Must call yadgar --version
    assert "yadgar --version" in func_body, (
        f"_resolve_backend_version does not call 'yadgar --version'.\nFunction body:\n{func_body}"
    )
    # Must use awk to extract backend version
    assert "awk" in func_body, (
        f"_resolve_backend_version does not use awk for extraction.\nFunction body:\n{func_body}"
    )


# ---------------------------------------------------------------------------
# T8 — yadgar/cli/version.py module exists with print_version_summary callable
# ---------------------------------------------------------------------------


def test_t8_version_module_exists() -> None:
    """yadgar/cli/version.py must exist and expose print_version_summary."""
    assert VERSION_MODULE.exists(), f"yadgar/cli/version.py not found at {VERSION_MODULE}"
    content = VERSION_MODULE.read_text(encoding="utf-8")
    assert "print_version_summary" in content, (
        "print_version_summary callable not found in yadgar/cli/version.py"
    )
