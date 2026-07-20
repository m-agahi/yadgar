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
    HookCapability,
    McpAuth,
    McpEntrySchema,
    McpFormat,
    PathSpec,
    RulesBridge,
    StopMechanism,
)
from yadgar.core.install.platform_paths import get_claude_config_dir

# ADR-0143 snapshot date — every hook_capability row below is a primary-source
# verification snapshot as of this date (#59 / ADR-0145, 2026-07-18). Every
# client car re-verifies before its build (the gate is per-car, not retired).
_VERIFIED = "2026-07-18"


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
    # Claude Code is the reference: all 5 hooks + blocking Stop (the checkpoint
    # self-report loop). This is the LIVE, verified surface (not a snapshot claim).
    hook_capability=HookCapability(
        session_start=True,
        user_prompt_submit=True,
        post_tool_use=True,
        pre_compact=True,
        stop=StopMechanism.BLOCK,
        verified_date=_VERIFIED,
    ),
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
    # #59: 10 events, only 3 block. Stop does NOT block AND no transcript hook →
    # checkpoint degrades to opportunistic capture (R5). The other 4 map cleanly.
    hook_capability=HookCapability(
        session_start=True,
        user_prompt_submit=True,
        post_tool_use=True,
        pre_compact=True,
        stop=StopMechanism.NONE,
        verified_date=_VERIFIED,
    ),
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
    # Car B re-verify (2026-07-20, primary source — corrects the ADR-0145
    # 2026-07-18 snapshot's "22 events, 8 blocking, Stop blocks / 5-of-5"):
    #   * Config: `.cursor/hooks.json` (`{"version":1,"hooks":{event:[...]}}`);
    #     sessionStart/preCompact/postToolUse/stop ARE documented events (≥v2.4;
    #     sessionStart was rejected pre-2.4, forum #149566, fixed v2.4).
    #   * INJECT IS BROKEN upstream: `additional_context` on sessionStart /
    #     postToolUse is accepted+merged but never surfaced to the model, and
    #     beforeSubmitPrompt output is not respected at all (open forum bugs,
    #     mid-2026). → session_start + user_prompt_submit are NON-FUNCTIONAL.
    #   * `stop` is observation-only (`followup_message` auto-continues; it does
    #     NOT block) → StopMechanism.NONE (NOT BLOCK).
    #   * postToolUse (capture) + preCompact (drain) are fire-and-POST and DO
    #     work — the only two the Cursor emitter wires.
    hook_capability=HookCapability(
        session_start=False,
        user_prompt_submit=False,
        post_tool_use=True,
        pre_compact=True,
        stop=StopMechanism.NONE,
        verified_date="2026-07-20",
    ),
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
    # #59: 6 hooks + Kanban store. TaskStart≈SessionStart, UserPromptSubmit,
    # PostToolUse present; NO PreCompact (drain rides PostToolUse/TaskCancel);
    # Stop blocking-tbd → conservatively NONE until Car D re-verifies.
    hook_capability=HookCapability(
        session_start=True,
        user_prompt_submit=True,
        post_tool_use=True,
        pre_compact=False,
        stop=StopMechanism.NONE,
        verified_date=_VERIFIED,
    ),
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
    # #59: 12 hooks. NO SessionStart (inject rides pre_user_prompt first-fire);
    # pre_user_prompt≈UserPromptSubmit; post_mcp_tool_use≈PostToolUse; NO
    # PreCompact; Stop via transcript (post_cascade_response_with_transcript).
    hook_capability=HookCapability(
        session_start=False,
        user_prompt_submit=True,
        post_tool_use=True,
        pre_compact=False,
        stop=StopMechanism.TRANSCRIPT,
        verified_date=_VERIFIED,
    ),
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
    # #59: 10 events — HAS SessionStart (ADR-0145 corrects the survey's "no
    # SessionStart"). UserPromptSubmit blocks; PostToolUse present; NO PreCompact;
    # Stop blocking-tbd → NONE until Car E re-verifies empirically.
    hook_capability=HookCapability(
        session_start=True,
        user_prompt_submit=True,
        post_tool_use=True,
        pre_compact=False,
        stop=StopMechanism.NONE,
        verified_date=_VERIFIED,
    ),
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
    # #59: 5 hooks. session.start; NO UserPromptSubmit; tool.result≈PostToolUse;
    # NO PreCompact; agent.end (transcript)≈Stop. Thinnest port.
    hook_capability=HookCapability(
        session_start=True,
        user_prompt_submit=False,
        post_tool_use=True,
        pre_compact=False,
        stop=StopMechanism.TRANSCRIPT,
        verified_date=_VERIFIED,
    ),
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
    # UNVERIFIED — #59 left OpenCode's hook event names + payload schema "TBC".
    # Car A's payload spike resolves this before its emitter is built; these
    # values are provisional (assume-supported, block-TBD) and MUST be
    # re-confirmed in Car A. The opencode_plugin emitter is a Car-0 stub.
    hook_capability=HookCapability(
        session_start=True,
        user_prompt_submit=True,
        post_tool_use=True,
        pre_compact=True,
        # OpenCode cannot block turn-end (no blocking Stop surface) → NONE, not
        # BLOCK. Registry fix folded in by Car B (flagged during the OpenCode
        # survey); Car A re-confirms empirically when it builds the emitter.
        stop=StopMechanism.NONE,
        verified_date=_VERIFIED,
    ),
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
