"""v5.46.6 — empty-string directory_context: the normalisation is DELETED.

ORIGINAL SUBJECT: ``insert_memory`` coerced ``directory_context=''`` to
``'global'`` at write time, so SurrealDB equality queries (``= \'\'``) could not
break anchor surfacing in embedded mode, where empty-string round-trips are
unreliable in SurrealDB 2.x.

C5 (0047 PR#40, ADR-0227) removed the premise on three axes. The file is
INVERTED rather than deleted — it is the only coverage of what happens to a
write that names no directory, and that question did not go away when the
answer changed:

* The COERCION is gone. ``'global'`` is never an identity, and a write path
  that manufactures one is the sentinel §1.4 forbids, so ``memory.py``'s three
  ``or "global"`` expressions were deleted. An empty (or ``None``)
  ``directory_context`` now reaches migration 016's NOT NULL / non-empty
  constraint and is REFUSED. Two tests assert that refusal where they used to
  assert the coercion.
* Every insert must NAME a ``project_id``. The storage chokepoint became "the
  caller's value, or a raise". That is test SETUP, not subject matter: this
  file is about ``directory_context``, a column that survives until C11.
* The GLOBAL ANCHOR BUCKET was re-keyed. ``get_anchored_memories_scoped``
  matched ``directory_context IN ('', 'global')`` and now matches the
  ``global`` TAG, because §1.4 splits OWNERSHIP (project_id) from REACH (a
  tag). The last two tests pin both halves of that re-key.
"""

from __future__ import annotations

import pytest

#: Identity these inserts write under. The subject here is ``directory_context``;
#: the project_id is present only because C5 made it mandatory at the chokepoint.
_TEST_PROJECT_ID = "owner/repo"


@pytest.fixture
def storage(tmp_path):
    from yadgar._shared.storage import StorageEngine

    engine = StorageEngine(str(tmp_path / "dc_norm.db"), embedding_dim=384)
    yield engine
    engine.close()


class TestEmptyStringDCNormalization:
    """insert_memory REFUSES an empty/absent directory_context (C5 deleted the coercion)."""

    def test_empty_string_directory_context_is_now_refused(self, storage):
        """INVERTED: there is no normalisation left to perform.

        v5.46.6 coerced ``directory_context=''`` to ``'global'`` at write time.
        C5 (ADR-0227) deleted that expression along with the other two
        ``or "global"`` mints in ``memory.py`` -- ``'global'`` is never an
        identity, and a write path that manufactures one is exactly the
        sentinel §1.4 forbids. With the coercion gone the value reaches the
        schema as it arrived, and migration 016's NOT NULL / non-empty
        constraint refuses it.

        This is a strictly better guarantee than the one it replaces: the old
        behaviour turned a caller bug into a plausible-looking row in a
        namespace nobody chose, and this one makes the caller fix the call.
        """
        with pytest.raises(RuntimeError, match="directory_context"):
            storage.insert_memory(
                {
                    "content": "empty dc test",
                    "directory_context": "",
                    "tags": [],
                    "project_id": _TEST_PROJECT_ID,
                }
            )

    def test_none_directory_context_is_now_refused(self, storage):
        """INVERTED: the absent-field case is refused for the same reason.

        The original asserted only that ``None`` did not BECOME ``''``. The
        column is ``string`` and NOT NULL, so with the coercion deleted a
        ``None`` cannot be stored at all -- which is the same fail-loud answer
        the empty string now gets, rather than two different outcomes for two
        spellings of "the caller named no directory".
        """
        with pytest.raises(RuntimeError, match="directory_context"):
            storage.insert_memory(
                {
                    "content": "no dc test",
                    "directory_context": None,
                    "tags": [],
                    "project_id": _TEST_PROJECT_ID,
                }
            )

    def test_nonempty_dc_preserved(self, storage):
        """Non-empty directory_context is stored verbatim."""
        mid = storage.insert_memory(
            {
                "content": "explicit dc test",
                "directory_context": "/repos/myproject",
                "tags": [],
                "project_id": _TEST_PROJECT_ID,
            }
        )
        rows = storage._q(f"SELECT directory_context FROM memory:{mid}")
        assert rows, "memory row not found"
        assert rows[0].get("directory_context") == "/repos/myproject", (
            f"expected '/repos/myproject', got {rows[0].get('directory_context')!r}"
        )

    def test_global_dc_preserved(self, storage):
        """'global' directory_context is stored verbatim (no double-normalisation)."""
        mid = storage.insert_memory(
            {
                "content": "global dc test",
                "directory_context": "global",
                "tags": [],
                "project_id": _TEST_PROJECT_ID,
            }
        )
        rows = storage._q(f"SELECT directory_context FROM memory:{mid}")
        assert rows, "memory row not found"
        assert rows[0].get("directory_context") == "global"

    def test_global_directory_context_no_longer_buys_global_reach(self, storage):
        """C5 re-keyed the global bucket from directory_context to the ``global`` TAG.

        Inverted, not deleted. The original inserted ``dc=''``, relied on the
        now-deleted coercion to turn it into ``'global'``, and asserted the row
        surfaced in the global anchor bucket -- which held while
        ``get_anchored_memories_scoped`` matched
        ``directory_context IN ('', 'global')``. C5 re-keyed that reader to
        ``'global' INSIDE tags`` because §1.4 splits OWNERSHIP (project_id,
        always a real registered project) from REACH (a tag).

        So the assertion becomes its converse, written against the value the
        coercion used to produce: a row whose DIRECTORY says ``global`` does
        not get global reach. This is the narrow-bucket state C5 shipped
        knowingly -- 7 tagged rows against ~349 stamped ones -- which C6's
        operator-invoked backfill closes by ADDING THE TAG to those rows, not
        by restoring the predicate. If a later car makes this test fail, the
        question to ask is which of the two it did.
        """
        storage.insert_memory(
            {
                "content": "dc-global anchor",
                "directory_context": "global",
                "tags": ["_anchor"],
                "is_protected": True,
                "is_stale": False,
                "project_id": _TEST_PROJECT_ID,
            }
        )
        result = storage.get_anchored_memories_scoped(directory="/some/project", limit=20)
        assert "dc-global anchor" not in [r["content"] for r in result], (
            "directory_context='global' must NOT buy global reach: C5 keys the "
            "global bucket on the 'global' TAG"
        )

    def test_global_tagged_anchor_does_surface_in_global_bucket(self, storage):
        """The other half of the re-key: the TAG is what grants reach now.

        Without this, the inverted test above would pass just as well if the
        global bucket were broken outright rather than merely re-keyed.
        """
        storage.insert_memory(
            {
                "content": "tagged global anchor",
                "directory_context": "global",
                "tags": ["_anchor", "global"],
                "is_protected": True,
                "is_stale": False,
                "project_id": _TEST_PROJECT_ID,
            }
        )
        result = storage.get_anchored_memories_scoped(directory="/some/project", limit=20)
        assert "tagged global anchor" in [r["content"] for r in result]
