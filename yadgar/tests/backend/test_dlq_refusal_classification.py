"""Ledger task 229 — a DLQ entry must not contradict itself, and a refusal must not retry.

Two independent defects, one stored artifact. The real instance
(``dlq/.events.log``, 2026-08-19T18:37:19Z) recorded BOTH at once::

    "attempts": 20,
    "classification": "transient",
    "last_error": "wiki page mutability='locked' forbids insert_wiki_page ...",
    "failure_reason": "permanent_error"

(a) ``classification`` and ``failure_reason`` disagree about the same fact,
    because ``_move_to_dlq``'s ``failure_reason`` parameter DEFAULTED to
    ``"permanent_error"`` and the retry-exhaustion call site passed nothing.
    Every exhausted-retry entry was mislabelled, not just this one.

(b) A ``WikiImmutableError`` is a deliberate refusal that can never succeed, yet
    it burned all 20 transient retries over 13 hours before landing.

The assertions here are on the STORED SIDECAR, not on "did it reach the DLQ" —
the defect was never that the entry was missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yadgar._shared.file_queue.queue import FileQueue
from yadgar._shared.storage.mutability_gate import WikiImmutableError
from yadgar.backend.queue_drainer import (
    DrainerConfig,
    QueueDrainer,
    _Attempt,
    _classify_error,
)

# The verbatim message the live gate produced, from the .events.log entry above.
_REAL_REFUSAL_MESSAGE = (
    "wiki page mutability='locked' forbids insert_wiki_page "
    "(page_id=None slug=None page_type='adr'). "
    "Pass _sanctioned=True for server-side lifecycle transitions, "
    "or use wiki_set_mutability to override."
)


def _locked_refusal() -> WikiImmutableError:
    return WikiImmutableError(
        _REAL_REFUSAL_MESSAGE,
        mutability="locked",
        page={"id": 7710, "slug": "m-agahi_yadgar_adr-0425", "page_type": "adr"},
        op="insert_wiki_page",
    )


@pytest.fixture()
def drainer(tmp_path):
    """A real drainer over a real FileQueue — no DB, no embeddings.

    ``max_transient_attempts=1`` + ``backoff_base_s=0`` so one failure exercises
    the genuine retry-exhaustion path without twenty backoff waits.
    """
    return QueueDrainer(
        queue=FileQueue(tmp_path),
        storage_factory=lambda: None,
        drain_interval=9999,
        config=DrainerConfig(max_transient_attempts=1, backoff_base_s=0.0),
    )


@pytest.fixture()
def stock_drainer(tmp_path):
    """The SHIPPED retry policy — ``max_transient_attempts`` is the stock 20.

    The refusal tests must not run against a lowered cap: with the cap at 1, a
    refusal reaching the DLQ after one attempt is indistinguishable from
    ordinary exhaustion, and ``attempts == 1`` would pass without the fix. At 20
    the pre-fix drainer records one failure and leaves the file in the queue, so
    the sidecar does not exist at all.
    """
    return QueueDrainer(
        queue=FileQueue(tmp_path),
        storage_factory=lambda: None,
        drain_interval=9999,
    )


def _queued(drainer: QueueDrainer, name: str = "job.json") -> Path:
    path = drainer._queue.queue_dir / name
    path.write_text(json.dumps({"op": "wiki_add", "payload": {}}))
    return path


def _sidecar(drainer: QueueDrainer, name: str = "job.json") -> dict:
    return json.loads((drainer._queue.dlq_dir / (name + ".error.json")).read_text())


# ── (a) the general defect: the default that contradicts the data ────────────


class TestFailureReasonFollowsClassification:
    def test_transient_exhaustion_does_not_claim_permanent(self, drainer):
        """The exact contradiction from the .events.log entry.

        A caller that passes no ``failure_reason`` must not have one invented
        that the sibling field disproves.
        """
        path = _queued(drainer)
        attempt = _Attempt(count=20, classification="transient", last_error="boom")

        drainer._move_to_dlq(path, attempt, "wiki_add")

        meta = _sidecar(drainer)
        assert meta["classification"] == "transient"
        assert meta["failure_reason"] == "transient_error"
        assert meta["failure_reason"] != "permanent_error"

    def test_permanent_exhaustion_still_says_permanent_error(self, drainer):
        """The parse-error path was accidentally correct — keep it that way."""
        path = _queued(drainer)
        attempt = _Attempt(count=3, classification="permanent", last_error="bad json")

        drainer._move_to_dlq(path, attempt, "memorize")

        assert _sidecar(drainer)["failure_reason"] == "permanent_error"

    def test_an_explicit_reason_is_never_overridden(self, drainer):
        """The three call sites that already name a reason must be untouched."""
        path = _queued(drainer)
        attempt = _Attempt(count=1, classification="permanent", last_error="dupe")

        drainer._move_to_dlq(path, attempt, "wiki_add", failure_reason="duplicate_detected")

        assert _sidecar(drainer)["failure_reason"] == "duplicate_detected"

    def test_retry_exhaustion_through_apply_pending_agrees_with_itself(self, drainer, monkeypatch):
        """End-to-end at the real seam, not at the helper.

        ``max_transient_attempts=1``, so this single failure IS exhaustion.
        """

        def _boom(data, path):
            raise RuntimeError("connection reset by peer")

        monkeypatch.setattr(drainer, "_apply_with_stage_metrics", _boom)
        path = _queued(drainer)

        drainer._apply_pending("job.json", path, {"op": "wiki_add"}, "wiki_add", 1000.0)

        meta = _sidecar(drainer)
        assert meta["classification"] == "transient"
        assert meta["failure_reason"] == "transient_error"


# ── (b) the specific defect: a refusal is not a transient fault ──────────────


class TestRefusalSkipsRetries:
    def test_a_locked_page_refusal_does_not_burn_the_retry_budget(self, stock_drainer, monkeypatch):
        """attempts must be 1, not 20 — one apply was attempted, one is recorded."""

        def _refuse(data, path):
            raise _locked_refusal()

        monkeypatch.setattr(stock_drainer, "_apply_with_stage_metrics", _refuse)
        path = _queued(stock_drainer)

        stock_drainer._apply_pending("job.json", path, {"op": "wiki_add"}, "wiki_add", 1000.0)

        meta = _sidecar(stock_drainer)
        assert meta["attempts"] == 1
        assert meta["classification"] == "permanent"

    def test_the_refusal_names_its_own_reason(self, stock_drainer, monkeypatch):
        """``wiki_page_locked``, not the collapsed ``policy_rejected``.

        The operator fix differs per reason — a locked page needs
        ``wiki_set_mutability``, a derived page needs its generator re-run.
        """

        def _refuse(data, path):
            raise _locked_refusal()

        monkeypatch.setattr(stock_drainer, "_apply_with_stage_metrics", _refuse)

        stock_drainer._apply_pending(
            "job.json", _queued(stock_drainer), {"op": "wiki_add"}, "wiki_add", 1.0
        )

        assert _sidecar(stock_drainer)["failure_reason"] == "wiki_page_locked"

    def test_the_refusal_report_is_carried_as_metadata(self, stock_drainer, monkeypatch):
        """The structured evidence exists; it must not be dropped at this seam."""

        def _refuse(data, path):
            raise _locked_refusal()

        monkeypatch.setattr(stock_drainer, "_apply_with_stage_metrics", _refuse)

        stock_drainer._apply_pending(
            "job.json", _queued(stock_drainer), {"op": "wiki_add"}, "wiki_add", 1.0
        )

        fmeta = _sidecar(stock_drainer)["failure_metadata"]
        assert fmeta["mutability"] == "locked"
        assert fmeta["slug"] == "m-agahi_yadgar_adr-0425"
        assert fmeta["page_type"] == "adr"

    def test_the_in_memory_attempt_entry_is_released(self, stock_drainer, monkeypatch):
        """A refusal can arrive on a RETRY, so the tracker entry may already exist.

        The queue file is gone after the move; a surviving entry is a leak.
        """

        def _refuse(data, path):
            raise _locked_refusal()

        monkeypatch.setattr(stock_drainer, "_apply_with_stage_metrics", _refuse)
        stock_drainer._attempts["job.json"] = _Attempt(count=4, classification="transient")

        stock_drainer._apply_pending(
            "job.json", _queued(stock_drainer), {"op": "wiki_add"}, "wiki_add", 1.0
        )

        assert "job.json" not in stock_drainer._attempts
        # Four prior failures plus this one — the count stays honest either way.
        assert _sidecar(stock_drainer)["attempts"] == 5


class TestWhyNotAStringMatch:
    """Evidence for choosing ``isinstance(exc, AdminRefusal)`` over another regex.

    ``_classify_error``'s ``\\b4\\d\\d\\b`` was meant to catch an HTTP 4xx. It
    also matches any bare three-digit number starting with 4 anywhere in the
    message — and the refusal message embeds ``page_id=``.
    """

    def test_the_real_refusal_message_reads_as_transient(self):
        """This is the 20-retry bug, reproduced at its root."""
        assert _classify_error(_REAL_REFUSAL_MESSAGE) == "transient"

    def test_a_page_id_decides_the_retry_budget(self):
        """The SAME refusal, classified differently by a database id.

        page_id=456 → 3 retries. page_id=4567 → 20. Nothing about the refusal
        changed; a string matcher cannot be made to care.
        """
        tmpl = "wiki page mutability='locked' forbids insert_wiki_page (page_id={})."
        assert _classify_error(tmpl.format(456)) == "permanent"
        assert _classify_error(tmpl.format(4567)) == "transient"


class TestTaxonomyBuckets:
    """A refusal reason must land where ``dlq_inspect`` / ``dlq_requeue`` look.

    Not cosmetic: ``dlq_requeue``'s taxonomy check is the ONLY thing that stops
    an operator requeueing a job the drainer will refuse again. A refusal reason
    outside the taxonomy mints a requeue loop.
    """

    def test_refusal_reasons_are_rejections(self):
        from yadgar.core.server.tools.admin_dlq import _REJECTION_TAXONOMY

        for reason in ("wiki_page_locked", "wiki_page_derived", "wiki_page_immutable"):
            assert reason in _REJECTION_TAXONOMY, reason

    def test_transient_exhaustion_stays_requeueable(self):
        """A genuine transient exhaustion SHOULD be retryable — it is a failure."""
        from yadgar.core.server.tools.admin_dlq import _REJECTION_TAXONOMY, _matches_filter

        assert "transient_error" not in _REJECTION_TAXONOMY
        assert _matches_filter("transient_error", "failures") is True
        assert _matches_filter("transient_error", "rejections") is False

    def test_a_locked_refusal_is_listed_by_the_rejections_filter(self):
        from yadgar.core.server.tools.admin_dlq import _matches_filter

        assert _matches_filter("wiki_page_locked", "rejections") is True
        assert _matches_filter("wiki_page_locked", "failures") is False
