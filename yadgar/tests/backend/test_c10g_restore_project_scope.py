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
* ``checkpoint`` and ``memory_block`` took the **path** when C10g wrote this,
  because neither table had a ``project_id`` column and ``memory_block``'s
  writer (``block_create(directory=…)``) was a C11-blocked site that still
  wrote real paths. Flipping those two then would have made blocks vanish from
  every restore.

**C11 (migration 033) DISCHARGED that block, and the last class now pins the NEW
boundary rather than the old one.** Both writers moved (``create_block`` and
``insert_checkpoint`` stamp ``project_id``), so both sinks moved with them —
C10g's rule was "a sink moves only when its WRITER has already moved", and it
has. What the class asserts now is the half C11 added to that rule: **a sink
moves onto a new key ALONE only when something makes the OLD rows reachable, and
nothing does here.** ``project_backfill._TABLES`` is ``("memory", "wiki_page")``
and plan §8 defines no backfill step for ``checkpoint`` or ``memory_block``, so
both reads keep a transitional legacy arm. Dropping it would not be the bounded
degraded window §8 5b sanctions for memory/wiki — it would be permanent silent
loss of every checkpoint and every user-curated block written before this car.

The class is therefore still as load-bearing as the first: it is what stops a
later car "finishing the job" by deleting the legacy arm before the drop PR
retires the column it reads.
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
#: stored" bucket. Every row it claims is then in ``exclude_ids`` and cannot
#: appear in the hot bucket — so a hot-bucket test on a handful of fresh rows
#: measures NOTHING. Overfilling past the cap is what pushes the oldest rows out
#: of "recent" and into "hot", which is the only way to exercise this sink
#: end-to-end through ``restore``. The overfill is still needed AFTER the recent
#: bucket became project-scoped: the exclusion is within one project, so an
#: in-project row still has to be pushed past the cap to reach ``hot``.
#:
#: **The comment that used to sit here said the recent bucket "is corpus-wide
#: and takes no scope at all", and treated that as a separate seam this car did
#: not own.** It was the documentation half of a live cross-project leak: step 3
#: called ``get_recent_memories(limit=...)`` with no project, against a callee
#: whose WHERE had no project predicate, and rendered the result into
#: ``## Working Memory (Recently Stored)`` on EVERY restore. Restoring
#: ``/home/max/git/nix`` returned quinyx/ai and quinyx/application-gitops rows.
#: ``TestRecentMemoriesBucket`` below is what stops it coming back.
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

        **This test used to assert on the COUNT only, and said so:** its own
        docstring excused the omission because "the corpus-wide 'recently
        stored' bucket will still surface these rows in the markdown, which is
        that other seam's business, not this sink's". A guard that watches one
        counter while the payload it is guarding carries the other project's
        text is vacuous — the markdown IS the thing injected into a session.
        The ``formatted`` assertion is the one that would have caught the leak.
        """
        storage, embeddings, replay = engines
        _overfill(storage, embeddings, _OTHER_PROJECT, "someone elses row")

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["hot_memories"] == 0
        assert "someone elses row" not in result["formatted"], (
            "another project's rows reached the restored markdown — a bucket "
            "count of 0 does not mean the payload is clean"
        )

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
        assert "someone elses row" not in result["formatted"], (
            "a restore that resolved NO project still rendered another "
            "project's memories — no scope must mean empty, never corpus-wide"
        )


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
    """Sinks 1 and 4 — ``checkpoint`` and ``memory_block``: project_id + a legacy arm.

    **Rewritten by C11, not deleted.** C10g pinned these two sinks to the PATH
    because their tables had no ``project_id`` column; migration 033 added it
    and both writers now stamp it, so the sinks moved. Every test below is kept
    because the property it guards is kept: a row written under a real PATH must
    STILL restore. That is now the transitional second arm rather than the only
    arm, and it is the thing a later car is most likely to delete — nothing
    backfills either table, so deleting it silently orphans the whole historical
    corpus. ``TestProjectIdArmOnTheReKeyedSinks`` below adds the other half.
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

    def test_an_unstamped_checkpoint_is_reachable_by_path_and_not_by_identity(self, engines):
        """INVERTED IN PLACE by C11, not deleted — and it still fails for its reason.

        C10g asserted the row is stored under the PATH and is invisible to a
        project_id lookup, to catch a premature flip of sink 1. That is still
        exactly what a checkpoint written WITHOUT a project (the pre-C11 corpus,
        and any caller that names none) does — so the assertion survives with
        its meaning intact: the legacy arm is what finds it, the identity arm
        cannot, and if a later car deleted the legacy arm this goes red.

        The companion — a STAMPED checkpoint IS found by identity — is in
        ``TestProjectIdArmOnTheReKeyedSinks``.
        """
        storage, embeddings, replay = engines
        replay.create_checkpoint(_PATH, CheckpointContext(current_task="path-keyed"))

        assert storage.get_active_checkpoint(_PATH) is not None
        assert storage.get_active_checkpoint(_PROJECT) is None
        # And the two-arm read finds it, which is what restore() actually issues.
        assert storage.get_active_checkpoint(_PATH, project_id=_PROJECT) is not None

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


class TestProjectIdArmOnTheReKeyedSinks:
    """C11 — sinks 1 and 4 now resolve by ``project_id`` too, and do not leak.

    The other half of the rewritten boundary. ``TestPathKeyedSinksAreUnchanged``
    proves the LEGACY arm still finds pre-C11 rows; these prove the IDENTITY arm
    finds post-C11 ones and that neither arm admits another project.

    Each test uses a caller ``directory`` that deliberately does NOT match the
    row's stored path, so only the ``project_id`` arm can satisfy it. A test that
    reused ``_PATH`` for both would pass under either routing and prove nothing.
    """

    _ELSEWHERE = "/some/other/checkout"

    def test_a_stamped_checkpoint_is_found_by_identity_alone(self, engines):
        storage, embeddings, replay = engines
        replay.create_checkpoint(
            _PATH,
            CheckpointContext(current_task="stamped task"),
            project_id=_PROJECT,
        )

        result = replay.restore(self._ELSEWHERE, project_id=_PROJECT)

        assert result["checkpoint"] is not None
        assert "stamped task" in result["formatted"]

    def test_a_stamped_block_is_found_by_identity_alone(self, engines):
        storage, embeddings, replay = engines
        storage.create_block(
            name="current_task",
            content="block body found by identity",
            scope="project",
            directory=_PATH,
            project_id=_PROJECT,
        )

        result = replay.restore(self._ELSEWHERE, project_id=_PROJECT)

        assert result["memory_blocks"] >= 1
        assert "block body found by identity" in result["formatted"]

    def test_another_projects_checkpoint_is_not_restored(self, engines):
        storage, embeddings, replay = engines
        replay.create_checkpoint(
            "/home/max/git/other",
            CheckpointContext(current_task="not your checkpoint"),
            project_id=_OTHER_PROJECT,
        )

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["checkpoint"] is None

    def test_another_projects_block_is_not_restored(self, engines):
        storage, embeddings, replay = engines
        storage.create_block(
            name="current_task",
            content="not your block",
            scope="project",
            directory="/home/max/git/other",
            project_id=_OTHER_PROJECT,
        )

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["memory_blocks"] == 0
        assert "not your block" not in result["formatted"]

    def test_the_drain_stamps_the_auto_checkpoint(self, engines):
        """The drain writer and the restore reader must stay symmetric on BOTH arms.

        ``pre_compact_drain`` is the highest-volume checkpoint producer (every
        compaction). If it kept writing unstamped rows while restore read the
        identity arm first, the symmetry C10g's sink-1 note depends on would
        hold only by accident of the legacy arm.
        """
        storage, embeddings, replay = engines
        replay.pre_compact_drain(_PATH, project_id=_PROJECT)

        row = storage.get_active_checkpoint("", project_id=_PROJECT)
        assert row is not None
        assert row.get("project_id") == _PROJECT


class TestRecentMemoriesBucket:
    """Sink 3b — ``_fetch_recent_memories_safe`` → ``get_recent_memories``.

    THE LEAK THIS CAR CLOSES. Step 3 of ``restore`` called
    ``get_recent_memories(limit=max_memories)`` with no project, against a callee
    that had no project parameter to receive one, and rendered the result into
    ``## Working Memory (Recently Stored)``. It fired on EVERY restore — with or
    without a resolved identity — so it was the one sink that leaked even when
    every other sink correctly returned empty.

    The predicate added below is on ``directory_context``, NOT the separate
    ``project_id`` column, for the same reason ``get_memories_for_directory``
    states: C10f moved ``memorize``'s stamp onto ``directory_context``, so that
    column is where the identity lives for every row written since. Re-keying
    onto ``project_id`` here would strand the whole post-C10f corpus.
    """

    def test_another_projects_rows_are_not_in_the_recent_bucket(self, engines):
        storage, embeddings, replay = engines
        _overfill(storage, embeddings, _OTHER_PROJECT, "someone elses recent row")

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["recent_memories"] == 0
        assert "someone elses recent row" not in result["formatted"]

    def test_this_projects_rows_do_reach_the_recent_bucket(self, engines):
        """Scoping must not empty the bucket for its OWN project."""
        storage, embeddings, replay = engines
        _insert_memorize_shaped_row(storage, embeddings, "my own recent row", _PROJECT)

        result = replay.restore(_PATH, project_id=_PROJECT)

        assert result["recent_memories"] >= 1
        assert "my own recent row" in result["formatted"]

    def test_absent_project_yields_an_empty_recent_bucket(self, engines):
        """No project named → EMPTY, never corpus-wide.

        Same posture as ``_fetch_hot_memories``: the two ``_forward_restore``
        callers that bypass the MCP tool (the post-compact HTTP hook and the
        CLI) resolve their project non-fatally, so ``None`` is a COMMON case
        here, not a rare one.
        """
        storage, embeddings, replay = engines
        _overfill(storage, embeddings, _OTHER_PROJECT, "someone elses recent row")

        result = replay.restore(_PATH)

        assert result["recent_memories"] == 0
        assert "someone elses recent row" not in result["formatted"]

    def test_the_storage_primitive_still_supports_an_unscoped_read(self, engines):
        """``get_recent_memories(project_id=None)`` stays corpus-wide.

        The empty-on-no-scope rule belongs to the RESTORE sink, not to the
        storage primitive — the same split ``get_recent_memories_since`` already
        uses, where ``directory=None`` means "all directories". Pinning it stops
        a later car pushing restore's policy down into a general-purpose read
        other callers may legitimately want whole.
        """
        storage, embeddings, _replay = engines
        _insert_memorize_shaped_row(storage, embeddings, "row a", _PROJECT)
        _insert_memorize_shaped_row(storage, embeddings, "row b", _OTHER_PROJECT)

        rows = storage.get_recent_memories(limit=10)

        assert {r["directory_context"] for r in rows} == {_PROJECT, _OTHER_PROJECT}

    def test_the_storage_primitive_filters_when_given_a_project(self, engines):
        storage, embeddings, _replay = engines
        _insert_memorize_shaped_row(storage, embeddings, "row a", _PROJECT)
        _insert_memorize_shaped_row(storage, embeddings, "row b", _OTHER_PROJECT)

        rows = storage.get_recent_memories(limit=10, project_id=_PROJECT)

        assert [r["content"] for r in rows] == ["row a"]


class _StubCognitiveMap:
    """Minimal SR map: returns whatever ids it was handed, at max proximity.

    ``CognitiveMap.navigate_to`` walks a corpus-wide coordinate dict (every
    memory that made it into the SR matrix), and the ``search_vectors`` call
    that seeds it takes the unscoped HNSW-KNN arm. Neither is project-aware and
    neither can be made so without re-keying the SR matrix itself — so the
    filter belongs at the ``_predict_memories`` consumer. This stub stands in
    for that corpus-wide walk so the test measures the FILTER, not the map.
    """

    def __init__(self, ids: list[int]):
        self._ids = ids

    def has_sufficient_data(self) -> bool:
        return True

    def navigate_to(self, query_embedding, embeddings_engine, top_k: int = 10):
        return [(mid, 0.9) for mid in self._ids[:top_k]]


class _StubEmbeddings:
    """``encode`` that always yields bytes.

    The real engine returns ``None`` when ``sentence-transformers`` is absent,
    and ``_predict_memories`` early-returns on that — which would make the SR
    tests below pass vacuously in exactly the environment they run in.
    """

    @staticmethod
    def encode(_text):
        return b"\x00\x00\x00\x00"


class TestPredictedMemoriesBucket:
    """Sink 5 — ``_predict_memories`` (SR cognitive-map navigation).

    ``navigate_to`` walks the whole SR coordinate space and ``get_memory(mid)``
    is a bare ``SELECT * FROM memory:{id}`` with no predicate, so every id the
    map offered was hydrated and rendered regardless of owner.

    Accepted consequence of filtering at the consumer: other projects' ids still
    consume the ``top_k`` budget, so this bucket can come back short or empty
    even when in-project rows exist. That is degradation, not leakage — the
    module's own rule is that losing an injection is recoverable and leaking one
    is not. No test here asserts a specific predicted COUNT.
    """

    @staticmethod
    def _replay_with_map(storage, ids):
        return CheckpointRestore(
            storage=storage,
            embeddings=_StubEmbeddings(),
            settings=Settings(),
            cognitive_map=_StubCognitiveMap(ids),
        )

    def test_another_projects_predicted_row_is_dropped(self, engines):
        storage, embeddings, _replay = engines
        mine = _insert_memorize_shaped_row(storage, embeddings, "my predicted row", _PROJECT)
        theirs = _insert_memorize_shaped_row(
            storage, embeddings, "their predicted row", _OTHER_PROJECT
        )
        replay = self._replay_with_map(storage, [theirs, mine])

        predicted = replay._predict_memories(None, _PROJECT, set(), 8)

        contents = [m["content"] for m in predicted]
        assert "their predicted row" not in contents
        assert "my predicted row" in contents

    def test_no_project_predicts_nothing_even_with_a_checkpoint_task(self, engines):
        """The guard is load-bearing precisely BECAUSE of the checkpoint arm.

        ``_build_sr_query`` returns the checkpoint's ``current_task`` when there
        is one, so it yields a non-empty query even with no project — an
        early return keyed only on the query string would let the SR path run
        unscoped for every restore that had a checkpoint.
        """
        storage, embeddings, _replay = engines
        theirs = _insert_memorize_shaped_row(
            storage, embeddings, "their predicted row", _OTHER_PROJECT
        )
        replay = self._replay_with_map(storage, [theirs])

        predicted = replay._predict_memories({"current_task": "some task"}, "", set(), 8)

        assert predicted == []
