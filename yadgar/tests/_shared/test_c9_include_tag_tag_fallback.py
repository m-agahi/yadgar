"""Regression test for task #205 — tag-aware recall with NULL embeddings.

5 pre-existing red tests (4 in ``test_tag_aware_recall_s3`` + 1 in
``test_agent_prompt_discovery_s6::test_flag_on_recall_include_surfaces``)
all assert that ``recall(tags=["agent-prompt"])`` returns at least one
page. Two failure mechanics stack to produce the red:

    1. ``agent_prompt_save`` → ``wiki.add`` → ``_compute_embedding`` returns
       ``None`` when sentence-transformers is not installed (CI matrix
       without the ``ml`` extra, dev containers). The wiki row is stored
       with ``embedding = NULL``.

    2. ``recall(tags=["agent-prompt"])`` → ``WikiProvider.candidates`` →
       ``WikiStore.query(include_tag="agent-prompt")``. The include_tag
       dispatch arm runs ONLY the vector-tagged arm
       (``_collect_scores_dispatch``); vector returns nothing when both
       the query embedding is None and every page has NULL embedding.

The FTS arm would not save this — FTS is unavailable in embedded mode
(SurrealDB has no FULLTEXT index unless the schema migration runs with a
remote ``_db_url``), and the test fixtures use embedded storage. So
neither vector nor FTS produces scores, and the include_tag path returns
empty results even though a perfectly good page with the requested tag
is sitting in the corpus.

Car C9 (#205): when the include_tag dispatch arm produces zero scores
AND both the query embedding and the corpus pages are NULL-embedded,
fall back to a TAG-ONLY SQL query — ``SELECT id FROM wiki_page WHERE
tags CONTAINS $tag AND (scope)``. The caller asked for "all pages with
tag X" and a tag-only match IS the answer when there is no signal to
rank against. This is the user's INTENT on the include_tag path (ADR-
0007 library discovery), and the previous behaviour silently dropped
the entire result set instead of degrading to tag equality.

The fix is local to ``WikiStore._collect_scores_dispatch`` and only
fires when both fallback signals are absent (vector returned empty AND
embedder unavailable). When the embedder IS available, the existing
hybrid ranking is preserved unchanged.

Test surfaces the bug at the WikiStore layer with a controlled
embedder that always returns None — same shape as the dev-container
environment the original 5 tests fail under.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar._shared.storage import StorageEngine
from yadgar._shared.storage.directory import RecallScope
from yadgar._shared.wiki.store import WikiStore


@pytest.fixture()
def storage(tmp_path):
    s = StorageEngine.__new__(StorageEngine)
    s._q = MagicMock(return_value=[])
    s._rows_to_dicts = MagicMock(return_value=[])
    # Vector-tagged path returns empty (embedder unavailable case).
    s.search_wiki_vectors_tagged = MagicMock(return_value=[])
    s.get_wiki_page = MagicMock(return_value=None)
    return s


@pytest.fixture()
def wiki_store_null_embeddings(storage):
    """WikiStore with mocked storage + NULL-returning embeddings (embed-unavailable case)."""
    embeddings = MagicMock()
    embeddings.encode_query.return_value = None
    embeddings.encode.return_value = None
    embeddings.encode_document.return_value = None
    embeddings.get_model_name.return_value = "all-MiniLM-L6-v2"
    return WikiStore(storage, embeddings)


def test_include_tag_falls_back_to_tag_only_when_embedding_unavailable(
    wiki_store_null_embeddings, storage
):
    """Regression #205: include_tag path MUST run a tag-only SQL when embeddings are NULL.

    Without this fallback, ``recall(tags=["agent-prompt"])`` returns
    nothing on a corpus written through an embedder-less deployment
    even when the page has the requested tag — silent recall death.
    """
    page_id = 42

    # The tag-only fallback should issue a SELECT against wiki_page that
    # filters by tag membership + scope, NOT requiring any embedding.
    def fake_q(surql: str, params=None):
        if "tags CONTAINS" in surql:
            return [{"id": page_id}]
        return []

    storage._q.side_effect = fake_q

    wiki_store_null_embeddings.query(
        query="audit this pull request for vulnerabilities",
        tags=["agent-prompt"],
        include_tag="agent-prompt",
        scope=RecallScope(project_id="owner/repo", opt_in_tags=["agent-prompt"], unscoped=False),
    )

    # The dispatch must have reached a tag-only SELECT — the include_tag
    # path must not silently return [] on an embedder-less corpus.
    tag_queries = [call for call in storage._q.call_args_list if "tags CONTAINS" in call.args[0]]
    assert tag_queries, (
        "include_tag dispatch skipped tag-only fallback — recall(tags=[X]) "
        "returns nothing on an embedder-less corpus even on tag matches."
    )
    # Vector-tagged arm short-circuited on None embedding — the storage
    # call is NEVER reached when query_embedding is None (the guard in
    # _collect_wiki_vector_scores_tagged:1066). The dispatch then reaches
    # the tag-only fallback path. When the embedder IS available, the
    # vector arm runs first; when it isn't, the tag-only fallback runs
    # directly. Both behaviors are correct.
    storage.search_wiki_vectors_tagged.assert_not_called()


def test_include_tag_keeps_vector_when_embedding_available(wiki_store_null_embeddings, storage):
    """Sanity check: when embeddings work, the include_tag path uses vector.

    Anti-regression guard so the tag-only fallback never replaces the
    semantic path when it can produce results — it's a FALLBACK, not
    a replacement.
    """
    wiki_store_null_embeddings._embeddings.encode_query.return_value = b"\x00" * 1536
    storage.search_wiki_vectors_tagged.return_value = []

    wiki_store_null_embeddings.query(
        query="audit this pull request for vulnerabilities",
        tags=["agent-prompt"],
        include_tag="agent-prompt",
        scope=RecallScope(project_id="owner/repo", opt_in_tags=["agent-prompt"], unscoped=False),
    )

    # The vector-tagged arm must have been called when embeddings are
    # available — even though the test doesn't expect any matches here,
    # the path must run before the fallback.
    assert storage.search_wiki_vectors_tagged.called, (
        "include_tag path skipped vector search even with embeddings available"
    )
