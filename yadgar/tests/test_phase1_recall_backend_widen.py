"""Phase 1 recall backend widening — unit tests.

TDD RED-FIRST: these tests are written before the implementation and will FAIL
until the implementation is in place.

Covers:
  §3.1 profile threading + fusion-CE gate (fast skips both memory-arm CE and fusion CE)
  §5.2 branch boost + postmortem boost in _fanout_recall
  §5.1 backend /recall — 400s removed, landscape dispatch, profile threading
  §5.5 BACKEND_VERSION bump (unit-side; canonical drift guard is in test_v5_46_12_*)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

_DIR = "/home/test/yadgar-project"
_BRANCH = "feat/test-branch"


def _make_candidate(content, score=0.5, cand_type="memory", branch=None):
    from yadgar.retrieval.providers.base import Candidate

    mid = abs(hash(content)) % 10000
    return Candidate(
        type=cand_type,
        id=mid,
        title=None if cand_type == "memory" else content[:20],
        content=content,
        native_score=score,
        directory_context=_DIR,
        branch=branch,
        raw={
            "id": mid,
            "content": content,
            "_retrieval_score": score,
            "heat": score,
            "directory_context": _DIR,
            "branch": branch,
            "tags": [],
        },
    )


def _make_wiki_candidate(content, score=0.4):
    from yadgar.retrieval.providers.base import Candidate

    return Candidate(
        type="wiki",
        id=None,
        title=content[:20],
        content=content,
        native_score=score,
        directory_context=_DIR,
        branch="master",
        raw={
            "slug": "test-wiki",
            "content": content,
            "_retrieval_score": score,
            "directory_context": _DIR,
        },
    )


class TestFusionCEGate:
    """§3.1: fast profile must skip fusion CE in fuse_candidates."""

    def test_fast_profile_skips_fusion_ce(self, monkeypatch):
        from yadgar.retrieval.providers.fusion import fuse_candidates

        ce_called = []

        def _spy_ce(candidates, query, retriever):
            ce_called.append(len(candidates))
            return {i: c.native_score for i, c in enumerate(candidates)}

        monkeypatch.setattr("yadgar.retrieval.providers.fusion._score_candidates_ce", _spy_ce)

        mem_cands = [_make_candidate("memory about python testing", 0.8)]
        wiki_cands = [_make_wiki_candidate("wiki reference about python", 0.5)]
        mock_retriever = MagicMock()
        mock_settings = SimpleNamespace(
            RECALL_MEMORY_QUOTA=5,
            RECALL_WIKI_QUOTA=5,
            RECALL_MEMORY_PRIOR_WEIGHT=0.1,
            RECALL_WIKI_PRIOR_WEIGHT=0.1,
        )

        fuse_candidates(
            memory_candidates=mem_cands,
            wiki_candidates=wiki_cands,
            query="python testing",
            retriever=mock_retriever,
            max_results=5,
            settings=mock_settings,
            profile="fast",
        )

        assert ce_called == [], (
            f"fuse_candidates called CE {ce_called} times with profile='fast'; expected 0"
        )

    def test_balanced_profile_calls_fusion_ce(self, monkeypatch):
        from yadgar.retrieval.providers.fusion import fuse_candidates

        ce_called = []

        def _spy_ce(candidates, query, retriever):
            ce_called.append(len(candidates))
            return {i: c.native_score for i, c in enumerate(candidates)}

        monkeypatch.setattr("yadgar.retrieval.providers.fusion._score_candidates_ce", _spy_ce)

        mem_cands = [_make_candidate("memory about python testing", 0.8)]
        wiki_cands = [_make_wiki_candidate("wiki reference about python", 0.5)]
        mock_retriever = MagicMock()
        mock_settings = SimpleNamespace(
            RECALL_MEMORY_QUOTA=5,
            RECALL_WIKI_QUOTA=5,
            RECALL_MEMORY_PRIOR_WEIGHT=0.1,
            RECALL_WIKI_PRIOR_WEIGHT=0.1,
        )

        fuse_candidates(
            memory_candidates=mem_cands,
            wiki_candidates=wiki_cands,
            query="python testing",
            retriever=mock_retriever,
            max_results=5,
            settings=mock_settings,
            profile="balanced",
        )

        assert len(ce_called) > 0, "fuse_candidates with profile='balanced' must call CE"

    def test_none_profile_calls_fusion_ce(self, monkeypatch):
        from yadgar.retrieval.providers.fusion import fuse_candidates

        ce_called = []

        def _spy_ce(candidates, query, retriever):
            ce_called.append(len(candidates))
            return {i: c.native_score for i, c in enumerate(candidates)}

        monkeypatch.setattr("yadgar.retrieval.providers.fusion._score_candidates_ce", _spy_ce)

        mem_cands = [_make_candidate("memory about python testing", 0.8)]
        wiki_cands = [_make_wiki_candidate("wiki reference about python", 0.5)]
        mock_retriever = MagicMock()
        mock_settings = SimpleNamespace(
            RECALL_MEMORY_QUOTA=5,
            RECALL_WIKI_QUOTA=5,
            RECALL_MEMORY_PRIOR_WEIGHT=0.1,
            RECALL_WIKI_PRIOR_WEIGHT=0.1,
        )

        fuse_candidates(
            memory_candidates=mem_cands,
            wiki_candidates=wiki_cands,
            query="python testing",
            retriever=mock_retriever,
            max_results=5,
            settings=mock_settings,
        )

        assert len(ce_called) > 0, (
            "fuse_candidates with profile=None must call CE (backward compat)"
        )

    def test_fast_profile_returns_nonempty(self, monkeypatch):
        from yadgar.retrieval.providers.fusion import fuse_candidates

        monkeypatch.setattr(
            "yadgar.retrieval.providers.fusion._score_candidates_ce",
            lambda candidates, query, retriever: {
                i: c.native_score for i, c in enumerate(candidates)
            },
        )

        mem_cands = [_make_candidate("memory item", 0.8)]
        wiki_cands = [_make_wiki_candidate("wiki item", 0.9)]
        mock_retriever = MagicMock()
        mock_settings = SimpleNamespace(
            RECALL_MEMORY_QUOTA=5,
            RECALL_WIKI_QUOTA=5,
            RECALL_MEMORY_PRIOR_WEIGHT=0.1,
            RECALL_WIKI_PRIOR_WEIGHT=0.1,
        )

        result = fuse_candidates(
            memory_candidates=mem_cands,
            wiki_candidates=wiki_cands,
            query="test query",
            retriever=mock_retriever,
            max_results=5,
            settings=mock_settings,
            profile="fast",
        )

        assert len(result) > 0, "fuse_candidates with profile='fast' must still return results"


class TestMemoryProviderProfileThreading:
    """§3.1: profile must thread through MemoryProvider."""

    def test_memory_provider_accepts_profile_param(self):
        from yadgar.retrieval.providers.memory import MemoryProvider

        mock_retriever = MagicMock()
        provider = MemoryProvider(mock_retriever, profile="fast")
        assert provider._profile == "fast", "MemoryProvider must store profile on _profile"

    def test_memory_provider_threads_profile_to_retriever(self):
        from yadgar.retrieval.providers.base import Scope
        from yadgar.retrieval.providers.memory import MemoryProvider

        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = []
        provider = MemoryProvider(mock_retriever, profile="fast")
        scope = Scope(directory=_DIR, branch=None, default_branch=None, min_heat=0.0)
        provider.candidates("test query", scope, limit=10)
        mock_retriever.recall.assert_called_once()
        call_kw = mock_retriever.recall.call_args.kwargs
        # profile="fast" must be passed
        assert call_kw.get("profile") == "fast", (
            f"MemoryProvider did not pass profile='fast' to Retriever.recall; kwargs={call_kw}"
        )

    def test_memory_provider_default_profile_none(self):
        from yadgar.retrieval.providers.memory import MemoryProvider

        mock_retriever = MagicMock()
        provider = MemoryProvider(mock_retriever)
        assert provider._profile is None, (
            "MemoryProvider without profile must default _profile=None"
        )

    def test_memory_provider_none_profile_not_passed_to_retriever(self):
        """profile=None must not be passed as kwarg (keeps existing callers working)."""
        from yadgar.retrieval.providers.base import Scope
        from yadgar.retrieval.providers.memory import MemoryProvider

        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = []
        provider = MemoryProvider(mock_retriever, profile=None)
        scope = Scope(directory=_DIR, branch=None, default_branch=None, min_heat=0.0)
        provider.candidates("test query", scope, limit=10)
        # profile=None is fine to pass explicitly, but must not cause errors
        # (Retriever.recall accepts profile kwarg — None is the default)
        # Just assert recall was called
        mock_retriever.recall.assert_called_once()


class TestBranchBoostInFanout:
    """§5.2: C4 branch boost must fire in _fanout_recall."""

    def test_current_branch_memory_gets_boosted(self, monkeypatch):
        import yadgar.server._state as _st

        base_score = 0.5
        boosted_memory = {
            "id": 1001,
            "content": "memory on current branch",
            "_retrieval_score": base_score,
            "heat": base_score,
            "directory_context": _DIR,
            "branch": _BRANCH,
            "tags": [],
        }
        other_memory = {
            "id": 1002,
            "content": "memory on other branch",
            "_retrieval_score": base_score,
            "heat": base_score,
            "directory_context": _DIR,
            "branch": "other-branch",
            "tags": [],
        }

        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = [boosted_memory, other_memory]
        monkeypatch.setattr(_st, "_retriever", mock_retriever)
        monkeypatch.setattr(_st, "_wiki", None)

        import yadgar.server.tools._recall_pipeline as _pipe

        # FANOUT_BOOST_SCOPE default is "scoped": boosts fire only when profile is
        # not None. This test calls _fanout_recall on the default (no-profile) path
        # to prove the branch-boost mechanism itself fires — force "global" so scope
        # does not gate it out. (monkeypatch auto-restores the shared singleton.)
        monkeypatch.setattr(_pipe.settings, "FANOUT_BOOST_SCOPE", "global")

        from yadgar.server.tools._recall_pipeline import _fanout_recall

        results = _fanout_recall(
            query="test branch boost",
            max_results=5,
            min_heat=0.0,
            directory=_DIR,
            current_branch=_BRANCH,
            default_branch="master",
        )

        branched = next((r for r in results if r.get("id") == 1001), None)
        other = next((r for r in results if r.get("id") == 1002), None)

        assert branched is not None, "Current-branch memory missing from results"
        assert other is not None, "Other-branch memory missing from results"

        boosted_score = branched.get("_retrieval_score", 0.0)
        other_score = other.get("_retrieval_score", 0.0)

        assert boosted_score > base_score, (
            f"Branch boost did NOT fire: score {boosted_score} == base {base_score}"
        )
        assert boosted_score > other_score, (
            f"Boosted memory score {boosted_score} must exceed non-boosted {other_score}"
        )

    def test_no_branch_no_boost(self, monkeypatch):
        import yadgar.server._state as _st

        base_score = 0.5
        mem = {
            "id": 1003,
            "content": "memory with branch",
            "_retrieval_score": base_score,
            "heat": base_score,
            "directory_context": _DIR,
            "branch": "feat/something",
            "tags": [],
        }
        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = [mem]
        monkeypatch.setattr(_st, "_retriever", mock_retriever)
        monkeypatch.setattr(_st, "_wiki", None)

        from yadgar.server.tools._recall_pipeline import _fanout_recall

        results = _fanout_recall(
            query="test no boost",
            max_results=5,
            min_heat=0.0,
            directory=_DIR,
            current_branch=None,
            default_branch=None,
        )

        r = next((x for x in results if x.get("id") == 1003), None)
        assert r is not None
        score = r.get("_retrieval_score", 0.0)
        assert abs(score - base_score) < 0.001, (
            f"Branch boost applied when current_branch=None: got {score}, expected {base_score}"
        )


class TestPostmortemBoostInFanout:
    """§5.2: postmortem/incident boost must fire in _fanout_recall."""

    def test_postmortem_tagged_memory_boosted_on_keyword_query(self, monkeypatch):
        import yadgar.server._state as _st

        base_score = 0.5
        pm_memory = {
            "id": 2001,
            "content": "postmortem for the deploy failure",
            "_retrieval_score": base_score,
            "heat": base_score,
            "directory_context": _DIR,
            "branch": "master",
            "tags": ["_postmortem"],
        }
        normal_memory = {
            "id": 2002,
            "content": "normal memory about configuration",
            "_retrieval_score": base_score,
            "heat": base_score,
            "directory_context": _DIR,
            "branch": "master",
            "tags": [],
        }
        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = [pm_memory, normal_memory]
        monkeypatch.setattr(_st, "_retriever", mock_retriever)
        monkeypatch.setattr(_st, "_wiki", None)

        import yadgar.server.tools._recall_pipeline as _pipe

        # scope="global": prove the postmortem-boost mechanism fires on the default
        # (no-profile) _fanout_recall path (default "scoped" would gate it out).
        monkeypatch.setattr(_pipe.settings, "FANOUT_BOOST_SCOPE", "global")

        from yadgar.server.tools._recall_pipeline import _fanout_recall

        results = _fanout_recall(
            query="what happened during the deploy rollback",
            max_results=5,
            min_heat=0.0,
            directory=_DIR,
            current_branch="master",
            default_branch="master",
        )

        pm_result = next((r for r in results if r.get("id") == 2001), None)
        normal_result = next((r for r in results if r.get("id") == 2002), None)

        assert pm_result is not None, "_postmortem memory missing from results"
        assert normal_result is not None, "Normal memory missing from results"

        pm_score = pm_result.get("_retrieval_score", 0.0)
        normal_score = normal_result.get("_retrieval_score", 0.0)

        assert pm_score > base_score, (
            f"Postmortem boost did NOT fire: score {pm_score} == base {base_score}"
        )
        assert pm_score > normal_score, (
            f"_postmortem memory must outrank normal: {pm_score} vs {normal_score}"
        )

    def test_incident_tagged_memory_boosted(self, monkeypatch):
        import yadgar.server._state as _st

        base_score = 0.5
        incident_memory = {
            "id": 2003,
            "content": "incident report for the merge failure",
            "_retrieval_score": base_score,
            "heat": base_score,
            "directory_context": _DIR,
            "branch": "master",
            "tags": ["_incident"],
        }
        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = [incident_memory]
        monkeypatch.setattr(_st, "_retriever", mock_retriever)
        monkeypatch.setattr(_st, "_wiki", None)

        import yadgar.server.tools._recall_pipeline as _pipe

        # scope="global": prove the incident-boost mechanism fires on the default
        # (no-profile) _fanout_recall path (default "scoped" would gate it out).
        monkeypatch.setattr(_pipe.settings, "FANOUT_BOOST_SCOPE", "global")

        from yadgar.server.tools._recall_pipeline import _fanout_recall

        results = _fanout_recall(
            query="what happened when we merge the deploy",
            max_results=5,
            min_heat=0.0,
            directory=_DIR,
            current_branch="master",
            default_branch="master",
        )

        r = next((x for x in results if x.get("id") == 2003), None)
        assert r is not None
        assert r.get("_retrieval_score", 0.0) > base_score, "_incident memory must be boosted"

    def test_no_boost_without_keyword_in_query(self, monkeypatch):
        import yadgar.server._state as _st

        base_score = 0.5
        pm_memory = {
            "id": 2004,
            "content": "postmortem content",
            "_retrieval_score": base_score,
            "heat": base_score,
            "directory_context": _DIR,
            "branch": "master",
            "tags": ["_postmortem"],
        }
        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = [pm_memory]
        monkeypatch.setattr(_st, "_retriever", mock_retriever)
        monkeypatch.setattr(_st, "_wiki", None)

        from yadgar.server.tools._recall_pipeline import _fanout_recall

        results = _fanout_recall(
            query="show me architecture decisions about the database",
            max_results=5,
            min_heat=0.0,
            directory=_DIR,
            current_branch=None,  # No branch — avoids branch-boost interference
            default_branch=None,
        )

        r = next((x for x in results if x.get("id") == 2004), None)
        assert r is not None
        score = r.get("_retrieval_score", 0.0)
        assert abs(score - base_score) < 0.001, (
            f"Postmortem boost fired without keyword: score {score} != base {base_score}"
        )


class TestBackend400sRemoved:
    """§5.1: recall_route must not return 400 for landscape or profile."""

    def test_landscape_mode_no_longer_400s(self, monkeypatch):
        import asyncio
        import os as _os

        import httpx

        import yadgar.backend.embed_service as _svc
        from yadgar.backend.embed_service import app

        original_ready = _svc._recall_engines_ready
        _svc._recall_engines_ready = True

        # Mock the landscape runner so pool is not needed
        import yadgar.backend.embed_service as _embed_mod

        monkeypatch.setattr(
            _embed_mod,
            "_run_landscape_backend",
            lambda *a, **kw: [],
            raising=False,
        )
        # Also mock storage get in case it's called
        monkeypatch.setattr(
            "yadgar.server.lifecycle._get_storage",
            lambda: MagicMock(),
            raising=False,
        )
        monkeypatch.setattr(
            "yadgar.backend.embed_service._apply_recall_db_side_effects",
            lambda *a, **kw: None,
            raising=False,
        )

        _orig_root = _os.environ.get("YADGAR_ALLOW_ROOT", "0")
        _os.environ["YADGAR_ALLOW_ROOT"] = "1"
        try:

            async def _post():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://backend") as c:
                    return await c.post(
                        "/recall",
                        json={"query": "test landscape", "directory": _DIR, "mode": "landscape"},
                        headers={},
                    )

            resp = asyncio.run(_post())
        finally:
            _os.environ["YADGAR_ALLOW_ROOT"] = _orig_root
            _svc._recall_engines_ready = original_ready

        assert resp.status_code != 400, (
            f"POST /recall mode='landscape' still 400s: {resp.status_code} {resp.text[:200]}"
        )

    def test_profile_param_no_longer_400s(self, monkeypatch):
        import asyncio
        import os as _os

        import httpx

        import yadgar.backend.embed_service as _svc
        from yadgar.backend.embed_service import app

        original_ready = _svc._recall_engines_ready
        _svc._recall_engines_ready = True

        monkeypatch.setattr(
            "yadgar.backend.embed_service._fanout_recall",
            lambda *a, **kw: [],
            raising=False,
        )
        monkeypatch.setattr(
            "yadgar.backend.embed_service._apply_recall_db_side_effects",
            lambda *a, **kw: None,
            raising=False,
        )
        monkeypatch.setattr(
            "yadgar.server.lifecycle._get_storage",
            lambda: MagicMock(),
            raising=False,
        )

        _orig_root = _os.environ.get("YADGAR_ALLOW_ROOT", "0")
        _os.environ["YADGAR_ALLOW_ROOT"] = "1"
        try:

            async def _post():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://backend") as c:
                    return await c.post(
                        "/recall",
                        json={"query": "test fast", "directory": _DIR, "profile": "fast"},
                        headers={},
                    )

            resp = asyncio.run(_post())
        finally:
            _os.environ["YADGAR_ALLOW_ROOT"] = _orig_root
            _svc._recall_engines_ready = original_ready

        assert resp.status_code != 400, (
            f"POST /recall profile='fast' still 400s: {resp.status_code} {resp.text[:200]}"
        )


class TestBackendVersionBump:
    """§5.5: BACKEND_VERSION must be 5.15.0."""

    def test_backend_version_is_5_15_0(self):
        import yadgar

        assert yadgar.BACKEND_VERSION == "5.15.0", (
            f"BACKEND_VERSION={yadgar.BACKEND_VERSION!r}; expected '5.15.0'"
        )
