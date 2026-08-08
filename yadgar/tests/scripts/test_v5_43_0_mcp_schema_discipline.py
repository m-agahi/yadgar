"""RED tests for v5.43.0 — MCP schema discipline: caller-context enforcement.

TDD: written BEFORE implementation. Tests start RED and go GREEN once
implementation progresses through phases 1–4.

Coverage (ADR-0215 retired the branch-context half of this file; what remains is
the DIRECTORY half, which is the part that still enforces anything):
Q3.  wiki_query uses directory to scope results when directory is supplied.
Q4.  wiki_query requires directory (v5.65 guard).

B1.  block_create(scope='project', directory=None) → missing_directory error (v5.42.5 guard).
B2.  agent_prompt_save(no directory) → missing_directory error (v5.42.5 guard).
"""

from __future__ import annotations

import re

import pytest

from yadgar.core import server

pytestmark = pytest.mark.usefixtures("recall_backend_bypass", "admin_backend_bypass")


# ── shared fixture ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("v5_43_0_mcp_schema_disci")
    server.init_engines(
        db_path=str(tmp_path / "test_mcp_schema.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _storage():
    import yadgar._shared.runtime.state as _st

    return _st._storage


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]


# ── helpers ────────────────────────────────────────────────────────────────────


def _insert_wiki_page(slug, title, content, directory="global"):
    """Insert a wiki page directly to storage for test setup."""
    st = _storage()
    return st.insert_wiki_page(
        {
            "slug": slug,
            "title": title,
            "content": content,
            "category": "reference",
            "tags": ["test"],
            "links": [],
            "source_memory_ids": [],
            "confidence": "high",
            "directory_context": directory,
        },
    )


def _insert_memory(content, directory="/proj/test"):
    """Insert a memory directly to storage for test setup."""
    st = _storage()
    return st.insert_memory(
        {
            "content": content,
            "embedding": None,
            "tags": ["test"],
            "directory_context": directory,
            "heat": 0.8,
            "confidence": 0.7,
            "is_stale": False,
            "file_hash": None,
            "embedding_model": "all-MiniLM-L6-v2",
            "_internal": True,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Q — wiki_query directory scoping
# ═══════════════════════════════════════════════════════════════════════════════


def test_q3_wiki_query_scopes_to_directory(tmp_path):
    """wiki_query with directory scopes results to that dir + global (Q3)."""
    from yadgar.core.server.tools.wiki import wiki_query

    _insert_wiki_page(
        "schema-q3-proj-a",
        "Schema Q3 Project A",
        "schema discipline project alpha",
        directory="/proj/alpha",
    )
    _insert_wiki_page(
        "schema-q3-proj-b",
        "Schema Q3 Project B",
        "schema discipline project beta",
        directory="/proj/beta",
    )

    results_a = wiki_query("schema discipline project", directory="/proj/alpha")
    results_b = wiki_query("schema discipline project", directory="/proj/beta")

    slugs_a = [r["slug"] for r in results_a]
    slugs_b = [r["slug"] for r in results_b]

    assert "schema-q3-proj-a" in slugs_a
    assert "schema-q3-proj-b" not in slugs_a
    assert "schema-q3-proj-b" in slugs_b
    assert "schema-q3-proj-a" not in slugs_b


def test_q4_wiki_query_requires_directory_v565():
    """wiki_query without directory raises ValueError (v5.65 Fix D).

    Pre-v5.65: fell back to os.getcwd() (daemon container path — mis-scoping).
    Post-v5.65: hard-require directory; callers must supply the real host path.

    Updated from test_q4_wiki_query_falls_back_to_daemon_cwd_when_no_hint.
    """
    import pytest

    from yadgar.core.server.tools.wiki import wiki_query

    with pytest.raises(ValueError, match="directory is required"):
        wiki_query("schema discipline query fallback smoke")


# ═══════════════════════════════════════════════════════════════════════════════
# B — regression guards: v5.42.5 boundary enforcement
# ═══════════════════════════════════════════════════════════════════════════════


def test_b1_block_create_rejects_project_scope_without_directory():
    """block_create(scope='project', directory=None) → missing_directory (B1, v5.42.5 guard)."""
    from yadgar.core.server.tools.blocks import block_create

    result = block_create(name="guard-b1", content="guard content", scope="project", directory=None)
    assert result.get("error") == "missing_directory", (
        f"B1: expected missing_directory, got {result}"
    )


def test_b2_agent_prompt_save_rejects_missing_directory():
    """agent_prompt_save without directory → missing_directory error (B2, v5.42.5 guard)."""
    from yadgar.core.server.tools.agent_prompts import agent_prompt_save

    result = agent_prompt_save(pattern="guard-b2-prompt", content="guard content", directory=None)
    assert result.get("error") == "missing_directory", (
        f"B2: expected missing_directory, got {result}"
    )
