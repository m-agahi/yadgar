"""v5.46.11 — yadgar-setup.sh must use the yadgar shim (pipx-aware), not system python3.

Bug: steps 6-10 of yadgar-setup.sh invoked yadgar CLI subcommands via
``python3 -m yadgar <subcommand>``. On a Rocky Linux VM installed via pipx,
/usr/bin/python3 is the system python which cannot see the yadgar package
(it lives in the pipx venv). This caused step 6 to fail with:
  /usr/bin/python3: No module named yadgar

Fix (v5.46.11):
- Lines 397/402/407/449: replace ``run python3 -m yadgar X`` → ``run yadgar X``
  (bare shim resolved via PATH, shebang points to pipx venv python).
- Lines 295/322: ``python3 -c "import yadgar; print(yadgar.__version__)"``
  replaced with ``_resolve_yadgar_version`` helper that extracts the venv
  python from the yadgar shim's shebang.
- Comment at line ~180: updated to reflect shim-based CLI design.

Test strategy: static analysis of yadgar-setup.sh.
  Test 1 — no forbidden invocation forms remain.
  Test 2 — shim usage assertions: CLI subcommand lines use bare ``yadgar`` shim.
  Test 3 — version helper: ``_resolve_yadgar_version`` function is defined.
  Test 4 — comment at _locate_setup_scripts updated (no longer says ``python3 -m yadgar``).

NOTE on the comment check: the regex ``python3 -m yadgar`` will match the
comment on line ~180 in the un-fixed script. That comment must also be
updated as part of the GREEN fix, so all four tests start RED together
and turn GREEN together.

Runner note: these are pure static tests with no server/mcp dependencies.
Run with --noconftest to avoid autouse fixture failures caused by missing mcp
module in dev/CI environments without the full server stack:
  pytest yadgar/tests/test_v5_46_11_pipx_cli_invocation.py --noconftest
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
YADGAR_SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"

# Regex that matches any invocation form using system python to call yadgar module.
# This covers both ``python3 -m yadgar`` and ``python -m yadgar``.
_FORBIDDEN_MODULE_INVOKE = re.compile(r"\bpython3?\s+-m\s+yadgar\b")

# Regex that matches the broken version-detect form.
_FORBIDDEN_VERSION_DETECT = re.compile(r"""python3?\s+-c\s+["']import yadgar""")

# Regex matching a correctly shimmed CLI call for each subcommand.
# Matches: run yadgar install-hooks, run yadgar install-subagents,
#          run yadgar config sync, run yadgar seed
_SHIM_CALL_PATTERN = re.compile(
    r"\brun\s+yadgar\s+"
    r"(?:install-hooks|install-subagents|config\s+sync|seed)\b"
)

# The four CLI subcommands that must be present as shim calls.
_EXPECTED_SUBCOMMANDS = [
    "install-hooks",
    "install-subagents",
    "config sync",
    "seed",
]


@pytest.fixture(scope="module")
def setup_sh_text() -> str:
    """Return the full text of yadgar-setup.sh."""
    assert YADGAR_SETUP_SH.exists(), f"yadgar-setup.sh not found at {YADGAR_SETUP_SH}"
    return YADGAR_SETUP_SH.read_text(encoding="utf-8")


class TestNoForbiddenInvocations:
    """Test 1 — no python3 -m yadgar or broken import calls remain."""

    def test_no_python_module_invoke(self, setup_sh_text: str) -> None:
        """No non-comment line must match 'python3? -m yadgar' — breaks on system python.

        The original comment on ~line 180 that documented the old design must
        also be updated (it said '`python3 -m yadgar` CLI subcommands instead.').
        Comment lines inside the _resolve_yadgar_version docblock are permitted
        when they explain the old broken form as context — but only comment lines.
        """
        matches = []
        for lineno, line in enumerate(setup_sh_text.splitlines(), start=1):
            stripped = line.strip()
            # Skip pure comment lines inside function docblocks
            if stripped.startswith("#"):
                continue
            if _FORBIDDEN_MODULE_INVOKE.search(line):
                matches.append((lineno, stripped))

        assert not matches, (
            "Found forbidden 'python3 -m yadgar' invocations (breaks on system python):\n"
            + "\n".join(f"  line {ln}: {text}" for ln, text in matches)
            + "\n\nFix: replace with bare 'yadgar' shim (see v5.46.11 fix plan)."
        )

    def test_no_broken_version_detect(self, setup_sh_text: str) -> None:
        """No non-comment line must use ``python3 -c 'import yadgar'`` for version detection.

        Comment lines inside _resolve_yadgar_version that explain the old broken form
        as context are permitted. Only actual shell invocations are forbidden.
        """
        matches = []
        for lineno, line in enumerate(setup_sh_text.splitlines(), start=1):
            stripped = line.strip()
            # Skip pure comment lines — _resolve_yadgar_version docblock references
            # the old form to explain why the new approach is needed.
            if stripped.startswith("#"):
                continue
            if _FORBIDDEN_VERSION_DETECT.search(line):
                matches.append((lineno, stripped))

        assert not matches, (
            "Found forbidden 'python3 -c import yadgar' version-detect forms:\n"
            + "\n".join(f"  line {ln}: {text}" for ln, text in matches)
            + "\n\nFix: use _resolve_yadgar_version() helper that reads shim shebang."
        )


class TestShimUsage:
    """Test 2 — CLI subcommand calls use the bare yadgar shim."""

    @pytest.mark.parametrize("subcommand", _EXPECTED_SUBCOMMANDS)
    def test_subcommand_uses_shim(self, setup_sh_text: str, subcommand: str) -> None:
        """Each CLI subcommand must appear as ``run yadgar <subcommand>``."""
        # Build specific pattern for this subcommand
        pattern = re.compile(r"\brun\s+yadgar\s+" + re.escape(subcommand) + r"\b")
        found = any(pattern.search(line) for line in setup_sh_text.splitlines())
        assert found, (
            f"Subcommand '{subcommand}' not found as bare shim call.\n"
            f"Expected a line matching: run yadgar {subcommand}\n"
            f"Fix: change 'run python3 -m yadgar {subcommand}' → 'run yadgar {subcommand}'"
        )


class TestVersionHelper:
    """Test 3 — _resolve_yadgar_version() helper is defined and used."""

    def test_helper_function_defined(self, setup_sh_text: str) -> None:
        """_resolve_yadgar_version function must be defined in setup.sh."""
        assert "_resolve_yadgar_version" in setup_sh_text, (
            "_resolve_yadgar_version() helper not found in yadgar-setup.sh.\n"
            "Fix: add helper near top of setup.sh (extracts venv python from shim shebang)."
        )

    def test_helper_reads_shim_shebang(self, setup_sh_text: str) -> None:
        """The helper must use shim shebang extraction (command -v yadgar or head -1)."""
        # Look for the shebang-extraction pattern in the function vicinity
        # Accept either 'command -v yadgar' or 'head -1' + sed pattern
        has_shim_lookup = "command -v yadgar" in setup_sh_text or (
            "head -1" in setup_sh_text and "yadgar_shim" in setup_sh_text
        )
        assert has_shim_lookup, (
            "_resolve_yadgar_version does not appear to use shim shebang extraction.\n"
            "Expected: command -v yadgar (shim lookup) + head -1 (shebang read).\n"
            "Fix: implement shim-shebang extraction as specified in v5.46.11 plan."
        )

    def test_helper_called_at_both_sites(self, setup_sh_text: str) -> None:
        """_resolve_yadgar_version must be called at both version-detect sites."""
        call_count = setup_sh_text.count("_resolve_yadgar_version")
        # At least 3: 1 definition + 2 call sites (step_pull_images + step_generate_units)
        assert call_count >= 3, (
            f"_resolve_yadgar_version appears {call_count} time(s); expected >= 3 "
            f"(1 definition + 2 call sites).\n"
            f"Fix: call _resolve_yadgar_version at both version= assignment sites "
            f"(step_pull_images and step_generate_units)."
        )


class TestCommentUpdated:
    """Test 4 — _locate_setup_scripts comment updated from python3 -m yadgar to shim design."""

    def test_comment_updated(self, setup_sh_text: str) -> None:
        """The _locate_setup_scripts docblock must no longer say 'python3 -m yadgar'.

        The original comment on ~line 180 described the old CLI design:
          '# `python3 -m yadgar` CLI subcommands instead.'
        This must be updated to reflect the shim-based design.

        NOTE: comment lines inside _resolve_yadgar_version that reference the old
        form as context are intentionally excluded from this check (they explain
        WHY the fix was needed, not prescribe the current design). This test focuses
        specifically on the _locate_setup_scripts comment which reflected the old
        architecture.
        """
        # Scan for any comment line that still says 'python3 -m yadgar'
        # _except_ for lines inside the _resolve_yadgar_version helper docblock.
        # Strategy: find _locate_setup_scripts section; within it, no comment
        # should say 'python3 -m yadgar'.
        lines = setup_sh_text.splitlines()
        in_locate_setup = False
        bad_comment_lines = []
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if "_locate_setup_scripts()" in line and not stripped.startswith("#"):
                in_locate_setup = True
            # Stop at next function definition
            elif in_locate_setup and stripped.endswith("()") and "{" not in stripped:
                break
            elif in_locate_setup and stripped.endswith("}"):
                in_locate_setup = False
            if in_locate_setup and stripped.startswith("#"):
                if _FORBIDDEN_MODULE_INVOKE.search(line):
                    bad_comment_lines.append((lineno, stripped))

        assert not bad_comment_lines, (
            "Found 'python3 -m yadgar' in _locate_setup_scripts comment(s).\n"
            "Update to reflect shim-based design (e.g. 'yadgar CLI shim (pipx-aware)'):\n"
            + "\n".join(f"  line {ln}: {text}" for ln, text in bad_comment_lines)
        )
