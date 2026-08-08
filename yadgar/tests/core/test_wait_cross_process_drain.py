"""Car 2 (task #29): cross-process drain nudge for wiki_add / memorize wait paths.

RCA: after the ADR-0078 core/backend split the live QueueDrainer runs ONLY in the
backend process. In the CORE MCP process ``_st._queue_drainer is None``, so the
old in-core ``drain_now()`` nudge was a SILENT NO-OP in production — the wait path
passively polled the backend's 30s-interval drainer, but the 15s wait budget
expired first → ``wait_timeout``.

The existing wait tests (test_wiki_add_wait.py / test_memorize_wait.py) wire a
real in-core drainer, so they DO NOT reproduce the production bug. These tests
force the production condition (``_st._queue_drainer is None``) and assert the
core wait-path now POSTs a ``drain_now`` nudge to the backend and the write
commits within the wait budget.

The mocked ``_forward_admin("drain_now", ...)`` side-effect actually drains the
real in-test queue (faithful cross-process model) so the assertion proves the
write COMMITS, not merely that the forward was called.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from yadgar.core import server

_TEST_DIR = "/home/max/git/yadgar"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wait_cross_process")
    server.init_engines(
        db_path=str(tmp_path / "cross_process.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture()
def _real_queue_no_core_drainer(tmp_path, _isolate_file_queue):
    """Wire a real FileQueue + drainer but leave _st._queue_drainer = None (prod model).

    Returns the real drainer so the mocked _forward_admin can drive drain_now()
    exactly as the backend admin op would over HTTP.
    """
    import yadgar._shared.runtime.state as _state_mod
    import yadgar.core.lifecycle.lifecycle as _cl
    from yadgar.backend.queue_drainer import FileQueue, QueueDrainer

    real_fq = FileQueue(tmp_path / "xproc_queue")
    real_drainer = QueueDrainer(
        queue=real_fq,
        storage_factory=lambda: _state_mod._storage,
        drain_interval=9999,  # never self-fires
    )

    def _get_fq():
        return real_fq

    # Crucial: _queue_drainer stays None in-core (the production condition).
    with (
        patch.object(_cl, "_get_file_queue", _get_fq),
        patch("yadgar.core.server.tools.wiki._get_file_queue", _get_fq),
        patch("yadgar.core.server.tools.memorize._get_file_queue", _get_fq),
        patch.object(_state_mod, "_queue_drainer", None),
        patch.object(_state_mod, "_file_queue", real_fq),
    ):
        yield real_drainer


def _make_unique_title(base: str) -> str:
    import uuid

    return f"{base} {uuid.uuid4().hex[:8]}"


class TestWikiAddCrossProcessDrain:
    def test_wait_true_nudges_backend_and_commits_when_core_drainer_none(
        self, _real_queue_no_core_drainer
    ):
        """wiki_add(wait=True) with _st._queue_drainer None → POSTs drain_now, commits."""
        real_drainer = _real_queue_no_core_drainer
        forward_calls = []

        def _fake_forward(op, payload, timeout_s=30.0):
            forward_calls.append(op)
            # Model the backend admin op: run the live backend drainer synchronously.
            processed = real_drainer.drain_now()
            return {"drained": True, "items_processed": processed}

        title = _make_unique_title("Cross Process Wiki")
        with patch("yadgar.core.server.tools.wiki._forward_admin", _fake_forward):
            result = server.wiki_add(
                title=title,
                content="cross-process commit content",
                wait=True,
                tags=["xproc"],
                directory=_TEST_DIR,
            )

        assert "drain_now" in forward_calls, (
            f"wait path must POST a drain_now nudge; forwards={forward_calls}"
        )
        assert result.get("stored") is True, f"expected commit, got: {result}"
        assert result.get("committed") is True, f"expected committed, got: {result}"
        assert not result.get("queued", False)

    def test_wait_true_non_fatal_when_forward_fails(self, _real_queue_no_core_drainer):
        """Backend down / older backend (no endpoint) → forward raises → swallowed.

        Falls through to the passive poll (today's behavior). With no live core
        drainer and a failed nudge nothing commits → wait_timeout (graceful, not
        a crash)."""
        real_drainer = _real_queue_no_core_drainer  # noqa: F841 — intentionally unused

        def _boom_forward(op, payload, timeout_s=30.0):
            raise RuntimeError("backend unreachable")

        title = _make_unique_title("Forward Fail Wiki")
        with (
            patch("yadgar.core.server.tools.wiki._forward_admin", _boom_forward),
            patch("yadgar._shared.config.get_settings") as _mock_cfg,
        ):
            _mock_cfg.return_value = type("_Cfg", (), {"WIKI_WRITE_WAIT_TIMEOUT_SECONDS": 0.3})()
            result = server.wiki_add(
                title=title,
                content="forward fail content",
                wait=True,
                tags=["xproc-fail"],
                directory=_TEST_DIR,
            )

        # Non-fatal: no crash, graceful wait_timeout (still queued, converging).
        assert result.get("stored") is False
        assert result.get("reason") == "wait_timeout"
        assert result.get("queued") is True


class TestMemorizeCrossProcessDrain:
    def test_wait_true_nudges_backend_and_commits_when_core_drainer_none(
        self, _real_queue_no_core_drainer, recall_backend_bypass
    ):
        """memorize(wait=True) with _st._queue_drainer None → POSTs drain_now, commits."""
        real_drainer = _real_queue_no_core_drainer
        forward_calls = []

        def _fake_forward(op, payload, timeout_s=30.0):
            forward_calls.append(op)
            processed = real_drainer.drain_now()
            return {"drained": True, "items_processed": processed}

        with patch("yadgar.core.server.tools.memorize._forward_admin", _fake_forward):
            result = server.memorize(
                content="cross-process memorize content unique qqq",
                context=_TEST_DIR,
                tags=["xproc-mem"],
                wait=True,
            )

        assert "drain_now" in forward_calls, (
            f"memorize wait path must POST a drain_now nudge; forwards={forward_calls}"
        )
        assert result.get("stored") is True, f"expected commit, got: {result}"
        assert result.get("committed") is True, f"expected committed, got: {result}"
