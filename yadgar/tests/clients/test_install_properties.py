"""Car 3 — Hypothesis property tests for install.py.

Invariants (≥200 examples each):

  P1  dry_run=True never creates files (same inputs → same output, zero writes).
  P2  print output for BEARER_LITERAL client never contains the raw token.
  P3  print output always contains the YADGAR_MCP_AUTH_TOKEN env-var name when
      mcp=True (any auth style except NONE).
  P4  install_client → print determinism: calling twice with same params produces
      byte-identical mcp and rules fragments.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from yadgar.core.install.clients.descriptor import (
    CapabilityTier,
    ClientDescriptor,
    McpAuth,
    McpEntrySchema,
    McpFormat,
    PathSpec,
)

_url_st = st.builds(
    lambda port: f"http://127.0.0.1:{port}/mcp",
    port=st.integers(min_value=1024, max_value=65535),
)

_token_st = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=4,
    max_size=64,
)

_version_st = st.builds(
    lambda a, b, c: f"{a}.{b}.{c}",
    a=st.integers(1, 10),
    b=st.integers(0, 999),
    c=st.integers(0, 99),
)

_schema_non_toml_st = st.sampled_from(
    [
        McpEntrySchema.STREAMABLE_HTTP_TYPE,
        McpEntrySchema.OPENCODE_REMOTE,
        McpEntrySchema.GEMINI_HTTPURL,
        McpEntrySchema.CLINE_STREAMABLEHTTP,
    ]
)


def _desc_for_props(tmp_path: Path, schema: McpEntrySchema, auth: McpAuth) -> ClientDescriptor:
    return ClientDescriptor(
        name="prop-test",
        mcp_config_path=PathSpec(global_factory=lambda: tmp_path / "mcp.json"),
        mcp_format=McpFormat.JSON,
        mcp_root_key=("mcpServers",),
        mcp_entry_schema=schema,
        mcp_auth=auth,
        rules_path=PathSpec(global_factory=lambda: tmp_path / "AGENTS.md"),
        rules_header="## Yadgar",
        rules_is_agents_md=True,
        rules_addendum=[],
        rules_bridge=None,
        hooks_kind=None,
        task_mirror=None,
        capability_tier=CapabilityTier.MCP_RULES,
    )


# ── P1: dry_run never creates files ──────────────────────────────────────────


@given(url=_url_st, token=_token_st, version=_version_st, schema=_schema_non_toml_st)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_dry_run_never_creates_files(url, token, version, schema, tmp_path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    tmp = tmp_path / "isolated"
    tmp.mkdir(exist_ok=True)
    desc = _desc_for_props(tmp, schema, McpAuth.BEARER_ENVREF)
    registry = {desc.name: desc}
    before = set(tmp.rglob("*"))
    install_client(
        desc.name,
        InstallOptions(url=url, token=token, version=version, mcp=True, rules=True, dry_run=True),
        registry=registry,
    )
    after = set(tmp.rglob("*"))
    assert before == after, f"dry_run created unexpected files: {after - before}"


# ── P2: BEARER_LITERAL never leaks raw token in dry_run output ───────────────


@given(url=_url_st, token=_token_st, version=_version_st, schema=_schema_non_toml_st)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_literal_token_never_in_print_output(url, token, version, schema, tmp_path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    tmp = tmp_path / "isolated"
    tmp.mkdir(exist_ok=True)
    desc = _desc_for_props(tmp, schema, McpAuth.BEARER_LITERAL)
    registry = {desc.name: desc}
    result = install_client(
        desc.name,
        InstallOptions(url=url, token=token, version=version, mcp=True, rules=False, dry_run=True),
        registry=registry,
    )
    if result["mcp"] is not None and token:
        assert token not in str(result["mcp"]["content"]), (
            "Raw token must never appear in dry_run output"
        )


# ── P3: env-var name appears for non-NONE auth ───────────────────────────────


@given(
    url=_url_st,
    token=_token_st,
    version=_version_st,
    schema=_schema_non_toml_st,
    auth=st.sampled_from([McpAuth.BEARER_ENVREF, McpAuth.BEARER_LITERAL]),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_envvar_name_in_print_output(url, token, version, schema, auth, tmp_path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    tmp = tmp_path / "isolated"
    tmp.mkdir(exist_ok=True)
    desc = _desc_for_props(tmp, schema, auth)
    registry = {desc.name: desc}
    result = install_client(
        desc.name,
        InstallOptions(url=url, token=token, version=version, mcp=True, rules=False, dry_run=True),
        registry=registry,
    )
    if result["mcp"] is not None and token:
        assert "YADGAR_MCP_AUTH_TOKEN" in str(result["mcp"]["content"])


# ── P4: print determinism (same inputs → byte-identical output) ───────────────


@given(url=_url_st, token=_token_st, version=_version_st, schema=_schema_non_toml_st)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_print_is_deterministic(url, token, version, schema, tmp_path):
    from yadgar.core.install.clients.install import InstallOptions, install_client

    tmp = tmp_path / "isolated"
    tmp.mkdir(exist_ok=True)
    desc = _desc_for_props(tmp, schema, McpAuth.BEARER_ENVREF)
    registry = {desc.name: desc}
    opts = InstallOptions(url=url, token=token, version=version, mcp=True, rules=True, dry_run=True)
    r1 = install_client(desc.name, opts, registry=registry)
    r2 = install_client(desc.name, opts, registry=registry)
    assert r1["mcp"] == r2["mcp"]
    assert r1["rules"] == r2["rules"]
