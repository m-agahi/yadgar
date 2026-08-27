"""C308 — ``restore()``'s SR-prediction bucket (#5) must accept legacy rows.

The C10g fix put the scope on ``directory_context`` — but a 2237-of-2352
slice of the live corpus was written BEFORE that move and still has
filesystem paths in ``directory_context`` (e.g. ``/home/max/git/yadgar``),
not the project_id. The hot/anchor/recent buckets all read memory via
``get_memories_for_directory`` / ``get_anchored_memories_scoped`` /
``get_recent_memories`` — storage primitives that had the legacy arm wired
in long before C10g. The SR bucket is the one that does NOT go through
a primitive: it fetches rows by id (``get_memory(mid)``) and filters
inline at the consumer, with a ``mem.get("directory_context") != project_id``
check that compared a path string to a project_id string and dropped
the entire legacy corpus.

Pin the predicate widened: a legacy row whose ``directory_context`` is a
real filesystem path for the same project must pass. A foreign project's
row — legacy OR new — must still be dropped (no widening into a leak).
"""

from __future__ import annotations

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.storage import StorageEngine
from yadgar.backend.restoration.checkpoint_restore import CheckpointRestore

_PROJECT = "m-agahi/yadgar"
_OTHER_PROJECT = "someone-else/other-repo"
_PATH = "/home/max/git/yadgar"
_OTHER_PATH = "/home/max/git/other"


@pytest.fixture
def temp_db(tmp_path):
    yield str(tmp_path / "test.db")


@pytest.fixture
def engines(temp_db):
    Settings(DB_PATH=temp_db)
    storage = StorageEngine(temp_db)
    embeddings = EmbeddingEngine()
    yield storage, embeddings
    storage.close()


def _insert_legacy_row(storage, embeddings, content: str, directory_context: str) -> int:
    """Insert a row in the SHAPE the pre-C10f corpus actually has.

    ``directory_context`` is a real filesystem path; ``project_id`` is NOT
    set, because the column did not exist when these rows were written.
    The Car-5 sentinel guard refuses any write whose ``directory_context``
    is a path-shaped string instead of a project_id, which is the right
    rule for the live write path — so the helper inserts through the
    guarded path and then patches the row back to its legacy shape, which
    is what a re-read of the on-disk corpus looks like.
    """
    mid = storage.insert_memory(
        {
            "content": content,
            "embedding": embeddings.encode(content),
            "tags": ["legacy"],
            "directory_context": "global",
            "heat": 1.0,
            "is_stale": False,
            "file_hash": None,
            "embedding_model": embeddings.get_model_name(),
            "project_id": "global",
        }
    )
    # Restore the legacy shape: filesystem path on directory_context, no project_id.
    storage._q(
        f"UPDATE type::record('memory', {mid}) SET directory_context = $dc, project_id = NONE",
        {"dc": directory_context},
    )
    return mid


def _insert_stamped_row(storage, embeddings, content: str, project_id: str) -> int:
    """Insert a row in the SHAPE the post-C10f corpus has."""
    return storage.insert_memory(
        {
            "content": content,
            "embedding": embeddings.encode(content),
            "tags": ["c10f"],
            "directory_context": project_id,
            "heat": 1.0,
            "is_stale": False,
            "file_hash": None,
            "embedding_model": embeddings.get_model_name(),
            "project_id": project_id,
        }
    )


class _StubCognitiveMap:
    """Returns whatever ids it was handed, at max proximity.

    See ``test_c10g_restore_project_scope._StubCognitiveMap`` for why the
    stub is necessary: the real ``navigate_to`` walks a corpus-wide
    coordinate dict and the test would otherwise be exercising the SR
    walk, not the filter under repair.
    """

    def __init__(self, ids: list[int]):
        self._ids = ids

    def has_sufficient_data(self) -> bool:
        return True

    def navigate_to(self, query_embedding, embeddings_engine, top_k: int = 10):
        return [(mid, 0.9) for mid in self._ids[:top_k]]


class _StubEmbeddings:
    @staticmethod
    def encode(_text):
        return b"\x00\x00\x00\x00"


class TestSRBucketAcceptsLegacyRows:
    """Sink 5 — the consumer-side filter in ``_predict_memories``."""

    @staticmethod
    def _replay_with_map(storage, ids):
        return CheckpointRestore(
            storage=storage,
            embeddings=_StubEmbeddings(),
            settings=Settings(),
            cognitive_map=_StubCognitiveMap(ids),
        )

    def test_legacy_path_row_for_this_project_is_kept(self, engines):
        """THE REGRESSION. Red before C308: path vs project_id string compare failed."""
        storage, embeddings = engines
        legacy_mine = _insert_legacy_row(storage, embeddings, "my legacy row", _PATH)
        replay = self._replay_with_map(storage, [legacy_mine])

        predicted = replay._predict_memories(None, _PROJECT, set(), 8, _PATH)

        contents = [m["content"] for m in predicted]
        assert "my legacy row" in contents, (
            "the SR bucket dropped a legacy row whose directory_context is a "
            "filesystem path under this project — the predicate compared a "
            "path to a project_id string and refused every pre-C10f row"
        )

    def test_stamped_row_for_this_project_is_still_kept(self, engines):
        """The widened predicate must not regress the post-C10f arm."""
        storage, embeddings = engines
        stamped_mine = _insert_stamped_row(storage, embeddings, "my stamped row", _PROJECT)
        replay = self._replay_with_map(storage, [stamped_mine])

        predicted = replay._predict_memories(None, _PROJECT, set(), 8, _PATH)

        contents = [m["content"] for m in predicted]
        assert "my stamped row" in contents

    def test_legacy_path_row_for_another_project_is_dropped(self, engines):
        """A foreign project's legacy row must not leak through the widening."""
        storage, embeddings = engines
        foreign_legacy = _insert_legacy_row(storage, embeddings, "their legacy row", _OTHER_PATH)
        replay = self._replay_with_map(storage, [foreign_legacy])

        predicted = replay._predict_memories(None, _PROJECT, set(), 8, _PATH)

        contents = [m["content"] for m in predicted]
        assert "their legacy row" not in contents, (
            "the widened predicate admitted another project's row — the path "
            "match must check the row's directory_context was THIS project's path"
        )

    def test_stamped_row_for_another_project_is_dropped(self, engines):
        """The C10g arm's other-project guard must survive the widening."""
        storage, embeddings = engines
        foreign_stamped = _insert_stamped_row(
            storage, embeddings, "their stamped row", _OTHER_PROJECT
        )
        replay = self._replay_with_map(storage, [foreign_stamped])

        predicted = replay._predict_memories(None, _PROJECT, set(), 8, _PATH)

        contents = [m["content"] for m in predicted]
        assert "their stamped row" not in contents
