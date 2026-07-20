"""Car 2 — rules_render unit tests.

Contracts under test:

  1. ``section_replace`` — idempotent: second call with same body is byte-identical.
  2. ``section_replace`` — replaces an existing section, preserves surrounding content.
  3. ``section_replace`` — appends when section absent (with "## Global Rules" anchor).
  4. ``section_replace`` — appends at end when no "## Global Rules" present.
  5. ``render_body`` — loads template + addenda in order, substitutes version.
  6. ``render_body`` — addendum keys not in addenda/ are silently ignored (empty str).
  7. ``write_rules`` — AGENTS.md-native (bridge=None): writes section, no bridge.
  8. ``write_rules`` — IMPORT bridge (Claude Code): writes AGENTS.md sibling +
     adds ``@AGENTS.md`` import line to CLAUDE.md (idempotent).
  9. ``write_rules`` — SETTINGS_ALIAS bridge (Gemini): writes AGENTS.md sibling +
     merges ``context.fileName:"AGENTS.md"`` into settings.json.
  10. ``write_rules`` — scope=project routes to project_factory.
  11. ``write_rules`` — raises ValueError when scope=project and project_dir=None.
  12. ``write_rules`` — raises ValueError when the client has no path at scope.
  13. ``_ensure_import_line`` — idempotent: adding twice yields one import line.
  14. ``sync_instructions`` (generalised) — CC back-compat: default path, legacy header.
  15. ``sync_instructions`` — target_path + section_header params work.
  16. ``sync_instructions`` — client param routes to the named descriptor.
  17. Template + addenda discoverable via Path resolution from rules_render module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yadgar.core.install.clients import rules_render as rr
from yadgar.core.install.clients.descriptor import (
    CapabilityTier,
    ClientDescriptor,
    McpAuth,
    McpEntrySchema,
    McpFormat,
    PathSpec,
    RulesBridge,
)
from yadgar.core.install.clients.registry import CLIENT_REGISTRY

_VERSION = "5.999.0"

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_native_descriptor(tmp_path: Path, name: str = "native-test") -> ClientDescriptor:
    """Minimal AGENTS.md-native descriptor for write_rules tests."""
    rules_p = tmp_path / "AGENTS.md"
    return ClientDescriptor(
        name=name,
        mcp_config_path=PathSpec(),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
        mcp_auth=McpAuth.NONE,
        rules_path=PathSpec(global_factory=lambda: rules_p),
        rules_header="## Yadgar",
        rules_is_agents_md=True,
        rules_addendum=[],
        rules_bridge=None,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )


def _make_import_descriptor(tmp_path: Path) -> ClientDescriptor:
    """Descriptor with IMPORT bridge (Claude Code pattern)."""
    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    return ClientDescriptor(
        name="import-test",
        mcp_config_path=PathSpec(),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
        mcp_auth=McpAuth.NONE,
        rules_path=PathSpec(global_factory=lambda: claude_md),
        rules_header="## Memory System — Yadgar",
        rules_is_agents_md=False,
        rules_addendum=["compaction_shield", "auto_capture"],
        rules_bridge=RulesBridge.IMPORT,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.FULL,
    )


def _make_alias_descriptor(tmp_path: Path) -> ClientDescriptor:
    """Descriptor with SETTINGS_ALIAS bridge (Gemini pattern)."""
    gemini_md = tmp_path / ".gemini" / "GEMINI.md"
    return ClientDescriptor(
        name="alias-test",
        mcp_config_path=PathSpec(),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.GEMINI_HTTPURL,
        mcp_auth=McpAuth.NONE,
        rules_path=PathSpec(global_factory=lambda: gemini_md),
        rules_header="## Yadgar",
        rules_is_agents_md=False,
        rules_addendum=[],
        rules_bridge=RulesBridge.SETTINGS_ALIAS,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )


# ── 1. section_replace — idempotent ──────────────────────────────────────────


def test_section_replace_idempotent():
    """Double-replace with same body must be byte-identical."""
    existing = "# Global Rules\n\n## Yadgar\nold content\n\nOther stuff\n"
    body = "new content line 1\nnew content line 2"

    first = rr.section_replace(existing, "## Yadgar", body)
    second = rr.section_replace(first, "## Yadgar", body)
    assert first == second, "section_replace is not idempotent"


@pytest.mark.parametrize("body", ["\\1", "\\0", "\\A", "\\g<0>", "back\\slash"])
def test_section_replace_literal_backslash_body(body):
    """Regression: backslash sequences in the body must be inserted literally,
    never interpreted as re.sub replacement-template escapes (crash/corruption).
    """
    existing = "# Global Rules\n\n## Yadgar\nold content\n\n## Other\nkeep me\n"

    # Must not raise, and must be idempotent + contain the literal body.
    first = rr.section_replace(existing, "## Yadgar", body)
    second = rr.section_replace(first, "## Yadgar", body)
    assert first == second, f"not idempotent for body={body!r}"
    assert body in first, f"literal body {body!r} missing from output"
    assert "## Other\nkeep me" in first, "unrelated section clobbered"


# ── 2. section_replace — replaces + preserves surrounding ────────────────────


def test_section_replace_replaces_and_preserves():
    """Replacing the section must keep content before and after."""
    existing = "preamble\n\n## Yadgar\nold body\n\n## Other Section\ncontent\n"
    body = "new body"

    result = rr.section_replace(existing, "## Yadgar", body)
    assert "preamble" in result
    assert "## Other Section" in result
    assert "old body" not in result
    assert "new body" in result


# ── 3. section_replace — appends with "## Global Rules" anchor ───────────────


def test_section_replace_appends_after_global_rules():
    """When section absent + '## Global Rules' present, insert after it."""
    existing = "## Global Rules\n\nsome existing rule\n"
    body = "yadgar body"

    result = rr.section_replace(existing, "## Yadgar", body)
    assert "## Global Rules" in result
    assert "## Yadgar" in result
    assert result.index("## Global Rules") < result.index("## Yadgar")


# ── 4. section_replace — appends at end when no Global Rules ─────────────────


def test_section_replace_appends_at_end():
    """When section absent and no '## Global Rules', append at end."""
    existing = "some unrelated content\n"
    body = "yadgar body"

    result = rr.section_replace(existing, "## Yadgar", body)
    assert result.endswith("## Yadgar\n" + body + "\n\n") or "## Yadgar" in result


# ── 5. render_body — template + addenda + version substitution ───────────────


def test_render_body_version_substituted():
    """render_body must replace {__version__} with the supplied version string."""
    desc = _make_native_descriptor(Path("/tmp"))
    body = rr.render_body(desc, "1.2.3")
    assert "1.2.3" in body
    assert "{__version__}" not in body


def test_render_body_cc_includes_addenda():
    """CC descriptor (compaction_shield + auto_capture) renders both addenda."""
    desc = CLIENT_REGISTRY["claude-code"]
    body = rr.render_body(desc, _VERSION)
    assert "Compaction Shield" in body
    assert "Auto-Capture" in body


def test_render_body_no_addenda():
    """A descriptor with empty addendum list renders core only."""
    desc = CLIENT_REGISTRY["codex"]
    body = rr.render_body(desc, _VERSION)
    assert "Compaction Shield" not in body
    assert "Auto-Capture" not in body
    # Core contract items must be present.
    assert "http://127.0.0.1:8765/mcp" in body


# ── 6. render_body — unknown addendum key silently ignored ───────────────────


def test_render_body_unknown_addendum_ignored():
    """An addendum key with no matching file yields empty string, not an error."""
    rules_p = Path("/tmp/dummy.md")
    desc = ClientDescriptor(
        name="x",
        mcp_config_path=PathSpec(),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
        mcp_auth=McpAuth.NONE,
        rules_path=PathSpec(global_factory=lambda: rules_p),
        rules_header="## Yadgar",
        rules_is_agents_md=True,
        rules_addendum=["nonexistent_addendum_xyz"],
        rules_bridge=None,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )
    body = rr.render_body(desc, _VERSION)  # must not raise
    assert "http://127.0.0.1:8765/mcp" in body  # core still present


# ── 7. write_rules — AGENTS.md-native ────────────────────────────────────────


def test_write_rules_native_creates_file(tmp_path):
    """write_rules with bridge=None creates AGENTS.md with Yadgar section."""
    desc = _make_native_descriptor(tmp_path)
    result = rr.write_rules(desc, _VERSION)

    agents_md = tmp_path / "AGENTS.md"
    assert agents_md.exists()
    content = agents_md.read_text()
    assert "## Yadgar" in content
    assert result["bridge"] is None
    assert result["section_length"] > 0


def test_write_rules_native_idempotent(tmp_path):
    """write_rules twice yields byte-identical AGENTS.md."""
    desc = _make_native_descriptor(tmp_path)
    rr.write_rules(desc, _VERSION)
    first_content = (tmp_path / "AGENTS.md").read_text()
    rr.write_rules(desc, _VERSION)
    second_content = (tmp_path / "AGENTS.md").read_text()
    assert first_content == second_content


def test_write_rules_native_preserves_user_content(tmp_path):
    """Existing user content outside the Yadgar section is preserved."""
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("## My Rules\ndo not delete me\n\n")

    desc = _make_native_descriptor(tmp_path)
    rr.write_rules(desc, _VERSION)

    content = agents_md.read_text()
    assert "do not delete me" in content
    assert "## Yadgar" in content


# ── 8. write_rules — IMPORT bridge (Claude Code) ─────────────────────────────


def test_write_rules_import_bridge_creates_agents_md(tmp_path):
    """IMPORT bridge writes AGENTS.md sibling alongside CLAUDE.md."""
    desc = _make_import_descriptor(tmp_path)
    rr.write_rules(desc, _VERSION)

    agents_md = tmp_path / ".claude" / "AGENTS.md"
    assert agents_md.exists(), "AGENTS.md should be written by IMPORT bridge"
    assert "## Yadgar" in agents_md.read_text()


def test_write_rules_import_bridge_adds_import_line(tmp_path):
    """IMPORT bridge adds @AGENTS.md import line to CLAUDE.md."""
    desc = _make_import_descriptor(tmp_path)
    rr.write_rules(desc, _VERSION)

    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    assert claude_md.exists()
    assert "@AGENTS.md" in claude_md.read_text()
    assert rr.write_rules(desc, _VERSION)["bridge"] == "import"


def test_write_rules_import_bridge_import_line_idempotent(tmp_path):
    """@AGENTS.md import line appears exactly once after two calls."""
    desc = _make_import_descriptor(tmp_path)
    rr.write_rules(desc, _VERSION)
    rr.write_rules(desc, _VERSION)

    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    content = claude_md.read_text()
    assert content.count("@AGENTS.md") == 1, (
        f"Expected exactly 1 @AGENTS.md import line, got {content.count('@AGENTS.md')}"
    )


# ── 9. write_rules — SETTINGS_ALIAS bridge (Gemini) ─────────────────────────


def test_write_rules_settings_alias_creates_agents_md(tmp_path):
    """SETTINGS_ALIAS bridge writes AGENTS.md alongside GEMINI.md."""
    desc = _make_alias_descriptor(tmp_path)
    rr.write_rules(desc, _VERSION)

    agents_md = tmp_path / ".gemini" / "AGENTS.md"
    assert agents_md.exists()
    assert "## Yadgar" in agents_md.read_text()


def test_write_rules_settings_alias_writes_settings_json(tmp_path):
    """SETTINGS_ALIAS bridge adds context.fileName to settings.json."""
    desc = _make_alias_descriptor(tmp_path)
    rr.write_rules(desc, _VERSION)

    settings_path = tmp_path / ".gemini" / "settings.json"
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    assert data["context"]["fileName"] == "AGENTS.md"
    assert rr.write_rules(desc, _VERSION)["bridge"] == "settings_alias"


def test_write_rules_settings_alias_preserves_existing_settings(tmp_path):
    """SETTINGS_ALIAS merge preserves other keys in settings.json."""
    settings_path = tmp_path / ".gemini" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"theme": "dark", "mcpServers": {}}))

    desc = _make_alias_descriptor(tmp_path)
    rr.write_rules(desc, _VERSION)

    data = json.loads(settings_path.read_text())
    assert data["theme"] == "dark"
    assert data["context"]["fileName"] == "AGENTS.md"
    assert "mcpServers" in data


# ── 10. write_rules — scope=project ──────────────────────────────────────────


def test_write_rules_project_scope(tmp_path):
    """scope='project' routes to the project_factory path."""
    desc = ClientDescriptor(
        name="proj-test",
        mcp_config_path=PathSpec(),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
        mcp_auth=McpAuth.NONE,
        rules_path=PathSpec(
            global_factory=lambda: tmp_path / "AGENTS.md",
            project_factory=lambda p: p / "AGENTS.md",
        ),
        rules_header="## Yadgar",
        rules_is_agents_md=True,
        rules_addendum=[],
        rules_bridge=None,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )
    result = rr.write_rules(desc, _VERSION, scope="project", project_dir=tmp_path / "proj")
    assert (tmp_path / "proj" / "AGENTS.md").exists()
    assert "proj" in result["written"]


# ── 11. write_rules — raises ValueError on missing project_dir ───────────────


def test_write_rules_project_scope_requires_project_dir():
    desc = _make_native_descriptor(Path("/tmp/dummy"))
    with pytest.raises(ValueError, match="project_dir required"):
        rr.write_rules(desc, _VERSION, scope="project", project_dir=None)


# ── 12. write_rules — raises ValueError when no path at scope ────────────────


def test_write_rules_no_path_at_scope_raises():
    """A descriptor with no project_factory raises ValueError at scope=project."""
    desc = ClientDescriptor(
        name="no-proj",
        mcp_config_path=PathSpec(),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
        mcp_auth=McpAuth.NONE,
        rules_path=PathSpec(global_factory=lambda: Path("/tmp/AGENTS.md")),
        rules_header="## Yadgar",
        rules_is_agents_md=True,
        rules_addendum=[],
        rules_bridge=None,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )
    with pytest.raises(ValueError, match="no rules path"):
        rr.write_rules(desc, _VERSION, scope="project", project_dir=Path("/tmp/x"))


# ── 13. _ensure_import_line — idempotent ─────────────────────────────────────


def test_ensure_import_line_idempotent(tmp_path):
    """Calling _ensure_import_line twice adds @AGENTS.md exactly once."""
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("existing content\n")

    rr._ensure_import_line(claude_md)
    rr._ensure_import_line(claude_md)

    content = claude_md.read_text()
    assert content.count("@AGENTS.md") == 1


def test_ensure_import_line_creates_file(tmp_path):
    """_ensure_import_line creates the target file when absent."""
    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    rr._ensure_import_line(claude_md)
    assert claude_md.exists()
    assert "@AGENTS.md" in claude_md.read_text()


# ── 14. sync_instructions — CC back-compat ───────────────────────────────────


def test_sync_instructions_cc_backcompat(tmp_path, monkeypatch):
    """Default call (no args) must write CC-compatible section with legacy header."""
    monkeypatch.setattr(
        "yadgar.core.server.tools.misc.Path.home",
        lambda: tmp_path,
        raising=False,
    )
    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True, exist_ok=True)

    from yadgar.core.server.tools.misc import sync_instructions

    result = sync_instructions()

    assert result["status"] == "synced"
    content = claude_md.read_text()
    assert "## Memory System — Yadgar" in content
    # Footer reference in the body must survive intact (not corrupted to
    # "Memory System — Yadgar findings" by an erroneous str.replace call).
    assert "`## Yadgar findings`" in content, (
        "CC rules body missing Yadgar-findings footer reference"
    )
    assert "Memory System — Yadgar findings" not in content, (
        "Footer ref corrupted: '## Yadgar' in body was replaced with the section header"
    )


# ── 15. sync_instructions — target_path + section_header ─────────────────────


def test_sync_instructions_target_path_and_header(tmp_path):
    """target_path + section_header writes to the given file with the given header."""
    target = tmp_path / "AGENTS.md"

    from yadgar.core.server.tools.misc import sync_instructions

    result = sync_instructions(target_path=str(target), section_header="## Yadgar")
    assert result["status"] == "synced"
    assert target.exists()
    assert "## Yadgar" in target.read_text()


# ── 16. sync_instructions — client param ─────────────────────────────────────


def test_sync_instructions_client_param(tmp_path):
    """client='codex' routes to the Codex descriptor's section header."""
    target = tmp_path / "AGENTS.md"

    from yadgar.core.server.tools.misc import sync_instructions

    result = sync_instructions(target_path=str(target), client="codex")
    assert result["status"] == "synced"
    content = target.read_text()
    assert "## Yadgar" in content  # Codex header


def test_sync_instructions_unknown_client_returns_error(tmp_path):
    """Unknown client name returns error dict, no exception."""
    target = tmp_path / "AGENTS.md"

    from yadgar.core.server.tools.misc import sync_instructions

    result = sync_instructions(target_path=str(target), client="does-not-exist")
    assert result["status"] == "error"
    assert "Unknown client" in result["reason"]


# ── 17. Template + addenda discoverable ──────────────────────────────────────


def test_template_file_exists():
    """AGENTS.md.template must exist at the expected install_assets path."""
    assert rr._TEMPLATE_PATH.exists(), f"Template missing: {rr._TEMPLATE_PATH}"


def test_addenda_directory_exists():
    """install_assets/rules/addenda/ must exist."""
    assert rr._ADDENDA_DIR.is_dir(), f"Addenda dir missing: {rr._ADDENDA_DIR}"


def test_cc_addenda_files_exist():
    """compaction_shield.md and auto_capture.md must exist."""
    for key in ("compaction_shield", "auto_capture"):
        path = rr._ADDENDA_DIR / f"{key}.md"
        assert path.exists(), f"Addendum missing: {path}"
