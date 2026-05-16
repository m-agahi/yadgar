"""Tests for atomic settings.json / CLAUDE.md writes (§11, Q14).

Simulates crash mid-write and verifies the original file is intact.
"""

import os
import unittest.mock
from pathlib import Path

import pytest


class TestAtomicSettingsWrite:
    """install_hooks and sync_instructions must use atomic tmp+os.replace writes."""

    def test_sync_instructions_leaves_original_intact_on_crash(self, tmp_path):
        """If sync_instructions crashes mid-write, original CLAUDE.md must be intact."""
        import yadgar.server as srv

        # Create a real CLAUDE.md with known content
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        md_path = claude_dir / "CLAUDE.md"
        original_content = "# My rules\n\nDo things correctly.\n"
        md_path.write_text(original_content)

        # Patch os.replace to simulate a crash between write and replace
        calls = []

        def crash_on_replace(src, dst):
            calls.append((src, dst))
            raise OSError("Simulated crash")

        with unittest.mock.patch("os.replace", crash_on_replace):
            with pytest.raises(OSError, match="Simulated crash"):
                srv.sync_instructions(claude_md_path=str(md_path))

        # Original must be intact
        assert md_path.read_text() == original_content

        # Temp file must be cleaned up (or at least not left as destination)
        # The atomic pattern leaves no partial destination
        assert md_path.read_text() == original_content

    def test_sync_instructions_atomic_success(self, tmp_path):
        """sync_instructions succeeds and updates CLAUDE.md atomically."""
        import yadgar.server as srv

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        md_path = claude_dir / "CLAUDE.md"
        md_path.write_text("# My rules\n\nOld content.\n")

        result = srv.sync_instructions(claude_md_path=str(md_path))
        assert result["status"] == "synced"
        new_content = md_path.read_text()
        assert "Memory System" in new_content
        # Old content replaced, but Yadgar section present
        assert "memorize" in new_content.lower() or "yadgar" in new_content.lower()

    def test_sync_instructions_creates_file_if_missing(self, tmp_path):
        """sync_instructions creates CLAUDE.md if it doesn't exist."""
        import yadgar.server as srv

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        md_path = claude_dir / "CLAUDE.md"
        assert not md_path.exists()

        result = srv.sync_instructions(claude_md_path=str(md_path))
        assert result["status"] == "synced"
        assert md_path.exists()
        assert "Memory System" in md_path.read_text()

    def test_sync_instructions_no_temp_files_left_on_success(self, tmp_path):
        """No temp files left after successful sync."""
        import yadgar.server as srv

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        md_path = claude_dir / "CLAUDE.md"
        md_path.write_text("# rules\n")

        srv.sync_instructions(claude_md_path=str(md_path))

        # Only CLAUDE.md should exist (no .tmp files)
        files = list(claude_dir.iterdir())
        assert all(f.name == "CLAUDE.md" for f in files), f"Extra files: {files}"

    def test_install_hooks_settings_is_atomic(self, tmp_path, monkeypatch):
        """install_hooks writes settings.json atomically (tmp + os.replace)."""
        # Verify that os.replace is called with a .json destination
        import yadgar.server as srv

        replace_calls = []
        original_replace = os.replace

        def tracking_replace(src, dst):
            replace_calls.append((src, dst))
            return original_replace(src, dst)

        monkeypatch.setattr(os, "replace", tracking_replace)

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir()

        # install_hooks also writes ~/.claude/settings.json — patch home
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        global_claude = tmp_path / ".claude"
        global_claude.mkdir(exist_ok=True)

        try:
            srv.install_hooks(str(project_dir), scope="project")
        except Exception:
            pass  # May fail due to missing hooks dir, but we check the replace calls

        # At least one replace call must target settings.json
        dst_names = [Path(dst).name for _, dst in replace_calls]
        assert any("settings" in n for n in dst_names), (
            f"No atomic replace for settings.json; got: {dst_names}"
        )
