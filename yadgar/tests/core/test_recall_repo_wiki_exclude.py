"""Car C (#83): recall fanout excludes repo_wiki pages by policy.

TDD RED-first suite.

Seam: WikiProvider.candidates() (backend/retrieval/providers/wiki.py) must
post-filter candidates whose get_policy(page.page_type).recall_disposition
== "exclude". This keeps wiki_query / wiki_read / wiki_list unaffected.

Acceptance criteria:
  R1  recall(type="wiki") does NOT return a stored page_type="repo_wiki" page.
  R2  wiki_query() DOES return the same page_type="repo_wiki" page.
  R3  A normal page (page_type=None) still appears in recall.
  R4  Flipping repo_wiki disposition to "include" via monkeypatch → page
      appears in recall (switchability).

Embedding-independent where possible: dir-scoping + tag-based checks.
"""

from __future__ import annotations

import sys

import pytest

import yadgar._shared.runtime.state as _st_mod

pytestmark = pytest.mark.usefixtures("recall_backend_bypass", "admin_backend_bypass")


# ---------------------------------------------------------------------------
# Module-scoped engine fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    from yadgar.core import server

    tmp_path = tmp_path_factory.mktemp("recall_repo_wiki_exclude")
    server.init_engines(
        db_path=str(tmp_path / "recall_repo_wiki_exclude.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wiki():
    return _st_mod._wiki


def _add_page(title: str, content: str, page_type=None, directory: str = "/home/max/git/yadgar"):
    """Direct WikiStore.add — synchronous, no queue."""
    from yadgar._shared.wiki.contract import WikiAddOptions

    return _wiki().add(
        title,
        content,
        category="reference",
        opts=WikiAddOptions(
            page_type=page_type,
            directory_context=directory,
        ),
    )


def _recall_fn():
    return sys.modules["yadgar.core.server.tools.recall"].recall


def _wiki_query_fn():
    return sys.modules["yadgar.core.server.tools.wiki"].wiki_query


_TEST_DIR = "/home/max/git/yadgar"

# Unique enough content to be retrievable.
_REPO_WIKI_CONTENT = (
    "## Purpose\nModule yadgar.retrieval.providers.wiki provides WikiProvider.\n"
    "## Exports\nWikiProvider\n## Design\nWraps WikiStore.query as SourceProvider.\n"
    "Candidate objects with type='wiki' passed to fusion pipeline.\n"
    "Directory-scoped post-filter applied via is_directory_eligible."
)
_NORMAL_CONTENT = (
    "Architecture overview of the yadgar indexing pipeline. "
    "Covers tokenisation, embedding, HNSW index build, and recall path. "
    "Key subsystem: WikiStore + MemoryProvider."
)

_REPO_WIKI_TITLE = "yadgar-retrieval-providers-wiki-module"
_NORMAL_TITLE = "yadgar-indexing-architecture-overview-car-c"


# ---------------------------------------------------------------------------
# Seed data (module-scoped so all tests share the same DB state)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _seed_pages(_engines):
    """Seed one repo_wiki page and one normal page.

    Function-scoped (re-seeds per test) NOT module-scoped: the conftest
    per-test data-wipe (_WIPE_TABLES includes wiki_page) runs on each test's
    teardown, so a module-scoped single seed is stranded for every test after
    the first — deterministically under single-process, flakily under xdist
    worker distribution (pre-existing; surfaced by full-suite runs). Re-seeding
    per test makes the page present for whichever test runs, in any order.
    """
    _add_page(_REPO_WIKI_TITLE, _REPO_WIKI_CONTENT, page_type="repo_wiki")
    _add_page(_NORMAL_TITLE, _NORMAL_CONTENT, page_type=None)
    yield


# ---------------------------------------------------------------------------
# R1 — recall(type="wiki") excludes repo_wiki
# ---------------------------------------------------------------------------


class TestRecallExcludesRepoWiki:
    def test_r1_repo_wiki_absent_from_recall(self):
        """R1: recall(type='wiki') must not contain any repo_wiki page."""
        recall = _recall_fn()
        results = recall(
            query="WikiProvider module yadgar retrieval providers wiki",
            directory=_TEST_DIR,
            type="wiki",
            max_results=20,
        )
        slugs = [r.get("slug") for r in results]
        page_types = [r.get("page_type") for r in results]
        assert "repo_wiki" not in page_types, (
            f"repo_wiki page leaked into recall fanout. types={page_types}, slugs={slugs}"
        )

    def test_r1_repo_wiki_slug_absent(self):
        """R1 (slug-level): stored repo_wiki slug absent from recall results."""
        recall = _recall_fn()
        results = recall(
            query="WikiProvider module yadgar retrieval providers wiki",
            directory=_TEST_DIR,
            type="wiki",
            max_results=20,
        )
        slugs = [r.get("slug") for r in results]
        # Title-derived slug for _REPO_WIKI_TITLE.
        import re

        expected_slug = re.sub(r"[^a-z0-9]+", "-", _REPO_WIKI_TITLE.lower()).strip("-")[:64]
        assert expected_slug not in slugs, (
            f"repo_wiki slug {expected_slug!r} found in recall results: {slugs}"
        )


# ---------------------------------------------------------------------------
# R2 — wiki_query DOES return the repo_wiki page
# ---------------------------------------------------------------------------


class TestWikiQueryStillReturnsRepoWiki:
    def test_r2_wiki_query_returns_repo_wiki(self):
        """R2: wiki_query() must still return repo_wiki page (read path unaffected)."""
        wiki_query = _wiki_query_fn()
        results = wiki_query(
            query="WikiProvider module yadgar retrieval providers wiki",
            directory=_TEST_DIR,
            max_results=20,
        )
        page_types = [r.get("page_type") for r in results]
        assert "repo_wiki" in page_types, (
            f"wiki_query should return repo_wiki page but did not. types={page_types}"
        )


# ---------------------------------------------------------------------------
# R3 — normal page still passes through WikiProvider (not filtered out)
# ---------------------------------------------------------------------------


class TestNormalPageAppearsInRecall:
    def test_r3_normal_page_not_filtered_by_policy(self):
        """R3: WikiProvider.candidates() does NOT filter pages with page_type=None."""
        from yadgar.backend.retrieval.providers.base import Scope
        from yadgar.backend.retrieval.providers.wiki import WikiProvider

        class _FakeWiki:
            def query(self, query, max_results=10, include_tag=None, exclude_tags=None, **kw):
                return [
                    {
                        "id": 7777,
                        "slug": "r3-normal-page",
                        "title": "R3 Normal Page",
                        "content": "normal content",
                        "page_type": None,  # no page_type → DEFAULT_POLICY → include
                        "directory_context": _TEST_DIR,
                        "branch": None,
                        "_retrieval_score": 0.9,
                    },
                    {
                        "id": 7778,
                        "slug": "r3-architecture-page",
                        "title": "R3 Architecture Page",
                        "content": "architecture content",
                        "page_type": "adr",  # ADR → DEFAULT_POLICY → include
                        "directory_context": _TEST_DIR,
                        "branch": None,
                        "_retrieval_score": 0.8,
                    },
                ]

        provider = WikiProvider(_FakeWiki())
        scope = Scope(directory=_TEST_DIR)
        candidates = provider.candidates("normal content", scope, limit=20)
        slugs = [c.id for c in candidates]
        assert "r3-normal-page" in slugs, (
            f"Normal page (page_type=None) must not be filtered from candidates. got: {slugs}"
        )
        assert "r3-architecture-page" in slugs, (
            f"ADR page must not be filtered from candidates. got: {slugs}"
        )

    def test_r3_only_exclude_disposition_filtered(self):
        """R3b: only 'exclude' disposition is filtered; 'include' pages pass through."""
        from yadgar.backend.retrieval.providers.base import Scope
        from yadgar.backend.retrieval.providers.wiki import WikiProvider

        class _FakeWiki:
            def query(self, query, max_results=10, include_tag=None, exclude_tags=None, **kw):
                return [
                    {
                        "id": 6666,
                        "slug": "r3b-repo-wiki",
                        "title": "R3b Repo Wiki",
                        "content": "repo wiki content",
                        "page_type": "repo_wiki",
                        "directory_context": _TEST_DIR,
                        "branch": None,
                        "_retrieval_score": 0.9,
                    },
                    {
                        "id": 6667,
                        "slug": "r3b-normal",
                        "title": "R3b Normal",
                        "content": "normal content",
                        "page_type": None,
                        "directory_context": _TEST_DIR,
                        "branch": None,
                        "_retrieval_score": 0.85,
                    },
                ]

        provider = WikiProvider(_FakeWiki())
        scope = Scope(directory=_TEST_DIR)
        candidates = provider.candidates("content", scope, limit=20)
        slugs = [c.id for c in candidates]
        assert "r3b-repo-wiki" not in slugs, "repo_wiki must be filtered"
        assert "r3b-normal" in slugs, "normal page must pass through"


# ---------------------------------------------------------------------------
# R4 — flip disposition to "include" → exclusion no longer filters the page
# ---------------------------------------------------------------------------


class TestDispositionFlipInclude:
    def test_r4_flip_to_include_candidates_not_filtered(self, monkeypatch):
        """R4: patching repo_wiki recall_disposition to 'include' removes exclusion filter.

        Tests the policy-driven exclusion switch at the WikiProvider level by
        injecting a controlled page dict and verifying that candidates() no longer
        drops it when the disposition is flipped to "include".
        """
        from yadgar._shared.wiki import policy as _policy_mod
        from yadgar._shared.wiki.policy import WikiPolicy
        from yadgar.backend.retrieval.providers.base import Scope
        from yadgar.backend.retrieval.providers.wiki import WikiProvider

        # Build a patched registry where repo_wiki is "include".
        patched = {
            "repo_wiki": WikiPolicy(
                gate_mode="identity",
                recall_disposition="include",
                dir_scope="strict",
                merge="never",
            )
        }
        monkeypatch.setattr(_policy_mod, "POLICY_BY_TYPE", patched)

        # Inject a fake WikiStore.query that returns one repo_wiki page.
        class _FakeWiki:
            def query(self, query, max_results=10, include_tag=None, exclude_tags=None, **kw):
                return [
                    {
                        "id": 9999,
                        "slug": "r4-test-repo-wiki-page",
                        "title": "R4 Test Repo Wiki Page",
                        "content": "flip test content",
                        "page_type": "repo_wiki",
                        "directory_context": _TEST_DIR,
                        "branch": None,
                        "_retrieval_score": 0.9,
                    }
                ]

        provider = WikiProvider(_FakeWiki())
        scope = Scope(directory=_TEST_DIR)
        candidates = provider.candidates("flip test", scope, limit=20)
        slugs = [c.id for c in candidates]
        assert "r4-test-repo-wiki-page" in slugs, (
            "After flipping repo_wiki disposition to 'include', "
            f"WikiProvider.candidates() should pass through the page. got: {slugs}"
        )

    def test_r4_default_disposition_filters_page(self, monkeypatch):
        """R4b: confirm default (exclude) disposition IS filtering repo_wiki pages.

        Verify the policy filter is active with the real POLICY_BY_TYPE.
        """
        from yadgar.backend.retrieval.providers.base import Scope
        from yadgar.backend.retrieval.providers.wiki import WikiProvider

        class _FakeWiki:
            def query(self, query, max_results=10, include_tag=None, exclude_tags=None, **kw):
                return [
                    {
                        "id": 8888,
                        "slug": "r4b-test-repo-wiki-page",
                        "title": "R4b Test Repo Wiki Page",
                        "content": "default exclude test",
                        "page_type": "repo_wiki",
                        "directory_context": _TEST_DIR,
                        "branch": None,
                        "_retrieval_score": 0.9,
                    }
                ]

        provider = WikiProvider(_FakeWiki())
        scope = Scope(directory=_TEST_DIR)
        candidates = provider.candidates("default exclude", scope, limit=20)
        slugs = [c.id for c in candidates]
        assert "r4b-test-repo-wiki-page" not in slugs, (
            "Default repo_wiki disposition='exclude' should filter page from candidates. "
            f"got: {slugs}"
        )
