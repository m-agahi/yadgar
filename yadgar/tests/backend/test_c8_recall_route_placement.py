"""Car C8 (0047 §5 C8) — the ROUTE half: where the set is loaded, and by whom.

Two properties live only here, and neither is visible from the clause builder:

1. **PLACEMENT.** The superseded set must be read on the EVENT-LOOP side, before
   ``asyncio.to_thread(_run_pipeline)``. ``_fanout_recall`` is sync and runs in a
   worker thread while ``asyncmy`` is async-only, so a lookup on the far side of
   that boundary needs a private event loop per recall — a pool bound to a loop
   that dies with the thread. The hazard is closed by PLACEMENT, not by handling,
   and a comment saying so does not survive a refactor. ``test_loader_runs_on_the
   _loop_thread_and_the_pipeline_does_not`` compares thread identities, which is
   the only assertion that actually fails when someone moves the call downstream.

2. **THE OPT-IN FREE RIDER.** ``recall(tags=["superseded"])`` must both suppress
   the load AND have the token stripped before the tags reach the pipeline.
   Without the strip, the token becomes ``WikiProvider``'s ``include_tag`` and
   pre-filters the corpus down to pages carrying a tag no ADR page has — an
   opt-in that silently returns nothing, which is worse than no opt-in.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

import yadgar.backend.embed_service.embed_service as _es
from yadgar.backend.embed_service import embed_service_routes as routes
from yadgar.backend.embed_service.embed_service_models import RecallRequest

_PROJECT = "m-agahi/yadgar"
_SLUGS = ("m-agahi_yadgar_adr-0114", "m-agahi_yadgar_adr-0196")


class _FakeSqlEngine:
    def __init__(self, rows):
        self._rows = rows

    async def list_adr_rows(self, *, project_id, status=None, **_kw):
        return [r for r in self._rows if r["project_id"] == project_id]


@pytest.fixture
def wired(monkeypatch):
    """Patch the route's seams and capture what reaches ``_fanout_recall``."""
    from yadgar.backend.retrieval import recall_pipeline as rp

    captured: dict = {}

    monkeypatch.setattr(_es, "_ensure_recall_engines", lambda: None, raising=False)
    monkeypatch.setattr(
        "yadgar._shared.runtime.lifecycle._get_storage", lambda: MagicMock(), raising=False
    )
    monkeypatch.setattr(
        "yadgar._shared.runtime.lifecycle._get_sql_storage",
        lambda: _FakeSqlEngine(
            [{"project_id": _PROJECT, "id": 114 + i, "body_slug": s} for i, s in enumerate(_SLUGS)]
        ),
        raising=False,
    )

    def _fake_fanout(**kwargs):
        captured.update(kwargs)
        captured["pipeline_thread"] = threading.get_ident()
        return []

    monkeypatch.setattr(rp, "_fanout_recall", _fake_fanout, raising=False)
    monkeypatch.setattr(rp, "_compute_db_boost", lambda _m, _s: ([], "now"), raising=False)
    return captured


def _req(tags: list[str] | None = None) -> RecallRequest:
    return RecallRequest(query="adr", project_id=_PROJECT, tags=tags)


class TestTheSetReachesThePipeline:
    @pytest.mark.asyncio
    async def test_ledger_slugs_arrive_on_the_recall_scope(self, wired):
        await routes.recall_route(_req(), None)
        scope = wired["recall_scope"]
        assert scope.project_id == _PROJECT
        assert set(scope.excluded_slugs) == set(_SLUGS), (
            "the superseded set did not reach _fanout_recall — recall would rank "
            "superseded ADRs normally, with no other symptom"
        )

    @pytest.mark.asyncio
    async def test_an_unrelated_tag_does_not_suppress_the_exclusion(self, wired):
        await routes.recall_route(_req(["agent-prompt"]), None)
        assert set(wired["recall_scope"].excluded_slugs) == set(_SLUGS)
        assert wired["tags"] == ["agent-prompt"], "unrelated tags must pass through untouched"


class TestPlacement:
    @pytest.mark.asyncio
    async def test_loader_runs_on_the_loop_thread_and_the_pipeline_does_not(
        self, monkeypatch, wired
    ):
        """The whole async-hazard argument, as an assertion rather than a comment.

        If the load is ever moved downstream of ``asyncio.to_thread`` it will
        run on the SAME thread as the pipeline, and this test goes red — which
        is the only mechanical guard against re-creating the per-recall private
        event loop.
        """
        from yadgar.backend.retrieval import superseded as sup

        seen: dict = {}
        _real = sup.load_superseded_slugs

        async def _spy(engine, *, project_id):
            seen["loader_thread"] = threading.get_ident()
            return await _real(engine, project_id=project_id)

        monkeypatch.setattr(sup, "load_superseded_slugs", _spy, raising=False)

        await routes.recall_route(_req(), None)

        assert seen["loader_thread"] == threading.get_ident(), (
            "the superseded lookup ran off the event-loop thread — asyncmy is "
            "async-only, so that means a private event loop per recall"
        )
        assert wired["pipeline_thread"] != seen["loader_thread"], (
            "the pipeline no longer runs in a worker thread; the placement "
            "argument this car rests on is no longer being exercised"
        )


class TestOptInFreeRider:
    @pytest.mark.asyncio
    async def test_opt_in_returns_superseded_adrs(self, wired):
        await routes.recall_route(_req(["superseded"]), None)
        assert wired["recall_scope"].excluded_slugs == (), (
            "an explicit opt-in must lift the exclusion — this is ADR-0206's own "
            "sanctioned exclusion-with-opt-in hatch and the reason ADR-0228 can "
            "NARROW ADR-0206 rather than overturn it"
        )

    @pytest.mark.asyncio
    async def test_opt_in_token_is_stripped_before_the_pipeline(self, wired):
        """Left in ``tags`` it becomes ``include_tag`` and returns nothing."""
        await routes.recall_route(_req(["superseded"]), None)
        assert wired["tags"] is None, (
            "the opt-in token reached the provider tags — it would become "
            "include_tag and pre-filter to a tag no ADR page carries"
        )

    @pytest.mark.asyncio
    async def test_opt_in_alongside_a_real_tag_keeps_the_real_one(self, wired):
        await routes.recall_route(_req(["superseded", "agent-prompt"]), None)
        assert wired["tags"] == ["agent-prompt"]
        assert wired["recall_scope"].excluded_slugs == ()


class TestEngineAbsence:
    @pytest.mark.asyncio
    async def test_absent_engine_degrades_to_no_exclusion_loudly(self, monkeypatch, wired, caplog):
        """There is no fail-closed option — so the degradation must be LOGGED."""
        monkeypatch.setattr(
            "yadgar._shared.runtime.lifecycle._get_sql_storage", lambda: None, raising=False
        )
        with caplog.at_level("WARNING"):
            await routes.recall_route(_req(), None)
        assert wired["recall_scope"].excluded_slugs == ()
        assert any("INACTIVE" in r.message for r in caplog.records), (
            "an empty exclusion set has no other symptom; silence here is the bug"
        )
