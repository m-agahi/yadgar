"""check_invariants auto-repair: worktree-orphaned memory rows (T2 fold-in).

Backfill half of the Q1 orphaned-memories fix: memory rows whose
``directory_context`` contains ``/.claude/worktrees/`` were written before
write-path normalization landed and are invisible to canonical-repo recall
(exact-match directory filter). check_invariants re-points them to the
canonical root (the prefix before the marker) and clears ``branch`` to the
canonical NULL slot so they become recall-visible again. Idempotent —
repaired rows no longer match the pattern.

``/tmp/*`` orphans are NOT auto-repaired: the canonical root is not derivable
from the path string alone.

TDD: written before the implementation.
"""

import pytest

from yadgar.backend.admin_exec.invariants import _run_check_invariants
from yadgar.core import server

#: C13 — every write in this file names a project explicitly.
#: ADR-0227 deleted the derivation that used to answer for it, so a
#: dict without this key is a hard UnresolvedProjectError at insert.
_TEST_PROJECT = "m-agahi/yadgar"

_MARKER = "/.claude/worktrees/"


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def _insert(directory_context: str) -> int:
    storage = server._get_storage()
    return storage.insert_memory(
        {
            "project_id": _TEST_PROJECT,
            "content": f"orphan test row {directory_context}",
            "directory_context": directory_context,
        }
    )


def test_worktree_orphan_rows_repointed_to_canonical_root():
    storage = server._get_storage()
    mid = _insert("/home/u/proj/.claude/worktrees/agent-abc")

    result = _run_check_invariants(storage)

    row = storage.get_memory(mid)
    assert row["directory_context"] == "/home/u/proj"
    assert row.get("branch") in (None, "NONE")
    assert any("worktree" in f.lower() for f in result["fixed"])


def test_worktree_orphan_repair_is_idempotent():
    storage = server._get_storage()
    _insert("/home/u/proj/.claude/worktrees/agent-abc")

    _run_check_invariants(storage)
    second = _run_check_invariants(storage)

    assert not any("worktree" in f.lower() for f in second["fixed"])


def test_non_worktree_rows_untouched():
    storage = server._get_storage()
    mid = _insert("/home/u/proj")

    _run_check_invariants(storage)

    row = storage.get_memory(mid)
    assert row["directory_context"] == "/home/u/proj"
    # C12 (ADR-0226): this used to read back the ``branch="master"`` the seeding
    # kwarg wrote. That kwarg is revoked — it re-created, untyped, the column
    # migration 029 dropped from the SCHEMALESS ``memory`` table. The test's own
    # subject (a non-worktree row is left alone by the repair) is unchanged; the
    # assertion is now the stronger post-C12 invariant, and it is on the stored
    # ROW because a re-created column has no definition to show in INFO FOR TABLE.
    assert "branch" not in row, "a writer re-created memory.branch untyped"


def test_tmp_orphans_not_repaired():
    """/tmp/* rows: canonical root not derivable from the string — leave verbatim."""
    storage = server._get_storage()
    mid = _insert("/tmp/some-throwaway-checkout")

    _run_check_invariants(storage)

    row = storage.get_memory(mid)
    assert row["directory_context"] == "/tmp/some-throwaway-checkout"
