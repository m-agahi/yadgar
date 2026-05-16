"""Robustness tests: Unicode edge cases, volume limits, and response schema contracts."""

import json

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from yadgar import server


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "robustness.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


# ── A. Unicode robustness ─────────────────────────────────────────────────────


@hsettings(max_examples=200, deadline=2000)
@given(st.text(alphabet=st.characters(min_codepoint=0, max_codepoint=0x10FFFF)))
def test_memorize_handles_arbitrary_unicode(content):
    """Any Unicode string returns sync within deadline; no unhandled exceptions.

    Uses the _engines autouse fixture (via module-level init).
    """
    r = server.memorize(content[:1000], "/tmp", [])
    assert isinstance(r, dict)
    assert "stored" in r
    if not r["stored"]:
        assert r["reason"] in {
            "content_too_large",
            "secret_detected",
            "blocked_by_policy",
            "invalid_unicode_surrogates",
        }, f"Unexpected reason: {r['reason']!r}"


@given(
    st.text(
        alphabet=st.characters(min_codepoint=0xD800, max_codepoint=0xDFFF),
        min_size=1,
    )
)
def test_memorize_rejects_unpaired_surrogates(surrogates):
    """Content with unpaired UTF-16 surrogates must be synchronously rejected."""
    r = server.memorize(f"prefix {surrogates} suffix", "/tmp", [])
    assert r["stored"] is False
    assert r["reason"] == "invalid_unicode_surrogates"


def test_memorize_unpaired_surrogate_rejected():
    """Direct surrogate codepoint is rejected with the expected reason."""
    r = server.memorize(f"bad content: {chr(0xD800)}", "/tmp", [])
    assert r["stored"] is False
    assert r["reason"] == "invalid_unicode_surrogates"


# ── B. Volume / wiki_list ─────────────────────────────────────────────────────


def test_wiki_list_response_bounded_at_scale(flush_queue):
    """wiki_list with N>limit pages must stay under 100 KB and omit content.

    Uses 120 pages: enough to exceed the default cap of 100 and prove pagination,
    while keeping the per-page drain cost (embedding + DB write) under the 60s
    pytest-timeout. A fuller 500+ page volume run lives outside the unit suite.
    """
    for i in range(120):
        server.wiki_add(
            title=f"Test page {i}",
            content=f"content {i} " * 5,
            category="reference",
        )
    flush_queue()
    result = server.wiki_list()
    assert len(result) <= 100, "Default limit must cap at 100"
    for p in result:
        assert "content" not in p, f"wiki_list must not include content; got keys: {list(p.keys())}"
    assert len(json.dumps(result)) < 100_000, "Response must be under 100 KB"


def test_wiki_list_returns_no_content(flush_queue):
    """wiki_list must never return content field in any item."""
    server.wiki_add(title="Check page", content="This content should not appear in list")
    flush_queue()
    result = server.wiki_list()
    for p in result:
        assert "content" not in p


def test_wiki_list_respects_limit(flush_queue):
    """wiki_list limit parameter must cap results."""
    for i in range(20):
        server.wiki_add(title=f"Limit page {i}", content=f"content {i}")
    flush_queue()
    result = server.wiki_list(limit=5)
    assert len(result) <= 5


def test_wiki_list_slug_prefix_filter(flush_queue):
    """slug_prefix filters to matching slugs only."""
    server.wiki_add(title="alpha one", content="content a1")
    server.wiki_add(title="alpha two", content="content a2")
    server.wiki_add(title="beta one", content="content b1")
    flush_queue()
    result = server.wiki_list(slug_prefix="alpha")
    for p in result:
        assert p["slug"].startswith("alpha"), f"Expected alpha prefix, got: {p['slug']!r}"


def test_wiki_list_negative_limit_returns_all(flush_queue):
    """wiki_list with negative limit must return all pages (no cap)."""
    for i in range(5):
        server.wiki_add(title=f"Neg limit page {i}", content=f"content neg {i}")
    flush_queue()
    result = server.wiki_list(limit=-1)
    assert len(result) >= 5, "Negative limit must not truncate results"


def test_wiki_list_zero_limit_returns_all(flush_queue):
    """wiki_list with limit=0 must return all pages (no cap)."""
    for i in range(5):
        server.wiki_add(title=f"Zero limit page {i}", content=f"content zero {i}")
    flush_queue()
    result = server.wiki_list(limit=0)
    assert len(result) >= 5, "Zero limit must not truncate results"


def test_wiki_list_huge_limit_returns_all(flush_queue):
    """wiki_list with limit larger than page count must return all pages."""
    for i in range(5):
        server.wiki_add(title=f"Huge limit page {i}", content=f"content huge {i}")
    flush_queue()
    result = server.wiki_list(limit=1_000_000)
    assert len(result) >= 5, "Huge limit must return all pages"


# ── C. Schema contracts ───────────────────────────────────────────────────────


def test_memorize_response_shape_async_path():
    """Fast path must return only known fields."""
    r = server.memorize("test content for schema check", "/tmp", [])
    assert set(r.keys()) <= {"stored", "queued", "queue_id", "reason"}, (
        f"Unexpected keys in response: {set(r.keys())}"
    )


def test_memorize_response_shape_too_large_path():
    """Too-large reject must return exactly {stored, reason, max_bytes}."""
    r = server.memorize("x" * 40_000, "/tmp", [])
    assert set(r.keys()) == {"stored", "reason", "max_bytes"}
    assert r["stored"] is False
    assert r["reason"] == "content_too_large"


def test_memorize_response_shape_surrogate_path():
    """Surrogate reject must return {stored, reason}."""
    r = server.memorize(chr(0xD800), "/tmp", [])
    assert r["stored"] is False
    assert r["reason"] == "invalid_unicode_surrogates"
    # Must not include max_bytes or other fields
    assert "max_bytes" not in r
