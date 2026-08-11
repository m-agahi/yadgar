"""E2E: writes succeed with NO branch context available at all (ADR-0215).

This is Car 2's POSITIVE exit criterion. Car 2 deletes the v5.42.3 ``missing_branch``
hard-reject *and* every test that asserted it, which makes a do-nothing stub
trivially green. This file is the counter: it asserts a behaviour that is
**impossible** while any part of that guard is alive.

The environment each test constructs is exactly the one the guard fired on:

  * No branch detection exists to fall back on — Car 6 deleted the helper the
    write path used to consult, so a real branch cannot leak in and make a
    test pass vacuously.
  * ``YADGAR_CI_BRANCH`` is DELETED from the environment. This was load-bearing
    when written: the repo's CI workflows then exported ``YADGAR_CI_BRANCH: master``,
    so without the delenv these tests would have been green pre-Car-2 on CI and red
    only on a developer host. Car 4 removed that export and Car 2 removed the last
    reader, so as of Car 10 the variable exists nowhere in the repo and the delenv
    is belt-and-braces. It is kept so the test still constructs the exact
    environment its name claims.
  * No ``branch_hint`` is passed to anything.

Pre-Car-2 that combination produced ``{"error": "missing_branch", "stored": False}``
at the MCP boundary for all four writers, and no row was ever created.

WHY THIS ALSO COVERS THE DRAINER HALF (``queue_drainer/dlq.py``)
---------------------------------------------------------------
``_enforcement_on`` is fail-safe — it returns True unless the env var is
explicitly falsy — so ``YADGAR_BRANCH_ENFORCEMENT`` is ON by default in tests.
Car 2 stops writing a ``branch`` key into the memorize/anchor/checkpoint queue
payloads. If the drainer's ``_validate_branch_context`` were left alive, those
now-branchless payloads would be rejected to the DLQ and no row would land — so
a car that removed only the MCP-boundary reject fails these tests. The drainer
half is not separately asserted because it cannot pass while it survives.

ASSERTION SHAPE — read-back, never "no error key"
-------------------------------------------------
Three of the four tools do not return a row id: ``memorize`` returns
``{stored, queued, queue_id}``, ``anchor`` returns ``{queued, status}``,
``checkpoint`` returns ``{queued, directory}``. Only ``update_active_work``
returns a row (``new_memory``). So the proof of a stored row is a **query
against storage** for a unique per-test token after the drainer runs — that
query IS the read-back. No assertion in this file is of the form "the result
dict lacks an error key"; a no-op stub that returns ``{}`` fails every one.

SURVIVING LATER CARS — deliberate design, do not "simplify" it away:
  * No call in this file passes a ``branch=`` / ``branch_hint=`` kwarg to any
    yadgar API, so Cars 5's signature removals cannot break it.
  * No assertion mentions ``branch``, so Car 9's column drop is a no-op here.
  * Both ``monkeypatch.setattr`` calls use ``raising=False``, so once Car 6
    deletes the detection helpers there is simply nothing left to patch — which
    is the same "no branch context" state the tests assert against.

Placement: ``yadgar/tests/e2e/`` so ``make e2e`` collects it. Live-surreal DB.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

PROJECT_DIR = "/home/test/yadgar-project"

#: Identity every write in this file names. ADR-0215 (the subject here) removed
#: the BRANCH dimension; C5/ADR-0227 then made the PROJECT dimension mandatory
#: at the same boundaries. Naming it keeps each test on its own subject: a
#: writer that refuses for want of an identity is not evidence about branch.
PROJECT_ID = "owner/repo"


def _no_branch_context_anywhere(monkeypatch) -> None:
    """Construct the exact environment the v5.42.3 hard-reject fired on.

    Car 4 deleted the CI export and Car 2 the last reader, so this delenv no
    longer changes behaviour — it is retained to keep the constructed environment
    faithful to the test's premise.
    """
    monkeypatch.delenv("YADGAR_CI_BRANCH", raising=False)


def _memory_rows_containing(storage, token: str) -> list[dict]:
    """Return memory rows whose content carries *token*. The read-back.

    Raw ``_q`` yields ``id`` as the SurrealDB record string (``memory:1``) while
    the tool layer reports the bare integer, so the id is normalised to an int
    here and the two are directly comparable.
    """
    rows = storage._q(
        "SELECT id, content, directory_context FROM memory WHERE string::contains(content, $t)",
        {"t": token},
    )
    for r in rows:
        r["id"] = int(str(r["id"]).rsplit(":", 1)[-1])
    return rows


class TestWritesSucceedWithoutBranchContext:
    """ADR-0215: branch context is not a precondition for storing knowledge."""

    def test_memorize_stores_a_row(self, e2e_engines, monkeypatch, _e2e_backend_drainer):
        """memorize() previously returned {"error": "missing_branch"} here."""
        from yadgar.core.server.tools.memorize import memorize

        _no_branch_context_anywhere(monkeypatch)
        storage = e2e_engines["storage"]
        token = "adr0215writepathmemorize"

        result = memorize(
            content=f"memorize note {token}",
            context=PROJECT_DIR,
            tags=["adr-0215"],
            project=PROJECT_ID,
        )
        assert result.get("stored") is True, (
            f"memorize must accept a write with no branch context (ADR-0215); got {result}"
        )

        _e2e_backend_drainer.drain_now()

        rows = _memory_rows_containing(storage, token)
        assert len(rows) == 1, (
            f"memorize must have stored exactly one row for token {token!r}; got {rows}"
        )
        assert rows[0]["id"] is not None
        assert rows[0]["directory_context"] == PROJECT_DIR

    def test_anchor_stores_a_row(self, e2e_engines, monkeypatch, _e2e_backend_drainer):
        """anchor() previously returned {"error": "missing_branch"} here."""
        from yadgar.core.server.tools.misc import anchor

        _no_branch_context_anywhere(monkeypatch)
        storage = e2e_engines["storage"]
        token = "adr0215writepathanchor"

        result = anchor(
            content=f"anchor note {token}",
            context=PROJECT_DIR,
            reason="adr-0215 write path",
            project=PROJECT_ID,
        )
        assert result.get("queued") is True, (
            f"anchor must accept a write with no branch context (ADR-0215); got {result}"
        )

        _e2e_backend_drainer.drain_now()

        rows = _memory_rows_containing(storage, token)
        assert len(rows) == 1, (
            f"anchor must have stored exactly one row for token {token!r}; got {rows}"
        )
        assert rows[0]["id"] is not None

    def test_checkpoint_stores_a_row(self, e2e_engines, monkeypatch, _e2e_backend_drainer):
        """checkpoint() previously returned {"error": "missing_branch"} here."""
        from yadgar.core.server.tools.misc import checkpoint

        _no_branch_context_anywhere(monkeypatch)
        storage = e2e_engines["storage"]
        token = "adr0215writepathcheckpoint"

        result = checkpoint(directory=PROJECT_DIR, current_task=f"task {token}", project=PROJECT_ID)
        assert result.get("queued") is True, (
            f"checkpoint must accept a write with no branch context (ADR-0215); got {result}"
        )

        _e2e_backend_drainer.drain_now()

        row = storage.get_active_checkpoint(PROJECT_DIR)
        assert row is not None, (
            f"checkpoint must have stored a row for {PROJECT_DIR!r} with no branch context"
        )
        assert row["id"] is not None
        assert token in row["current_task"]

    def test_update_active_work_stores_a_row(self, e2e_engines, monkeypatch):
        """update_active_work() previously returned {"error": "missing_branch"} here.

        Not queued — it forwards straight to the backend admin op, so no drain
        step. It is also the one writer that returns the row itself, so the
        returned id is asserted directly AND confirmed by a storage read-back.
        """
        from yadgar.core.server.tools.project import update_active_work

        _no_branch_context_anywhere(monkeypatch)
        storage = e2e_engines["storage"]
        token = "adr0215writepathactivework"

        result = update_active_work(directory=PROJECT_DIR, content=f"active work {token}")
        new_memory = result.get("new_memory")
        assert isinstance(new_memory, dict), (
            f"update_active_work must store a row with no branch context (ADR-0215); got {result}"
        )
        memory_id = new_memory.get("id")
        assert memory_id is not None, f"stored row carries no id: {new_memory}"

        rows = _memory_rows_containing(storage, token)
        assert [r["id"] for r in rows] == [memory_id], (
            f"read-back must return exactly the row update_active_work reported "
            f"(id={memory_id}); got {rows}"
        )
