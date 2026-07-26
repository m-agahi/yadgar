"""Car A (wiring half) — OpenCode hooks installed via the unified ``yadgar install``.

Verifies the orchestrator path (``install_client`` in
``yadgar/core/install/clients/install.py``) routes to the new
``_emit_opencode_plugin`` emitter when a user runs:

    yadgar install --client opencode [--hooks | --no-hooks]
    yadgar install --client opencode --print   # dry-run fragment

Plus the ``--no-hooks`` opt-out, the Gemini no-op gate, and the
auto-detect path.

The emitter itself is covered in ``test_hooks_render_opencode.py``;
this file covers the orchestrator glue (InstallOptions, the
``--hooks`` / ``--no-hooks`` flags, the dry-run fragment shape, the
auto-detect default).

Pattern follows ``test_install.py`` (use a real ClientDescriptor, real
``register_hooks`` with ``home_dir=tmp_path``) so we don't fight the
lazy-import dance inside ``install.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from yadgar.core.install.clients.descriptor import (
    CapabilityTier,
    ClientDescriptor,
    McpAuth,
    McpEntrySchema,
    McpFormat,
    PathSpec,
)
from yadgar.core.install.clients.install import (
    InstallOptions,
    install_client,
)


def _opencode_descriptor(tmp_path: Path) -> ClientDescriptor:
    """OpenCode-shaped descriptor whose MCP/rules paths live under tmp_path.

    Hooks resolve their own paths from ``home_dir`` (no descriptor field
    drives the plugin path), so we don't need a custom path for that —
    the test passes ``home_dir=tmp_path`` and the emitter writes
    ``tmp_path/.config/opencode/plugins/yadgar-hooks.ts``.
    """
    return ClientDescriptor(
        name="opencode",
        mcp_config_path=PathSpec(global_factory=lambda: tmp_path / "opencode.json"),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcp",),
        mcp_entry_schema=McpEntrySchema.OPENCODE_REMOTE,
        mcp_auth=McpAuth.BEARER_ENVREF,
        rules_path=PathSpec(global_factory=lambda: tmp_path / "AGENTS.md"),
        rules_header="## Yadgar",
        rules_is_agents_md=True,
        rules_addendum=[],
        rules_bridge=None,
        hooks_kind="opencode_plugin",
        task_mirror=None,
        capability_tier=CapabilityTier.FULL,
    )


def _gemini_descriptor(tmp_path: Path) -> ClientDescriptor:
    """Gemini-shaped descriptor (hooks_kind=None — advisory-only)."""
    return ClientDescriptor(
        name="gemini",
        mcp_config_path=PathSpec(global_factory=lambda: tmp_path / "gemini.json"),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.OPENCODE_REMOTE,
        mcp_auth=McpAuth.NONE,
        rules_path=PathSpec(global_factory=lambda: tmp_path / "GEMINI.md"),
        rules_header="## Yadgar",
        rules_is_agents_md=True,
        rules_addendum=[],
        rules_bridge=None,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )


def test_install_client_opencode_writes_plugin_file_under_tmp_path(tmp_path):
    """Real ``register_hooks`` runs against ``home_dir=tmp_path`` and writes the plugin."""
    desc = _opencode_descriptor(tmp_path)
    result = install_client(
        desc.name,
        opts=InstallOptions(
            mcp=False,  # only hooks for this test
            rules=False,
            hooks=True,
            home_dir=tmp_path,
            dry_run=False,
        ),
        registry={desc.name: desc},
    )

    assert result["client"] == "opencode"
    assert result["hooks"] is not None
    expected_path = tmp_path / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts"
    assert result["hooks"]["path"] == str(expected_path)
    assert expected_path.exists()
    # The plugin template is the canonical one from the emitter.
    body = expected_path.read_text()
    assert "// @yadgar-managed" in body
    assert "experimental.session.compacting" in body
    assert "tool.execute.after" in body


def test_install_client_opencode_no_hooks_skips_hooks_surface(tmp_path):
    """``--no-hooks`` opts out — the hooks key in the result is None and no file is written."""
    desc = _opencode_descriptor(tmp_path)
    result = install_client(
        desc.name,
        opts=InstallOptions(hooks=False, mcp=False, rules=False, home_dir=tmp_path),
        registry={desc.name: desc},
    )
    assert result["hooks"] is None
    # No plugin file written.
    assert not (tmp_path / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts").exists()


def test_install_client_gemini_hooks_is_noop_regardless_of_flag(tmp_path):
    """Gemini (hooks_kind=None) is always a no-op for hooks, even when opts.hooks=True."""
    desc = _gemini_descriptor(tmp_path)
    result = install_client(
        desc.name,
        opts=InstallOptions(hooks=True, mcp=False, rules=False, home_dir=tmp_path),
        registry={desc.name: desc},
    )
    # Gemini has no hook surface to wire — the result's hooks key is None.
    assert result["hooks"] is None


def test_install_client_opencode_dry_run_hooks_fragment(tmp_path):
    """``--print`` mode renders the hooks fragment under the standard {path, content} shape."""
    desc = _opencode_descriptor(tmp_path)
    result = install_client(
        desc.name,
        opts=InstallOptions(mcp=False, rules=False, hooks=True, home_dir=tmp_path, dry_run=True),
        registry={desc.name: desc},
    )

    assert result["dry_run"] is True
    hooks = result["hooks"]
    assert hooks is not None
    assert hooks["path"] == str(tmp_path / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts")
    # Content is the JSON-serialized emitter payload (machine-readable for nix).
    parsed = json.loads(hooks["content"])
    assert parsed["written"] is False
    # The 4 functional events are listed in the fragment preview.
    for event in ("session-start", "post-tool-capture", "pre-compact-drain", "stop"):
        assert event in parsed["events"]


def test_install_client_opencode_dry_run_writes_no_files(tmp_path):
    """Dry-run must not write the plugin file or the package.json."""
    desc = _opencode_descriptor(tmp_path)
    install_client(
        desc.name,
        opts=InstallOptions(hooks=True, mcp=False, rules=False, home_dir=tmp_path, dry_run=True),
        registry={desc.name: desc},
    )
    assert not (tmp_path / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts").exists()
    assert not (tmp_path / ".config" / "opencode" / "package.json").exists()


def test_install_client_opencode_scope_project(tmp_path):
    """Project scope writes the plugin under project_dir, not home_dir."""
    desc = _opencode_descriptor(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    result = install_client(
        desc.name,
        opts=InstallOptions(
            hooks=True,
            mcp=False,
            rules=False,
            home_dir=tmp_path,
            scope="project",
            project_dir=project,
        ),
        registry={desc.name: desc},
    )
    expected_path = project / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts"
    assert result["hooks"]["path"] == str(expected_path)
    assert expected_path.exists()
    # home_dir-scoped path is NOT touched.
    assert not (tmp_path / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts").exists()


def test_install_options_hooks_default_is_true():
    """InstallOptions.hooks defaults to True — matches the CLI default-on contract."""
    opts = InstallOptions()
    assert opts.hooks is True
    assert opts.mcp is True
    assert opts.rules is True
    assert opts.home_dir is None


def test_install_client_opencode_hooks_with_no_mcp_no_rules(tmp_path):
    """A user can wire ONLY hooks (--hooks without --mcp --rules)."""
    desc = _opencode_descriptor(tmp_path)
    result = install_client(
        desc.name,
        opts=InstallOptions(hooks=True, mcp=False, rules=False, home_dir=tmp_path, dry_run=True),
        registry={desc.name: desc},
    )
    assert result["hooks"] is not None
    assert result["mcp"] is None
    assert result["rules"] is None


def test_install_client_opencode_hooks_idempotent_re_run(tmp_path):
    """Re-running the install produces byte-identical output (replace-in-place)."""
    desc = _opencode_descriptor(tmp_path)
    install_client(
        desc.name,
        opts=InstallOptions(hooks=True, mcp=False, rules=False, home_dir=tmp_path),
        registry={desc.name: desc},
    )
    path = tmp_path / ".config" / "opencode" / "plugins" / "yadgar-hooks.ts"
    first = path.read_text()
    install_client(
        desc.name,
        opts=InstallOptions(hooks=True, mcp=False, rules=False, home_dir=tmp_path),
        registry={desc.name: desc},
    )
    assert path.read_text() == first


def test_install_client_opencode_preserves_existing_package_json(tmp_path):
    """A pre-existing package.json with other deps is not clobbered."""
    pkg_dir = tmp_path / ".config" / "opencode"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(
        json.dumps({"name": "user-opencode", "dependencies": {"user-dep": "^1.0.0"}})
    )
    desc = _opencode_descriptor(tmp_path)
    install_client(
        desc.name,
        opts=InstallOptions(hooks=True, mcp=False, rules=False, home_dir=tmp_path),
        registry={desc.name: desc},
    )
    pkg = json.loads((pkg_dir / "package.json").read_text())
    assert pkg["dependencies"]["user-dep"] == "^1.0.0"
    assert pkg["dependencies"]["execa"] == "^9.0.0"
    assert pkg["name"] == "user-opencode"
