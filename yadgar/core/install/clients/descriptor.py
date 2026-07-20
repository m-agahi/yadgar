"""``ClientDescriptor`` schema — the single point to add a new client.

Adding a client = one entry in ``registry.py``; no generator code changes. The
schema is the design doc §4.3 (authoritative superset of the task's summary):
it carries the MCP-registration surface, the rules surface, and the hook/task
surface (the latter merely CARRIED here — the hook layer is a later train).

Enum-typed fields are real ``Enum`` members so an invalid descriptor cannot be
constructed; the completeness test asserts enum-validity rather than magic
strings. ``PathSpec`` defers home/config-dir resolution to callables so no
descriptor bakes a literal absolute home path (cross-platform via
``platform_paths``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from yadgar._shared.observability.observe import observe


class McpFormat(StrEnum):
    """Serialization format of a client's MCP-registration config file."""

    JSON = "json"
    TOML = "toml"


class McpEntrySchema(StrEnum):
    """The shape of the per-client MCP server entry (5 shapes cover all 9 clients).

    The concrete serializers live in Car 1 (``mcp_register``); Car 0 only carries
    the tag so a descriptor selects its shape. D1 (no stdio) holds: none of these
    is a stdio/command shape — all are streamable-http/remote URL shapes.
    """

    STREAMABLE_HTTP_TYPE = "streamable_http_type"  # Claude Code / Cursor: type:"streamable-http"
    OPENCODE_REMOTE = "opencode_remote"  # OpenCode: type:"remote"
    GEMINI_HTTPURL = "gemini_httpurl"  # Gemini: httpUrl (precedence httpUrl>url>command)
    CLINE_STREAMABLEHTTP = "cline_streamablehttp"  # Cline: explicit type:"streamableHttp"
    CODEX_TOML = "codex_toml"  # Codex: [mcp_servers.yadgar] TOML table


class McpAuth(StrEnum):
    """How the bearer token is written into the client's config (D5).

    Prefer ``BEARER_ENVREF`` (``${YADGAR_MCP_AUTH_TOKEN}`` — one env var, no
    on-disk secret) where the client expands ``${...}``; ``BEARER_LITERAL`` is
    the fallback for clients that do not. The precise per-client expansion
    support is enumerated in Car 1; Car 0 carries the preference.
    """

    BEARER_ENVREF = "bearer_envref"
    BEARER_LITERAL = "bearer_literal"
    OAUTH = "oauth"
    NONE = "none"


class RulesBridge(StrEnum):
    """How a non-AGENTS.md-native client reaches the canonical rules body."""

    IMPORT = "import"  # Claude Code: @AGENTS.md import inside CLAUDE.md (D4)
    SYMLINK = "symlink"  # ln -s AGENTS.md <native>
    SETTINGS_ALIAS = "settings_alias"  # Gemini: context.fileName:"AGENTS.md" (D3)


class CapabilityTier(StrEnum):
    """Which install surfaces a client supports (design §7)."""

    FULL = "full"  # MCP + rules + hooks (+ task-mirror where present)
    MCP_RULES = "mcp_rules"  # MCP + rules only; no blocking hooks


class StopMechanism(StrEnum):
    """How a client can service the Stop checkpoint hook (plan §1/§2).

    Only Stop needs a *blocking* hook. A client that can neither block nor hand
    the daemon a transcript degrades Stop to opportunistic capture (4/5 hooks
    still work). Encoded structurally so "genuinely can't" is data, not a fake.
    """

    BLOCK = "block"  # inject the checkpoint prompt, model self-reports (CC's mechanism)
    TRANSCRIPT = "transcript"  # deliver a transcript the daemon derives from (Windsurf/Amp)
    NONE = "none"  # neither → opportunistic checkpoint only (Codex)


@dataclasses.dataclass(frozen=True)
class HookCapability:
    """Per-client hook-surface support matrix (plan §2, ADR-0143 snapshot).

    Enumerates which of the 5 core hook events a client supports plus the Stop
    mechanism, so the emitter emits ONLY the supported subset — the structural
    encoding of "this client genuinely can't support hook X" (never faked).

    The five booleans map to the 5 CC hooks (the ``session_start`` event is the
    ``session-start-context`` + ``post-compact-rehydrate`` pair; both ride the
    client's session-start surface). ``verified_date`` stamps the primary-source
    verification per ADR-0143 (fast-moving tools; re-verify before each build).
    """

    session_start: bool
    user_prompt_submit: bool
    post_tool_use: bool
    pre_compact: bool
    stop: StopMechanism
    verified_date: str  # ISO date of last primary-source verification (ADR-0143)


@dataclasses.dataclass(frozen=True)
class PathSpec:
    """Per-client config-file location, split into global + project variants.

    Each variant is a zero-arg / project-arg callable so home and OS config-dir
    resolution is deferred to call time (routed through ``platform_paths``); a
    ``None`` callable means the client has no file at that scope.
    """

    global_factory: Callable[[], Path] | None = None
    project_factory: Callable[[Path], Path] | None = None

    @observe(tier="hot")
    def resolve_global(self) -> Path | None:
        """Resolve the global-scope path, or None when the client has none."""
        return self.global_factory() if self.global_factory is not None else None

    @observe(tier="hot")
    def resolve_project(self, project_dir: Path) -> Path | None:
        """Resolve the project-scope path, or None when the client has none."""
        return self.project_factory(project_dir) if self.project_factory is not None else None


@dataclasses.dataclass(frozen=True)
class ClientDescriptor:
    """The complete per-client integration descriptor (design §4.3).

    Fields group into three surfaces: MCP-registration, rules, and hooks. The
    hook/task fields (``hooks_kind``, ``task_mirror``) are carried for the later
    hook-layer train and may be ``None`` for clients with no hook surface.
    """

    name: str

    # --- MCP registration ---
    mcp_config_path: PathSpec
    mcp_format: McpFormat
    mcp_root_key: tuple[str, ...]
    mcp_entry_schema: McpEntrySchema
    mcp_auth: McpAuth

    # --- rules ---
    rules_path: PathSpec
    rules_header: str
    rules_is_agents_md: bool
    rules_addendum: list[str]
    rules_bridge: RulesBridge | None

    # --- hooks (the hook-emitter layer — Car 0 makes hooks_kind live) ---
    hooks_kind: str | None
    task_mirror: str | None
    capability_tier: CapabilityTier
    # Per-client hook-event support matrix (plan §2). ``None`` only for clients
    # with no hook surface (``hooks_kind is None``, e.g. Gemini advisory-only).
    hook_capability: HookCapability | None = None
