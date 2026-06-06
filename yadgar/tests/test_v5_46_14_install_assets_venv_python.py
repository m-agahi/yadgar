"""v5.46.14 — _locate_install_assets must use venv python, not bare system python3.

Bug: yadgar-setup.sh step 9 fails on fresh Rocky Linux pipx installs because
``_locate_install_assets()`` calls bare ``python3 -c "import sys; ..."`` which
resolves to /usr/bin/python3 (system python). On Rocky Linux that python has
``sys.prefix=/usr``, so the candidate path is ``/usr/share/yadgar/install_assets/``
which does not exist — the wheel ships assets into the pipx venv, not /usr.

Same class of bug as v5.46.11 (_resolve_yadgar_version) and v5.46.12
(_resolve_backend_version). Fix pattern: extract a ``_get_venv_python()`` helper
(parallel to the shim-shebang extraction in _resolve_yadgar_version) and use it
in ``_locate_install_assets`` to obtain the correct venv python interpreter.

DRY refactor of _resolve_yadgar_version and _resolve_backend_version intentionally
skipped: test_v5_46_12_backend_version_canonical.py::test_resolve_backend_version_uses_shim_pattern
extracts the _resolve_backend_version() function body and asserts "command -v yadgar"
is present in it. Refactoring those helpers to call _get_venv_python() would remove
that literal and fail the existing test. Scope of v5.46.14: new helper + fix the
_locate_install_assets() call site only.

Tests (static analysis of yadgar-setup.sh):
  1. _get_venv_python() helper is defined.
  2. _get_venv_python falls back to 'python3' when yadgar shim absent.
  3. _locate_install_assets uses _get_venv_python (or shim-shebang pattern), not bare python3.
  4. No bare 'python3 -c' remains in the _locate_install_assets function body.
  5. (SKIPPED — DRY refactor skipped, see docstring above.)
  6. Static global: zero or one non-comment bare 'python3 -c' lines remain in full file;
     the only permitted bare call is inside _resolve_yadgar_version or _resolve_backend_version
     (pre-existing shim-shebang sanity checks) — NOT in _locate_install_assets body.

Runner note: pure static tests — no server/MCP dependencies.
Run with --noconftest to avoid autouse fixture failures:
  pytest yadgar/tests/test_v5_46_14_install_assets_venv_python.py --noconftest
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
YADGAR_SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"

# Regex: bare python3 -c invocation (non-comment lines)
_BARE_PYTHON3_C = re.compile(r"\bpython3\s+-c\b")

# Regex: _get_venv_python() call (in any context — subshell, assignment, etc.)
_GET_VENV_PYTHON_CALL = re.compile(r"_get_venv_python")


@pytest.fixture(scope="module")
def setup_sh_text() -> str:
    """Return full text of yadgar-setup.sh."""
    assert YADGAR_SETUP_SH.exists(), f"yadgar-setup.sh not found at {YADGAR_SETUP_SH}"
    return YADGAR_SETUP_SH.read_text(encoding="utf-8")


def _extract_function_body(text: str, fn_name: str) -> str | None:
    """Extract everything between '{' and closing '}' of a named bash function.

    Looks for ``fn_name() {`` or ``fn_name() {`` (with a { on the same or next
    line), then grabs text up to the first ``}`` at column 0 (bash convention).
    Returns None if function not found.
    """
    # Match "fn_name() {" optionally followed by content up to closing "}" at col 0
    pattern = re.compile(
        rf"{re.escape(fn_name)}\(\)\s*\{{(.+?)^}}",
        re.DOTALL | re.MULTILINE,
    )
    m = pattern.search(text)
    return m.group(1) if m else None


class TestGetVenvPythonHelper:
    """Test 1 — _get_venv_python() helper is defined in setup.sh."""

    def test_helper_defined(self, setup_sh_text: str) -> None:
        """_get_venv_python() must be defined as a bash function in setup.sh."""
        assert "_get_venv_python()" in setup_sh_text, (
            "_get_venv_python() helper not found in yadgar-setup.sh.\n"
            "Fix: add _get_venv_python() bash function that reads the yadgar shim shebang\n"
            "and returns the venv python path, falling back to 'python3' if shim absent.\n"
            "Example:\n"
            "  _get_venv_python() {\n"
            "      local yadgar_shim\n"
            "      yadgar_shim=$(command -v yadgar 2>/dev/null) || { echo 'python3'; return; }\n"
            "      [ -f \"$yadgar_shim\" ] || { echo 'python3'; return; }\n"
            "      head -1 \"$yadgar_shim\" | sed 's|^#!||'\n"
            "  }"
        )


class TestGetVenvPythonFallback:
    """Test 2 — _get_venv_python falls back to 'python3' when shim absent."""

    def test_fallback_to_python3(self, setup_sh_text: str) -> None:
        """_get_venv_python body must contain fallback echo 'python3'."""
        body = _extract_function_body(setup_sh_text, "_get_venv_python")
        assert body is not None, (
            "_get_venv_python() function body not found.\n"
            "Ensure the function closes with } at column 0."
        )
        assert "python3" in body, (
            "_get_venv_python() does not fall back to 'python3'.\n"
            "Fix: add fallback path e.g. '|| { echo \"python3\"; return; }' when shim absent.\n"
            "This covers repo-checkout development where the yadgar shim may not exist."
        )
        assert "command -v yadgar" in body or "yadgar_shim" in body, (
            "_get_venv_python() does not appear to look up the yadgar shim.\n"
            "Fix: use 'command -v yadgar' to find the shim before reading its shebang.\n"
            "Fallback to 'python3' when shim absent."
        )


class TestLocateInstallAssetsUsesVenvPython:
    """Test 3 — _locate_install_assets uses _get_venv_python (not bare python3)."""

    def test_uses_get_venv_python_helper(self, setup_sh_text: str) -> None:
        """_locate_install_assets body must call _get_venv_python helper."""
        body = _extract_function_body(setup_sh_text, "_locate_install_assets")
        assert body is not None, (
            "_locate_install_assets() function body not found.\n"
            "Ensure the function closes with } at column 0."
        )
        uses_helper = _GET_VENV_PYTHON_CALL.search(body) is not None
        # Also accept direct shim-shebang expansion as an alternative pattern
        uses_shim_pattern = "command -v yadgar" in body or (
            "head -1" in body and "yadgar_shim" in body
        )
        assert uses_helper or uses_shim_pattern, (
            "_locate_install_assets() does not use _get_venv_python or equivalent shim pattern.\n"
            "Fix: replace bare 'python3 -c ...' with '$(_get_venv_python) -c ...' or equivalent.\n"
            "This ensures the venv python (not system /usr/bin/python3) resolves sys.prefix."
        )


class TestNoBareSystemPythonInLocateInstallAssets:
    """Test 4 — no bare 'python3 -c' in _locate_install_assets body."""

    def test_no_bare_python3_c_in_body(self, setup_sh_text: str) -> None:
        """_locate_install_assets must not contain a bare 'python3 -c' call.

        Bare 'python3 -c' resolves to /usr/bin/python3 on Rocky Linux, which has
        sys.prefix=/usr — causing the install_assets lookup to fail.
        """
        body = _extract_function_body(setup_sh_text, "_locate_install_assets")
        assert body is not None, (
            "_locate_install_assets() function body not found.\n"
            "Ensure the function closes with } at column 0."
        )
        bad_lines = []
        for lineno_in_fn, line in enumerate(body.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _BARE_PYTHON3_C.search(line):
                bad_lines.append((lineno_in_fn, stripped))

        assert not bad_lines, (
            "Found bare 'python3 -c' inside _locate_install_assets() (breaks on system python):\n"
            + "\n".join(f"  fn-line {ln}: {text}" for ln, text in bad_lines)
            + "\n\nFix: replace with '$(_get_venv_python) -c ...' so the venv python is used."
        )


class TestGlobalBareSystemPythonCount:
    """Test 6 — global: bare 'python3 -c' count in non-comment lines is 0 or at most 2.

    Post-fix, _locate_install_assets should have 0 bare invocations.
    The pre-existing helpers (_resolve_yadgar_version, _resolve_backend_version) each
    have one sanity-check call '$venv_python -c "import sys"' — those use $venv_python
    (variable), not bare 'python3', so they are NOT matched by this regex.

    Allowed: zero non-comment lines with bare 'python3 -c' after the fix.
    """

    def test_no_bare_python3_c_non_comment(self, setup_sh_text: str) -> None:
        """No non-comment lines should contain bare 'python3 -c'.

        After fix:
          - _locate_install_assets uses _get_venv_python (no more bare python3 -c)
          - _resolve_yadgar_version uses $venv_python (not bare python3)
          - _resolve_backend_version uses $venv_python (not bare python3)
        So count should be 0.
        """
        bad_lines = []
        for lineno, line in enumerate(setup_sh_text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _BARE_PYTHON3_C.search(line):
                bad_lines.append((lineno, stripped))

        assert not bad_lines, (
            "Found non-comment bare 'python3 -c' invocations in yadgar-setup.sh after fix:\n"
            + "\n".join(f"  line {ln}: {text}" for ln, text in bad_lines)
            + "\n\nAll bare 'python3 -c' calls should use a venv-aware interpreter.\n"
            "Fix: replace with '$(_get_venv_python) -c ...' or '$venv_python -c ...' (variable form)."
        )
