"""Tests for memorize(wait=True) read-your-writes surface (Car 25 Seam 3).

memorize() is async: the default call enqueues and returns {stored, queued,
queue_id} before the drainer runs. wait=True mirrors wiki_add's wait semantics —
it enqueues, nudges the drainer (drain_now()), and polls the shared archive/dlq
dirs (FileQueue.wait_for_job) for the job's terminal state, returning a
committed/converged status when the write drains (or wait_timeout on budget).

Reuses the exact wait/drain plumbing wiki_add uses (FileQueue.wait_for_job +
QueueDrainer.drain_now); no new drain machinery is introduced.

Fixture note (why NO bespoke _wire_drainer): the conftest autouse
``admin_backend_bypass`` (gated on the ``_engines`` fixture name) already wires
ONE in-process QueueDrainer bound to the lazy global file queue
(``yadgar.core.lifecycle.lifecycle._get_file_queue``). memorize enqueues through
that SAME global (``memorize._get_file_queue`` is the same symbol), and the
wait path's internal ``drain_now()`` drives ``_st._queue_drainer`` — the conftest
drainer. Riding this single queue (as test_memorize_async does) is race-free.
A second, module-local drainer on a bespoke FileQueue (the wiki-test pattern)
creates two drainers on two queues; under ``--dist loadgroup -n 4`` the winner of
``_st._queue_drainer`` is load-ordered, so ``drain_now()`` can drain the wrong
queue and the enqueued job never archives → spurious wait_timeout. Single queue
kills that flake.
"""

from unittest.mock import patch

import pytest

from yadgar.core import server


@pytest.fixture(scope="module", autouse=True)
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("memorize_wait")
    server.init_engines(
        db_path=str(tmp_path / "wait_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


# ── wait=False (default) — async behavior unchanged ──────────────────────────


def test_wait_false_default_returns_queued():
    result = server.memorize("wait false default content", "/tmp/waitfalse", ["test"])
    assert result["stored"] is True
    assert result["queued"] is True
    assert "queue_id" in result
    assert not result.get("committed", False)


# ── wait=True — read-your-writes: committed status after real drain ───────────


def test_wait_true_returns_committed_after_drain(recall_backend_bypass):
    """memorize(wait=True) blocks until the drainer commits, returning a
    committed status (not just queued), and the memory is actually persisted
    (real drain — recall finds it without a separate flush)."""
    import yadgar._shared.runtime.state as _st

    # memorize enqueues via `from yadgar.core.lifecycle import _get_file_queue`
    # — assert against that exact symbol (the tools.memorize attribute is the
    # decorated tool function, not the module, so it can't be introspected here).
    from yadgar.core.lifecycle import _get_file_queue as _mem_get_fq

    # Single-queue invariant: the drainer the wait path nudges (_st._queue_drainer)
    # must own the SAME FileQueue memorize enqueues to. If these ever diverge,
    # drain_now() drains the wrong queue and the job never archives → the exact
    # two-drainer race this test guards against.
    assert _st._queue_drainer is not None, "conftest admin_backend_bypass must wire a drainer"
    assert _st._queue_drainer._queue is _mem_get_fq(), (
        "wait path drainer and memorize enqueue-queue must be the same FileQueue"
    )

    content = "wait true committed content unique zzz"
    # Shrink the wait budget (mirrors test_wiki_add_wait's 0.3) so any FUTURE
    # wrong-queue regression fails fast (~0.3s) instead of hanging the full 5s.
    # The happy path is unaffected: drain_now() is synchronous, so the job is
    # already archived before wait_for_job polls — the timeout never bites here.
    # (tools.memorize resolves to the decorated tool fn, not the module — reach
    # the module's `settings` object via sys.modules to patch the knob it reads.)
    import sys

    _mem_mod = sys.modules["yadgar.core.server.tools.memorize"]
    with patch.object(_mem_mod.settings, "WIKI_WRITE_WAIT_TIMEOUT_SECONDS", 0.3):
        result = server.memorize(content, "/tmp/waittrue", ["test"], wait=True)

    assert result.get("committed") is True, f"expected committed, got: {result}"
    assert result["stored"] is True
    assert result.get("queued") is False

    # committed must mean the write actually landed — recall finds it with no
    # separate flush_queue() call (the wait path already drained it).
    hits = server.recall(content[:40], directory="/tmp/waittrue")
    assert any(h["content"] == content for h in hits), "committed memory not persisted"
