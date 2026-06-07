"""Tests for macOS launchd plist templates + wrapper scripts (v5.47.x).

TDD: written before full implementation, confirmed red before green.

Covers:
  - 5 new .plist.in templates (vacuum, nightly-cycle, vacuum-trigger, worktree-sweep)
    plus the 2 existing daemon plists (yadgar, yadgar-backend) for @TOKEN@ migration.
  - XML + plistlib parseability after @TOKEN@ substitution with test values.
  - generate_launchd.sh: recognises all 7 templates, substitutes all @TOKEN@ tokens.
  - Wrapper scripts: exist, executable, use gtimeout/timeout detection (D3),
    use explicit export pattern (D4).
  - yadgar-secrets-activation.sh: exists, handles op inject, writes mode 600 logic.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

# Prefer defusedxml (XXE-safe) over stdlib xml.etree.ElementTree.
# defusedxml is declared in [project.optional-dependencies.test] in pyproject.toml.
# If the Nix shell env doesn't include it yet, fall back to stdlib with a warning —
# the plists being parsed are our own generated output (not attacker-controlled),
# so the risk is low, but defusedxml is strictly preferred.
try:
    import defusedxml.ElementTree as ET  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    import warnings
    import xml.etree.ElementTree as ET  # type: ignore[no-redef]

    warnings.warn(
        "defusedxml not installed — falling back to stdlib xml.etree.ElementTree. "
        "Install: nix shell nixpkgs#python3Packages.defusedxml",
        stacklevel=1,
    )

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parents[2]
LAUNCHD_DIR = REPO_ROOT / "scripts" / "install" / "launchd"
GENERATOR = REPO_ROOT / "scripts" / "install" / "generate_launchd.sh"

# Test substitution values — stand-ins for the @TOKEN@ placeholders
TEST_SUBS = {
    "@YADGAR_RUNTIME@": "/usr/local/bin/podman",
    "@YADGAR_CORE_IMAGE@": "openfantasy/yadgar:test",
    "@YADGAR_BACKEND_IMAGE@": "openfantasy/yadgar-backend:test",
    "@YADGAR_INSTALL_PREFIX@": "/Users/testuser/.yadgar",
    "@YADGAR_SECRETS_ENV_FILE@": "/Users/testuser/.config/yadgar/secrets.env",
    "@YADGAR_HOME@": "/Users/testuser",
    "@YADGAR_SCRIPTS_DIR@": "/Users/testuser/.local/share/yadgar/scripts",
}

ALL_TEMPLATES = [
    "com.openfantasy.yadgar.plist.in",
    "com.openfantasy.yadgar-backend.plist.in",
    "com.openfantasy.yadgar-vacuum.plist.in",
    "com.openfantasy.yadgar-nightly-cycle.plist.in",
    "com.openfantasy.yadgar-vacuum-trigger.plist.in",
    "com.openfantasy.yadgar-worktree-sweep.plist.in",
]

# Templates that must include the StartCalendarInterval UTC-warning comment (D8)
SCHEDULED_TEMPLATES = [
    "com.openfantasy.yadgar-vacuum.plist.in",
    "com.openfantasy.yadgar-nightly-cycle.plist.in",
    "com.openfantasy.yadgar-worktree-sweep.plist.in",
]

WRAPPER_SCRIPTS = [
    "yadgar-vacuum-wrapper.sh",
    "yadgar-nightly-cycle-wrapper.sh",
    "yadgar-vacuum-trigger-wrapper.sh",
    "yadgar-worktree-sweep-wrapper.sh",
]

SECRETS_SCRIPT = "yadgar-secrets-activation.sh"


def _substitute(content: str) -> str:
    """Apply all TEST_SUBS to a template string."""
    for token, value in TEST_SUBS.items():
        content = content.replace(token, value)
    return content


def _load_template(name: str) -> str:
    return (LAUNCHD_DIR / name).read_text()


# ── Template existence ─────────────────────────────────────────────────────────


class TestTemplateExistence:
    @pytest.mark.parametrize("template", ALL_TEMPLATES)
    def test_template_file_exists(self, template: str) -> None:
        assert (LAUNCHD_DIR / template).is_file(), f"Template not found: {LAUNCHD_DIR / template}"


# ── Required plist keys ────────────────────────────────────────────────────────


class TestTemplateRequiredKeys:
    @pytest.mark.parametrize("template", ALL_TEMPLATES)
    def test_has_label_key(self, template: str) -> None:
        content = _load_template(template)
        assert "<key>Label</key>" in content, f"{template}: missing <key>Label</key>"

    @pytest.mark.parametrize("template", ALL_TEMPLATES)
    def test_has_program_arguments_key(self, template: str) -> None:
        content = _load_template(template)
        assert "<key>ProgramArguments</key>" in content, (
            f"{template}: missing <key>ProgramArguments</key>"
        )

    @pytest.mark.parametrize("template", ALL_TEMPLATES)
    def test_no_dollar_brace_tokens_remain(self, template: str) -> None:
        """All templates must use @TOKEN@ style, not ${TOKEN} (D1)."""
        content = _load_template(template)
        # Allow $(...) in comments or shell strings, but no ${VAR} substitution tokens
        dollar_tokens = re.findall(r"\$\{[A-Z_]+\}", content)
        assert not dollar_tokens, (
            f"{template}: found ${{TOKEN}} style tokens (must use @TOKEN@): {dollar_tokens}"
        )


# ── StartCalendarInterval UTC warning (D8) ────────────────────────────────────


class TestD8UtcWarning:
    @pytest.mark.parametrize("template", SCHEDULED_TEMPLATES)
    def test_has_utc_warning_comment(self, template: str) -> None:
        content = _load_template(template)
        assert "NOTE: StartCalendarInterval fires in LOCAL TIME, not UTC." in content, (
            f"{template}: missing D8 UTC warning comment"
        )

    def test_vacuum_trigger_has_no_calendar_interval(self) -> None:
        """vacuum-trigger uses WatchPaths, not StartCalendarInterval."""
        content = _load_template("com.openfantasy.yadgar-vacuum-trigger.plist.in")
        assert "StartCalendarInterval" not in content
        assert "WatchPaths" in content


# ── Oneshot plist properties ───────────────────────────────────────────────────


class TestOneshotProperties:
    ONESHOT_TEMPLATES = [
        "com.openfantasy.yadgar-vacuum.plist.in",
        "com.openfantasy.yadgar-nightly-cycle.plist.in",
        "com.openfantasy.yadgar-vacuum-trigger.plist.in",
        "com.openfantasy.yadgar-worktree-sweep.plist.in",
    ]

    @pytest.mark.parametrize("template", ONESHOT_TEMPLATES)
    def test_run_at_load_false(self, template: str) -> None:
        """Oneshot plists must not RunAtLoad (timer/path drives them)."""
        content = _load_template(template)
        # RunAtLoad must be followed by <false/>, not <true/>
        assert re.search(r"<key>RunAtLoad</key>\s*<false/>", content), (
            f"{template}: RunAtLoad must be <false/> for oneshot"
        )

    @pytest.mark.parametrize("template", ONESHOT_TEMPLATES)
    def test_keep_alive_false(self, template: str) -> None:
        """Oneshot plists must not KeepAlive (D7)."""
        content = _load_template(template)
        assert re.search(r"<key>KeepAlive</key>\s*<false/>", content), (
            f"{template}: KeepAlive must be bare <false/> for oneshot (D7)"
        )


# ── XML parseability after substitution ───────────────────────────────────────


class TestXmlParseability:
    @pytest.mark.parametrize("template", ALL_TEMPLATES)
    def test_xml_parses_after_substitution(self, template: str) -> None:
        raw = _load_template(template)
        substituted = _substitute(raw)
        try:
            ET.fromstring(substituted)
        except ET.ParseError as exc:
            pytest.fail(f"{template}: XML parse error after substitution: {exc}")


# ── plistlib parseability after substitution ──────────────────────────────────


class TestPlistlibParseability:
    @pytest.mark.parametrize("template", ALL_TEMPLATES)
    def test_plistlib_parses_after_substitution(self, template: str) -> None:
        raw = _load_template(template)
        substituted = _substitute(raw)
        try:
            data = plistlib.loads(substituted.encode())
        except Exception as exc:
            pytest.fail(f"{template}: plistlib parse error: {exc}")
        assert isinstance(data, dict), f"{template}: plist root is not a dict"
        assert "Label" in data, f"{template}: plist missing 'Label' key"
        assert "ProgramArguments" in data, f"{template}: plist missing 'ProgramArguments' key"

    def test_vacuum_plist_has_start_calendar_interval(self) -> None:
        raw = _load_template("com.openfantasy.yadgar-vacuum.plist.in")
        data = plistlib.loads(_substitute(raw).encode())
        assert "StartCalendarInterval" in data
        sched = data["StartCalendarInterval"]
        assert sched.get("Weekday") == 0  # Sunday
        assert sched.get("Hour") == 4
        assert sched.get("Minute") == 0

    def test_nightly_cycle_plist_has_start_calendar_interval(self) -> None:
        raw = _load_template("com.openfantasy.yadgar-nightly-cycle.plist.in")
        data = plistlib.loads(_substitute(raw).encode())
        assert "StartCalendarInterval" in data
        sched = data["StartCalendarInterval"]
        assert sched.get("Hour") == 19
        assert sched.get("Minute") == 0

    def test_worktree_sweep_plist_has_start_calendar_interval(self) -> None:
        raw = _load_template("com.openfantasy.yadgar-worktree-sweep.plist.in")
        data = plistlib.loads(_substitute(raw).encode())
        assert "StartCalendarInterval" in data
        sched = data["StartCalendarInterval"]
        assert sched.get("Weekday") == 0  # Sunday
        assert sched.get("Hour") == 2
        assert sched.get("Minute") == 0

    def test_vacuum_trigger_plist_has_watch_paths(self) -> None:
        raw = _load_template("com.openfantasy.yadgar-vacuum-trigger.plist.in")
        data = plistlib.loads(_substitute(raw).encode())
        assert "WatchPaths" in data
        paths = data["WatchPaths"]
        assert isinstance(paths, list) and len(paths) >= 1
        assert ".yadgar/triggers" in paths[0]


# ── generate_launchd.sh integration ───────────────────────────────────────────


class TestGenerateLaunchdScript:
    def test_generator_script_exists(self) -> None:
        assert GENERATOR.is_file()

    def test_generator_script_executable(self) -> None:
        assert os.access(GENERATOR, os.X_OK)

    def test_generator_renders_all_templates(self, tmp_path: Path) -> None:
        """Generator must render all 6 plists (2 daemons + 4 oneshots) to output dir."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        env = {
            **os.environ,
            "YADGAR_LAUNCHD_OUTPUT_DIR": str(tmp_path / "LaunchAgents"),
            "YADGAR_RUNTIME": "/usr/local/bin/podman",
            "YADGAR_INSTALL_PREFIX": "/Users/testuser/.yadgar",
            "YADGAR_SECRETS_ENV_FILE": "/Users/testuser/.config/yadgar/secrets.env",
            "YADGAR_BACKEND_IMAGE": "openfantasy/yadgar-backend:test",
            "YADGAR_CORE_IMAGE": "openfantasy/yadgar:test",
        }
        result = subprocess.run(
            ["bash", str(GENERATOR)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"generate_launchd.sh failed:\n{result.stderr}"

        out_dir = tmp_path / "LaunchAgents"
        expected_plists = [
            "com.openfantasy.yadgar.plist",
            "com.openfantasy.yadgar-backend.plist",
            "com.openfantasy.yadgar-vacuum.plist",
            "com.openfantasy.yadgar-nightly-cycle.plist",
            "com.openfantasy.yadgar-vacuum-trigger.plist",
            "com.openfantasy.yadgar-worktree-sweep.plist",
        ]
        for plist_name in expected_plists:
            plist_path = out_dir / plist_name
            assert plist_path.is_file(), f"Generator did not produce {plist_name}"

    def test_generator_substitutes_all_tokens(self, tmp_path: Path) -> None:
        """Generated plists must contain no residual @TOKEN@ placeholders."""
        env = {
            **os.environ,
            "YADGAR_LAUNCHD_OUTPUT_DIR": str(tmp_path / "LaunchAgents"),
            "YADGAR_RUNTIME": "/usr/local/bin/podman",
            "YADGAR_INSTALL_PREFIX": "/Users/testuser/.yadgar",
            "YADGAR_SECRETS_ENV_FILE": "/Users/testuser/.config/yadgar/secrets.env",
            "YADGAR_BACKEND_IMAGE": "openfantasy/yadgar-backend:test",
            "YADGAR_CORE_IMAGE": "openfantasy/yadgar:test",
        }
        subprocess.run(["bash", str(GENERATOR)], env=env, capture_output=True)
        out_dir = tmp_path / "LaunchAgents"
        for plist_path in sorted(out_dir.glob("*.plist")):
            content = plist_path.read_text()
            residual = re.findall(r"@[A-Z_]+@", content)
            assert not residual, f"{plist_path.name}: residual @TOKEN@ after generation: {residual}"


# ── Wrapper script checks ──────────────────────────────────────────────────────


class TestWrapperScripts:
    @pytest.mark.parametrize("script", WRAPPER_SCRIPTS)
    def test_wrapper_exists(self, script: str) -> None:
        assert (LAUNCHD_DIR / script).is_file(), f"Wrapper not found: {LAUNCHD_DIR / script}"

    @pytest.mark.parametrize("script", WRAPPER_SCRIPTS)
    def test_wrapper_executable(self, script: str) -> None:
        path = LAUNCHD_DIR / script
        assert os.access(path, os.X_OK), f"Wrapper not executable: {path}"

    # vacuum-trigger is a fast kickstart script (no long-running exec);
    # D3 timeout detection applies only to wrappers that exec a long job.
    TIMEOUT_WRAPPERS = [
        "yadgar-vacuum-wrapper.sh",
        "yadgar-nightly-cycle-wrapper.sh",
        "yadgar-worktree-sweep-wrapper.sh",
    ]

    @pytest.mark.parametrize("script", TIMEOUT_WRAPPERS)
    def test_wrapper_has_timeout_detection(self, script: str) -> None:
        """D3: long-running wrappers must use gtimeout/timeout detection pattern."""
        content = (LAUNCHD_DIR / script).read_text()
        assert "gtimeout" in content, f"{script}: missing gtimeout reference (D3)"
        assert "timeout" in content, f"{script}: missing timeout reference (D3)"

    @pytest.mark.parametrize(
        "script", ["yadgar-vacuum-wrapper.sh", "yadgar-nightly-cycle-wrapper.sh"]
    )
    def test_wrapper_has_explicit_export_pattern(self, script: str) -> None:
        """D4: env-loading wrappers must use explicit key-by-key export."""
        content = (LAUNCHD_DIR / script).read_text()
        assert "KEYS_NEEDED" in content, (
            f"{script}: missing KEYS_NEEDED explicit export pattern (D4)"
        )
        # Must NOT use 'set -a' + 'source' pattern
        assert "set -a" not in content, f"{script}: must not use 'set -a' / source leak (D4)"

    @pytest.mark.parametrize("script", WRAPPER_SCRIPTS)
    def test_wrapper_syntax_check(self, script: str) -> None:
        """bash -n syntax check on every wrapper."""
        result = subprocess.run(
            ["bash", "-n", str(LAUNCHD_DIR / script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script}: bash -n syntax error:\n{result.stderr}"


# ── Secrets activation script ──────────────────────────────────────────────────


class TestSecretsActivationScript:
    def test_secrets_script_exists(self) -> None:
        assert (LAUNCHD_DIR / SECRETS_SCRIPT).is_file()

    def test_secrets_script_executable(self) -> None:
        assert os.access(LAUNCHD_DIR / SECRETS_SCRIPT, os.X_OK)

    def test_secrets_script_references_op_inject(self) -> None:
        content = (LAUNCHD_DIR / SECRETS_SCRIPT).read_text()
        assert "op inject" in content, "secrets activation script must call 'op inject'"

    def test_secrets_script_sets_mode_600(self) -> None:
        content = (LAUNCHD_DIR / SECRETS_SCRIPT).read_text()
        assert "chmod 600" in content, "secrets activation script must set mode 600 on secrets.env"

    def test_secrets_script_handles_missing_template(self) -> None:
        """Script must exit 0 (not 1) if template is absent (graceful skip)."""
        # Run script with a non-existent template path and capture exit code
        result = subprocess.run(
            ["bash", str(LAUNCHD_DIR / SECRETS_SCRIPT)],
            env={
                **os.environ,
                # Point HOME to a tmp dir with no template
                "HOME": tempfile.mkdtemp(),
            },
            capture_output=True,
            text=True,
        )
        # Should exit 0 with an INFO message, not error
        assert result.returncode == 0, (
            f"secrets script failed with no template (expected graceful skip):\n{result.stderr}"
        )
        assert "INFO" in result.stdout or "skipping" in result.stdout.lower(), (
            "secrets script should print INFO when no template found"
        )

    def test_secrets_script_syntax_check(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(LAUNCHD_DIR / SECRETS_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"secrets activation script bash -n error:\n{result.stderr}"
