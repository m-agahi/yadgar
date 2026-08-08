"""Phase 3 wiki architecture tests.

Covers:
  - Relevance-gated wiki blending (threshold > 0.3, no positional interleave)
  - Episodic query detection skips wiki blending
  - Bidirectional memory↔wiki linking (wiki_refs on memories)
"""

import pytest

from yadgar._shared.storage import StorageEngine
from yadgar._shared.wiki import WikiAddOptions
from yadgar.core import server
from yadgar.core.server import _is_episodic_query
from yadgar.tests.conftest import memorize_sync

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wiki_phase3")
    server.init_engines(
        db_path=str(tmp_path / "p3_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _storage() -> StorageEngine:
    return server._get_storage()


def _wiki():
    return server._wiki


# ── A. Episodic query detection ───────────────────────────────────────────────


class TestIsEpisodicQuery:
    def test_temporal_keyword_yesterday(self):
        assert _is_episodic_query("what did I do yesterday") is True

    def test_temporal_keyword_today(self):
        assert _is_episodic_query("what happened today") is True

    def test_temporal_keyword_last_week(self):
        assert _is_episodic_query("notes from last week") is True

    def test_temporal_keyword_recently(self):
        assert _is_episodic_query("recently changed files") is True

    def test_temporal_keyword_ago(self):
        assert _is_episodic_query("two hours ago we fixed it") is True

    def test_non_episodic_query(self):
        assert _is_episodic_query("architecture of the storage engine") is False

    def test_non_episodic_query_technical(self):
        assert _is_episodic_query("how does the WRRF retrieval work") is False

    def test_case_insensitive(self):
        assert _is_episodic_query("What did I do YESTERDAY") is True

    def test_empty_query(self):
        assert _is_episodic_query("") is False


# ── B. Relevance-gated wiki blending ─────────────────────────────────────────


class TestWikiBlendingThreshold:
    def test_episodic_query_skips_wiki(self, recall_backend_bypass):
        """Temporal/episodic queries must NOT blend wiki results."""
        _wiki().add("Architecture Overview", "Core design of the system.", "architecture")
        server.memorize(content="Fixed a bug yesterday.", context="/tmp", tags=[])
        results = server.recall(query="what happened yesterday", max_results=5, directory="/tmp")
        wiki_hits = [r for r in results if r.get("_source") == "wiki"]
        assert len(wiki_hits) == 0

    def test_relevant_wiki_appears_in_results(self, flush_queue, recall_backend_bypass):
        """A wiki page with sufficient relevance should surface in recall."""
        _wiki().add(
            "Storage Engine Design",
            "The storage engine uses SurrealDB for persistence and WRRF for retrieval.",
            "architecture",
            opts=WikiAddOptions(confidence="high"),
        )
        server.memorize(
            content="Storage engine handles all persistence operations.",
            context="/tmp",
            tags=[],
        )
        flush_queue()
        results = server.recall(query="storage engine design", max_results=10, directory="/tmp")
        wiki_hits = [r for r in results if r.get("_source") == "wiki"]
        # At least one wiki result should be present (relevance gate passed)
        assert len(wiki_hits) >= 1

    def test_results_sorted_by_score(self, recall_backend_bypass):
        """Blended results must be sorted by _retrieval_score descending."""
        _wiki().add(
            "Test Architecture", "Key design decisions for the test system.", "architecture"
        )
        server.memorize(content="Test system architecture notes.", context="/tmp", tags=[])
        results = server.recall(query="test architecture", max_results=10, directory="/tmp")
        if len(results) >= 2:
            scores = [r.get("_retrieval_score", 0.0) for r in results]
            assert scores == sorted(scores, reverse=True)

    def test_blended_total_respects_max_results(self, recall_backend_bypass):
        """Output length must never exceed max_results."""
        for i in range(3):
            _wiki().add(
                f"Wiki Page {i}",
                f"Content about topic {i} with detailed technical information.",
                "reference",
            )
        for i in range(5):
            server.memorize(content=f"Memory {i} about topic {i}.", context="/tmp", tags=[])
        results = server.recall(query="topic", max_results=4, directory="/tmp")
        assert len(results) <= 4


# ── C. Bidirectional memory↔wiki linking ─────────────────────────────────────


class TestBidirectionalLinking:
    def test_add_links_source_memories(self):
        """wiki_add with source_memory_ids should update wiki_refs on each memory."""
        mem_result = memorize_sync(
            content="Designed the storage engine using SurrealDB.",
            context="/tmp",
            tags=[],
        )
        mid = mem_result.get("id")
        assert mid is not None

        _wiki().add(
            "Storage Design",
            "The storage engine uses SurrealDB.",
            "architecture",
            opts=WikiAddOptions(source_memory_ids=[mid]),
        )

        mem = _storage().get_memory(mid)
        refs = mem.get("wiki_refs") or []
        assert "storage-design" in refs

    def test_upsert_merges_source_memories_and_links(self):
        """Upserting a wiki page with new memory IDs links those memories too."""
        m1 = memorize_sync(content="First design note.", context="/tmp", tags=[])["id"]
        m2 = memorize_sync(content="Second design note.", context="/tmp", tags=[])["id"]

        _wiki().add("Design Notes", "Initial design.", opts=WikiAddOptions(source_memory_ids=[m1]))
        _wiki().add("Design Notes", "Updated design.", opts=WikiAddOptions(source_memory_ids=[m2]))

        mem2 = _storage().get_memory(m2)
        assert "design-notes" in (mem2.get("wiki_refs") or [])

    def test_ingest_links_source_memories_new_page(self):
        """wiki_ingest on a new page should link source memories."""
        mid = memorize_sync(content="Wrote the ingestion module.", context="/tmp", tags=[])["id"]
        _wiki().ingest("Notes about ingestion.", title="Ingestion Notes", source_memory_ids=[mid])
        mem = _storage().get_memory(mid)
        assert "ingestion-notes" in (mem.get("wiki_refs") or [])

    def test_ingest_links_source_memories_existing_page(self):
        """wiki_ingest on an existing page should also link new source memories."""
        _wiki().add("Existing Page", "Initial content.")
        mid = memorize_sync(content="Added to existing page.", context="/tmp", tags=[])["id"]
        _wiki().ingest("Update content.", title="Existing Page", source_memory_ids=[mid])
        mem = _storage().get_memory(mid)
        assert "existing-page" in (mem.get("wiki_refs") or [])

    def test_no_duplicate_refs(self):
        """Adding the same page twice should not duplicate wiki_refs."""
        mid = memorize_sync(content="Original note.", context="/tmp", tags=[])["id"]
        _wiki().add("My Page", "Content.", opts=WikiAddOptions(source_memory_ids=[mid]))
        _wiki().add("My Page", "Updated content.", opts=WikiAddOptions(source_memory_ids=[mid]))
        mem = _storage().get_memory(mid)
        refs = mem.get("wiki_refs") or []
        assert refs.count("my-page") == 1

    def test_nonexistent_memory_id_skipped(self):
        """Linking to a nonexistent memory ID must not raise."""
        _wiki().add("Safe Page", "Content.", opts=WikiAddOptions(source_memory_ids=[99999]))
        # No exception = pass

    def test_empty_source_memory_ids_no_error(self):
        """wiki_add with empty source_memory_ids must not raise."""
        _wiki().add("Standalone Page", "Content.", opts=WikiAddOptions(source_memory_ids=[]))
        # No exception = pass
