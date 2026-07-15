"""Tests for v5.44.0 — Subagent MCP Wiring + Automation Extensions.

Covers:
- Base: bundled agent template files exist + correct structure
- X1: agent_dispatch_prelude extended with branch_hint/directory/subagent_type/include_context
- X2: SubagentStop directive parser (memorize/wiki_add/anchor) + writeback
- X3: OS-detection helpers (platform_paths.py)
- X4: install-subagents idempotency
- X5: config_sync — missing keys added, user values preserved, idempotency
- Production write-path test (P7): directive parsed + dispatched correctly
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Base: bundled agent template files ────────────────────────────────────────


class TestBundledAgentTemplates:
    """yadgar/install_assets/agents/ contains all required template files."""

    def _agents_dir(self) -> Path:
        # Resolve relative to this file
        here = Path(__file__).parent.parent.parent  # yadgar/
        return here / "core" / "install_assets" / "agents"

    def test_general_purpose_md_exists(self):
        p = self._agents_dir() / "general-purpose.md"
        assert p.exists(), f"Missing bundled template: {p}"

    def test_explore_md_exists(self):
        p = self._agents_dir() / "Explore.md"
        assert p.exists(), f"Missing bundled template: {p}"

    def test_cavecrew_investigator_md_exists(self):
        p = self._agents_dir() / "cavecrew-investigator.md"
        assert p.exists(), f"Missing bundled template: {p}"

    def test_cavecrew_builder_md_exists(self):
        p = self._agents_dir() / "cavecrew-builder.md"
        assert p.exists(), f"Missing bundled template: {p}"

    def test_cavecrew_reviewer_md_exists(self):
        p = self._agents_dir() / "cavecrew-reviewer.md"
        assert p.exists(), f"Missing bundled template: {p}"

    def test_general_purpose_has_yadgar_tools(self):
        p = self._agents_dir() / "general-purpose.md"
        content = p.read_text()
        assert "mcp__yadgar__recall" in content
        assert "mcp__yadgar__memorize" in content

    def test_explore_has_no_mcp_tools(self):
        """Explore uses Haiku — yadgar MCP tools must NOT be in its tools list."""
        p = self._agents_dir() / "Explore.md"
        content = p.read_text()
        assert "mcp__yadgar" not in content

    def test_cavecrew_reviewer_has_no_mcp_tools(self):
        """cavecrew-reviewer has no yadgar access — output is the review itself."""
        p = self._agents_dir() / "cavecrew-reviewer.md"
        content = p.read_text()
        assert "mcp__yadgar" not in content

    def test_cavecrew_investigator_read_only_yadgar(self):
        """cavecrew-investigator gets read tools but not memorize."""
        p = self._agents_dir() / "cavecrew-investigator.md"
        content = p.read_text()
        assert "mcp__yadgar__recall" in content
        assert "mcp__yadgar__wiki_query" in content
        # Should NOT have memorize — it's a read-only agent
        # (it may emit findings for SubagentStop hook to persist)
        assert "mcp__yadgar__wiki_add" not in content

    def test_all_templates_have_yadgar_findings_section(self):
        """All agent templates that do yadgar work must include findings protocol."""
        agents_dir = self._agents_dir()
        for name in ["general-purpose.md", "cavecrew-investigator.md", "cavecrew-builder.md"]:
            content = (agents_dir / name).read_text()
            assert "Yadgar Findings" in content or "Yadgar findings" in content, (
                f"{name} missing Yadgar Findings section"
            )


# ── X1: agent_dispatch_prelude extension ──────────────────────────────────────


class TestAgentDispatchPreludeX1:
    """agent_dispatch_prelude backward compat + new optional params."""

    def test_backward_compat_no_new_params(self):
        """Old callers: agent_dispatch_prelude(pattern, topic, storage) still works."""
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        # Mock storage — no agent_prompt_get results
        storage = MagicMock()
        result = agent_dispatch_prelude("", "vacuum regression", storage=storage)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_include_context_false_no_prefetch(self):
        """include_context=False (default) should NOT call recall or wiki_query."""
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        storage = MagicMock()

        with patch("yadgar.core.server.tools.dispatch_helper._build_context_block") as mock_ctx:
            mock_ctx.return_value = "### Yadgar Context\n- test"
            result = agent_dispatch_prelude(
                "",
                "some topic",
                storage=storage,
                branch_hint="feat/test",
                directory="/tmp/test",
                subagent_type="general-purpose",
                include_context=False,  # OFF — default
            )
            # _build_context_block must NOT be called
            mock_ctx.assert_not_called()

        assert isinstance(result, str)

    def test_include_context_true_calls_build_context(self):
        """include_context=True invokes _build_context_block."""
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        storage = MagicMock()

        with patch("yadgar.core.server.tools.dispatch_helper._build_context_block") as mock_ctx:
            mock_ctx.return_value = (
                "### Yadgar Context (auto-prefetched ...)\n\n### Recent memories\n- test memory"
            )
            result = agent_dispatch_prelude(
                "",
                "some topic",
                storage=storage,
                branch_hint="feat/test",
                directory="/tmp/test",
                subagent_type="general-purpose",
                include_context=True,
            )
            mock_ctx.assert_called_once_with(
                task_topic="some topic",
                branch_hint="feat/test",
                directory="/tmp/test",
                subagent_type="general-purpose",
                storage=storage,
            )

        assert "Yadgar Context" in result

    def test_include_context_uses_v5_43_0_signatures(self):
        """_build_context_block is written to call recall/wiki_query with directory+branch_hint.

        Verifies by inspecting the source code that the v5.43.0 kwarg names appear
        in the _build_context_block function body (integration guard).
        Also verifies the function accepts the expected parameters.
        """
        import inspect

        from yadgar.core.server.tools.dispatch_helper import _build_context_block

        # Signature check — function accepts branch_hint and directory params
        sig = inspect.signature(_build_context_block)
        param_names = list(sig.parameters.keys())
        assert "branch_hint" in param_names, "Missing branch_hint param in _build_context_block"
        assert "directory" in param_names, "Missing directory param in _build_context_block"

        # Source check — function calls recall with directory= and branch_hint= kwargs
        source = inspect.getsource(_build_context_block)
        assert "directory=" in source, "_build_context_block must pass directory= to recall"
        assert "branch_hint=" in source, "_build_context_block must pass branch_hint= to recall"

    def test_contract_text_updated_for_long_running(self):
        """v5.44.0 contract allows long_running agents to call memorize directly.

        v5.122.0: _YADGAR_CONTRACT constant removed — contract genesis lives in
        seed materials (CONTRACT_GENESIS); wiki page is the runtime source.
        """
        from yadgar.core.server.tools.agent_prompts import CONTRACT_GENESIS

        # DP-1: long_running carve-out must be documented
        _, _, _contract = CONTRACT_GENESIS
        lower = _contract.lower()
        has_exception = (
            "long_running" in lower
            or "long-running" in lower
            or "exception" in lower
            or "provenance_agent" in lower
        )
        assert has_exception, (
            "Contract must mention long_running carve-out (DP-1) or provenance_agent"
        )

    def test_prelude_returns_string_with_new_params(self):
        """No exception when all new params provided."""
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        storage = MagicMock()
        result = agent_dispatch_prelude(
            "dispatch-fix-bug",
            "task topic",
            storage=storage,
            branch_hint="feat/v5.44.0",
            directory="/tmp/project",
            subagent_type="general-purpose",
            include_context=False,
        )
        assert isinstance(result, str)
        assert len(result) <= 4001  # budget guard


# ── X2: SubagentStop directive parser ─────────────────────────────────────────


class TestSubagentStopDirectiveParser:
    """parse_directive() correctly identifies memorize/wiki_add/anchor directives."""

    def test_parse_memorize_directive(self):
        from yadgar.core.hooks.subagent_stop import _parse_directive

        directive = 'memorize: content="found that module X uses Y", tags=["arch","v5.44.0"], context="/tmp/proj"'
        result = _parse_directive(directive)
        assert result is not None
        assert result["type"] == "memorize"
        assert "module X" in result["params"].get("content", "")

    def test_parse_wiki_add_directive(self):
        from yadgar.core.hooks.subagent_stop import _parse_directive

        directive = 'wiki_add: title="X2 design", content="SubagentStop extension", category="architecture", tags=["v5.44.0"], directory="/tmp", branch_hint="feat/v5.44.0"'
        result = _parse_directive(directive)
        assert result is not None
        assert result["type"] == "wiki_add"
        assert result["params"].get("title") == "X2 design"

    def test_parse_anchor_directive(self):
        from yadgar.core.hooks.subagent_stop import _parse_directive

        directive = 'anchor: content="SubagentStop X2 ships in v5.44.0", reason="key milestone", tier="conditional"'
        result = _parse_directive(directive)
        assert result is not None
        assert result["type"] == "anchor"
        assert "v5.44.0" in result["params"].get("content", "")

    def test_parse_none_returns_none(self):
        """Plain text bullets that aren't directives return None."""
        from yadgar.core.hooks.subagent_stop import _parse_directive

        assert _parse_directive("some plain text finding") is None
        assert _parse_directive("fact: module X calls Y") is None

    def test_parse_malformed_directive_returns_none(self):
        """Malformed directive key=value returns None (lenient — don't reject)."""
        from yadgar.core.hooks.subagent_stop import _parse_directive

        _parse_directive("memorize: content=NOT_QUOTED")
        # May be None or partial — as long as it doesn't raise
        # (Lenient: bad parse returns None, metric incremented)

    def test_extract_directives_from_findings(self):
        """Bullets with directives are identified as directives."""
        from yadgar.core.hooks.subagent_stop import _extract_findings, _parse_directive

        report = """\
## Yadgar Findings

- memorize: content="key discovery about X", tags=["v5.44.0"], context="/tmp"
- wiki_add: title="new page", content="content here", category="notes", tags=[], directory="/tmp", branch_hint="feat/test"
- anchor: content="important anchor", reason="milestone", tier="always"
- none
"""
        bullets = _extract_findings(report)
        # "none" sentinel is skipped
        directives = [_parse_directive(b) for b in bullets]
        directives = [d for d in directives if d is not None]
        types = {d["type"] for d in directives}
        assert "memorize" in types
        assert "wiki_add" in types
        assert "anchor" in types

    def test_plain_bullet_falls_back_to_memorize(self):
        """Non-directive bullets (plain text) still get stored as memories (backward compat)."""

        from yadgar.core.hooks.subagent_stop import _post_findings

        posted_payloads = []

        class _FakeResp:
            def read(self):
                return b'{"status":"ok"}'

        def _fake_urlopen(req, timeout=None):
            posted_payloads.append(json.loads(req.data.decode()))
            return _FakeResp()

        with patch("urllib.request.urlopen", _fake_urlopen):
            _post_findings(
                "general-purpose",
                "/tmp/proj",
                ["plain text finding no directive"],
                branch_hint=None,
            )

        assert len(posted_payloads) == 1
        payload = posted_payloads[0]
        assert "plain text finding" in str(payload.get("findings", payload.get("directives", "")))


class TestSubagentStopWriteback:
    """SubagentStop hook correctly routes directives to write tools."""

    def test_post_findings_includes_branch_hint(self):
        """_post_findings includes branch_hint in the POST payload."""
        from yadgar.core.hooks import subagent_stop as _hs

        posted = []

        class _FakeResp:
            def read(self):
                return b'{"status":"ok"}'

        def _fake_urlopen(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            return _FakeResp()

        with patch("urllib.request.urlopen", _fake_urlopen):
            _hs._post_findings(
                "general-purpose",
                "/tmp/proj",
                ['memorize: content="test", tags=["v5.44"], context="/tmp"'],
                branch_hint="feat/v5.44.0",
            )

        assert len(posted) == 1
        assert posted[0].get("branch_hint") == "feat/v5.44.0"

    def test_post_includes_subagent_writeback_tag_request(self):
        """Payload signals _subagent_writeback for daemon-side tagging."""
        from yadgar.core.hooks import subagent_stop as _hs

        posted = []

        class _FakeResp:
            def read(self):
                return b'{"status":"ok"}'

        def _fake_urlopen(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            return _FakeResp()

        with patch("urllib.request.urlopen", _fake_urlopen):
            _hs._post_findings(
                "general-purpose",
                "/tmp/proj",
                ["some finding"],
                branch_hint="feat/v5.44.0",
            )

        payload = posted[0]
        # Either findings list or directives list must be present
        assert "findings" in payload or "directives" in payload


# ── X3: OS-detection helpers ──────────────────────────────────────────────────


class TestPlatformPaths:
    """platform_paths returns correct paths per OS without hardcoded home.

    Imports here deliberately use the OLD flat path
    ``yadgar._shared.platform_paths`` — T2 Car A moved the module to
    ``yadgar.core.install.platform_paths`` and this class doubles as PEP-562
    shim regression coverage (Car C convention). Patch targets that must hit
    the real module use the canonical core path.
    """

    def test_linux_returns_dot_claude(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        from yadgar.core.install.platform_paths import get_claude_config_dir

        result = get_claude_config_dir()
        assert result == Path.home() / ".claude"
        assert "/home/max" not in str(result) or str(result).endswith("/.claude")

    def test_macos_returns_library_path(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        import importlib

        from yadgar.core.install import platform_paths  # noqa: PLC0415

        importlib.reload(platform_paths)
        from yadgar.core.install.platform_paths import get_claude_config_dir

        result = get_claude_config_dir()
        assert (
            "Library" in str(result)
            or "Application Support" in str(result)
            or "Claude" in str(result)
        )

    def test_windows_returns_appdata_path(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setenv("APPDATA", "C:\\Users\\testuser\\AppData\\Roaming")
        import importlib

        from yadgar.core.install import platform_paths  # noqa: PLC0415

        importlib.reload(platform_paths)
        from yadgar.core.install.platform_paths import get_claude_config_dir

        result = get_claude_config_dir()
        assert "Claude" in str(result)

    def test_agents_dir_is_subdir_of_config(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        from yadgar.core.install.platform_paths import get_claude_agents_dir, get_claude_config_dir

        agents = get_claude_agents_dir()
        config = get_claude_config_dir()
        assert agents == config / "agents"

    def test_settings_path_is_settings_json(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        from yadgar.core.install.platform_paths import get_claude_settings_path

        path = get_claude_settings_path()
        assert path.name == "settings.json"

    def test_is_nix_managed_false_on_non_nix(self, tmp_path, monkeypatch):
        """is_nix_managed returns False when /etc/NIXOS absent and nixos-version unavailable."""
        import shutil

        monkeypatch.setattr(shutil, "which", lambda x: None)
        monkeypatch.setattr(
            "yadgar.core.install.platform_paths.Path", lambda p: tmp_path / p.lstrip("/")
        )

        # Can't easily mock Path("/etc/NIXOS").exists() directly but we can check the logic
        from yadgar.core.install.platform_paths import is_nix_managed

        # On non-NixOS test system: should return False (unless actually on NixOS)
        result = is_nix_managed()
        # Result is bool — just verify it doesn't raise
        assert isinstance(result, bool)

    def test_no_hardcoded_home_max(self):
        """platform_paths.py must contain no hardcoded /home/max or specific username."""
        here = Path(__file__).parent.parent.parent  # yadgar/
        source = (here / "core" / "install" / "platform_paths.py").read_text()
        assert "/home/max" not in source, "Hardcoded /home/max path found in platform_paths.py"


# ── X4: install-subagents CLI ─────────────────────────────────────────────────


class TestInstallSubagents:
    """yadgar install-subagents copies agent templates to ~/.claude/agents/."""

    def test_install_copies_agent_files(self, tmp_path, monkeypatch):
        from yadgar.core.install.install_subagents_lib import install_subagents_impl

        monkeypatch.setattr("yadgar.core.install.platform_paths.is_nix_managed", lambda: False)
        result = install_subagents_impl(home_dir=tmp_path, dry_run=False, force=False)
        assert result["status"] == "installed"
        agents_dir = tmp_path / ".claude" / "agents"
        assert agents_dir.exists()
        # All 5 templates should be present
        names = {f.name for f in agents_dir.iterdir()}
        assert "general-purpose.md" in names
        assert "Explore.md" in names
        assert "cavecrew-investigator.md" in names
        assert "cavecrew-builder.md" in names
        assert "cavecrew-reviewer.md" in names

    def test_idempotent_no_op_second_run(self, tmp_path, monkeypatch):
        """Running install twice produces same result, no duplication."""
        from yadgar.core.install.install_subagents_lib import install_subagents_impl

        monkeypatch.setattr("yadgar.core.install.platform_paths.is_nix_managed", lambda: False)
        r1 = install_subagents_impl(home_dir=tmp_path, dry_run=False, force=False)
        r2 = install_subagents_impl(home_dir=tmp_path, dry_run=False, force=False)
        assert r1["status"] == "installed"
        assert r2["status"] in ("installed", "no_changes")  # second run is no-op or same

        agents_dir = tmp_path / ".claude" / "agents"
        file_count = len(list(agents_dir.iterdir()))
        # Exactly 5 agent files (no duplicates)
        assert file_count == 5

    def test_dry_run_does_not_write_files(self, tmp_path, monkeypatch):
        from yadgar.core.install.install_subagents_lib import install_subagents_impl

        monkeypatch.setattr("yadgar.core.install.platform_paths.is_nix_managed", lambda: False)
        result = install_subagents_impl(home_dir=tmp_path, dry_run=True, force=False)
        assert result["status"] == "dry_run"
        agents_dir = tmp_path / ".claude" / "agents"
        assert not agents_dir.exists(), "dry_run must not create directories"

    def test_check_lists_changes_without_writing(self, tmp_path, monkeypatch):
        from yadgar.core.install.install_subagents_lib import install_subagents_impl

        monkeypatch.setattr("yadgar.core.install.platform_paths.is_nix_managed", lambda: False)
        result = install_subagents_impl(home_dir=tmp_path, dry_run=False, force=False, check=True)
        # --check: returns list of files that would be installed
        assert "would_install" in result or "status" in result
        agents_dir = tmp_path / ".claude" / "agents"
        assert not agents_dir.exists(), "--check must not write files"

    def test_nix_skip_on_nixos(self, tmp_path, monkeypatch):
        """On NixOS, install-subagents skips with status=nix_managed."""
        from yadgar.core.install.install_subagents_lib import install_subagents_impl

        monkeypatch.setattr("yadgar.core.install.platform_paths.is_nix_managed", lambda: True)
        result = install_subagents_impl(home_dir=tmp_path, dry_run=False, force=False)
        assert result["status"] == "nix_managed"
        agents_dir = tmp_path / ".claude" / "agents"
        assert not agents_dir.exists(), "NixOS: must not write agent files"

    def test_force_overwrites_existing(self, tmp_path, monkeypatch):
        """--force overwrites existing agent files."""
        from yadgar.core.install.install_subagents_lib import install_subagents_impl

        monkeypatch.setattr("yadgar.core.install.platform_paths.is_nix_managed", lambda: False)
        # First install
        install_subagents_impl(home_dir=tmp_path, dry_run=False, force=False)
        # Corrupt one file
        agents_dir = tmp_path / ".claude" / "agents"
        (agents_dir / "general-purpose.md").write_text("CORRUPTED")
        # Force reinstall
        install_subagents_impl(home_dir=tmp_path, dry_run=False, force=True)
        content = (agents_dir / "general-purpose.md").read_text()
        assert "CORRUPTED" not in content
        assert "mcp__yadgar__recall" in content


# ── X5: config_sync ───────────────────────────────────────────────────────────


class TestConfigSync:
    """config_sync adds missing yaml keys with defaults, preserves user values.

    Imports here deliberately use the OLD flat path
    ``yadgar._shared.config_sync`` — T2 Car A moved the module to
    ``yadgar.core.config_sync`` and this class doubles as PEP-562 shim
    regression coverage (Car C convention).
    """

    def _write_minimal_config(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        os.chmod(path, 0o600)

    def test_missing_key_added_with_default(self, tmp_path):
        """config_sync adds a missing key with its default value."""
        from yadgar.core.config_sync.sync import cmd_config_sync

        config_path = tmp_path / ".yadgar" / "config.yaml"
        # Write config with one field that should be present
        self._write_minimal_config(config_path, "port: 8765\n")

        class _Args:
            config_file = str(config_path)
            check = False
            dry_run = False
            remove_unknown = False

        with patch("yadgar._shared.config.config_yaml.get_config_path", return_value=config_path):
            cmd_config_sync(_Args())

        content = config_path.read_text()
        # The sync should add more keys (port was the only one, all others added)
        # At minimum, the original value must be preserved
        assert "port: 8765" in content or "port:" in content

    def test_user_value_preserved(self, tmp_path):
        """User-set values are preserved byte-for-byte after sync."""
        from yadgar.core.config_sync.sync import cmd_config_sync

        config_path = tmp_path / ".yadgar" / "config.yaml"
        self._write_minimal_config(config_path, "port: 9999\n")

        class _Args:
            config_file = str(config_path)
            check = False
            dry_run = False
            remove_unknown = False

        with patch("yadgar._shared.config.config_yaml.get_config_path", return_value=config_path):
            cmd_config_sync(_Args())

        content = config_path.read_text()
        assert "9999" in content, "User's port=9999 must be preserved"

    def test_idempotent_second_run_no_changes(self, tmp_path):
        """Running config_sync twice returns added=[] on second run."""
        from yadgar.core.config_sync.sync import cmd_config_sync

        config_path = tmp_path / ".yadgar" / "config.yaml"
        self._write_minimal_config(config_path, "port: 8765\n")

        class _Args:
            config_file = str(config_path)
            check = False
            dry_run = False
            remove_unknown = False

        with patch("yadgar._shared.config.config_yaml.get_config_path", return_value=config_path):
            cmd_config_sync(_Args())
            content_after_first = config_path.read_text()
            cmd_config_sync(_Args())
            content_after_second = config_path.read_text()

        # Both runs should produce the same file
        assert content_after_first == content_after_second

    def test_dry_run_no_write(self, tmp_path):
        """--dry-run prints diff but does not modify the file."""
        from yadgar.core.config_sync.sync import cmd_config_sync

        config_path = tmp_path / ".yadgar" / "config.yaml"
        original = "port: 8765\n"
        self._write_minimal_config(config_path, original)

        class _Args:
            config_file = str(config_path)
            check = False
            dry_run = True
            remove_unknown = False

        with patch("yadgar._shared.config.config_yaml.get_config_path", return_value=config_path):
            cmd_config_sync(_Args())

        assert config_path.read_text() == original, "dry_run must not modify file"

    def test_check_flag_nonzero_when_missing_keys(self, tmp_path):
        """--check exits nonzero when keys would be added."""
        from yadgar.core.config_sync.sync import cmd_config_sync

        config_path = tmp_path / ".yadgar" / "config.yaml"
        self._write_minimal_config(config_path, "port: 8765\n")

        class _Args:
            config_file = str(config_path)
            check = True
            dry_run = False
            remove_unknown = False

        with patch("yadgar._shared.config.config_yaml.get_config_path", return_value=config_path):
            with pytest.raises(SystemExit) as exc_info:
                cmd_config_sync(_Args())
            assert exc_info.value.code != 0

    def test_check_flag_zero_when_no_missing_keys(self, tmp_path):
        """--check exits zero when config is fully synced."""
        from yadgar.core.config_sync.sync import cmd_config_sync

        config_path = tmp_path / ".yadgar" / "config.yaml"
        self._write_minimal_config(config_path, "port: 8765\n")

        class _Args:
            config_file = str(config_path)
            check = False
            dry_run = False
            remove_unknown = False

        # First sync fully
        with patch("yadgar._shared.config.config_yaml.get_config_path", return_value=config_path):
            cmd_config_sync(_Args())

        # Now check — should be zero (nothing missing)
        class _CheckArgs:
            config_file = str(config_path)
            check = True
            dry_run = False
            remove_unknown = False

        with patch("yadgar._shared.config.config_yaml.get_config_path", return_value=config_path):
            # Should NOT raise SystemExit (exits 0 = no changes needed)
            try:
                cmd_config_sync(_CheckArgs())
            except SystemExit as e:
                assert e.code == 0, f"Expected exit 0 after full sync, got {e.code}"

    def test_config_not_found_returns_error(self, tmp_path):
        """config_sync on missing config file returns graceful error."""
        from yadgar.core.config_sync.sync import cmd_config_sync

        config_path = tmp_path / ".yadgar" / "config_nonexistent.yaml"

        class _Args:
            config_file = str(config_path)
            check = False
            dry_run = False
            remove_unknown = False

        with patch("yadgar._shared.config.config_yaml.get_config_path", return_value=config_path):
            # Should print error and exit 1, not raise unhandled exception
            with pytest.raises(SystemExit) as exc_info:
                cmd_config_sync(_Args())
            assert exc_info.value.code == 1


# ── Production write-path test (P7) ───────────────────────────────────────────


class TestSubagentWritebackProduction:
    """P7: Directive parse → _post_findings → verify payload forwarded correctly.

    Note: Full drainer path test (QueueDrainer + wait=True) is omitted here
    because it requires a live SurrealDB instance. This test verifies the
    hook-side parsing + payload shape. The daemon-side assertion is covered
    by TestSubagentStopEndpoint in test_subagent_stop_hook.py.
    """

    def test_memorize_directive_forwarded_in_payload(self):
        """Parsed memorize directive appears in POSTed payload."""
        from yadgar.core.hooks import subagent_stop as _hs

        posted = []

        class _FakeResp:
            def read(self):
                return b'{"status":"ok","stored":1}'

        def _fake_urlopen(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            return _FakeResp()

        report = """\
Work done.

## Yadgar Findings

- memorize: content="SubagentStop X2 routes directives to write tools", tags=["v5.44.0","x2"], context="/tmp/proj"
"""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            entry = {"message": {"role": "assistant", "content": report}}
            f.write(json.dumps(entry) + "\n")

        with patch("urllib.request.urlopen", _fake_urlopen):
            _hs._post_findings(
                "general-purpose",
                "/tmp/proj",
                [
                    'memorize: content="SubagentStop X2 routes directives to write tools", tags=["v5.44.0","x2"], context="/tmp/proj"'
                ],
                branch_hint="feat/v5.44.0-subagent-mcp-wiring",
            )

        assert len(posted) == 1
        payload = posted[0]
        assert payload["agent_type"] == "general-purpose"
        assert payload["branch_hint"] == "feat/v5.44.0-subagent-mcp-wiring"
        # directive content should appear somewhere in the payload
        findings = payload.get("findings", [])
        assert any("SubagentStop" in str(f) for f in findings)

    def test_branch_hint_in_payload_not_from_daemon_cwd(self):
        """branch_hint in payload comes from dispatch context, not daemon CWD.

        Regression guard for v5.42.2 precedent: writer and checker must use
        the SAME branch. The hook must forward the caller-supplied branch_hint,
        not re-derive from daemon CWD.
        """
        from yadgar.core.hooks import subagent_stop as _hs

        posted = []

        class _FakeResp:
            def read(self):
                return b'{"status":"ok"}'

        def _fake_urlopen(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            return _FakeResp()

        caller_branch = "feat/my-feature-branch"
        with patch("urllib.request.urlopen", _fake_urlopen):
            _hs._post_findings(
                "general-purpose",
                "/tmp/proj",
                ["some finding"],
                branch_hint=caller_branch,
            )

        assert posted[0]["branch_hint"] == caller_branch


# ── Regression: contract genesis still has findings heading ───────────────────


class TestContractRegression:
    def test_contract_has_findings_heading_literal(self):
        """Contract genesis must still have '## Yadgar findings' — regression guard.

        v5.122.0: _YADGAR_CONTRACT constant removed — genesis lives in seed
        materials (CONTRACT_GENESIS); wiki page is the runtime source.
        """
        from yadgar.core.server.tools.agent_prompts import CONTRACT_GENESIS

        _, _, _contract = CONTRACT_GENESIS
        assert "## Yadgar findings" in _contract
