"""check_invariants auto-repair: worktree-orphaned memory rows (T2 fold-in).

Backfill half of the Q1 orphaned-memories fix: memory rows whose
``directory_context`` contains ``/.claude/worktrees/`` were written before
write-path normalization landed and are invisible to canonical-repo recall
(exact-match directory filter). check_invariants re-points them to the
canonical root (the prefix before the marker) so they become recall-visible
again. Idempotent — repaired rows no longer match the pattern.

The repair used to also write ``branch = NONE`` and both its log line and its
``fixed`` entry claimed the branch had been "cleared to canonical slot".
Migration 029 DROPPED that column, so the write was a no-op and the claim was
false. Both are gone; ``test_repair_makes_no_claim_about_branch`` is what stops
a reader re-adding a write for a column that no longer exists.

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
    # ``row.get("branch") in (None, "NONE")`` used to stand here; it is
    # satisfied by a column that does not exist, so it passed either way.
    assert "branch" not in row, "the repair re-created memory.branch untyped"
    assert any("worktree" in f.lower() for f in result["fixed"])


def test_repair_makes_no_claim_about_branch():
    """The ``fixed`` entry must not describe work the repair does not do.

    Migration 029 dropped ``memory.branch``. The repair's ``SET branch = NONE``
    was a no-op against a column that no longer exists, and the operator-facing
    string still announced it — a false receipt is worse than a silent one,
    because it is the only thing an operator reads.
    """
    storage = server._get_storage()
    _insert("/home/u/proj/.claude/worktrees/agent-abc")

    result = _run_check_invariants(storage)

    worktree_entries = [f for f in result["fixed"] if "worktree" in f.lower()]
    assert worktree_entries, "the repair reported nothing at all"
    assert not any("branch" in f.lower() for f in worktree_entries)


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
