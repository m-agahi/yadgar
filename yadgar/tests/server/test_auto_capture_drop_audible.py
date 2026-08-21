"""Car 20 / ledger task 303 — a dropped action_log batch must be audible.

``/hooks/auto-capture`` accumulates 5 actions per session then flushes them.
``_split_batch_by_project`` drops every action carrying no ``project_id``
(correctly — the drainer would reject such a row and it would land in the DLQ).
The *drop* was the problem, not the dropping: it was announced with a
``logger.debug`` the container's INFO level never emits, and an HTTP 200
``{"status": "dropped", "reason": "no_project_id"}`` that carries no count.

So when the standalone PostToolUse script stopped sending ``project_id``, the
capture pipeline went dead for ~6 days while every signal said healthy — 536
POSTs, all 200, an empty DLQ, and no log line anywhere. That is failure
rendered as well-formed success.

Two branches drop, and BOTH must be audible:

1. **wholly unattributed** — ``_groups`` empty, early return.
2. **partially unattributed** — some actions have an identity, some do not.
   This one already put ``dropped_unattributed`` in the response but logged
   nothing at all, so the common case (one orphan in an otherwise-healthy
   batch) was the quietest of the two.

The mechanism is the one this failure class already uses elsewhere —
``observe_project_id_skip(writer, count)`` beside a WARNING, exactly as
``backend/consolidation/cleanup.py`` does for the same rows one stage later.

The response stays **200**: the hook client catches ``HTTPError``, closes it
and returns, so a non-200 changes nothing observable at the hook while adding
a request-failure path to a fire-and-forget call. Loud and countable, not
failing.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_WRITER = "auto_capture_batch"


def _skip_counter_value(writer: str) -> float:
    from yadgar._shared.observability.metrics import yadgar_project_id_skipped_total

    for metric in yadgar_project_id_skipped_total.collect():
        for sample in metric.samples:
            if sample.labels.get("writer") == writer and sample.name.endswith("_total"):
                return sample.value
    return 0.0


def _call_handler(actions: list[dict], session_id: str) -> tuple[int, dict]:
    """Drive hook_auto_capture to a FLUSH and return (status_code, json body).

    The handler flushes at 5 actions, so 4 are pre-seeded into the batch and
    the 5th arrives on the request.
    """
    import yadgar._shared.runtime.state as _st
    import yadgar.core.server.http as _http

    seed, last = actions[:-1], actions[-1]

    mock_limiter = MagicMock()
    mock_limiter.allow.return_value = True

    mock_request = MagicMock()
    mock_request.json = AsyncMock(
        return_value={
            "tool_name": last["tool_name"],
            "summary": last.get("summary", ""),
            "directory": last.get("directory", "/tmp/test"),
            "session_id": session_id,
            "project_id": last.get("project_id", ""),
        }
    )

    batch = {
        session_id: [
            {
                "tool_name": a["tool_name"],
                "summary": a.get("summary", ""),
                "directory": a.get("directory", "/tmp/test"),
                "session_id": session_id,
                "project_id": a.get("project_id", ""),
            }
            for a in seed
        ]
    }

    fq = MagicMock()
    with (
        patch.object(_st, "_auto_capture_limiter", mock_limiter),
        patch.object(_st, "_storage", MagicMock()),
        patch.object(_st, "_consolidation", None),
        patch.object(_st, "_action_batch", batch),
        patch.object(_st, "_action_batch_lock", asyncio.Lock()),
        patch("yadgar.core.lifecycle._get_file_queue", return_value=fq),
    ):
        resp = asyncio.run(_http.hook_auto_capture(mock_request))

    import json as _json

    return resp.status_code, _json.loads(bytes(resp.body).decode())


def _action(pid: str, name: str = "Edit") -> dict:
    return {"tool_name": name, "summary": f"s-{pid or 'orphan'}", "project_id": pid}


# ── 1. wholly-unattributed batch ────────────────────────────────────────────


def test_wholly_dropped_batch_logs_at_warning(caplog: pytest.LogCaptureFixture):
    """A whole batch discarded must not be announced at DEBUG.

    This is the exact line that hid Car 20's outage: the container runs at
    INFO, so a logger.debug for a whole discarded batch emitted nothing.
    """
    actions = [_action("") for _ in range(5)]
    with caplog.at_level(logging.WARNING, logger="yadgar"):
        status, body = _call_handler(actions, "sess-wholly-dropped")

    assert status == 200, "the hook is fire-and-forget; the drop must not fail the request"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, (
        "a wholly-unattributed batch must log at WARNING — a logger.debug is "
        "invisible at the container's INFO level, which is how a 6-day capture "
        "outage looked identical to a healthy pipeline"
    )
    assert any("project_id" in r.getMessage() for r in warnings), (
        f"the warning must name the cause; got {[r.getMessage() for r in warnings]}"
    )


def test_wholly_dropped_batch_reports_the_count_in_the_response():
    actions = [_action("") for _ in range(5)]
    _status, body = _call_handler(actions, "sess-wholly-dropped-count")

    assert body.get("status") == "dropped"
    assert body.get("dropped_unattributed") == 5, (
        "the 200 response must carry the dropped count so a caller can see "
        f"that nothing landed; got {body!r}"
    )


def test_wholly_dropped_batch_increments_the_skip_metric():
    before = _skip_counter_value(_WRITER)
    actions = [_action("") for _ in range(5)]
    _call_handler(actions, "sess-wholly-dropped-metric")
    after = _skip_counter_value(_WRITER)

    assert after == before + 5, (
        f"yadgar_project_id_skipped_total{{writer={_WRITER}}} must count every "
        f"dropped action; before={before}, after={after}"
    )


# ── 2. partially-unattributed batch ─────────────────────────────────────────


def test_partial_drop_logs_at_warning(caplog: pytest.LogCaptureFixture):
    """The quieter half: a batch that DID write rows, minus some orphans.

    ``dropped_unattributed`` was already in the response here, but nothing was
    logged at any level — so the common case was even quieter than the total
    outage it neighbours.
    """
    actions = [
        _action("m-agahi/yadgar"),
        _action(""),
        _action("m-agahi/yadgar"),
        _action(""),
        _action("m-agahi/yadgar"),
    ]
    with caplog.at_level(logging.WARNING, logger="yadgar"):
        status, body = _call_handler(actions, "sess-partial")

    assert status == 200
    assert body.get("status") == "captured"
    assert body.get("dropped_unattributed") == 2

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, (
        "a partially-unattributed batch drops rows too and must log at WARNING; "
        "it previously logged nothing at any level"
    )


def test_partial_drop_increments_the_skip_metric():
    before = _skip_counter_value(_WRITER)
    actions = [
        _action("m-agahi/yadgar"),
        _action(""),
        _action("m-agahi/yadgar"),
        _action("m-agahi/yadgar"),
        _action("m-agahi/yadgar"),
    ]
    _call_handler(actions, "sess-partial-metric")
    after = _skip_counter_value(_WRITER)

    assert after == before + 1, f"the single orphan must be counted; before={before}, after={after}"


# ── 3. the healthy path stays silent ────────────────────────────────────────


def test_fully_attributed_batch_logs_no_warning_and_counts_nothing(
    caplog: pytest.LogCaptureFixture,
):
    """Loud on loss, silent on success — otherwise the WARNING becomes noise."""
    before = _skip_counter_value(_WRITER)
    actions = [_action("m-agahi/yadgar") for _ in range(5)]
    with caplog.at_level(logging.WARNING, logger="yadgar"):
        status, body = _call_handler(actions, "sess-healthy")
    after = _skip_counter_value(_WRITER)

    assert status == 200
    assert body.get("status") == "captured"
    assert body.get("dropped_unattributed") == 0
    assert after == before, "a healthy batch must not increment the skip counter"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "a fully attributed batch must not warn — a warning that fires on the "
        "healthy path is one operators learn to ignore"
    )


# ── 4. the label an operator reads is the label we emit ─────────────────────


def test_writer_label_is_documented_on_the_counter():
    """``yadgar_project_id_skipped_total``'s help text enumerates its writers.

    That list reads as exhaustive on ``/metrics``, so a label emitted but not
    named there is an undocumented series — the operator sees a counter rising
    under a writer the metric's own description says does not exist. I23 checks
    that declared metrics HAVE writers; nothing checked that a writer is
    declared, which is the direction this car added a label in.
    """
    from yadgar._shared.observability.metrics import yadgar_project_id_skipped_total

    doc = next(iter(yadgar_project_id_skipped_total.collect())).documentation
    assert _WRITER in doc, (
        f"{_WRITER!r} must appear in the counter's help string, which reads as an "
        f"exhaustive writer list; got: {doc!r}"
    )
