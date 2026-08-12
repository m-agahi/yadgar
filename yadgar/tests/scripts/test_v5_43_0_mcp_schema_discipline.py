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


#: C5 (ADR-0227): the storage chokepoint takes the caller's value or raises,
#: so every direct-insert fixture names one. These tests vary DIRECTORY, so a
#: single shared identity keeps the directory assertions meaningful.
_TEST_PROJECT_ID = "owner/repo"


def _insert_wiki_page(slug, title, content, directory="global", project_id=None):
    """Insert a wiki page directly to storage for test setup.

    project_id defaults to the shared _TEST_PROJECT_ID; Q3 below overrides it
    per page — wiki_query's scope key is project_id (Car C7), so isolating
    two pages by directory alone while sharing one project_id no longer
    isolates anything.
    """
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
            "project_id": project_id if project_id is not None else _TEST_PROJECT_ID,
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
            "project_id": _TEST_PROJECT_ID,
            "_internal": True,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Q — wiki_query directory scoping
# ═══════════════════════════════════════════════════════════════════════════════


def test_q3_wiki_query_scopes_to_directory(tmp_path):
    """wiki_query scopes results to the caller's project (Q3).

    RE-KEYED (0047 PR#40 Car C7, commit a33401c0 "one WHERE clause — project +
    global reach tag..."): wiki_query's RecallScope now filters on project_id,
    not directory — ``_st._wiki.query(..., scope=RecallScope(project_id=...))``
    in yadgar/core/server/tools/wiki.py. The original test varied only
    `directory` while both calls named the SAME `project=` override, which
    defeated the isolation it meant to prove once project wins over directory
    (Car M precedence: project > session > directory-derived > raise) — both
    pages share the caller's project_id, so both surface for both calls.
    Directory can no longer even be the sole scope key: C5 deleted the
    directory-derivation tier, so a call naming no `project=` and no session
    identity raises `UnresolvedProjectError` rather than falling back to
    directory. This is NOT the same code path as project_brief's
    directory-keyed `key_wiki_pages`/wiki_list (TestProjectBriefWikiScoping,
    test_directory_scoping_v562.py) — those deliberately were NOT touched by
    C7 and stay directory-keyed; wiki_query specifically was.

    The isolation axis this test proves is now project_id, so the two pages
    are seeded under two DIFFERENT project_ids and queried with matching
    overrides — `directory` is still supplied (wiki_query hard-requires it)
    but is logged-and-ignored once `project=` wins.
    """
    from yadgar.core.server.tools.wiki import wiki_query

    _PROJ_A = "owner/schema-q3-proj-a-repo"
    _PROJ_B = "owner/schema-q3-proj-b-repo"

    _insert_wiki_page(
        "schema-q3-proj-a",
        "Schema Q3 Project A",
        "schema discipline project alpha",
        directory="/proj/alpha",
        project_id=_PROJ_A,
    )
    _insert_wiki_page(
        "schema-q3-proj-b",
        "Schema Q3 Project B",
        "schema discipline project beta",
        directory="/proj/beta",
        project_id=_PROJ_B,
    )

    results_a = wiki_query("schema discipline project", directory="/proj/alpha", project=_PROJ_A)
    results_b = wiki_query("schema discipline project", directory="/proj/beta", project=_PROJ_B)

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
