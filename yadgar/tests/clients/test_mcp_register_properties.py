"""Car 1 — Hypothesis property tests for MCP-registration serializers.

Invariants (≥200 examples each):

  P1  URL always appears in serialized entry.
  P2  BEARER_ENVREF emits ``${YADGAR_MCP_AUTH_TOKEN}`` and NEVER the literal token.
  P3  BEARER_LITERAL emits the literal token and NEVER ``${...}``.
  P4  McpAuth.NONE → no ``headers`` key.
  P5  All five serializers are pure functions (same inputs → same output).
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from yadgar.core.install.clients import mcp_register as mr
from yadgar.core.install.clients.descriptor import (
    CapabilityTier,
    ClientDescriptor,
    McpAuth,
    McpEntrySchema,
    McpFormat,
    PathSpec,
)

_ENVREF = "${YADGAR_MCP_AUTH_TOKEN}"

# Strategies ──────────────────────────────────────────────────────────────────

_url_st = st.builds(
    lambda host, port: f"http://{host}:{port}/mcp",
    host=st.just("127.0.0.1"),
    port=st.integers(min_value=1024, max_value=65535),
)

_token_st = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    min_size=1,
    max_size=64,
)

_schema_st = st.sampled_from(list(McpEntrySchema))


def _desc(schema: McpEntrySchema, auth: McpAuth) -> ClientDescriptor:
    fmt = McpFormat.TOML if schema is McpEntrySchema.CODEX_TOML else McpFormat.JSON
    return ClientDescriptor(
        name="prop-test",
        mcp_config_path=PathSpec(),
        mcp_format=fmt,
        mcp_root_key=("mcpServers",),
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


# ── P1: URL always present ────────────────────────────────────────────────────


@settings(max_examples=250, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(schema=_schema_st, url=_url_st)
def test_p1_url_always_in_entry(schema, url):
    """P1: every schema includes the URL somewhere in the entry."""
    entry = mr.build_entry(_desc(schema, McpAuth.NONE), url=url)
    all_values = str(entry)
    assert url in all_values


# ── P2: BEARER_ENVREF always env-ref, never literal ──────────────────────────


_LONG_TOKEN_ST = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=16,
    max_size=64,
)


@settings(max_examples=250, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(schema=_schema_st, url=_url_st, token=_LONG_TOKEN_ST)
def test_p2_envref_never_leaks_literal_token(schema, url, token):
    """P2: BEARER_ENVREF → env-ref in Authorization; literal token NOT in Authorization header.

    Token strategy uses lowercase only (min 16 chars) so it can never coincide
    with a substring of ``${YADGAR_MCP_AUTH_TOKEN}`` (all-uppercase).
    """
    entry = mr.build_entry(_desc(schema, McpAuth.BEARER_ENVREF), url=url, token=token)
    # The env-ref must appear in the Authorization header value.
    auth = entry.get("headers", {}).get("Authorization", "")
    assert _ENVREF in auth, f"env-ref not in Authorization: {auth!r}"
    # The literal token (lowercase-only, ≥16 chars) must NOT appear in the header.
    assert token not in auth, f"literal token leaked into Authorization: {auth!r}"


# ── P3: BEARER_LITERAL emits literal, never env-ref ──────────────────────────


@settings(max_examples=250, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(schema=_schema_st, url=_url_st, token=_LONG_TOKEN_ST)
def test_p3_literal_auth_bakes_token_not_envref(schema, url, token):
    """P3: BEARER_LITERAL bakes the literal token in Authorization; no env-ref emitted."""
    entry = mr.build_entry(_desc(schema, McpAuth.BEARER_LITERAL), url=url, token=token)
    auth = entry.get("headers", {}).get("Authorization", "")
    assert token in auth, f"literal token not in Authorization: {auth!r}"
    assert _ENVREF not in auth, f"env-ref should not appear in literal Authorization: {auth!r}"


# ── P4: NONE auth → no headers ───────────────────────────────────────────────


@settings(max_examples=250, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(schema=_schema_st, url=_url_st, token=_token_st)
def test_p4_none_auth_no_headers(schema, url, token):
    """P4: McpAuth.NONE → ``headers`` key absent for all schemas."""
    entry = mr.build_entry(_desc(schema, McpAuth.NONE), url=url, token=token)
    assert "headers" not in entry


# ── P5: Purity — same inputs → same output ───────────────────────────────────


@settings(max_examples=250, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    schema=_schema_st,
    auth=st.sampled_from(list(McpAuth)),
    url=_url_st,
    token=_token_st,
)
def test_p5_build_entry_is_pure(schema, auth, url, token):
    """P5: build_entry is a pure function — identical inputs produce identical outputs."""
    desc = _desc(schema, auth)
    first = mr.build_entry(desc, url=url, token=token)
    second = mr.build_entry(desc, url=url, token=token)
    assert first == second
