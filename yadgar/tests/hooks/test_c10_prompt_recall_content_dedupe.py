"""C10 — task #340: auto-recall throttle must be content-dedupe, not time-only.

Pre-C10: ``hook_prompt_recall`` throttled by HARDCODED TIME windows
(180s session-context + 120s per-directory). The risk the throttle exists
to suppress is REDUNDANT INJECTION, not call-rate. A time gate cannot tell
"this prompt is asking the same question as last prompt" from "this prompt
is asking a brand new question at the 121st second" — both return the same
``{"text": ""}`` shape and the second case wastes the injection.

C10 design:
  * Primary gate is CONTENT-BASED: if the current prompt's topic-set
    overlaps ≥80% with the last emission's topic-set, skip. The first
    emission in a session always runs (no prior emission to compare to).
  * Time gate stays as a SECONDARY HARD CAP: never more than N recalls per
    minute. Defensive — a runaway hook should still be cheap, and an
    accidental loop in the operator's session should not hammer the
    backend.

The throttle state dict grows by ONE entry: ``_last_emitted_topics``
keyed by directory. The last-emission text is hashed into a set of
topical tokens; an 80% Jaccard overlap with the current query's tokens
is the gate.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch


def _make_mock_request(params: dict) -> MagicMock:
    req = MagicMock()
    req.query_params = MagicMock()
    req.query_params.get = MagicMock(side_effect=lambda k, d="": params.get(k, d))
    return req


# ── Helpers ────────────────────────────────────────────────────────────────────


def _run_hook(query: str, directory: str, project: str = "owner/repo"):
    """Call ``hook_prompt_recall`` and return (status_code, body_dict)."""
    import yadgar.core.server.http as _http

    req = _make_mock_request({"query": query, "directory": directory, "project": project})

    async def _call():
        return await _http.hook_prompt_recall(req)

    resp = asyncio.run(_call())
    return resp.status_code, json.loads(resp.body)


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestPromptRecallContentDedupe:
    """The primary throttle is content overlap, not wall-clock time."""

    def test_first_emission_always_runs_no_prior_state(self):
        """With no prior emission stored, the hook MUST run the recall path.

        The first prompt of a session is the canonical case — there is no
        prior content to dedupe against.
        """
        with (
            patch("yadgar._shared.runtime.state._last_emitted_topics", {}),
            patch("yadgar._shared.runtime.state._last_session_context", {}),
            patch("yadgar._shared.runtime.state._last_prompt_recall", {}),
            patch(
                "yadgar.core.server.http._recall_with_timeout",
                return_value=[],
            ) as recall_mock,
        ):
            _run_hook("kubernetes pod restart policy", "/proj/unique-1")

            assert recall_mock.called, (
                "first emission must always run — there is no prior topic "
                "set to dedupe against. Pre-C10 time gate would still let "
                "this through (no prior timestamp) but C10's content gate "
                "must agree."
            )

    def test_high_overlap_skips_without_re_running_recall(self):
        """A prompt whose topic-set overlaps ≥80% with the last emission
        must skip the recall path. The skipped envelope is the existing
        ``{"text": "", "skipped": "content_dedupe"}`` shape.
        """
        from yadgar._shared.runtime import state as _st

        directory = "/proj/overlap-test"
        prior_emission = ["kubernetes", "pod", "restart", "policy", "deployment"]
        # Same query → 100% overlap → skip.
        current_query = "kubernetes pod restart policy deployment"

        with (
            patch.object(_st, "_last_emitted_topics", {directory: prior_emission}),
            patch.object(_st, "_last_session_context", {}),
            patch.object(_st, "_last_prompt_recall", {}),
            patch(
                "yadgar.core.server.http._recall_with_timeout",
                side_effect=AssertionError("recalled even though overlap≥80%"),
            ),
        ):
            status, body = _run_hook(current_query, directory)

        assert body.get("text") == "", body
        assert body.get("skipped") == "content_dedupe", (
            f"high-overlap prompt must skip via the content-dedupe gate, "
            f"got body={body!r}. The 120s time gate would still let this "
            f"through at the 121st second even when the prompt is identical."
        )

    def test_low_overlap_does_not_skip(self):
        """A prompt whose topic-set overlaps <80% with the last emission
        must NOT skip — even immediately after a prior emission.

        This is the entire point of replacing the time gate: a brand-new
        question should recall even if it lands at the 1st second.
        """
        directory = "/proj/distinct-test"
        prior_emission = ["kubernetes", "pod", "restart", "policy", "deployment"]
        # 5 prior topics, 5 brand-new topics → 0% overlap → run.
        current_query = "postgres vacuum autovacuum bloat index wraparound"

        with (
            patch("yadgar._shared.runtime.state._last_emitted_topics", {directory: prior_emission}),
            patch("yadgar._shared.runtime.state._last_session_context", {}),
            patch("yadgar._shared.runtime.state._last_prompt_recall", {}),
            patch(
                "yadgar.core.server.http._recall_with_timeout",
                return_value=[{"content": "new", "directory_context": directory}],
            ) as recall_mock,
        ):
            status, body = _run_hook(current_query, directory)

        assert recall_mock.called, (
            "low-overlap prompt must run the recall path — content gate "
            "failing-open is the whole reason this gate exists."
        )
        assert body.get("skipped") != "content_dedupe"

    def test_content_dedupe_is_primary_not_time(self):
        """A prompt landing AT THE 121st SECOND must still skip if its
        topic-set matches the last emission ≥80%. The time gate is no
        longer the primary signal.
        """
        import time as _time

        directory = "/proj/post-window"
        prior_emission = ["redis", "cache", "eviction", "ttl", "maxmemory"]
        # 200s past the prior recall timestamp — far past the 120s gate.
        # The content gate is the only thing that decides now.
        stale_time = _time.monotonic() - 200.0

        with (
            patch("yadgar._shared.runtime.state._last_emitted_topics", {directory: prior_emission}),
            patch("yadgar._shared.runtime.state._last_session_context", {}),
            patch("yadgar._shared.runtime.state._last_prompt_recall", {directory: stale_time}),
            patch(
                "yadgar.core.server.http._recall_with_timeout",
                side_effect=AssertionError("recalled despite ≥80% topic overlap"),
            ),
        ):
            status, body = _run_hook("redis cache eviction ttl maxmemory", directory)

        assert body.get("skipped") == "content_dedupe", (
            f"content gate must be primary — at 200s after a prior emission, "
            f"the time gate is already passed, but the content gate must "
            f"still skip a same-topic prompt. Got body={body!r}."
        )

    def test_emitted_topics_recorded_after_successful_recall(self):
        """After a successful recall, the emitted topics MUST be stored
        under the directory key so the NEXT call's content gate has
        something to compare against.
        """
        from yadgar._shared.runtime import state as _st

        directory = "/proj/record-emit"
        emitted_topics: dict = {}

        with (
            patch.object(_st, "_last_emitted_topics", emitted_topics),
            patch.object(_st, "_last_session_context", {}),
            patch.object(_st, "_last_prompt_recall", {}),
            patch(
                "yadgar.core.server.http._recall_with_timeout",
                return_value=[
                    {
                        "content": "redis cluster sharding gossip protocol",
                        "directory_context": directory,
                        "project_id": "owner/repo",
                    },
                    {
                        "content": "kubernetes ingress controller tls",
                        "directory_context": directory,
                        "project_id": "owner/repo",
                    },
                ],
            ),
        ):
            _run_hook("redis cluster sharding gossip", directory)

        assert directory in emitted_topics, (
            "after a successful recall the hook must record the emitted "
            "topic-set under the directory key so the next call's content "
            "gate can dedupe. Pre-C10 the hook only recorded the timestamp."
        )
        stored = emitted_topics[directory]
        # The stored topics come from the emitted content, not the query.
        assert any("redis" in t for t in stored), stored
        assert any("kubernetes" in t for t in stored), stored


class TestTimeGateBecomesSecondary:
    """The 180s/120s windows are no longer the primary gate — they exist
    only as a hard cap against runaway hooks. A prompt landing past the
    window with NO prior emission still runs the recall.
    """

    def test_no_prior_emission_at_any_time_always_runs(self):
        """First-ever call to a directory at any wall-clock time: no prior
        emission to dedupe against, recall path runs.

        Pre-C10 this was time-gated: a session 181s after startup could
        see ``{"skipped": "session_context_recent"}`` for the first-ever
        prompt of the directory. C10 removes that.
        """
        import time as _time

        directory = "/proj/first-ever"
        # Pre-stamp session-context to simulate a session 200s after startup.
        very_old = _time.monotonic() - 200.0

        with (
            patch("yadgar._shared.runtime.state._last_emitted_topics", {}),
            patch.object(
                _st := __import__(
                    "yadgar._shared.runtime.state", fromlist=["_last_session_context"]
                ),
                "_last_session_context",
                {directory: very_old},
            ),
            patch.object(_st, "_last_prompt_recall", {}),
            patch(
                "yadgar.core.server.http._recall_with_timeout",
                return_value=[],
            ) as recall_mock,
        ):
            _run_hook("brand new topic", directory)

        assert recall_mock.called, (
            "first-ever call at any wall-clock time must run recall — the "
            "time gate is no longer primary. Pre-C10 a 181s-after-startup "
            "session hit ``session_context_recent`` regardless of whether "
            "any content was ever emitted."
        )
