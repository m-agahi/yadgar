"""``CLIENT_REGISTRY`` — one ``ClientDescriptor`` per supported client.

The single point to add/correct a client. Paths, formats, transports, and auth
come verbatim from the design doc §2.2 (MCP registration) and §3.2 (rules
files). Home resolution goes through ``Path.home()`` / ``platform_paths`` — no
literal absolute home is baked. LOCKED decisions D1-D5 are encoded in the enum
choices (no stdio; Gemini ``settings_alias``; Claude Code ``import``; env-ref
auth preference).

Where the design flags a global path as install/OS-variant (Cursor / Windsurf /
Cline), the descriptor is the single correction point — see inline notes.
"""

from __future__ import annotations

from pathlib import Path

from yadgar.core.install.clients.descriptor import (
    CapabilityTier,
    ClientDescriptor,
    McpAuth,
    McpEntrySchema,
    McpFormat,
    PathSpec,
    RulesBridge,
)
from yadgar.core.install.platform_paths import get_claude_config_dir


def _home() -> Path:
    return Path.home()


# ── PathSpec factories (deferred resolution — no literal home baked) ─────────


def _agents_md_project(project_dir: Path) -> Path:
    return project_dir / "AGENTS.md"


_CLAUDE_CODE = ClientDescriptor(
    name="claude-code",
    mcp_config_path=PathSpec(global_factory=lambda: _home() / ".claude.json"),
    mcp_format=McpFormat.JSON,
    mcp_root_key=("mcpServers",),
    mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
    # CC bakes a literal token today (~/.claude.json); ${...} expansion unverified.
    # Car 1 TODO: probe CC env-ref support and flip to BEARER_ENVREF if it expands.
    mcp_auth=McpAuth.BEARER_LITERAL,
    rules_path=PathSpec(
        global_factory=lambda: get_claude_config_dir() / "CLAUDE.md",
        project_factory=lambda p: p / ".claude" / "CLAUDE.md",
    ),
    rules_header="## Memory System — Yadgar",
    rules_is_agents_md=False,  # reads CLAUDE.md; bridges to AGENTS.md
    rules_addendum=["compaction_shield", "auto_capture"],
    rules_bridge=RulesBridge.IMPORT,  # D4: @AGENTS.md import
    hooks_kind="claude_json",
    task_mirror=None,
    capability_tier=CapabilityTier.FULL,
)

_CODEX = ClientDescriptor(
    name="codex",
    mcp_config_path=PathSpec(global_factory=lambda: _home() / ".codex" / "config.toml"),
    mcp_format=McpFormat.TOML,
    mcp_root_key=("mcp_servers",),
    mcp_entry_schema=McpEntrySchema.CODEX_TOML,
    mcp_auth=McpAuth.BEARER_ENVREF,
    rules_path=PathSpec(
        global_factory=lambda: _home() / ".codex" / "AGENTS.md",
        project_factory=_agents_md_project,
    ),
    rules_header="## Yadgar",
    rules_is_agents_md=True,
    rules_addendum=[],
    rules_bridge=None,  # AGENTS.md-native
    hooks_kind="codex_hooks_json",
    task_mirror=None,
    capability_tier=CapabilityTier.FULL,
)

_GEMINI = ClientDescriptor(
    name="gemini",
    mcp_config_path=PathSpec(global_factory=lambda: _home() / ".gemini" / "settings.json"),
    mcp_format=McpFormat.JSON,
    mcp_root_key=("mcpServers",),
    mcp_entry_schema=McpEntrySchema.GEMINI_HTTPURL,  # httpUrl precedence
    mcp_auth=McpAuth.BEARER_ENVREF,
    rules_path=PathSpec(
        global_factory=lambda: _home() / ".gemini" / "GEMINI.md",
        project_factory=lambda p: p / "GEMINI.md",
    ),
    rules_header="## Yadgar",
    rules_is_agents_md=False,  # GEMINI.md, aliased to AGENTS.md via settings
    rules_addendum=[],  # hook-less: no compaction-shield/auto-capture addenda
    rules_bridge=RulesBridge.SETTINGS_ALIAS,  # D3: context.fileName:"AGENTS.md"
    hooks_kind=None,  # advisory-only hooks, cannot block
    task_mirror=None,
    capability_tier=CapabilityTier.MCP_RULES,
)

_CURSOR = ClientDescriptor(
    name="cursor",
    # Global path is install-variant; ~/.cursor/mcp.json is the documented global.
    mcp_config_path=PathSpec(
        global_factory=lambda: _home() / ".cursor" / "mcp.json",
        project_factory=lambda p: p / ".cursor" / "mcp.json",
    ),
    mcp_format=McpFormat.JSON,
    mcp_root_key=("mcpServers",),
    mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
    mcp_auth=McpAuth.BEARER_ENVREF,
    rules_path=PathSpec(
        project_factory=lambda p: p / ".cursor" / "rules" / "yadgar.mdc",
    ),
    rules_header="## Yadgar",
    rules_is_agents_md=False,  # .mdc (frontmatter) + AGENTS.md belt-and-suspenders
    rules_addendum=[],
    rules_bridge=None,
    hooks_kind="cursor_hooks",
    task_mirror=None,
    capability_tier=CapabilityTier.FULL,
)

_CLINE = ClientDescriptor(
    name="cline",
    # MCP settings live in VS Code globalStorage; exact path is install-variant
    # (verify per-OS before shipping — the descriptor is the correction point).
    mcp_config_path=PathSpec(
        global_factory=lambda: (
            get_claude_config_dir().parent
            / "Code"
            / "User"
            / "globalStorage"
            / "saoudrizwan.claude-dev"
            / "settings"
            / "cline_mcp_settings.json"
        ),
    ),
    mcp_format=McpFormat.JSON,
    mcp_root_key=("mcpServers",),
    mcp_entry_schema=McpEntrySchema.CLINE_STREAMABLEHTTP,  # explicit streamableHttp
    mcp_auth=McpAuth.BEARER_ENVREF,
    rules_path=PathSpec(
        global_factory=lambda: _home() / ".agents" / "AGENTS.md",
        project_factory=lambda p: p / ".clinerules" / "yadgar.md",
    ),
    rules_header="## Yadgar",
    rules_is_agents_md=False,  # .clinerules/ + AGENTS.md
    rules_addendum=[],
    rules_bridge=None,
    hooks_kind="cline_hooks",
    task_mirror="cline_kanban",
    capability_tier=CapabilityTier.FULL,
)

_WINDSURF = ClientDescriptor(
    name="windsurf",
    mcp_config_path=PathSpec(
        global_factory=lambda: _home() / ".codeium" / "windsurf" / "mcp_config.json",
    ),
    mcp_format=McpFormat.JSON,
    mcp_root_key=("mcpServers",),
    mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
    mcp_auth=McpAuth.BEARER_ENVREF,
    rules_path=PathSpec(
        project_factory=lambda p: p / ".windsurf" / "rules" / "yadgar.md",
    ),
    rules_header="## Yadgar",
    rules_is_agents_md=False,  # .windsurf/rules/*.md + AGENTS.md
    rules_addendum=[],
    rules_bridge=None,
    hooks_kind="windsurf_hooks",
    task_mirror=None,
    capability_tier=CapabilityTier.FULL,
)

_KIRO = ClientDescriptor(
    name="kiro",
    mcp_config_path=PathSpec(
        global_factory=lambda: _home() / ".kiro" / "settings" / "mcp.json",
        project_factory=lambda p: p / ".kiro" / "settings" / "mcp.json",
    ),
    mcp_format=McpFormat.JSON,
    mcp_root_key=("mcpServers",),
    mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
    mcp_auth=McpAuth.BEARER_ENVREF,
    rules_path=PathSpec(
        project_factory=lambda p: p / ".kiro" / "steering" / "yadgar.md",
    ),
    rules_header="## Yadgar",
    rules_is_agents_md=False,  # .kiro/steering/*.md
    rules_addendum=[],
    rules_bridge=None,
    hooks_kind="kiro_hooks_json",
    task_mirror="kiro_specs",
    capability_tier=CapabilityTier.FULL,
)

_AMP = ClientDescriptor(
    name="amp",
    mcp_config_path=PathSpec(
        global_factory=lambda: _home() / ".config" / "amp" / "settings.json",
    ),
    mcp_format=McpFormat.JSON,
    mcp_root_key=("amp", "mcpServers"),  # nested under amp.
    mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
    mcp_auth=McpAuth.BEARER_ENVREF,
    rules_path=PathSpec(
        global_factory=lambda: _home() / ".config" / "amp" / "AGENTS.md",
        project_factory=_agents_md_project,
    ),
    rules_header="## Yadgar",
    rules_is_agents_md=True,  # AGENTS.md native (→AGENT.md→CLAUDE.md fallback)
    rules_addendum=[],
    rules_bridge=None,
    hooks_kind="amp_hooks",
    task_mirror=None,
    capability_tier=CapabilityTier.FULL,
)

_OPENCODE = ClientDescriptor(
    name="opencode",
    mcp_config_path=PathSpec(
        global_factory=lambda: _home() / ".config" / "opencode" / "opencode.json",
    ),
    mcp_format=McpFormat.JSON,
    mcp_root_key=("mcp",),
    mcp_entry_schema=McpEntrySchema.OPENCODE_REMOTE,  # type:"remote"
    mcp_auth=McpAuth.BEARER_ENVREF,
    rules_path=PathSpec(
        global_factory=lambda: _home() / ".config" / "opencode" / "AGENTS.md",
        project_factory=_agents_md_project,
    ),
    rules_header="## Yadgar",
    rules_is_agents_md=True,
    rules_addendum=[],
    rules_bridge=None,
    hooks_kind="opencode_plugin",
    task_mirror=None,
    capability_tier=CapabilityTier.FULL,
)


CLIENT_REGISTRY: dict[str, ClientDescriptor] = {
    d.name: d
    for d in (
        _CLAUDE_CODE,
        _CODEX,
        _GEMINI,
        _CURSOR,
        _CLINE,
        _WINDSURF,
        _KIRO,
        _AMP,
        _OPENCODE,
    )
}
