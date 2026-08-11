"""C11 (0047 PR#40 §5) — the writers on the tables migration 033 declares.

**The assertion is on the CREATE statement's PARAMETER SET, not merely on the
resulting row.** A row-level assertion passes when the value happens to be
readable back through some other path; the parameter set is what proves the
write itself carries the identity. Every test here therefore records `_q` calls
on a real ``StorageEngine`` (delegating, not stubbing, so the row is really
written and the two-arm read tests below run against it).

**DUAL-WRITE, deliberately.** These writers stamp ``project_id`` AND keep
writing the legacy ``directory`` / ``directory_context`` column. The plan's TDD
line ("no ``directory`` value is written") is deferred to the drop PR for two
independent reasons, both asserted below so a later car cannot quietly take the
legacy write out without reading them:

  1. ADR-0225 keeps the legacy columns *because the backfill derives from them*.
     A row written between this car and the drop carrying a ``project_id`` but no
     ``directory`` would be unattributable in BOTH directions — nothing for the
     backfill to derive from, nothing for an un-backfilled reader to match.
  2. Three live consumers read those columns today:
     ``backend/causal_discovery/pc.py`` filters episodes on ``e["directory"]``,
     ``backend/consolidation/cls.py`` reads ``ep.get("directory")``, and
     ``backend/consolidation/cleanup.py`` takes the action-log row's
     ``directory`` as the summary memory's ``directory_context``.

``project_backfill._TABLES`` is ``("memory", "wiki_page")`` and plan §8 names no
backfill step for these tables, so the reads keep a transitional legacy arm: a
``project_id``-only predicate here would not be the degraded window §8 5b
sanctions but permanent silent loss of the historical corpus.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage import StorageEngine

#: ADR-0227 deleted the derivation tier — every write here names its project.
_PROJECT = "m-agahi/yadgar"
_OTHER_PROJECT = "someone-else/other-repo"

#: The caller's filesystem path. Deliberately NOT equal to ``_PROJECT``: a test
#: reusing one string for both passes under either keying.
_PATH = "/home/max/git/yadgar"


@pytest.fixture
def storage(tmp_path):
    return StorageEngine(str(tmp_path / "test.db"))


@pytest.fixture
def recorder(storage, monkeypatch):
    """Record every ``(sql, params)`` while still executing the real write."""
    calls: list[tuple[str, dict]] = []
    original = storage._q

    def _spy(surql, params=None):
        calls.append((surql, dict(params or {})))
        return original(surql, params)

    monkeypatch.setattr(storage, "_q", _spy)
    return calls


def _creates(calls: list[tuple[str, dict]], table: str) -> list[tuple[str, dict]]:
    return [(sql, p) for sql, p in calls if "CREATE" in sql.upper() and table in sql]


class TestMemoryBlockWriter:
    """``create_block`` stamps ``project_id`` into the CREATE parameter set."""

    def test_create_statement_binds_the_callers_project_id(self, storage, recorder):
        storage.create_block(
            name="current_task",
            content="body",
            scope="project",
            directory=_PATH,
            project_id=_PROJECT,
        )
        creates = _creates(recorder, "memory_block")
        assert creates, "no CREATE issued for memory_block"
        sql, params = creates[-1]
        assert "project_id = $project_id" in sql
        assert params["project_id"] == _PROJECT

    def test_create_statement_still_binds_the_legacy_directory(self, storage, recorder):
        """Dual-write — see this module's docstring for both reasons."""
        storage.create_block(
            name="current_task",
            content="body",
            scope="project",
            directory=_PATH,
            project_id=_PROJECT,
        )
        _sql, params = _creates(recorder, "memory_block")[-1]
        assert params["directory"] == _PATH

    def test_an_unnamed_project_is_not_substituted(self, storage, recorder):
        """No caller value → the NONE LITERAL, never the path and never a sentinel.

        ``NONE`` has to be a literal in the statement rather than a bound
        ``None``: ``project_id`` is ``option<string>`` and a bound Python
        ``None`` arrives as SQL ``NULL``, which SurrealDB rejects outright —
        *"Expected `none | string` but found `NULL`"*. ``project_id_set_fragment``
        owns that distinction; asserting it here is what stops a later edit
        "simplifying" it back into a bind and taking every unstamped write down.
        """
        storage.create_block(name="current_task", content="body", scope="project", directory=_PATH)
        sql, params = _creates(recorder, "memory_block")[-1]
        assert "project_id = NONE" in sql
        assert "project_id" not in params
        assert _PATH not in {v for v in params.values() if isinstance(v, str)} - {
            params["directory"]
        }

    def test_global_blocks_carry_no_project(self, storage, recorder):
        """A global block belongs to no project; stamping one would be a mint."""
        storage.create_block(name="gotchas", content="body", scope="global")
        sql, params = _creates(recorder, "memory_block")[-1]
        assert "project_id = NONE" in sql
        assert "project_id" not in params

    def test_a_global_block_is_unstamped_even_when_a_project_is_named(self, storage, recorder):
        """``scope='global'`` wins over a caller-supplied project_id.

        Otherwise a global block would become invisible to every project but
        one — the reach concept inverted.
        """
        storage.create_block(name="gotchas", content="body", scope="global", project_id=_PROJECT)
        sql, params = _creates(recorder, "memory_block")[-1]
        assert "project_id = NONE" in sql
        assert "project_id" not in params


class TestMemoryBlockTwoArmRead:
    """``list_blocks`` resolves by ``project_id`` AND by the legacy path."""

    def test_a_stamped_block_is_found_by_project_id_alone(self, storage):
        storage.create_block(
            name="current_task",
            content="stamped body",
            scope="project",
            directory=_PATH,
            project_id=_PROJECT,
        )
        rows = storage.list_blocks(
            scope="project", directory="/some/other/path", project_id=_PROJECT
        )
        assert [r["content"] for r in rows] == ["stamped body"]

    def test_a_legacy_block_is_still_found_by_path(self, storage):
        """The transitional arm. No backfill covers ``memory_block`` (§8)."""
        storage.create_block(
            name="current_task", content="legacy body", scope="project", directory=_PATH
        )
        rows = storage.list_blocks(scope="project", directory=_PATH, project_id=_PROJECT)
        assert [r["content"] for r in rows] == ["legacy body"]

    def test_another_projects_block_does_not_leak(self, storage):
        storage.create_block(
            name="current_task",
            content="not yours",
            scope="project",
            directory="/home/max/git/other",
            project_id=_OTHER_PROJECT,
        )
        rows = storage.list_blocks(scope="project", directory=_PATH, project_id=_PROJECT)
        assert rows == []

    def test_scope_none_returns_global_plus_both_arms(self, storage):
        storage.create_block(name="gotchas", content="global body", scope="global")
        storage.create_block(
            name="current_task",
            content="stamped body",
            scope="project",
            directory="/elsewhere",
            project_id=_PROJECT,
        )
        rows = storage.list_blocks(scope=None, directory=_PATH, project_id=_PROJECT)
        contents = sorted(r["content"] for r in rows)
        assert contents == ["global body", "stamped body"]

    def test_no_project_id_falls_back_to_the_path_arm_only(self, storage):
        """A caller with no identity must not widen to every project's blocks.

        ADR-0225's generalised lesson (C10g's ``_fetch_hot_memories`` leak): a
        widening ``else`` becomes the DEFAULT the moment the key changes.
        """
        storage.create_block(
            name="current_task",
            content="not yours",
            scope="project",
            directory="/home/max/git/other",
            project_id=_OTHER_PROJECT,
        )
        rows = storage.list_blocks(scope="project", directory=_PATH, project_id=None)
        assert rows == []


class TestEpisodeWriter:
    """``insert_episode`` stamps ``project_id`` and keeps ``directory``."""

    def test_create_statement_binds_the_project_id(self, storage, recorder):
        storage.insert_episode(
            {
                "session_id": "s1",
                "directory": _PATH,
                "project_id": _PROJECT,
                "raw_content": "content",
            }
        )
        sql, params = _creates(recorder, "episode")[-1]
        assert "project_id = $project_id" in sql
        assert params["project_id"] == _PROJECT

    def test_create_statement_still_binds_the_legacy_directory(self, storage, recorder):
        """``causal_discovery/pc.py`` and ``consolidation/cls.py`` read it."""
        storage.insert_episode(
            {
                "session_id": "s1",
                "directory": _PATH,
                "project_id": _PROJECT,
                "raw_content": "content",
            }
        )
        _sql, params = _creates(recorder, "episode")[-1]
        assert params["directory"] == _PATH

    def test_an_episode_with_no_project_is_not_substituted(self, storage, recorder):
        """NONE literal, not a bound None — see the memory_block twin for why."""
        storage.insert_episode({"session_id": "s1", "directory": _PATH, "raw_content": "content"})
        sql, params = _creates(recorder, "episode")[-1]
        assert "project_id = NONE" in sql
        assert "project_id" not in params
        assert params["directory"] == _PATH


class TestCheckpointWriter:
    """``insert_checkpoint`` stamps ``project_id`` and keeps ``directory_context``."""

    def test_create_statement_binds_the_project_id(self, storage, recorder):
        storage.insert_checkpoint(
            {"directory_context": _PATH, "project_id": _PROJECT, "current_task": "t"}
        )
        sql, params = _creates(recorder, "checkpoint")[-1]
        assert "project_id = $project_id" in sql
        assert params["project_id"] == _PROJECT

    def test_create_statement_still_binds_the_legacy_directory_context(self, storage, recorder):
        storage.insert_checkpoint(
            {"directory_context": _PATH, "project_id": _PROJECT, "current_task": "t"}
        )
        _sql, params = _creates(recorder, "checkpoint")[-1]
        assert params["dir"] == _PATH

    def test_supersession_still_deletes_by_the_legacy_key(self, storage):
        """One checkpoint per directory. The DELETE must not start missing rows.

        Re-keying the supersede DELETE onto ``project_id`` while legacy rows
        carry none would leave every pre-C11 checkpoint behind and let
        ``ORDER BY created_at DESC LIMIT 1`` keep returning a stale one.
        """
        storage.insert_checkpoint({"directory_context": _PATH, "current_task": "first"})
        storage.insert_checkpoint(
            {"directory_context": _PATH, "project_id": _PROJECT, "current_task": "second"}
        )
        rows = storage._q("SELECT * FROM checkpoint")
        assert len(rows) == 1
        assert rows[0]["current_task"] == "second"


class TestCheckpointTwoArmRead:
    """``get_active_checkpoint`` resolves by ``project_id`` AND by the legacy path."""

    def test_a_stamped_checkpoint_is_found_by_project_id_alone(self, storage):
        storage.insert_checkpoint(
            {"directory_context": _PATH, "project_id": _PROJECT, "current_task": "stamped"}
        )
        row = storage.get_active_checkpoint("/some/other/path", project_id=_PROJECT)
        assert row is not None
        assert row["current_task"] == "stamped"

    def test_a_legacy_checkpoint_is_still_found_by_path(self, storage):
        storage.insert_checkpoint({"directory_context": _PATH, "current_task": "legacy"})
        row = storage.get_active_checkpoint(_PATH, project_id=_PROJECT)
        assert row is not None
        assert row["current_task"] == "legacy"

    def test_another_projects_checkpoint_does_not_leak(self, storage):
        storage.insert_checkpoint(
            {
                "directory_context": "/home/max/git/other",
                "project_id": _OTHER_PROJECT,
                "current_task": "not yours",
            }
        )
        assert storage.get_active_checkpoint(_PATH, project_id=_PROJECT) is None


class TestActionLogWriterAlreadyStamped:
    """C4 already re-keyed this writer — C11 verifies, it does not redo."""

    def test_create_statement_binds_the_project_id(self, storage, recorder):
        storage.insert_action_log(
            tool_name="Read",
            tool_input_summary="x",
            directory=_PATH,
            session_id="s1",
            timestamp="2026-08-12T00:00:00Z",
            project_id=_PROJECT,
        )
        sql, params = _creates(recorder, "action_log")[-1]
        assert "project_id = $project_id" in sql
        assert params["project_id"] == _PROJECT

    def test_create_statement_still_binds_the_legacy_directory(self, storage, recorder):
        """``consolidation/cleanup.py`` reads it as the summary's directory_context."""
        storage.insert_action_log(
            tool_name="Read",
            tool_input_summary="x",
            directory=_PATH,
            session_id="s1",
            timestamp="2026-08-12T00:00:00Z",
            project_id=_PROJECT,
        )
        _sql, params = _creates(recorder, "action_log")[-1]
        assert params["directory"] == _PATH
