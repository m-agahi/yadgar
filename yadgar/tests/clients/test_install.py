"""Car 3 — install.py (orchestrator lib) tests.

Contracts under test:

  1. ``install_client`` --mcp-only → calls register_mcp, no write_rules.
  2. ``install_client`` --rules-only → calls write_rules, no register_mcp.
  3. ``install_client`` --all → calls both register_mcp and write_rules.
  4. ``install_client`` --mcp + --print → no writes, returns MCP fragment.
  5. ``install_client`` --rules + --print → no writes, returns rules fragment.
  6. ``install_client`` --all + --print → no writes, returns both fragments.
  7. ``print_fragments`` determinism: same inputs → byte-identical output.
  8. ``print_fragments`` never writes any file (no side effects).
  9. BEARER_LITERAL client → --print emits env-ref, NOT literal token.
  10. install_client raises ValueError for unknown client name.
  11. install_client --auto-detect → installs for all detected clients.
  12. Exit-code: install returns 0 on success.
  13. ``print_fragments`` returns structured dict with path→content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yadgar.core.install.clients.descriptor import (
    CapabilityTier,
    ClientDescriptor,
    McpAuth,
    McpEntrySchema,
    McpFormat,
    PathSpec,
    RulesBridge,
)

_URL = "http://127.0.0.1:8765/mcp"
_TOKEN = "supersecret_token_xyz"
_VERSION = "5.154.0"


def _make_test_descriptor(tmp_path: Path, name: str = "test-client") -> ClientDescriptor:
    """Descriptor whose paths live in tmp_path so tests are isolated."""
    mcp_file = tmp_path / "mcp_config.json"
    rules_file = tmp_path / "AGENTS.md"
    return ClientDescriptor(
        name=name,
        mcp_config_path=PathSpec(global_factory=lambda: mcp_file),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.OPENCODE_REMOTE,
        mcp_auth=McpAuth.BEARER_ENVREF,
        rules_path=PathSpec(global_factory=lambda: rules_file),
        rules_header="## Yadgar",
        rules_is_agents_md=True,
        rules_addendum=[],
        rules_bridge=None,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )


def _make_literal_descriptor(tmp_path: Path) -> ClientDescriptor:
    """Descriptor with BEARER_LITERAL auth (like claude-code)."""
    mcp_file = tmp_path / "claude.json"
    rules_file = tmp_path / "CLAUDE.md"
    return ClientDescriptor(
        name="literal-client",
        mcp_config_path=PathSpec(global_factory=lambda: mcp_file),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.STREAMABLE_HTTP_TYPE,
        mcp_auth=McpAuth.BEARER_LITERAL,
        rules_path=PathSpec(global_factory=lambda: rules_file),
        rules_header="## Memory System — Yadgar",
        rules_is_agents_md=False,
        rules_addendum=[],
        rules_bridge=RulesBridge.IMPORT,
        hooks_kind="claude_json",
        task_mirror=None,
        capability_tier=CapabilityTier.FULL,
    )


# ── 1. --mcp-only ─────────────────────────────────────────────────────────────


def test_install_mcp_only_writes_mcp_not_rules(tmp_path: Path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    desc = _make_test_descriptor(tmp_path)
    registry = {desc.name: desc}
    result = install_client(
        desc.name,
        InstallOptions(url=_URL, token=_TOKEN, version=_VERSION, mcp=True, rules=False),
        registry=registry,
    )
    mcp_file = desc.mcp_config_path.resolve_global()
    rules_file = desc.rules_path.resolve_global()
    assert mcp_file is not None and mcp_file.exists(), "MCP file must be written"
    assert rules_file is not None and not rules_file.exists(), "Rules file must NOT be written"
    assert result["mcp"] is not None
    assert result["rules"] is None


# ── 2. --rules-only ───────────────────────────────────────────────────────────


def test_install_rules_only_writes_rules_not_mcp(tmp_path: Path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    desc = _make_test_descriptor(tmp_path)
    registry = {desc.name: desc}
    result = install_client(
        desc.name,
        InstallOptions(url=_URL, token=_TOKEN, version=_VERSION, mcp=False, rules=True),
        registry=registry,
    )
    mcp_file = desc.mcp_config_path.resolve_global()
    rules_file = desc.rules_path.resolve_global()
    assert mcp_file is not None and not mcp_file.exists(), "MCP file must NOT be written"
    assert rules_file is not None and rules_file.exists(), "Rules file must be written"
    assert result["mcp"] is None
    assert result["rules"] is not None


# ── 3. --all ──────────────────────────────────────────────────────────────────


def test_install_all_writes_both(tmp_path: Path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    desc = _make_test_descriptor(tmp_path)
    registry = {desc.name: desc}
    result = install_client(
        desc.name,
        InstallOptions(url=_URL, token=_TOKEN, version=_VERSION, mcp=True, rules=True),
        registry=registry,
    )
    assert desc.mcp_config_path.resolve_global().exists()
    assert desc.rules_path.resolve_global().exists()
    assert result["mcp"] is not None
    assert result["rules"] is not None


# ── 4. --mcp + --print → no writes ────────────────────────────────────────────


def test_print_mcp_no_writes(tmp_path: Path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    desc = _make_test_descriptor(tmp_path)
    registry = {desc.name: desc}
    result = install_client(
        desc.name,
        InstallOptions(
            url=_URL, token=_TOKEN, version=_VERSION, mcp=True, rules=False, dry_run=True
        ),
        registry=registry,
    )
    mcp_file = desc.mcp_config_path.resolve_global()
    assert mcp_file is not None and not mcp_file.exists(), "No file must be written in dry_run mode"
    assert result["mcp"] is not None
    assert result["rules"] is None


# ── 5. --rules + --print → no writes ─────────────────────────────────────────


def test_print_rules_no_writes(tmp_path: Path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    desc = _make_test_descriptor(tmp_path)
    registry = {desc.name: desc}
    result = install_client(
        desc.name,
        InstallOptions(
            url=_URL, token=_TOKEN, version=_VERSION, mcp=False, rules=True, dry_run=True
        ),
        registry=registry,
    )
    rules_file = desc.rules_path.resolve_global()
    assert rules_file is not None and not rules_file.exists()
    assert result["rules"] is not None
    assert result["mcp"] is None


# ── 6. --all + --print → no writes ────────────────────────────────────────────


def test_print_all_no_writes(tmp_path: Path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    desc = _make_test_descriptor(tmp_path)
    registry = {desc.name: desc}
    result = install_client(
        desc.name,
        InstallOptions(
            url=_URL, token=_TOKEN, version=_VERSION, mcp=True, rules=True, dry_run=True
        ),
        registry=registry,
    )
    assert desc.mcp_config_path.resolve_global() is not None
    assert not desc.mcp_config_path.resolve_global().exists()
    assert not desc.rules_path.resolve_global().exists()
    assert result["mcp"] is not None
    assert result["rules"] is not None


# ── 7. print_fragments determinism ────────────────────────────────────────────


def test_print_fragments_deterministic(tmp_path: Path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    desc = _make_test_descriptor(tmp_path)
    registry = {desc.name: desc}
    opts = InstallOptions(
        url=_URL, token=_TOKEN, version=_VERSION, mcp=True, rules=True, dry_run=True
    )
    r1 = install_client(desc.name, opts, registry=registry)
    r2 = install_client(desc.name, opts, registry=registry)
    assert r1["mcp"] == r2["mcp"], "MCP fragment must be identical across calls"
    assert r1["rules"] == r2["rules"], "Rules fragment must be identical across calls"


# ── 8. print_fragments writes nothing ─────────────────────────────────────────


def test_print_fragments_no_file_side_effects(tmp_path: Path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    desc = _make_test_descriptor(tmp_path)
    registry = {desc.name: desc}
    before = set(tmp_path.rglob("*"))

    install_client(
        desc.name,
        InstallOptions(
            url=_URL, token=_TOKEN, version=_VERSION, mcp=True, rules=True, dry_run=True
        ),
        registry=registry,
    )

    after = set(tmp_path.rglob("*"))
    assert before == after, "No files should be created in dry_run mode"


# ── 9. BEARER_LITERAL → --print emits env-ref, NOT literal ────────────────────


def test_print_literal_client_emits_envref_not_secret(tmp_path: Path):
    """BEARER_LITERAL clients get env-ref in --print output; token must not appear."""
    from yadgar.core.install.clients.install import InstallOptions, install_client

    desc = _make_literal_descriptor(tmp_path)
    registry = {desc.name: desc}
    result = install_client(
        desc.name,
        InstallOptions(
            url=_URL, token=_TOKEN, version=_VERSION, mcp=True, rules=False, dry_run=True
        ),
        registry=registry,
    )
    fragment = result["mcp"]
    assert fragment is not None
    # Token must not leak into the output
    assert _TOKEN not in str(fragment), "Literal token must NOT appear in --print output"
    # The env-ref must be present
    assert "YADGAR_MCP_AUTH_TOKEN" in str(fragment), "Env-ref must appear in --print output"


# ── 10. ValueError for unknown client ────────────────────────────────────────


def test_install_unknown_client_raises():
    from yadgar.core.install.clients.install import InstallOptions, install_client

    with pytest.raises(ValueError, match="Unknown client"):
        install_client(
            "nonexistent-client",
            InstallOptions(url=_URL, token="", version=_VERSION, mcp=True, rules=True),
        )


# ── 11. --auto-detect ────────────────────────────────────────────────────────


def test_auto_detect_installs_detected(tmp_path: Path):
    """install_auto_detect installs only for clients whose dirs exist."""
    from yadgar.core.install.clients import install as install_mod

    mcp_file = tmp_path / "mcp.json"
    rules_file = tmp_path / "AGENTS.md"
    present = ClientDescriptor(
        name="present",
        mcp_config_path=PathSpec(global_factory=lambda: mcp_file),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.OPENCODE_REMOTE,
        mcp_auth=McpAuth.BEARER_ENVREF,
        rules_path=PathSpec(global_factory=lambda: rules_file),
        rules_header="## Yadgar",
        rules_is_agents_md=True,
        rules_addendum=[],
        rules_bridge=None,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )
    # Make the config dir exist (parent of mcp_file = tmp_path, already exists)

    absent = ClientDescriptor(
        name="absent",
        mcp_config_path=PathSpec(global_factory=lambda: tmp_path / "absent" / "cfg.json"),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=McpEntrySchema.OPENCODE_REMOTE,
        mcp_auth=McpAuth.BEARER_ENVREF,
        rules_path=PathSpec(global_factory=lambda: tmp_path / "absent_rules.md"),
        rules_header="## Yadgar",
        rules_is_agents_md=True,
        rules_addendum=[],
        rules_bridge=None,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )
    registry = {"present": present, "absent": absent}
    from yadgar.core.install.clients.install import InstallOptions  # noqa: PLC0415

    results = install_mod.install_auto_detect(
        InstallOptions(url=_URL, token="", version=_VERSION, mcp=True, rules=True),
        registry=registry,
    )
    names = [r["client"] for r in results]
    assert "present" in names
    assert "absent" not in names


# ── 12. install returns 0 on success ────────────────────────────────────────


def test_install_returns_success_shape(tmp_path: Path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    desc = _make_test_descriptor(tmp_path)
    registry = {desc.name: desc}
    result = install_client(
        desc.name,
        InstallOptions(url=_URL, token="", version=_VERSION, mcp=True, rules=True),
        registry=registry,
    )
    assert isinstance(result, dict)
    assert "client" in result
    assert result["client"] == desc.name


# ── 13. print_fragments returns structured dict ───────────────────────────────


def test_print_fragments_structure(tmp_path: Path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    desc = _make_test_descriptor(tmp_path)
    registry = {desc.name: desc}
    result = install_client(
        desc.name,
        InstallOptions(url=_URL, token="", version=_VERSION, mcp=True, rules=True, dry_run=True),
        registry=registry,
    )
    # mcp fragment: should be a dict with at least a "path" and "content" key
    mcp = result["mcp"]
    assert isinstance(mcp, dict)
    assert "path" in mcp
    assert "content" in mcp
    # rules fragment
    rules = result["rules"]
    assert isinstance(rules, dict)
    assert "path" in rules
    assert "content" in rules
