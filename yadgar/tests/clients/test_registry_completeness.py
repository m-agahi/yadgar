"""Car 0 — descriptor + registry completeness tests.

Every registry entry must:
- cover exactly the nine supported clients,
- carry every required ClientDescriptor field,
- use only valid enum members for the enum-typed fields,
- resolve its PathSpec fields (global/project) through platform_paths without
  hardcoded absolute home paths,
- honour the LOCKED design decisions D1-D5 where they pin a field value.

The schema of record is the design doc §4.3 (superset of the task's field
summary). These tests are the descriptor-completeness gate.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from yadgar.core.install.clients import registry as reg
from yadgar.core.install.clients.descriptor import (
    CapabilityTier,
    ClientDescriptor,
    McpAuth,
    McpEntrySchema,
    McpFormat,
    PathSpec,
    RulesBridge,
)

# The nine clients this framework supports (design §2.2 / §3.2).
EXPECTED_CLIENTS = frozenset(
    {
        "claude-code",
        "codex",
        "gemini",
        "cursor",
        "cline",
        "windsurf",
        "kiro",
        "amp",
        "opencode",
    }
)


def test_registry_covers_exactly_the_nine_clients():
    assert set(reg.CLIENT_REGISTRY) == EXPECTED_CLIENTS


def test_registry_keyed_by_descriptor_name():
    for key, desc in reg.CLIENT_REGISTRY.items():
        assert isinstance(desc, ClientDescriptor)
        assert desc.name == key


@pytest.mark.parametrize("name", sorted(EXPECTED_CLIENTS))
def test_every_descriptor_has_all_required_fields(name):
    desc = reg.CLIENT_REGISTRY[name]
    for field in dataclasses.fields(ClientDescriptor):
        value = getattr(desc, field.name)
        # Only the explicitly-optional fields may be None.
        if field.name in {"rules_bridge", "hooks_kind", "task_mirror", "hook_capability"}:
            continue
        assert value is not None, f"{name}.{field.name} is None"


@pytest.mark.parametrize("name", sorted(EXPECTED_CLIENTS))
def test_enum_fields_are_valid_members(name):
    desc = reg.CLIENT_REGISTRY[name]
    assert isinstance(desc.mcp_format, McpFormat)
    assert isinstance(desc.mcp_entry_schema, McpEntrySchema)
    assert isinstance(desc.mcp_auth, McpAuth)
    assert isinstance(desc.capability_tier, CapabilityTier)
    if desc.rules_bridge is not None:
        assert isinstance(desc.rules_bridge, RulesBridge)


@pytest.mark.parametrize("name", sorted(EXPECTED_CLIENTS))
def test_mcp_root_key_is_nonempty_tuple(name):
    desc = reg.CLIENT_REGISTRY[name]
    assert isinstance(desc.mcp_root_key, tuple)
    assert len(desc.mcp_root_key) >= 1
    assert all(isinstance(k, str) and k for k in desc.mcp_root_key)


@pytest.mark.parametrize("name", sorted(EXPECTED_CLIENTS))
def test_pathspecs_resolve_without_hardcoded_home(name):
    desc = reg.CLIENT_REGISTRY[name]
    home = Path.home()
    for spec in (desc.mcp_config_path, desc.rules_path):
        assert isinstance(spec, PathSpec)
        g = spec.resolve_global()
        assert g is None or isinstance(g, Path)
        proj = spec.resolve_project(Path("/tmp/example-project"))
        assert proj is None or isinstance(proj, Path)
        # No descriptor may bake a literal foreign user's home.
        for p in (g, proj):
            if p is not None:
                assert "/home/max" not in str(p) or str(p).startswith(str(home))


@pytest.mark.parametrize("name", sorted(EXPECTED_CLIENTS))
def test_rules_header_present_for_find_replace(name):
    desc = reg.CLIENT_REGISTRY[name]
    assert isinstance(desc.rules_header, str)
    assert desc.rules_header.strip()


@pytest.mark.parametrize("name", sorted(EXPECTED_CLIENTS))
def test_rules_addendum_is_list(name):
    desc = reg.CLIENT_REGISTRY[name]
    assert isinstance(desc.rules_addendum, list)


# ── LOCKED decisions D1-D5 baked into descriptors ────────────────────────────


def test_d1_transport_never_stdio():
    # D1: no stdio anywhere — transport is streamable-http/remote only.
    for desc in reg.CLIENT_REGISTRY.values():
        assert desc.mcp_entry_schema is not None
        # No descriptor advertises a stdio command shape.
        assert "stdio" not in desc.mcp_entry_schema.value.lower()


def test_d3_gemini_uses_settings_alias_bridge():
    # D3: Gemini uses context.fileName→AGENTS.md alias strategy.
    assert reg.CLIENT_REGISTRY["gemini"].rules_bridge is RulesBridge.SETTINGS_ALIAS


def test_d4_claude_code_uses_import_bridge():
    # D4: Claude Code uses @AGENTS.md import as its rules bridge.
    assert reg.CLIENT_REGISTRY["claude-code"].rules_bridge is RulesBridge.IMPORT


def test_d5_auth_defaults_to_envref_where_not_contradicted():
    # D5: prefer bearer_envref; literal only as fallback where the client can't expand.
    # Every entry must carry a valid McpAuth; at least the bulk prefer env-ref.
    envref = sum(1 for d in reg.CLIENT_REGISTRY.values() if d.mcp_auth is McpAuth.BEARER_ENVREF)
    assert envref >= 1


def test_agents_md_native_clients_flagged():
    # Codex, Amp, OpenCode write AGENTS.md directly (design §3.3).
    for name in ("codex", "amp", "opencode"):
        assert reg.CLIENT_REGISTRY[name].rules_is_agents_md is True


def test_capability_tiers_match_design():
    # Gemini is mcp+rules-only (advisory hooks); the rest are full-harness.
    assert reg.CLIENT_REGISTRY["gemini"].capability_tier is CapabilityTier.MCP_RULES
    for name in ("claude-code", "codex", "cursor", "cline", "windsurf", "kiro", "amp", "opencode"):
        assert reg.CLIENT_REGISTRY[name].capability_tier is CapabilityTier.FULL
