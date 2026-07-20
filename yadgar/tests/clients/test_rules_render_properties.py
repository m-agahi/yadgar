"""Car 2 — Hypothesis property tests for rules_render.

Invariants (≥200 examples each):

  P1  section_replace is idempotent:
      section_replace(section_replace(x, h, b), h, b) == section_replace(x, h, b)

  P2  render_body output always contains the MCP endpoint URL.

  P3  render_body never contains un-substituted ``{__version__}`` placeholder.

  P4  Addenda composition is deterministic (same descriptor + version →
      same output every time).
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from yadgar.core.install.clients import rules_render as rr
from yadgar.core.install.clients.registry import CLIENT_REGISTRY

# ── Strategies ────────────────────────────────────────────────────────────────

_version_st = st.builds(
    lambda maj, min_, patch: f"{maj}.{min_}.{patch}",
    maj=st.integers(min_value=0, max_value=9),
    min_=st.integers(min_value=0, max_value=999),
    patch=st.integers(min_value=0, max_value=999),
)

# Arbitrary surrounding text that must not start with "## " (to avoid
# interfering with the pattern-stop boundary).
_safe_text_st = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=200,
).filter(lambda s: not any(line.startswith("## ") for line in s.splitlines()))

_section_header_st = st.builds(
    lambda suffix: f"## {suffix}",
    suffix=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz -",
        min_size=3,
        max_size=30,
    ),
)

_body_st = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=300,
).filter(lambda s: not any(line.startswith("## ") for line in s.splitlines()))

# All client descriptors from the registry.
_descriptor_st = st.sampled_from(list(CLIENT_REGISTRY.values()))


# ── P1: section_replace idempotence ──────────────────────────────────────────


@settings(max_examples=250, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(content=_safe_text_st, header=_section_header_st, body=_body_st)
def test_p1_section_replace_idempotent(content, header, body):
    """P1: section_replace(section_replace(x,h,b),h,b) == section_replace(x,h,b)."""
    first = rr.section_replace(content, header, body)
    second = rr.section_replace(first, header, body)
    assert first == second, (
        f"section_replace not idempotent.\nheader={header!r}\nbody={body!r}\nfirst != second"
    )


# ── P2: render_body always contains MCP endpoint ─────────────────────────────

_MCP_URL = "http://127.0.0.1:8765/mcp"


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(descriptor=_descriptor_st, version=_version_st)
def test_p2_render_body_contains_mcp_url(descriptor, version):
    """P2: every client's rendered body includes the canonical MCP endpoint."""
    body = rr.render_body(descriptor, version)
    assert _MCP_URL in body, f"MCP URL missing from rendered body for client={descriptor.name!r}"


# ── P3: render_body never contains un-substituted placeholder ────────────────


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(descriptor=_descriptor_st, version=_version_st)
def test_p3_render_body_no_unsubstituted_placeholder(descriptor, version):
    """P3: {__version__} placeholder is always replaced in the rendered body."""
    body = rr.render_body(descriptor, version)
    assert "{__version__}" not in body, (
        f"Un-substituted placeholder found for client={descriptor.name!r}"
    )


# ── P4: addenda composition is deterministic ─────────────────────────────────


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(descriptor=_descriptor_st, version=_version_st)
def test_p4_render_body_deterministic(descriptor, version):
    """P4: calling render_body twice with same args yields identical output."""
    first = rr.render_body(descriptor, version)
    second = rr.render_body(descriptor, version)
    assert first == second, f"render_body not deterministic for client={descriptor.name!r}"
