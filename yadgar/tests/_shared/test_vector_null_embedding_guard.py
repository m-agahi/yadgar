"""Car F1 — the scoped vector arms must survive an ``embedding = NULL`` row.

THE BUG (CI run 31567583261, PR #40, introduced by C7 ``a33401c0``):

    RuntimeError: SurrealDB error: Incorrect arguments for function
    vector::similarity::cosine(). Argument 1 was the wrong type.
    Expected `array<number>` but found `NULL`

C7's scoped arm already carried ``embedding IS NOT NONE`` — and the crash
happened anyway. The reason is that **SurrealDB's NONE and NULL are different
values**, and only the first is excluded by that guard. Probed directly against
a scratch engine (server transport, the shape CI runs):

    row                      is_none  is_null  `IS NOT NONE`
    embedding omitted         True     False   False   (excluded)
    embedding = NONE          True     False   False   (excluded)
    embedding = NULL          False    True    True    (ADMITTED → crash)
    embedding = $p, p=None    False    True    True    (ADMITTED → crash)
    embedding = <vector>      False    False   True    (admitted, correct)

The last-but-one row is the writer seam: over the HTTP transport a Python
``None`` bound as a query parameter serialises to JSON ``null`` and lands as
SurrealDB NULL, not NONE. ``_shared/storage/wiki.py::
get_wiki_pages_without_embedding`` documented this years before C7 was written:

    "SurrealDB distinguishes NONE (field absent) from null (explicit null).
     We catch both: rows inserted via JSON params receive null (not NONE)
     when embedding=None is passed."

The converse also holds and is why ONE guard is not enough: ``IS NOT NULL``
alone ADMITS a NONE row (confirmed by the same probe, and independently by the
comment at ``backend/graph/graph_api.py:320``). Both arms are load-bearing.

The KNN arms are NOT affected — the HNSW index simply never offers a
non-array row as a neighbour (probed: a corpus containing the NULL row above
returns only the real-vector row from ``<|40, 40|>``, no error). That is why
this only bites the brute-force shapes C7 introduced.

Two independent tests, because the read guard and the writer fix each hide the
other's regression:

  * ``TestScopedVectorSearchSurvivesNullEmbedding`` seeds the NULL row with
    RAW SQL, so it holds even after the writers stop minting NULL.
  * ``TestWritersDoNotMintNullEmbeddings`` goes through the real writer and
    asserts the stored value is NONE.
"""

from __future__ import annotations

import pytest

_MODEL = "all-MiniLM-L6-v2"
_DIR = "/tmp/f1-null-embedding-guard"
_PROJECT = "f1-owner/f1-repo"


@pytest.fixture(scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("f1_null_embedding_guard")
    from yadgar.core import server

    server.init_engines(db_path=str(tmp_path / "test.db"), embedding_model=_MODEL)
    yield
    server.shutdown()


@pytest.fixture
def storage(_engines):
    from yadgar._shared.runtime.lifecycle import _get_storage

    return _get_storage()


def _query_vector(storage) -> bytes:
    from yadgar._shared.runtime.lifecycle import _get_embeddings

    return _get_embeddings().encode("null embedding guard probe")


def _scope(project_id: str = _PROJECT) -> tuple[str, dict]:
    from yadgar._shared.storage.directory import build_project_scope_clause

    return build_project_scope_clause(project_id)


class TestScopedVectorSearchSurvivesNullEmbedding:
    """The scoped (brute-force cosine) arms must not hand NULL to ``cosine()``.

    The NULL row is created with RAW SQL on purpose: seeding it through
    ``insert_memory`` would make this test depend on the writer defect, so
    reverting the read guard alone would leave it green once the writers are
    fixed — a mutation-check that proves nothing.
    """

    def _seed_null_memory(self, storage, rid: int, content: str) -> None:
        storage._q(
            f"CREATE memory:{rid} SET content = $c, heat = 1.0, is_stale = false, "
            f"directory_context = $d, project_id = $p, tags = [], embedding = NULL",
            {"c": content, "d": _DIR, "p": _PROJECT},
        )

    def test_search_vectors_scoped_ignores_null_embedding_rows(self, storage):
        self._seed_null_memory(storage, 8801, "f1 null row memory arm")
        scope_sql, scope_params = _scope()

        # Must not raise "Expected `array<number>` but found `NULL`".
        results = storage.search_vectors(
            _query_vector(storage),
            top_k=10,
            min_heat=0.0,
            scope_sql=scope_sql,
            scope_params=scope_params,
        )

        assert 8801 not in [mid for mid, _ in results]

    def test_search_wiki_vectors_scoped_ignores_null_embedding_rows(self, storage):
        storage._q(
            "CREATE wiki_page:8802 SET slug = 'f1-null-row', title = 'f1 null row', "
            "content = 'f1 null row wiki arm', tags = [], links = [], "
            "directory_context = $d, project_id = $p, embedding = NULL",
            {"d": _DIR, "p": _PROJECT},
        )
        scope_sql, scope_params = _scope()

        results = storage.search_wiki_vectors(
            _query_vector(storage),
            top_k=10,
            scope_sql=scope_sql,
            scope_params=scope_params,
        )

        assert 8802 not in [pid for pid, _ in results]

    def test_search_wiki_vectors_tagged_ignores_null_embedding_rows(self, storage):
        """``search_wiki_vectors_tagged`` is brute-force UNCONDITIONALLY.

        The other two arms only take the cosine-projection shape when a
        ``scope_sql`` is supplied; this one has no KNN fallback at all, so it is
        exposed on every call — with or without a scope.
        """
        storage._q(
            "CREATE wiki_page:8803 SET slug = 'f1-null-tagged', title = 'f1 null tagged', "
            "content = 'f1 null row tagged arm', tags = ['f1-null-tag'], links = [], "
            "directory_context = $d, project_id = $p, embedding = NULL",
            {"d": _DIR, "p": _PROJECT},
        )

        results = storage.search_wiki_vectors_tagged(
            _query_vector(storage),
            include_tag="f1-null-tag",
            top_k=10,
        )

        assert 8803 not in [pid for pid, _ in results]

    def test_real_vector_row_still_surfaces(self, storage):
        """The guard must exclude NULL rows WITHOUT excluding real ones.

        A guard that returns nothing also "does not crash" — this is the arm
        that makes the tests above mean something.
        """
        from yadgar._shared.runtime.lifecycle import _get_embeddings

        content = "f1 guard positive control unique token zephyrine"
        emb = _get_embeddings().encode(content)
        mid = storage.insert_memory(
            {
                "content": content,
                "embedding": emb,
                "directory_context": _DIR,
                "project_id": _PROJECT,
                "tags": [],
                "heat": 1.0,
            }
        )
        scope_sql, scope_params = _scope()

        results = storage.search_vectors(
            emb, top_k=10, min_heat=0.0, scope_sql=scope_sql, scope_params=scope_params
        )

        assert mid in [m for m, _ in results]

    def test_wiki_page_written_with_an_embedding_still_stores_the_array(self, storage):
        """Positive control for the WIKI writer specifically.

        ``insert_wiki_page`` binds its embedding through a conditional splat
        (``**({"embedding": ...} if ... else {})``) so the key is absent on the
        NONE path. If that condition were inverted or the key dropped outright,
        EVERY new wiki page would silently store no embedding and the corpus
        would go unsearchable — a failure the NONE-path test above cannot see.
        """
        from yadgar._shared.runtime.lifecycle import _get_embeddings

        pid = storage.insert_wiki_page(
            {
                "slug": "f1-writer-with-embedding",
                "title": "f1 writer wiki positive control",
                "content": "an embedding was supplied",
                "embedding": _get_embeddings().encode("an embedding was supplied"),
                "directory_context": _DIR,
                "project_id": _PROJECT,
                "tags": [],
            }
        )

        rows = storage._q(
            f"SELECT embedding IS NONE AS is_none, embedding IS NULL AS is_null, "
            f"array::len(embedding) AS n FROM wiki_page:{pid}"
        )

        assert rows and rows[0]["is_none"] is False
        assert rows[0]["is_null"] is False
        assert rows[0]["n"] == storage._embedding_dim


class TestWritersDoNotMintNullEmbeddings:
    """A row written without an embedding must store NONE, never NULL.

    Independent of the read guard: this fails if the writer binds Python
    ``None`` into an ``embedding = $p`` parameter, whatever the read side does.
    """

    def _embedding_state(self, storage, table: str, rid: int) -> dict:
        rows = storage._q(
            f"SELECT embedding IS NONE AS is_none, embedding IS NULL AS is_null FROM {table}:{rid}"
        )
        assert rows, f"{table}:{rid} not found"
        return rows[0]

    def test_insert_memory_without_embedding_stores_none_not_null(self, storage):
        mid = storage.insert_memory(
            {
                "content": "f1 writer memory arm, no embedding",
                "directory_context": _DIR,
                "project_id": _PROJECT,
                "tags": [],
                "heat": 1.0,
            }
        )

        state = self._embedding_state(storage, "memory", mid)

        assert state["is_none"] is True
        assert state["is_null"] is False

    def test_insert_wiki_page_without_embedding_stores_none_not_null(self, storage):
        pid = storage.insert_wiki_page(
            {
                "slug": "f1-writer-no-embedding",
                "title": "f1 writer wiki arm",
                "content": "no embedding supplied",
                "directory_context": _DIR,
                "project_id": _PROJECT,
                "tags": [],
            }
        )

        state = self._embedding_state(storage, "wiki_page", pid)

        assert state["is_none"] is True
        assert state["is_null"] is False

    def test_update_memory_compression_clearing_embedding_stores_none_not_null(self, storage):
        from yadgar._shared.runtime.lifecycle import _get_embeddings

        mid = storage.insert_memory(
            {
                "content": "f1 compression arm seed",
                "embedding": _get_embeddings().encode("f1 compression arm seed"),
                "directory_context": _DIR,
                "project_id": _PROJECT,
                "tags": [],
                "heat": 1.0,
            }
        )

        storage.update_memory_compression(mid, "f1 compressed", None, 1)

        state = self._embedding_state(storage, "memory", mid)
        assert state["is_none"] is True
        assert state["is_null"] is False
