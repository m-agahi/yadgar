"""v5.45.0 Step 1 TDD — seed anchors + yadgar seed --anchors flag (RED)."""

import shutil
import subprocess
import sys

from yadgar.tests._paths import REPO_ROOT

BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"

# v5.88: anchors.yaml moved to the canonical seed materials dir (one place to edit seeds).
ANCHORS_YAML = REPO_ROOT / "yadgar" / "core" / "seed" / "materials" / "anchors.yaml"


class TestV5_45SeedAnchorsLoader:
    """Tests for yadgar/seed/materials/anchors.yaml + yadgar seed --anchors flag."""

    def test_v5_45_anchors_yaml_exists(self):
        """yadgar/seed/materials/anchors.yaml must exist."""
        assert ANCHORS_YAML.exists(), (
            f"yadgar/seed/materials/anchors.yaml not found at {ANCHORS_YAML}"
        )

    def test_v5_45_anchors_yaml_valid(self):
        """anchors.yaml must be valid YAML with at least 6 entries."""
        try:
            import yaml
        except ImportError:
            # Try ruamel.yaml
            from ruamel.yaml import YAML

            yaml_parser = YAML()
            with open(ANCHORS_YAML) as f:
                data = yaml_parser.load(f)
            entries = data if isinstance(data, list) else data.get("anchors", [])
            assert len(entries) >= 6, (
                f"anchors.yaml must have at least 6 entries, got {len(entries)}"
            )
            return
        with open(ANCHORS_YAML) as f:
            data = yaml.safe_load(f)
        entries = data if isinstance(data, list) else data.get("anchors", [])
        assert len(entries) >= 6, f"anchors.yaml must have at least 6 entries, got {len(entries)}"

    def test_v5_45_anchors_yaml_has_required_fields(self):
        """Each anchor entry must have at least 'content' and 'tags' fields."""
        try:
            import yaml

            with open(ANCHORS_YAML) as f:
                data = yaml.safe_load(f)
        except ImportError:
            from ruamel.yaml import YAML

            yaml_parser = YAML()
            with open(ANCHORS_YAML) as f:
                data = yaml_parser.load(f)

        entries = data if isinstance(data, list) else data.get("anchors", [])
        for i, entry in enumerate(entries):
            assert "content" in entry, f"Anchor entry {i} missing 'content' field"
            assert "tags" in entry, f"Anchor entry {i} missing 'tags' field"

    def test_v5_45_seed_subcommand_accepts_anchors_flag(self):
        """yadgar seed --anchors <file> must be a valid CLI invocation (no error on --help)."""
        result = subprocess.run(
            [sys.executable, "-m", "yadgar", "seed", "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        # After Step 3 impl, --anchors flag must appear in help output
        # For now we just confirm seed subcommand has --help
        # RED test: --anchors must appear in help after impl
        assert result.returncode == 0, (
            f"yadgar seed --help failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # This fails RED until --anchors is added:
        assert "--anchors" in result.stdout, (
            "yadgar seed must accept --anchors flag (not yet implemented — this is the RED test)"
        )

    def test_v5_45_seed_anchors_cli_flag_parsing(self, tmp_path):
        """yadgar seed --anchors <file> must parse without error (with a valid yaml)."""
        # Create minimal valid anchors file
        anchor_file = tmp_path / "test_anchors.yaml"
        anchor_file.write_text(
            "anchors:\n  - content: 'Test anchor for wiki RMW'\n    tags: ['_anchor', 'wiki']\n"
        )
        result = subprocess.run(
            [sys.executable, "-m", "yadgar", "seed", "--anchors", str(anchor_file), "--dry-run"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        # RED: fails until --anchors implemented; should exit 0 with dry-run
        assert result.returncode == 0, (
            f"yadgar seed --anchors --dry-run failed\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


class TestV5_45InstallRulesDedup:
    """Tests for scripts/install/append_claude_rules.sh deduplication."""

    APPEND_RULES_SH = REPO_ROOT / "scripts" / "install" / "append_claude_rules.sh"
    CLAUDE_FRAGMENT = REPO_ROOT / "install_assets" / "CLAUDE.md.fragment"

    def test_v5_45_append_claude_rules_script_exists(self):
        """append_claude_rules.sh must exist."""
        assert self.APPEND_RULES_SH.exists(), "scripts/install/append_claude_rules.sh not found"

    def test_v5_45_claude_fragment_exists(self):
        """install_assets/CLAUDE.md.fragment must exist."""
        assert self.CLAUDE_FRAGMENT.exists(), "install_assets/CLAUDE.md.fragment not found"

    def test_v5_45_claude_fragment_has_markers(self):
        """CLAUDE.md.fragment must have begin/end markers for dedup."""
        content = self.CLAUDE_FRAGMENT.read_text()
        assert "YADGAR-RULES-BEGIN" in content, (
            "CLAUDE.md.fragment must contain <!-- YADGAR-RULES-BEGIN --> marker"
        )
        assert "YADGAR-RULES-END" in content, (
            "CLAUDE.md.fragment must contain <!-- YADGAR-RULES-END --> marker"
        )

    def test_v5_45_install_rules_appends_fragment(self, tmp_path):
        """append_claude_rules.sh must append fragment to target CLAUDE.md."""
        target_claude = tmp_path / "CLAUDE.md"
        target_claude.write_text("# Existing CLAUDE.md\n\nSome existing content.\n")

        result = subprocess.run(
            [BASH, str(self.APPEND_RULES_SH)],
            capture_output=True,
            text=True,
            env={
                **__import__("os").environ,
                "YADGAR_CLAUDE_MD_TARGET": str(target_claude),
                "YADGAR_FRAGMENT_PATH": str(self.CLAUDE_FRAGMENT),
            },
        )
        assert result.returncode == 0, (
            f"append_claude_rules.sh failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        result_content = target_claude.read_text()
        assert "YADGAR-RULES-BEGIN" in result_content, (
            "Fragment begin marker not in target after append"
        )

    def test_v5_45_install_rules_deduplicates(self, tmp_path):
        """Running append_claude_rules.sh twice must not duplicate the fragment."""
        target_claude = tmp_path / "CLAUDE.md"
        target_claude.write_text("# Existing content\n")
        env = {
            **__import__("os").environ,
            "YADGAR_CLAUDE_MD_TARGET": str(target_claude),
            "YADGAR_FRAGMENT_PATH": str(self.CLAUDE_FRAGMENT),
        }
        # Run twice
        for _ in range(2):
            subprocess.run(
                [BASH, str(self.APPEND_RULES_SH)],
                capture_output=True,
                text=True,
                env=env,
            )
        content = target_claude.read_text()
        # Count occurrences of begin marker
        count = content.count("YADGAR-RULES-BEGIN")
        assert count == 1, f"Fragment appended {count} times — should be exactly 1 (idempotent)"
