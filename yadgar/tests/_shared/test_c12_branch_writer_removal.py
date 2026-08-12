"""C12 (0047 PR#40 §5) — no storage writer can still emit ``branch`` (ADR-0226).

**The assertion is on the CREATE statement's PARAMETER SET and its SurQL, not
merely on the resulting row.** ADR-0226's stated trap is precisely that a
row-level or ``INFO FOR TABLE``-level check cannot see the defect: ``memory``,
``wiki_page`` and ``wiki_page_version`` are all SCHEMALESS, so ``REMOVE FIELD``
drops only the type definition while **any surviving writer re-creates the
column untyped and ``INFO FOR TABLE`` still looks clean**. *"Killing the writers
is the actual safety property; the schema statement alone never was."*

Two of the writers covered here were LIVE re-creation paths for a column
migration 029 had already dropped, not merely tidy-up:

  * ``insert_wiki_page(page, branch=…)`` appended ``branch = $branch`` to the
    **``wiki_page``** SET clause — 029's own table.
  * ``insert_memory(memory, branch=…)`` did the same on **``memory``** via
    ``_build_memory_insert_clause``.

The remaining three wrote the ``wiki_page_version`` snapshot that migration 032
now drops: ``update_wiki_page``, ``set_wiki_page_metadata`` and
``insert_wiki_page_version``.

Shape follows ``test_c11_project_id_writers.py``: ``_q`` is spied on a real
``StorageEngine`` (delegating, not stubbing) so the row is genuinely written and
the statement really executed.
"""

from __future__ import annotations

import inspect

import pytest

from yadgar._shared.storage import StorageEngine

_PROJECT = "m-agahi/yadgar"
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


def _page(slug: str) -> dict:
    return {
        "title": "T",
        "slug": slug,
        "content": "body",
        "category": "reference",
        "tags": [],
        "links": [],
        "confidence": 1.0,
        "embedding": None,
        "source_memory_ids": [],
        "directory_context": _PATH,
        "project_id": _PROJECT,
    }


def _memory() -> dict:
    return {
        "content": "body",
        "embedding": None,
        "tags": [],
        "directory_context": _PATH,
        "project_id": _PROJECT,
        "heat": 0.5,
        "is_stale": False,
        "file_hash": None,
        "embedding_model": None,
    }


def _assert_no_branch(calls: list[tuple[str, dict]], table: str) -> None:
    """No statement touching *table* may mention ``branch`` in SurQL or params."""
    touching = [(sql, p) for sql, p in calls if table in sql]
    assert touching, f"no statement issued against {table}"
    for sql, params in touching:
        assert "branch" not in sql, f"{table} statement still emits branch: {sql!r}"
        assert not [k for k in params if "branch" in k], (
            f"{table} statement still binds a branch param: {sorted(params)!r}"
        )


class TestSeedingKwargsAreGone:
    """The kwargs themselves — ADR-0226 revokes them, they are the re-creation path."""

    def test_insert_memory_has_no_branch_parameter(self) -> None:
        sig = inspect.signature(StorageEngine.insert_memory)
        assert "branch" not in sig.parameters

    def test_insert_wiki_page_has_no_branch_parameter(self) -> None:
        sig = inspect.signature(StorageEngine.insert_wiki_page)
        assert "branch" not in sig.parameters

    def test_build_memory_insert_clause_has_no_branch_parameter(self) -> None:
        """The private builder is where the kwarg actually became SurQL."""
        sig = inspect.signature(StorageEngine._build_memory_insert_clause)
        assert "branch" not in sig.parameters

    def test_anchor_memory_has_no_branch_parameter(self) -> None:
        from yadgar.backend.restoration.checkpoint_restore import CheckpointRestore

        sig = inspect.signature(CheckpointRestore.anchor_memory)
        assert "branch" not in sig.parameters

    def test_insert_memory_rejects_a_branch_kwarg(self, storage) -> None:
        """Not merely absent from the signature — passing it must fail loudly."""
        with pytest.raises(TypeError):
            storage.insert_memory(_memory(), branch="master")

    def test_insert_wiki_page_rejects_a_branch_kwarg(self, storage) -> None:
        with pytest.raises(TypeError):
            storage.insert_wiki_page(_page("rejects-kwarg"), branch="master")


class TestInsertWikiPageWriter:
    """``insert_wiki_page`` wrote ``branch`` to BOTH tables. Neither may survive."""

    def test_wiki_page_create_never_emits_branch(self, storage, recorder) -> None:
        """029 dropped ``wiki_page.branch``; this writer used to re-create it untyped."""
        storage.insert_wiki_page(_page("insert-page"))
        _assert_no_branch(recorder, "wiki_page")

    def test_wiki_page_version_create_never_emits_branch(self, storage, recorder) -> None:
        storage.insert_wiki_page(_page("insert-version"))
        _assert_no_branch(recorder, "wiki_page_version")

    def test_the_row_still_carries_everything_else(self, storage) -> None:
        pid = storage.insert_wiki_page(_page("insert-intact"))
        page = storage.get_wiki_page(pid)
        assert page is not None
        assert page["slug"] == "insert-intact"
        assert page["project_id"] == _PROJECT


class TestUpdateWikiPageWriter:
    """``update_wiki_page``'s version snapshot carried ``merged.get("branch")``."""

    def test_version_create_never_emits_branch(self, storage, recorder) -> None:
        pid = storage.insert_wiki_page(_page("update-me"))
        recorder.clear()
        storage.update_wiki_page(pid, {"content": "new body"})
        _assert_no_branch(recorder, "wiki_page_version")

    def test_the_version_snapshot_is_still_written(self, storage) -> None:
        pid = storage.insert_wiki_page(_page("update-still-versions"))
        storage.update_wiki_page(pid, {"content": "new body"})
        assert storage.get_max_version_for_page(pid) == 2


class TestSetWikiPageMetadataWriter:
    """The THIRD snapshot path — the one the plan's file:line list nearly missed."""

    def test_version_create_never_emits_branch(self, storage, recorder) -> None:
        pid = storage.insert_wiki_page(_page("metadata-me"))
        recorder.clear()
        storage.set_wiki_page_metadata(pid, "directory_context", "/other/path")
        _assert_no_branch(recorder, "wiki_page_version")

    def test_the_metadata_write_still_lands(self, storage) -> None:
        pid = storage.insert_wiki_page(_page("metadata-lands"))
        storage.set_wiki_page_metadata(pid, "directory_context", "/other/path")
        page = storage.get_wiki_page(pid)
        assert page is not None
        assert page["directory_context"] == "/other/path"


class TestInsertWikiPageVersionWriter:
    """The migration seeder's public entry point — ``snapshot.get("branch")``."""

    def test_create_never_emits_branch(self, storage, recorder) -> None:
        pid = storage.insert_wiki_page(_page("direct-version"))
        recorder.clear()
        storage.insert_wiki_page_version(
            pid,
            {"title": "T", "content": "c", "category": None, "tags": [], "confidence": 1.0},
            "a summary",
        )
        _assert_no_branch(recorder, "wiki_page_version")

    def test_a_branch_key_in_the_snapshot_dict_is_ignored(self, storage, recorder) -> None:
        """A stale caller dict must not become a column again — the SCHEMALESS trap."""
        pid = storage.insert_wiki_page(_page("stale-snapshot"))
        recorder.clear()
        storage.insert_wiki_page_version(
            pid,
            {"title": "T", "content": "c", "tags": [], "branch": "master"},
            "a summary",
        )
        _assert_no_branch(recorder, "wiki_page_version")


class TestInsertMemoryWriter:
    """``insert_memory(branch=)`` re-created ``memory.branch`` — 029's own table."""

    def test_memory_create_never_emits_branch(self, storage, recorder) -> None:
        storage.insert_memory(_memory())
        _assert_no_branch(recorder, "memory")

    def test_the_row_still_carries_everything_else(self, storage) -> None:
        mid = storage.insert_memory(_memory())
        mem = storage.get_memory(mid)
        assert mem is not None
        assert mem["project_id"] == _PROJECT
        assert mem["directory_context"] == _PATH
