"""C10g (0047 PR#40 §5) — ``restore()``'s five sinks are routed per table.

C10f moved ``memorize``'s scope stamp: a new ``memory`` row now carries the
resolved **project_id** in ``directory_context``
(``backend/write_exec/_memorize_phases/_phase_store.py``,
``backend/curation/ingestion.py``), never the caller's ``context`` path. The
readers behind ``restore()`` were still handed filesystem paths, so the
hot-memories and anchor buckets matched zero rows for everything written after
that car — silently, because ``WHERE directory_context = $dir`` raises nothing
when it matches nothing.

These tests pin the routing that closes it. ``restore`` is a FAN-OUT and the
sinks are deliberately NOT uniform:

* ``memory``-backed sinks (anchors, hot memories, gap detection) take the
  **project_id**, because that is what the write path now stamps.
* ``checkpoint`` and ``memory_block`` keep taking the **path**, because neither
  table has a ``project_id`` column and ``memory_block``'s writer
  (``block_create(directory=…)``) is a C11-blocked site that still writes real
  paths. Flipping those two would make blocks vanish from every restore.

The last class is therefore as load-bearing as the first: it is what stops a
later car "finishing the job" by flipping the two sinks that must not move.
"""

from __future__ import annotations

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.restoration.contract import CheckpointContext
from yadgar._shared.storage import StorageEngine
from yadgar.backend.restoration.checkpoint_restore import CheckpointRestore

#: ADR-0227 deleted the derivation tier — every write here names its project.
_PROJECT = "m-agahi/yadgar"
_OTHER_PROJECT = "someone-else/other-repo"

#: The caller's filesystem path. Deliberately NOT equal to ``_PROJECT``: a test
#: that used the same string for both would pass under either routing.
_PATH = "/home/max/git/yadgar"


@pytest.fixture
def temp_db(tmp_path):
    # surrealkv needs a directory path (not an existing file)
    yield str(tmp_path / "test.db")


@pytest.fixture
def engines(temp_db):
    settings = Settings(DB_PATH=temp_db)
    storage = StorageEngine(temp_db)
    embeddings = EmbeddingEngine()
    replay = CheckpointRestore(
        storage=storage,
        embeddings=embeddings,
        settings=settings,
    )
    yield storage, embeddings, replay
    storage.close()


def _insert_memorize_shaped_row(storage, embeddings, content: str, project_id: str) -> int:
    """Insert the row shape ``memorize`` writes AFTER C10f.

    Mirrors ``_phase_store._direct_insert`` verbatim on the two fields this car
    is about: ``directory_context`` carries the resolved project_id, and
    ``project_id`` carries it too. Nothing here stores the caller's path.
    """
    return storage.insert_memory(
        {
            "content": content,
            "embedding": embeddings.encode(content),
            "tags": ["c10g"],
            "directory_context": project_id,
            "heat": 1.0,
            "is_stale": False,
            "file_hash": None,
            "embedding_model": embeddings.get_model_name(),
            "project_id": project_id,
        }
    )


#: ``REPLAY_MAX_RESTORE_MEMORIES`` (8) rows fill restore's step-3 "recently
#: stored" bucket, which is corpus-wide and takes no scope at all. Every row it
#: claims is then in ``exclude_ids`` and cannot appear in the hot bucket — so a
#: hot-bucket test on a handful of fresh rows measures NOTHING. Overfilling past
#: the cap is what pushes the oldest rows out of "recent" and into "hot", which
#: is the only way to exercise this sink end-to-end through ``restore``.
#: (That the recent bucket is unscoped is a separate pre-existing seam; it is
#: not one of the five sinks this car routes, and it takes no directory.)
_OVERFILL = 12


def _overfill(storage, embeddings, project_id: str, label: str) -> str:
    """Write ``_OVERFILL`` rows for one project; return the oldest one's text."""
    oldest = ""
    for i in range(_OVERFILL):
        content = f"{label} {i:02d}"
        if i == 0:
            oldest = content
        _insert_memorize_shaped_row(storage, embeddings, content, project_id)
    return oldest


class TestHotMemoriesBucket:
    """Sink 3 — ``_fetch_hot_memories`` → ``get_memories_for_directory``."""

    def test_project_stamped_row_reaches_the_hot_bucket(self, engines):
        """THE REGRESSION. Red before C10g: the bucket was handed ``_PATH``."""
        storage, embeddings, replay = engines
        oldest = _overfill(storage, embeddings, _PROJECT, "hot row for this project")

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["hot_memories"] >= 1, (
            "rows written by memorize(project=...) did not reach restore's hot "
            "bucket — the sink is still keyed on the caller's path"
        )
        assert oldest in result["formatted"]

    def test_another_projects_rows_are_not_in_the_hot_bucket(self, engines):
        """Routing must SCOPE, not merely stop being empty.

        Asserts on the COUNT only: the corpus-wide "recently stored" bucket
        will still surface these rows in the markdown, which is that other
        seam's business, not this sink's.
        """
        storage, embeddings, replay = engines
        _overfill(storage, embeddings, _OTHER_PROJECT, "someone elses row")

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["hot_memories"] == 0

    def test_absent_project_does_not_widen_to_the_whole_corpus(self, engines):
        """No project named → EMPTY bucket, never a corpus-wide one.

        Before C10g the no-scope branch fell through to
        ``get_memories_by_heat(HOT_THRESHOLD)``, and ``HOT_THRESHOLD`` defaults
        to ``0.0`` — i.e. every memory in the DB. That branch was near-dead
        while a path was almost always supplied; routing the sink onto
        project_id makes ``None`` the COMMON case for the two ``_forward_restore``
        callers that bypass the MCP tool (the post-compact HTTP hook and the
        CLI, which resolves its project non-fatally). A rare widening branch
        would have become the default one.

        ``hook_project_id`` states the governing rule: losing an injection is
        recoverable, leaking one is not.
        """
        storage, embeddings, replay = engines
        _overfill(storage, embeddings, _OTHER_PROJECT, "someone elses row")

        result = replay.restore(_PATH)

        assert result["hot_memories"] == 0


class TestAnchorBucket:
    """Sink 2 — the anchor STAMP and ``get_anchored_memories_scoped`` move together.

    C10f attempted this half and reverted it whole: moving either side alone
    makes every anchor unreachable, and the two fixing files belonged to
    then-live cars. Both halves are pinned here so a future car cannot move one.
    """

    def test_anchor_written_with_a_project_is_restored_with_that_project(self, engines):
        storage, embeddings, replay = engines
        replay.anchor_memory(
            content="Use React not Vue",
            context=_PATH,
            tags=["framework"],
            reason="Team decision",
            project_id=_PROJECT,
        )

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["anchored_memories"] >= 1
        assert "React" in result["formatted"]

    def test_the_anchor_stamp_is_the_project_not_the_context(self, engines):
        """The WRITE half, asserted directly on the stored row.

        Without this, a reader-only change could be "fixed" by flipping the
        reader back onto the path and the pair would still look green.
        """
        storage, embeddings, replay = engines
        mid = replay.anchor_memory(
            content="stamp check",
            context=_PATH,
            tags=["framework"],
            project_id=_PROJECT,
        )

        row = storage.get_memory(mid)
        assert row is not None
        assert row["directory_context"] == _PROJECT, (
            "anchor_memory still stamps the caller's context path; memorize "
            "stamps the project_id and the two writers must agree"
        )

    def test_another_projects_anchor_is_not_restored(self, engines):
        storage, embeddings, replay = engines
        replay.anchor_memory(
            content="not your anchor",
            context=_PATH,
            tags=["framework"],
            project_id=_OTHER_PROJECT,
        )

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["anchored_memories"] == 0
        assert "not your anchor" not in result["formatted"]


class TestPathKeyedSinksAreUnchanged:
    """Sinks 1 and 4 — ``checkpoint`` and ``memory_block`` still key on the PATH.

    Neither table has a ``project_id`` column (migration 031 declared it on
    ``wiki_page`` + ``memory`` only), and ``memory_block``'s writer still stores
    real paths. C11 owns moving them; until then, flipping them here would be a
    silent data-loss change, not a completion.
    """

    def test_checkpoint_still_resolves_by_path(self, engines):
        storage, embeddings, replay = engines
        replay.create_checkpoint(
            _PATH,
            CheckpointContext(
                current_task="Refactoring auth module",
                key_decisions=["Switch to JWT"],
            ),
        )

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["checkpoint"] is not None
        assert "Refactoring auth module" in result["formatted"]

    def test_drain_written_checkpoint_is_found_by_restore(self, engines):
        """The drain writer and the restore reader must stay symmetric."""
        storage, embeddings, replay = engines
        replay.pre_compact_drain(_PATH)

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["checkpoint"] is not None

    def test_checkpoint_is_not_looked_up_by_project_id(self, engines):
        """Flipping sink 1 would break this: the row is stored under the PATH."""
        storage, embeddings, replay = engines
        replay.create_checkpoint(_PATH, CheckpointContext(current_task="path-keyed"))

        assert storage.get_active_checkpoint(_PATH) is not None
        assert storage.get_active_checkpoint(_PROJECT) is None

    def test_memory_blocks_still_resolve_by_path(self, engines):
        storage, embeddings, replay = engines
        storage.create_block(
            name="current_task",
            content="block body written under a real path",
            scope="project",
            directory=_PATH,
        )

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["memory_blocks"] >= 1
        assert "block body written under a real path" in result["formatted"]
