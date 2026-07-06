"""Unit tests for v6 T6 Step 1 — SourceProvider abstraction.

Coverage:
  1. MemoryProvider.candidates() wraps Retriever.recall() → normalized Candidates
  2. WikiProvider.candidates() wraps WikiStore.query() → normalized Candidates
  3. Candidate fields (type, id, title, content, native_score, directory_context, branch, raw)
  4. Scope carries directory + branch fields
  5. MemoryProvider type == "memory", WikiProvider type == "wiki"
  6. No calls to recall() or wiki_query() MCP tools — providers are pure extraction
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar.retrieval.providers.base import Candidate, Scope, SourceProvider
from yadgar.retrieval.providers.memory import MemoryProvider
from yadgar.retrieval.providers.wiki import WikiProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_memory_dict(mid: int = 1, score: float = 0.8) -> dict:
    return {
        "id": mid,
        "content": f"memory content {mid}",
        "heat": 0.6,
        "_retrieval_score": score,
        "directory_context": "/home/user/project",
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
        "directory_context": "/home/user/project",
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
        directory="/home/user/project",
        branch="feat/test",
        default_branch="master",
        min_heat=0.0,
    )


# ---------------------------------------------------------------------------
# 1. Scope dataclass
# ---------------------------------------------------------------------------


class TestScope:
    def test_scope_fields(self):
        scope = Scope(
            directory="/project",
            branch="main",
            default_branch="main",
            min_heat=0.1,
        )
        assert scope.directory == "/project"
        assert scope.branch == "main"
        assert scope.default_branch == "main"
        assert scope.min_heat == 0.1

    def test_scope_optional_defaults(self):
        scope = Scope(directory="/project")
        assert scope.branch is None
        assert scope.default_branch is None
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
            directory_context="/project",
            branch="main",
            raw=raw,
        )
        assert c.type == "memory"
        assert c.id == 1
        assert c.title is None
        assert c.content == "hello"
        assert c.native_score == 0.9
        assert c.directory_context == "/project"
        assert c.branch == "main"
        assert c.raw is raw

    def test_candidate_wiki_type(self):
        c = Candidate(
            type="wiki",
            id="overview-slug",
            title="Overview",
            content="Wiki content here",
            native_score=0.7,
            directory_context=None,
            branch=None,
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
        mock_retriever.recall.assert_called_once_with(
            "test query",
            max_results=10,
            min_heat=0.0,
            current_branch="feat/test",
            default_branch="master",
            profile=None,
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
        assert first.directory_context == "/home/user/project"
        assert first.branch == "master"

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
        mem = {"id": 5, "content": "c", "heat": 0.4, "directory_context": None, "branch": None}
        retriever.recall.return_value = [mem]
        provider = MemoryProvider(retriever)
        results = provider.candidates("q", default_scope, limit=5)
        assert results[0].native_score == pytest.approx(0.4)

    def test_candidates_scope_min_heat_forwarded(self, mock_retriever):
        scope = Scope(directory="/p", min_heat=0.3)
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
        mock_wiki.query.assert_called_once_with(
            "test query", max_results=5, include_tag=None, exclude_tags=None
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
        assert first.directory_context == "/home/user/project"
        assert first.branch == "master"

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
