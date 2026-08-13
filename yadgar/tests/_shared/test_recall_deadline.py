"""ADR-0077 hotfix — client deadline propagation into the backend recall pipeline.

Measured: post-#166 the hook's httpx client aborts at HOOK_RECALL_TIMEOUT_S (2.0s)
but the backend keeps computing up to ~2s past client abandonment — wasted CPU
that also holds the recall semaphore against the next hook.

Fix under test (red-first, cheap between-stage cancellation):
  1. _forward_to_backend accepts deadline_ms and includes it in the payload
     ONLY when not None (MCP recall path unchanged: no key sent).
  2. Backend RecallRequest accepts deadline_ms (int | None, default None).
  3. recall_route converts deadline_ms to a monotonic deadline and threads it
     into _fanout_recall.
  4. _fanout_recall: deadline already exceeded → skip the WikiProvider arm;
     threads deadline into MemoryProvider.
  5. Retriever.recall(deadline=...): checks between signal-collection stages —
     exceeded → skip remaining collects + skip the rerank pipeline, return the
     partial fusion result.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from yadgar._shared.storage.directory import RecallScope

_DIR = "/home/test/yadgar-project"


# ---------------------------------------------------------------------------
# 1. _forward_to_backend payload carries deadline_ms (opt-in only)
# ---------------------------------------------------------------------------


class TestForwardCarriesDeadline:
    def _capture_payload(self, monkeypatch, **kwargs):
        import httpx

        captured: dict = {}

        def _fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"results": []}
            return resp

        monkeypatch.setenv("YADGAR_EMBED_URL", "http://backend:8001")
        monkeypatch.setattr(httpx, "post", _fake_post)

        from yadgar.core.server.tools.recall import _forward_to_backend

        _forward_to_backend(
            query="q",
            max_results=5,
            min_heat=0.0,
            directory=_DIR,
            type_filter="all",
            tags=None,
            **kwargs,
        )
        return captured["payload"]

    def test_deadline_ms_included_when_set(self, monkeypatch):
        payload = self._capture_payload(monkeypatch, deadline_ms=2000)
        assert payload.get("deadline_ms") == 2000, payload

    def test_deadline_ms_omitted_when_none(self, monkeypatch):
        """MCP recall path (deadline_ms default None) must NOT send the key —
        keeps the payload wire-compatible with an older backend."""
        payload = self._capture_payload(monkeypatch)
        assert "deadline_ms" not in payload, payload


# ---------------------------------------------------------------------------
# 2 + 3. Backend RecallRequest + route threading
# ---------------------------------------------------------------------------


class TestBackendRouteThreadsDeadline:
    def test_recall_request_accepts_deadline_ms(self):
        from yadgar.backend.embed_service import RecallRequest

        # Car C7 (0047 §5 C7): project_id is now REQUIRED on RecallRequest
        # (it is the scope key; directory is retained but optional/non-scoping).
        req = RecallRequest(query="q", directory=_DIR, project_id="t/r", deadline_ms=1500)
        assert req.deadline_ms == 1500
        assert RecallRequest(query="q", directory=_DIR, project_id="t/r").deadline_ms is None

    def test_route_converts_deadline_ms_to_monotonic_deadline(self, monkeypatch):
        import asyncio
        import os as _os

        import httpx

        import yadgar.backend.embed_service.embed_service as _svc
        from yadgar.backend.embed_service import app

        original_ready = _svc._recall_engines_ready
        _svc._recall_engines_ready = True

        captured: dict = {}

        def _fake_fanout(*a, **kw):
            captured.update(kw)
            return []

        monkeypatch.setattr("yadgar.backend.retrieval.recall_pipeline._fanout_recall", _fake_fanout)
        monkeypatch.setattr(
            "yadgar.backend.retrieval.recall_pipeline._apply_recall_db_side_effects",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "yadgar._shared.runtime.lifecycle._get_storage",
            lambda: MagicMock(),
            raising=False,
        )

        _orig_root = _os.environ.get("YADGAR_ALLOW_ROOT", "0")
        _os.environ["YADGAR_ALLOW_ROOT"] = "1"
        try:

            async def _post():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://b") as c:
                    return await c.post(
                        "/recall",
                        json={
                            "query": "q",
                            "directory": _DIR,
                            "project_id": "t/r",
                            "deadline_ms": 5000,
                        },
                    )

            t0 = time.monotonic()
            resp = asyncio.run(_post())
        finally:
            _os.environ["YADGAR_ALLOW_ROOT"] = _orig_root
            _svc._recall_engines_ready = original_ready

        assert resp.status_code == 200, resp.text[:200]
        deadline = captured.get("deadline")
        assert deadline is not None, f"deadline not threaded into _fanout_recall: {captured}"
        # ~5s in the future on the monotonic clock (loose bounds for slow CI)
        assert t0 + 1.0 < deadline < t0 + 10.0, (deadline, t0)

    def test_route_threads_none_deadline_when_absent(self, monkeypatch):
        import asyncio
        import os as _os

        import httpx

        import yadgar.backend.embed_service.embed_service as _svc
        from yadgar.backend.embed_service import app

        original_ready = _svc._recall_engines_ready
        _svc._recall_engines_ready = True

        captured: dict = {}

        def _fake_fanout(*a, **kw):
            captured.update(kw)
            return []

        monkeypatch.setattr("yadgar.backend.retrieval.recall_pipeline._fanout_recall", _fake_fanout)
        monkeypatch.setattr(
            "yadgar.backend.retrieval.recall_pipeline._apply_recall_db_side_effects",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "yadgar._shared.runtime.lifecycle._get_storage",
            lambda: MagicMock(),
            raising=False,
        )

        _orig_root = _os.environ.get("YADGAR_ALLOW_ROOT", "0")
        _os.environ["YADGAR_ALLOW_ROOT"] = "1"
        try:

            async def _post():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://b") as c:
                    return await c.post(
                        "/recall",
                        json={"query": "q", "directory": _DIR, "project_id": "t/r"},
                    )

            resp = asyncio.run(_post())
        finally:
            _os.environ["YADGAR_ALLOW_ROOT"] = _orig_root
            _svc._recall_engines_ready = original_ready

        assert resp.status_code == 200, resp.text[:200]
        assert captured.get("deadline") is None, captured


# ---------------------------------------------------------------------------
# 4. _fanout_recall: exceeded deadline skips the wiki arm; threads to memory
# ---------------------------------------------------------------------------


def _fanout_with_deadline(deadline):
    import yadgar._shared.runtime.state as _st
    import yadgar.backend.retrieval.recall_pipeline as _pl

    wiki_cls = MagicMock(name="WikiProvider")
    wiki_cls.return_value.candidates.return_value = []
    mem_cls = MagicMock(name="MemoryProvider")
    mem_cls.return_value.candidates.return_value = []

    with (
        patch.object(_pl, "WikiProvider", wiki_cls),
        patch.object(_pl, "MemoryProvider", mem_cls),
        patch.object(_st, "_retriever", MagicMock()),
        patch.object(_st, "_wiki", MagicMock()),
    ):
        _pl._fanout_recall(
            query="architecture decisions",
            max_results=5,
            min_heat=0.0,
            recall_scope=RecallScope(project_id=_DIR),
            type_filter="all",
            tags=None,
            profile=None,
            deadline=deadline,
        )
    return mem_cls, wiki_cls


class TestFanoutDeadline:
    def test_exceeded_deadline_skips_wiki_arm(self):
        mem, wiki = _fanout_with_deadline(deadline=time.monotonic() - 1.0)
        wiki.assert_not_called()
        mem.assert_called_once()  # memory arm always attempted (partial result)

    def test_future_deadline_keeps_wiki_arm(self):
        _mem, wiki = _fanout_with_deadline(deadline=time.monotonic() + 60.0)
        wiki.assert_called_once()

    def test_deadline_threaded_into_memory_provider(self):
        mem, _wiki = _fanout_with_deadline(deadline=time.monotonic() + 60.0)
        _args, kwargs = mem.call_args
        assert "deadline" in kwargs, mem.call_args


# ---------------------------------------------------------------------------
# 5. Retriever.recall between-stage deadline checks
# ---------------------------------------------------------------------------


class TestRetrieverRecallDeadline:
    def _run_recall(self, deadline):
        """Drive Retriever.recall unbound on a MagicMock self — collect stages
        are mock methods we can assert (not) called."""
        from yadgar.backend.retrieval.core import Retriever

        mock = MagicMock()
        mock._settings = MagicMock()
        mock._resolve_query_and_candidate_k.return_value = ({}, None, False, [], 15)
        mock._collect_vector_scores.return_value = ([], None)
        mock._collect_graph_temporal_scores.return_value = (0.0, False)
        mock._fuse_scores.return_value = ([], {})
        mock._build_initial_results.return_value = ([], set(), False)
        mock._apply_rerank_pipeline.side_effect = lambda mems, _seen, _ctx: mems
        mock._strip_embeddings.side_effect = lambda mems: mems

        # Car H1 (§1.3): a falsy project_id now raises at the clause builder
        # instead of silently unscoping — this suite is about deadline
        # behavior, not scoping, so a real project_id keeps the premise true.
        out = Retriever.recall(mock, "q", max_results=5, deadline=deadline, project_id=_DIR)
        return mock, out

    def test_exceeded_deadline_skips_all_collect_stages(self):
        mock, out = self._run_recall(deadline=time.monotonic() - 1.0)
        mock._collect_fts_scores.assert_not_called()
        mock._collect_vector_scores.assert_not_called()
        mock._collect_graph_temporal_scores.assert_not_called()
        assert out == []

    def test_exceeded_deadline_skips_rerank_pipeline(self):
        mock, _out = self._run_recall(deadline=time.monotonic() - 1.0)
        mock._apply_rerank_pipeline.assert_not_called()

    def test_future_deadline_runs_all_stages(self):
        mock, _out = self._run_recall(deadline=time.monotonic() + 60.0)
        mock._collect_fts_scores.assert_called_once()
        mock._collect_vector_scores.assert_called_once()
        mock._collect_graph_temporal_scores.assert_called_once()
        mock._apply_rerank_pipeline.assert_called_once()

    def test_none_deadline_runs_all_stages(self):
        mock, _out = self._run_recall(deadline=None)
        mock._collect_fts_scores.assert_called_once()
        mock._apply_rerank_pipeline.assert_called_once()

    def test_graph_temporal_deadline_hit_skips_rerank(self):
        """When the extracted stage-3-5 helper reports deadline_hit=True, the
        rerank pipeline is skipped and the partial fusion result returned."""
        from yadgar.backend.retrieval.core import Retriever

        mock = MagicMock()
        mock._settings = MagicMock()
        mock._resolve_query_and_candidate_k.return_value = ({}, None, False, [], 15)
        mock._collect_vector_scores.return_value = ([], None)
        mock._collect_graph_temporal_scores.return_value = (0.0, True)  # deadline hit
        mock._fuse_scores.return_value = ([], {})
        mock._build_initial_results.return_value = ([], set(), False)
        mock._strip_embeddings.side_effect = lambda mems: mems

        # Car H1 (§1.3): see the identical comment in ``_run_recall`` above.
        Retriever.recall(
            mock, "q", max_results=5, deadline=time.monotonic() + 60.0, project_id=_DIR
        )
        mock._apply_rerank_pipeline.assert_not_called()


class TestCollectGraphTemporalScoresHelper:
    """Unit tests for the extracted stage-3-5 helper itself."""

    def _run(self, deadline):
        from yadgar.backend.retrieval.core import Retriever
        from yadgar.backend.retrieval.scoring import FTSParams

        mock = MagicMock()
        mock._collect_temporal_scores.return_value = 0.7
        params = FTSParams(
            query="q",
            enabled_signals=None,
            open_domain_subqueries=[],
            open_domain_mode=False,
            candidate_k=15,
            min_heat=0.0,
        )
        result = Retriever._collect_graph_temporal_scores(mock, params, {}, [], deadline)
        return mock, result

    def test_no_deadline_runs_all_three(self):
        mock, (w_temporal, hit) = self._run(deadline=None)
        mock._collect_ppr_scores.assert_called_once()
        mock._collect_spreading_scores.assert_called_once()
        mock._collect_temporal_scores.assert_called_once()
        assert w_temporal == 0.7
        assert hit is False

    def test_exceeded_deadline_stops_after_ppr(self):
        mock, (w_temporal, hit) = self._run(deadline=time.monotonic() - 1.0)
        mock._collect_ppr_scores.assert_called_once()  # first stage always attempted
        mock._collect_spreading_scores.assert_not_called()
        mock._collect_temporal_scores.assert_not_called()
        assert w_temporal == 0.0
        assert hit is True
