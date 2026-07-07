"""Unit tests for yadgar/core/scripts/yadgar_setup.py — Python shim.

Wave 5 group B coverage. Strategy: import the module directly (normal Python
import path works — no hyphen). Patch filesystem + os calls to exercise all
branches without spawning real processes or requiring yadgar-setup.sh to exist.

Untestable floor: sys.exit(1) in _find_setup_sh when neither path exists is
exercised via SystemExit assertion. os.execv in main() is patched out to avoid
replacing the test process. The exec call itself is verified by checking the
mock was called with correct args — the line IS covered.

TDD: written before verifying coverage (red → green).
"""

from __future__ import annotations

# The module lives at yadgar/core/scripts/yadgar_setup.py — importable normally.
import importlib.util as _ilu
from pathlib import Path
from unittest.mock import patch

import pytest

_MOD_PATH = Path(__file__).parent.parent / "core" / "scripts" / "yadgar_setup.py"


def _load_module():
    """Load yadgar_setup fresh each call (avoids state leakage between tests)."""
    spec = _ilu.spec_from_file_location("yadgar_setup_fresh", _MOD_PATH)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _find_setup_sh — share_path exists (primary branch)
# ---------------------------------------------------------------------------


class TestFindSetupShPrimary:
    def test_returns_share_path_when_exists(self, tmp_path):
        """Primary: sys.prefix/share/yadgar/scripts/yadgar-setup.sh found."""
        # Build the expected share path under tmp_path
        fake_prefix = tmp_path / "prefix"
        share_dir = fake_prefix / "share" / "yadgar" / "scripts"
        share_dir.mkdir(parents=True)
        script = share_dir / "yadgar-setup.sh"
        script.write_text("#!/bin/bash\necho hello\n")

        mod = _load_module()
        with patch.object(mod.sys, "prefix", str(fake_prefix)):
            result = mod._find_setup_sh()

        assert result == script

    def test_returns_path_object(self, tmp_path):
        fake_prefix = tmp_path / "prefix"
        share_dir = fake_prefix / "share" / "yadgar" / "scripts"
        share_dir.mkdir(parents=True)
        (share_dir / "yadgar-setup.sh").write_text("#!/bin/bash\n")

        mod = _load_module()
        with patch.object(mod.sys, "prefix", str(fake_prefix)):
            result = mod._find_setup_sh()

        assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# _find_setup_sh — fallback (repo checkout layout)
# ---------------------------------------------------------------------------


class TestFindSetupShFallback:
    def test_returns_repo_path_when_share_missing(self, tmp_path):
        """Fallback: repo checkout layout scripts/install/yadgar-setup.sh found."""
        # The module is at yadgar/core/scripts/yadgar_setup.py.
        # repo_root = __file__.parent.parent.parent.parent = repo root
        # repo_path = repo_root / "scripts/install/yadgar-setup.sh"
        # We simulate this by patching Path(__file__) chain.
        mod = _load_module()

        # Build a fake repo root with the expected script
        fake_repo = tmp_path / "repo"
        install_dir = fake_repo / "scripts" / "install"
        install_dir.mkdir(parents=True)
        expected = install_dir / "yadgar-setup.sh"
        expected.write_text("#!/bin/bash\necho setup\n")

        # Make sys.prefix point nowhere (so share_path.exists() is False)
        fake_prefix = tmp_path / "nonexistent_prefix"

        # Patch __file__ on the module so repo_root resolves to fake_repo
        # Module file is at <fake_repo>/yadgar/core/scripts/yadgar_setup.py
        fake_module_file = fake_repo / "yadgar" / "core" / "scripts" / "yadgar_setup.py"

        with (
            patch.object(mod.sys, "prefix", str(fake_prefix)),
            patch.object(mod, "__file__", str(fake_module_file)),
        ):
            result = mod._find_setup_sh()

        assert result == expected

    def test_fallback_path_is_path_object(self, tmp_path):
        mod = _load_module()

        fake_repo = tmp_path / "repo"
        install_dir = fake_repo / "scripts" / "install"
        install_dir.mkdir(parents=True)
        (install_dir / "yadgar-setup.sh").write_text("#!/bin/bash\n")

        fake_prefix = tmp_path / "nope"
        fake_module_file = fake_repo / "yadgar" / "core" / "scripts" / "yadgar_setup.py"

        with (
            patch.object(mod.sys, "prefix", str(fake_prefix)),
            patch.object(mod, "__file__", str(fake_module_file)),
        ):
            result = mod._find_setup_sh()

        assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# _find_setup_sh — neither path exists → sys.exit(1)
# ---------------------------------------------------------------------------


class TestFindSetupShNotFound:
    def test_exits_when_neither_path_found(self, tmp_path, capsys):
        """When neither share_path nor repo_path exists, exits with code 1."""
        mod = _load_module()

        fake_prefix = tmp_path / "nonexistent"
        # Point __file__ to a location where repo_root/scripts/install/ also won't exist
        fake_module_file = (
            tmp_path / "totally_absent" / "yadgar" / "core" / "scripts" / "yadgar_setup.py"
        )

        with (
            patch.object(mod.sys, "prefix", str(fake_prefix)),
            patch.object(mod, "__file__", str(fake_module_file)),
        ):
            with pytest.raises(SystemExit) as exc_info:
                mod._find_setup_sh()

        assert exc_info.value.code == 1

    def test_error_message_printed_to_stderr(self, tmp_path, capsys):
        """When not found, an error is printed to stderr."""
        mod = _load_module()

        fake_prefix = tmp_path / "nonexistent"
        fake_module_file = tmp_path / "x" / "yadgar" / "core" / "scripts" / "yadgar_setup.py"

        with (
            patch.object(mod.sys, "prefix", str(fake_prefix)),
            patch.object(mod, "__file__", str(fake_module_file)),
        ):
            with pytest.raises(SystemExit):
                mod._find_setup_sh()

        captured = capsys.readouterr()
        assert "yadgar-setup.sh" in captured.err
        assert "ERROR" in captured.err


# ---------------------------------------------------------------------------
# main() — normal execution path
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_calls_execv_with_bash(self, tmp_path):
        """main() should exec /bin/bash <setup_sh> [argv...]."""
        mod = _load_module()

        fake_sh = tmp_path / "yadgar-setup.sh"
        fake_sh.write_text("#!/bin/bash\necho ok\n")
        # Not executable yet — main() should chmod it

        captured_execv = {}

        def fake_execv(path, args):
            captured_execv["path"] = path
            captured_execv["args"] = args

        with (
            patch.object(mod, "_find_setup_sh", return_value=fake_sh),
            patch.object(mod.os, "execv", side_effect=fake_execv),
            patch.object(mod.sys, "argv", ["yadgar-setup"]),
        ):
            mod.main()

        assert captured_execv["path"] == "/bin/bash"
        assert captured_execv["args"][0] == "/bin/bash"
        assert captured_execv["args"][1] == str(fake_sh)

    def test_main_forwards_argv(self, tmp_path):
        """main() forwards sys.argv[1:] to the shell script."""
        mod = _load_module()

        fake_sh = tmp_path / "yadgar-setup.sh"
        fake_sh.write_text("#!/bin/bash\n")

        captured_args = {}

        def fake_execv(path, args):
            captured_args["args"] = args

        with (
            patch.object(mod, "_find_setup_sh", return_value=fake_sh),
            patch.object(mod.os, "execv", side_effect=fake_execv),
            patch.object(mod.sys, "argv", ["yadgar-setup", "--flag", "value"]),
        ):
            mod.main()

        assert "--flag" in captured_args["args"]
        assert "value" in captured_args["args"]

    def test_main_sets_executable_bit_when_missing(self, tmp_path):
        """main() calls os.chmod when script is not already executable."""
        mod = _load_module()

        fake_sh = tmp_path / "yadgar-setup.sh"
        fake_sh.write_text("#!/bin/bash\n")
        # Remove execute permission
        fake_sh.chmod(0o644)

        chmod_calls = []

        def fake_execv(path, args):
            pass  # swallow

        with (
            patch.object(mod, "_find_setup_sh", return_value=fake_sh),
            patch.object(mod.os, "execv", side_effect=fake_execv),
            patch.object(mod.os, "chmod", side_effect=lambda p, m: chmod_calls.append((p, m))),
            patch.object(mod.os, "access", return_value=False),
            patch.object(mod.sys, "argv", ["yadgar-setup"]),
        ):
            mod.main()

        assert any(m == 0o755 for _, m in chmod_calls), "Expected chmod 0o755 call"

    def test_main_skips_chmod_when_already_executable(self, tmp_path):
        """main() skips os.chmod when script already has execute bit."""
        mod = _load_module()

        fake_sh = tmp_path / "yadgar-setup.sh"
        fake_sh.write_text("#!/bin/bash\n")
        fake_sh.chmod(0o755)

        chmod_calls = []

        def fake_execv(path, args):
            pass

        with (
            patch.object(mod, "_find_setup_sh", return_value=fake_sh),
            patch.object(mod.os, "execv", side_effect=fake_execv),
            patch.object(mod.os, "chmod", side_effect=lambda p, m: chmod_calls.append((p, m))),
            patch.object(mod.os, "access", return_value=True),
            patch.object(mod.sys, "argv", ["yadgar-setup"]),
        ):
            mod.main()

        assert chmod_calls == [], "chmod should not be called when already executable"

    def test_main_no_extra_argv(self, tmp_path):
        """main() with no extra CLI args passes only [bash, script_path]."""
        mod = _load_module()

        fake_sh = tmp_path / "yadgar-setup.sh"
        fake_sh.write_text("#!/bin/bash\n")

        captured_args = {}

        def fake_execv(path, args):
            captured_args["args"] = args

        with (
            patch.object(mod, "_find_setup_sh", return_value=fake_sh),
            patch.object(mod.os, "execv", side_effect=fake_execv),
            patch.object(mod.sys, "argv", ["yadgar-setup"]),
        ):
            mod.main()

        assert captured_args["args"] == ["/bin/bash", str(fake_sh)]
