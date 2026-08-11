"""Unit tests for v6 T6 Step 1 — SourceProvider abstraction.

Coverage:
  1. MemoryProvider.candidates() wraps Retriever.recall() → normalized Candidates
  2. WikiProvider.candidates() wraps WikiStore.query() → normalized Candidates
  3. Candidate fields (type, id, title, content, native_score, project_id, raw)
  4. Scope carries the project_id field
  5. MemoryProvider type == "memory", WikiProvider type == "wiki"
  6. No calls to recall() or wiki_query() MCP tools — providers are pure extraction

Car C7 (0047 §5 C7) re-keyed ``Scope.directory`` → ``Scope.project_id`` and
``Candidate.directory_context`` → ``Candidate.project_id`` (the scope is
pushed into the stage-1 SQL WHERE rather than applied as a Python
post-filter — see ``yadgar/backend/retrieval/providers/base.py``). Both
providers also now thread ``project_id=`` into their underlying store calls
(``Retriever.recall`` / ``WikiStore.query``), and ``MemoryProvider`` applies
an ``is_project_eligible`` residual guard on returned rows — so the mock
memory/wiki dicts below carry ``project_id`` (not ``directory_context``)
matching the scope under test, or eligibility silently drops them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar.backend.retrieval.providers.base import Candidate, Scope, SourceProvider
from yadgar.backend.retrieval.providers.memory import MemoryProvider
from yadgar.backend.retrieval.providers.wiki import WikiProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_memory_dict(mid: int = 1, score: float = 0.8) -> dict:
    return {
        "id": mid,
        "content": f"memory content {mid}",
        "heat": 0.6,
        "_retrieval_score": score,
        "project_id": "/home/user/project",
        "branch": "master",
        "tags": [],
    }


def _make_wiki_page(slug: str = "test-page", score: float = 0.75) -> dict:
    return {
        "id": 10,
        "slug": slug,
        "title": f"Test Page: {slug}",
        "content": "wiki page content about testing",
        "_retrieval_score": score,
        "project_id": "/home/user/project",
        "branch": "master",
        "tags": [],
    }


@pytest.fixture()
def mock_retriever():
    r = MagicMock()
    r.recall.return_value = [_make_memory_dict(1, 0.9), _make_memory_dict(2, 0.7)]
    return r


@pytest.fixture()
def mock_wiki():
    w = MagicMock()
    w.query.return_value = [_make_wiki_page("overview", 0.85), _make_wiki_page("detail", 0.5)]
    return w


@pytest.fixture()
def default_scope():
    return Scope(
        project_id="/home/user/project",
        min_heat=0.0,
    )


# ---------------------------------------------------------------------------
# 1. Scope dataclass
# ---------------------------------------------------------------------------


class TestScope:
    def test_scope_fields(self):
        scope = Scope(
            project_id="/project",
            min_heat=0.1,
        )
        assert scope.project_id == "/project"
        assert scope.min_heat == 0.1

    def test_scope_optional_defaults(self):
        scope = Scope(project_id="/project")
        assert scope.min_heat == 0.0


# ---------------------------------------------------------------------------
# 2. Candidate dataclass
# ---------------------------------------------------------------------------


class TestCandidate:
    def test_candidate_fields(self):
        raw = {"id": 1, "content": "hello"}
        c = Candidate(
            type="memory",
            id=1,
            title=None,
            content="hello",
            native_score=0.9,
            project_id="/project",
            raw=raw,
        )
        assert c.type == "memory"
        assert c.id == 1
        assert c.title is None
        assert c.content == "hello"
        assert c.native_score == 0.9
        assert c.project_id == "/project"
        assert c.raw is raw

    def test_candidate_wiki_type(self):
        c = Candidate(
            type="wiki",
            id="overview-slug",
            title="Overview",
            content="Wiki content here",
            native_score=0.7,
            project_id=None,
            raw={},
        )
        assert c.type == "wiki"
        assert c.id == "overview-slug"
        assert c.title == "Overview"


# ---------------------------------------------------------------------------
# 3. SourceProvider is abstract
# ---------------------------------------------------------------------------


class TestSourceProviderABC:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            SourceProvider()

    def test_concrete_subclass_must_implement(self):
        """A concrete subclass that only partially implements raises TypeError."""

        class Partial(SourceProvider):
            @property
            def type(self) -> str:
                return "partial"

            # Missing candidates()

        with pytest.raises(TypeError):
            Partial()


# ---------------------------------------------------------------------------
# 4. MemoryProvider
# ---------------------------------------------------------------------------


class TestMemoryProvider:
    def test_type_is_memory(self, mock_retriever):
        provider = MemoryProvider(mock_retriever)
        assert provider.type == "memory"

    def test_candidates_calls_retriever_recall(self, mock_retriever, default_scope):
        provider = MemoryProvider(mock_retriever)
        provider.candidates("test query", default_scope, limit=10)
        # Phase 2a forward-only: MemoryProvider now threads a `profile` kwarg
        # (None when constructed without one) into Retriever.recall().
        # ADR-0077: it also threads `deadline` (None when constructed without one).
        # Car C7: it also threads `project_id` (scope.project_id, was the
        # Python post-filter's `directory` — now the SQL-side scope key).
        mock_retriever.recall.assert_called_once_with(
            "test query",
            max_results=10,
            min_heat=0.0,
            profile=None,
            deadline=None,
            project_id="/home/user/project",
        )

    def test_candidates_returns_candidate_objects(self, mock_retriever, default_scope):
        provider = MemoryProvider(mock_retriever)
        results = provider.candidates("test query", default_scope, limit=5)
        assert len(results) == 2
        for c in results:
            assert isinstance(c, Candidate)
            assert c.type == "memory"

    def test_candidates_normalizes_fields(self, mock_retriever, default_scope):
        provider = MemoryProvider(mock_retriever)
        results = provider.candidates("test query", default_scope, limit=5)
        first = results[0]
        assert first.id == 1
        assert first.title is None  # memories have no title
        assert first.content == "memory content 1"
        assert first.native_score == pytest.approx(0.9)
        assert first.project_id == "/home/user/project"

    def test_candidates_raw_is_original_dict(self, mock_retriever, default_scope):
        provider = MemoryProvider(mock_retriever)
        results = provider.candidates("test query", default_scope, limit=5)
        assert results[0].raw["id"] == 1
        assert results[0].raw["content"] == "memory content 1"

    def test_candidates_skips_missing_id(self, default_scope):
        retriever = MagicMock()
        retriever.recall.return_value = [
            {"content": "no id here", "heat": 0.5},  # no id
            _make_memory_dict(3, 0.8),
        ]
        provider = MemoryProvider(retriever)
        results = provider.candidates("query", default_scope, limit=10)
        assert len(results) == 1
        assert results[0].id == 3

    def test_candidates_falls_back_to_heat_for_score(self, default_scope):
        retriever = MagicMock()
        # project_id matches default_scope's so the is_project_eligible residual
        # guard does not drop this row before native_score can be asserted —
        # this test is about the score fallback, not eligibility.
        mem = {
            "id": 5,
            "content": "c",
            "heat": 0.4,
            "project_id": "/home/user/project",
            "branch": None,
        }
        retriever.recall.return_value = [mem]
        provider = MemoryProvider(retriever)
        results = provider.candidates("q", default_scope, limit=5)
        assert results[0].native_score == pytest.approx(0.4)

    def test_candidates_scope_min_heat_forwarded(self, mock_retriever):
        scope = Scope(project_id="/p", min_heat=0.3)
        provider = MemoryProvider(mock_retriever)
        provider.candidates("q", scope, limit=5)
        call_kwargs = mock_retriever.recall.call_args[1]
        assert call_kwargs["min_heat"] == 0.3


# ---------------------------------------------------------------------------
# 5. WikiProvider
# ---------------------------------------------------------------------------


class TestWikiProvider:
    def test_type_is_wiki(self, mock_wiki):
        provider = WikiProvider(mock_wiki)
        assert provider.type == "wiki"

    def test_candidates_calls_wiki_query(self, mock_wiki, default_scope):
        provider = WikiProvider(mock_wiki)
        provider.candidates("test query", default_scope, limit=5)
        # Car C7: WikiProvider now also threads project_id (scope.project_id)
        # and opt_in_tags into WikiStore.query() — pushed into the stage-1
        # SQL WHERE rather than applied as a post-filter.
        mock_wiki.query.assert_called_once_with(
            "test query",
            max_results=5,
            include_tag=None,
            exclude_tags=None,
            project_id="/home/user/project",
            opt_in_tags=None,
        )

    def test_candidates_returns_candidate_objects(self, mock_wiki, default_scope):
        provider = WikiProvider(mock_wiki)
        results = provider.candidates("test query", default_scope, limit=5)
        assert len(results) == 2
        for c in results:
            assert isinstance(c, Candidate)
            assert c.type == "wiki"

    def test_candidates_normalizes_fields(self, mock_wiki, default_scope):
        provider = WikiProvider(mock_wiki)
        results = provider.candidates("test query", default_scope, limit=5)
        first = results[0]
        assert first.id == "overview"  # slug
        assert first.title == "Test Page: overview"
        assert first.content == "wiki page content about testing"
        assert first.native_score == pytest.approx(0.85)
        assert first.project_id == "/home/user/project"

    def test_candidates_raw_has_source_tag(self, mock_wiki, default_scope):
        provider = WikiProvider(mock_wiki)
        results = provider.candidates("test query", default_scope, limit=5)
        for c in results:
            assert c.raw.get("_source") == "wiki"

    def test_candidates_raw_preserves_page_data(self, mock_wiki, default_scope):
        provider = WikiProvider(mock_wiki)
        results = provider.candidates("test query", default_scope, limit=5)
        assert results[0].raw["slug"] == "overview"
        assert results[0].raw["title"] == "Test Page: overview"

    def test_candidates_skips_missing_id(self, default_scope):
        wiki = MagicMock()
        wiki.query.return_value = [
            {"content": "no id here"},  # no id
            _make_wiki_page("has-id", 0.6),
        ]
        provider = WikiProvider(wiki)
        results = provider.candidates("q", default_scope, limit=10)
        assert len(results) == 1
        assert results[0].id == "has-id"

    def test_candidates_uses_slug_as_id(self, default_scope):
        wiki = MagicMock()
        page = {"id": 99, "slug": "my-slug", "content": "c", "_retrieval_score": 0.5}
        wiki.query.return_value = [page]
        provider = WikiProvider(wiki)
        results = provider.candidates("q", default_scope, limit=5)
        assert results[0].id == "my-slug"

    def test_candidates_falls_back_to_id_when_no_slug(self, default_scope):
        wiki = MagicMock()
        page = {"id": 42, "content": "c", "_retrieval_score": 0.3}
        wiki.query.return_value = [page]
        provider = WikiProvider(wiki)
        results = provider.candidates("q", default_scope, limit=5)
        assert results[0].id == 42
