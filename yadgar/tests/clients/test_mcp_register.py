"""Car 1 — MCP-registration generator tests.

Contracts under test:
  - Per-format serialization correctness (one test per schema variant).
  - Merge preserves foreign servers (JSON) and comments (TOML).
  - Idempotent re-register (byte-identical file on second call).
  - Nested root-key (Amp: ``amp.mcpServers``).
  - Env-ref emission (BEARER_ENVREF → ``${YADGAR_MCP_AUTH_TOKEN}``).
  - Literal auth emission (BEARER_LITERAL → ``Bearer <token>``).
  - NONE auth → no ``headers`` key.
  - configure_mcp delegate round-trip (the daemon method still works and
    returns the expected ``{updated, old, new}`` shape).
  - register_mcp scope=project routing.
  - register_mcp raises ValueError on missing project_dir / no path at scope.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomlkit

from yadgar.core.install.clients import mcp_register as mr
from yadgar.core.install.clients.descriptor import (
    CapabilityTier,
    ClientDescriptor,
    McpAuth,
    McpEntrySchema,
    McpFormat,
    PathSpec,
)
from yadgar.core.install.clients.registry import CLIENT_REGISTRY

# ── Helpers ──────────────────────────────────────────────────────────────────

_URL = "http://127.0.0.1:8765/mcp"
_TOKEN = "test_token_abc123"
_ENVREF = "${YADGAR_MCP_AUTH_TOKEN}"


def _make_descriptor(
    name: str,
    schema: McpEntrySchema,
    fmt: McpFormat,
    root_key: tuple[str, ...],
    auth: McpAuth,
    global_path_factory=None,
    project_path_factory=None,
) -> ClientDescriptor:
    """Minimal descriptor factory for serializer tests."""
    return ClientDescriptor(
        name=name,
        mcp_config_path=PathSpec(
            global_factory=global_path_factory,
            project_factory=project_path_factory,
        ),
        mcp_format=fmt,
        mcp_root_key=root_key,
        mcp_entry_schema=schema,
        mcp_auth=auth,
        rules_path=PathSpec(),
        rules_header="## Yadgar",
        rules_is_agents_md=False,
        rules_addendum=[],
        rules_bridge=None,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )


# ── 1. Per-schema serialization correctness ──────────────────────────────────


def test_streamable_http_type_entry():
    """STREAMABLE_HTTP_TYPE → ``type:"streamable-http"`` + ``url``."""
    entry = mr.build_entry(CLIENT_REGISTRY["claude-code"], url=_URL, token=_TOKEN)
    assert entry["type"] == "streamable-http"
    assert entry["url"] == _URL
    # CC is BEARER_LITERAL — token is baked in.
    assert entry["headers"]["Authorization"] == f"Bearer {_TOKEN}"


def test_opencode_remote_entry():
    """OPENCODE_REMOTE → ``type:"remote"``."""
    entry = mr.build_entry(CLIENT_REGISTRY["opencode"], url=_URL)
    assert entry["type"] == "remote"
    assert entry["url"] == _URL
    # opencode uses BEARER_ENVREF → env-ref regardless of empty token.
    assert entry["headers"]["Authorization"] == f"Bearer {_ENVREF}"


def test_gemini_httpurl_entry():
    """GEMINI_HTTPURL → ``httpUrl`` key (highest precedence)."""
    entry = mr.build_entry(CLIENT_REGISTRY["gemini"], url=_URL)
    assert "httpUrl" in entry
    assert entry["httpUrl"] == _URL
    assert "url" not in entry  # Gemini uses httpUrl, not url
    # Gemini uses BEARER_ENVREF.
    assert entry["headers"]["Authorization"] == f"Bearer {_ENVREF}"


def test_cline_streamablehttp_entry():
    """CLINE_STREAMABLEHTTP → explicit ``streamableHttp`` type (camelCase)."""
    entry = mr.build_entry(CLIENT_REGISTRY["cline"], url=_URL)
    assert entry["type"] == "streamableHttp"
    assert entry["url"] == _URL
    assert entry["headers"]["Authorization"] == f"Bearer {_ENVREF}"


def test_codex_toml_entry():
    """CODEX_TOML → ``url`` key (TOML table; no ``type`` field)."""
    entry = mr.build_entry(CLIENT_REGISTRY["codex"], url=_URL)
    assert entry["url"] == _URL
    # Codex uses BEARER_ENVREF.
    assert entry["headers"]["Authorization"] == f"Bearer {_ENVREF}"


# ── 2. Auth emission ──────────────────────────────────────────────────────────


def test_bearer_envref_emits_env_ref_regardless_of_token():
    """BEARER_ENVREF always writes ``${YADGAR_MCP_AUTH_TOKEN}``, never the literal."""
    desc = _make_descriptor(
        "test-envref",
        McpEntrySchema.STREAMABLE_HTTP_TYPE,
        McpFormat.JSON,
        ("mcpServers",),
        McpAuth.BEARER_ENVREF,
    )
    entry = mr.build_entry(desc, url=_URL, token="some_literal_token")
    assert _ENVREF in entry["headers"]["Authorization"]
    assert "some_literal_token" not in entry["headers"]["Authorization"]


def test_bearer_literal_with_token():
    """BEARER_LITERAL + non-empty token → literal baked in."""
    desc = _make_descriptor(
        "test-literal",
        McpEntrySchema.STREAMABLE_HTTP_TYPE,
        McpFormat.JSON,
        ("mcpServers",),
        McpAuth.BEARER_LITERAL,
    )
    entry = mr.build_entry(desc, url=_URL, token=_TOKEN)
    assert entry["headers"]["Authorization"] == f"Bearer {_TOKEN}"
    assert _ENVREF not in entry["headers"]["Authorization"]


def test_bearer_literal_without_token_no_headers():
    """BEARER_LITERAL + empty token → no ``headers`` key."""
    desc = _make_descriptor(
        "test-literal-notoken",
        McpEntrySchema.STREAMABLE_HTTP_TYPE,
        McpFormat.JSON,
        ("mcpServers",),
        McpAuth.BEARER_LITERAL,
    )
    entry = mr.build_entry(desc, url=_URL, token="")
    assert "headers" not in entry


def test_auth_none_no_headers():
    """McpAuth.NONE → no ``headers`` key in any schema."""
    for schema in McpEntrySchema:
        fmt = McpFormat.TOML if schema is McpEntrySchema.CODEX_TOML else McpFormat.JSON
        desc = _make_descriptor(
            f"test-none-{schema}",
            schema,
            fmt,
            ("mcpServers",),
            McpAuth.NONE,
        )
        entry = mr.build_entry(desc, url=_URL, token="tok")
        assert "headers" not in entry, f"headers found for schema={schema}"


# ── 3. register_mcp — JSON merge (foreign-server preservation) ────────────────


def test_register_mcp_json_preserves_foreign_servers(tmp_path):
    """JSON register keeps pre-existing servers intact."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {"mcpServers": {"other": {"url": "http://other/mcp", "type": "streamable-http"}}},
            indent=2,
        )
    )
    desc = _make_descriptor(
        "t",
        McpEntrySchema.STREAMABLE_HTTP_TYPE,
        McpFormat.JSON,
        ("mcpServers",),
        McpAuth.BEARER_LITERAL,
        global_path_factory=lambda: cfg,
    )
    result = mr.register_mcp(desc, url=_URL, token=_TOKEN)
    out = json.loads(cfg.read_text())
    assert out["mcpServers"]["other"]["url"] == "http://other/mcp"
    assert out["mcpServers"]["yadgar"]["url"] == _URL
    assert result["updated"] == str(cfg)
    assert result["new"]["url"] == _URL


def test_register_mcp_json_captures_old_entry(tmp_path):
    """``old`` in return value reflects the prior yadgar entry."""
    cfg = tmp_path / "mcp.json"
    stale_url = "http://127.0.0.1:9999/mcp"
    cfg.write_text(
        json.dumps(
            {"mcpServers": {"yadgar": {"type": "streamable-http", "url": stale_url}}},
            indent=2,
        )
    )
    desc = _make_descriptor(
        "t",
        McpEntrySchema.STREAMABLE_HTTP_TYPE,
        McpFormat.JSON,
        ("mcpServers",),
        McpAuth.NONE,
        global_path_factory=lambda: cfg,
    )
    result = mr.register_mcp(desc, url=_URL)
    assert result["old"]["url"] == stale_url
    assert result["new"]["url"] == _URL


def test_register_mcp_json_idempotent(tmp_path):
    """Second identical register_mcp call yields byte-identical file."""
    cfg = tmp_path / "mcp.json"
    desc = _make_descriptor(
        "t",
        McpEntrySchema.STREAMABLE_HTTP_TYPE,
        McpFormat.JSON,
        ("mcpServers",),
        McpAuth.NONE,
        global_path_factory=lambda: cfg,
    )
    mr.register_mcp(desc, url=_URL)
    first = cfg.read_text()
    mr.register_mcp(desc, url=_URL)
    assert cfg.read_text() == first


def test_register_mcp_nested_root_key_amp(tmp_path):
    """Amp nests under ``amp.mcpServers`` — nested root_key handled."""
    cfg = tmp_path / "settings.json"
    cfg.write_text(json.dumps({"amp": {"other_setting": 42}}))
    desc = _make_descriptor(
        "amp-test",
        McpEntrySchema.STREAMABLE_HTTP_TYPE,
        McpFormat.JSON,
        ("amp", "mcpServers"),
        McpAuth.NONE,
        global_path_factory=lambda: cfg,
    )
    mr.register_mcp(desc, url=_URL)
    out = json.loads(cfg.read_text())
    assert out["amp"]["other_setting"] == 42
    assert out["amp"]["mcpServers"]["yadgar"]["url"] == _URL


# ── 4. register_mcp — TOML merge (comment preservation) ──────────────────────


def test_register_mcp_toml_preserves_comments(tmp_path):
    """TOML register preserves user comments and other tables."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "# user top-level comment\n"
        'model = "gpt-4"\n'
        "\n"
        "[mcp_servers.other]\n"
        'url = "http://other/mcp"\n'
    )
    desc = _make_descriptor(
        "codex-test",
        McpEntrySchema.CODEX_TOML,
        McpFormat.TOML,
        ("mcp_servers",),
        McpAuth.NONE,
        global_path_factory=lambda: cfg,
    )
    mr.register_mcp(desc, url=_URL)
    text = cfg.read_text()
    assert "# user top-level comment" in text
    doc = tomlkit.parse(text)
    assert doc["model"] == "gpt-4"
    assert doc["mcp_servers"]["other"]["url"] == "http://other/mcp"
    assert doc["mcp_servers"]["yadgar"]["url"] == _URL


def test_register_mcp_toml_idempotent(tmp_path):
    """Second TOML register yields byte-identical output."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('# keep\nmodel = "gpt-4"\n')
    desc = _make_descriptor(
        "codex-idempotent",
        McpEntrySchema.CODEX_TOML,
        McpFormat.TOML,
        ("mcp_servers",),
        McpAuth.NONE,
        global_path_factory=lambda: cfg,
    )
    mr.register_mcp(desc, url=_URL)
    first = cfg.read_text()
    mr.register_mcp(desc, url=_URL)
    assert cfg.read_text() == first


def test_register_mcp_toml_with_auth_header(tmp_path):
    """TOML entry with auth header round-trips via tomlkit (nested dict)."""
    cfg = tmp_path / "config.toml"
    desc = _make_descriptor(
        "codex-auth",
        McpEntrySchema.CODEX_TOML,
        McpFormat.TOML,
        ("mcp_servers",),
        McpAuth.BEARER_ENVREF,
        global_path_factory=lambda: cfg,
    )
    mr.register_mcp(desc, url=_URL)
    doc = tomlkit.parse(cfg.read_text())
    auth = doc["mcp_servers"]["yadgar"]["headers"]["Authorization"]
    assert auth == f"Bearer {_ENVREF}"


# ── 5. Project-scope routing ──────────────────────────────────────────────────


def test_register_mcp_project_scope(tmp_path):
    """scope='project' writes to project_factory path, not global."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    desc = _make_descriptor(
        "t-project",
        McpEntrySchema.STREAMABLE_HTTP_TYPE,
        McpFormat.JSON,
        ("mcpServers",),
        McpAuth.NONE,
        global_path_factory=lambda: tmp_path / "global_MUST_NOT_EXIST.json",
        project_path_factory=lambda p: p / ".cursor" / "mcp.json",
    )
    result = mr.register_mcp(desc, url=_URL, scope="project", project_dir=project_dir)
    expected = project_dir / ".cursor" / "mcp.json"
    assert expected.exists()
    assert result["updated"] == str(expected)
    assert not (tmp_path / "global_MUST_NOT_EXIST.json").exists()


def test_register_mcp_project_scope_requires_project_dir():
    """scope='project' without project_dir raises ValueError."""
    desc = _make_descriptor(
        "t-no-dir",
        McpEntrySchema.STREAMABLE_HTTP_TYPE,
        McpFormat.JSON,
        ("mcpServers",),
        McpAuth.NONE,
        project_path_factory=lambda p: p / "mcp.json",
    )
    with pytest.raises(ValueError, match="project_dir required"):
        mr.register_mcp(desc, url=_URL, scope="project", project_dir=None)


def test_register_mcp_raises_when_no_path_at_scope(tmp_path):
    """Client with no project_factory raises ValueError for scope='project'."""
    desc = _make_descriptor(
        "t-no-proj-path",
        McpEntrySchema.STREAMABLE_HTTP_TYPE,
        McpFormat.JSON,
        ("mcpServers",),
        McpAuth.NONE,
        global_path_factory=lambda: tmp_path / "global.json",
        # project_factory is None (PathSpec default)
    )
    with pytest.raises(ValueError, match="no config path"):
        mr.register_mcp(desc, url=_URL, scope="project", project_dir=tmp_path)


# ── 6. Real-registry smoke tests (one per client) ────────────────────────────


@pytest.mark.parametrize("client_name", sorted(CLIENT_REGISTRY.keys()))
def test_build_entry_all_registry_clients(client_name):
    """build_entry succeeds for every registry client (smoke test)."""
    desc = CLIENT_REGISTRY[client_name]
    entry = mr.build_entry(desc, url=_URL, token=_TOKEN)
    assert isinstance(entry, dict)
    # URL must appear somewhere in the entry values.
    assert any(_URL in str(v) for v in entry.values()), f"{client_name}: url not in entry"


@pytest.mark.parametrize("client_name", sorted(CLIENT_REGISTRY.keys()))
def test_register_mcp_all_clients_global(tmp_path, client_name):
    """register_mcp with a tmp global path succeeds for every registry client."""
    desc = CLIENT_REGISTRY[client_name]
    fmt = desc.mcp_format
    ext = ".toml" if fmt is McpFormat.TOML else ".json"
    cfg = tmp_path / f"{client_name}{ext}"

    # Swap in a tmp path factory so we don't touch real config files.
    # We use a custom descriptor copy with the patched path.
    import dataclasses

    tmp_desc = dataclasses.replace(
        desc,
        mcp_config_path=PathSpec(global_factory=lambda: cfg),
    )

    result = mr.register_mcp(tmp_desc, url=_URL, token=_TOKEN)
    assert cfg.exists()
    assert result["updated"] == str(cfg)


# ── 7. configure_mcp delegation ───────────────────────────────────────────────


def test_configure_mcp_delegates_to_register_mcp(tmp_path, monkeypatch):
    """daemon.configure_mcp result shape unchanged after delegation to Car 1."""
    fake_claude_json = tmp_path / ".claude.json"

    # Patch Path.home() so register_mcp_for_claude_code writes to tmp.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Ensure no stale token from the environment.
    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)

    from yadgar.core.install.clients.mcp_register import register_mcp_for_claude_code

    result = register_mcp_for_claude_code(port=8765)
    assert "updated" in result
    assert "old" in result
    assert "new" in result
    assert result["new"]["type"] == "streamable-http"
    assert "8765" in result["new"]["url"]
    assert fake_claude_json.exists()


def test_configure_mcp_with_token_bakes_literal(tmp_path, monkeypatch):
    """CC BEARER_LITERAL: token from env is baked literally into ~/.claude.json."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", _TOKEN)

    from yadgar.core.install.clients.mcp_register import register_mcp_for_claude_code

    result = register_mcp_for_claude_code(port=8765)
    assert result["new"]["headers"]["Authorization"] == f"Bearer {_TOKEN}"
    # The env-ref string must NOT appear in the literal output.
    assert _ENVREF not in result["new"]["headers"]["Authorization"]


def test_configure_mcp_preserves_foreign_servers(tmp_path, monkeypatch):
    """configure_mcp keeps foreign MCP servers (atomic merge path, not write_text)."""
    existing = tmp_path / ".claude.json"
    existing.write_text(
        json.dumps(
            {
                "mcpServers": {"foreign": {"type": "streamable-http", "url": "http://foreign"}},
                "someOtherKey": "must_survive",
            },
            indent=2,
        )
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)

    from yadgar.core.install.clients.mcp_register import register_mcp_for_claude_code

    register_mcp_for_claude_code(port=8765)
    out = json.loads(existing.read_text())
    assert out["mcpServers"]["foreign"]["url"] == "http://foreign"
    assert out["mcpServers"]["yadgar"]["type"] == "streamable-http"
    assert out["someOtherKey"] == "must_survive"


def test_configure_mcp_idempotent(tmp_path, monkeypatch):
    """Two configure_mcp calls produce byte-identical ~/.claude.json."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)

    from yadgar.core.install.clients.mcp_register import register_mcp_for_claude_code

    register_mcp_for_claude_code(port=8765)
    first = (tmp_path / ".claude.json").read_text()
    register_mcp_for_claude_code(port=8765)
    assert (tmp_path / ".claude.json").read_text() == first


def test_register_mcp_toml_codex_envref_idempotent(tmp_path, monkeypatch):
    """Codex BEARER_ENVREF + TOML: two register_mcp calls produce byte-identical file.

    tomlkit re-serialises assigned plain-dict values; verify stable output so
    Car 3 can trust idempotent TOML writes for Codex.
    """
    cfg_path = tmp_path / "config.toml"

    # Build a Codex-shape descriptor pointing at our tmp path.
    codex_base = CLIENT_REGISTRY["codex"]
    codex = ClientDescriptor(
        name=codex_base.name,
        mcp_config_path=PathSpec(global_factory=lambda: cfg_path),
        mcp_format=codex_base.mcp_format,
        mcp_root_key=codex_base.mcp_root_key,
        mcp_entry_schema=codex_base.mcp_entry_schema,
        mcp_auth=codex_base.mcp_auth,
        rules_path=codex_base.rules_path,
        rules_header=codex_base.rules_header,
        rules_is_agents_md=codex_base.rules_is_agents_md,
        rules_addendum=codex_base.rules_addendum,
        rules_bridge=codex_base.rules_bridge,
        hooks_kind=codex_base.hooks_kind,
        task_mirror=codex_base.task_mirror,
        capability_tier=codex_base.capability_tier,
    )

    mr.register_mcp(descriptor=codex, url="http://127.0.0.1:8765/mcp", token="")
    first = cfg_path.read_text()

    mr.register_mcp(descriptor=codex, url="http://127.0.0.1:8765/mcp", token="")
    assert cfg_path.read_text() == first, "TOML idempotence broken for Codex BEARER_ENVREF shape"
