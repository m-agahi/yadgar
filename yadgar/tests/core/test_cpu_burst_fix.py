"""v5.6.6 CPU burst fix — TDD tests.

Tests for:
  A. Hook lightweight profile (prompt-recall uses profile="fast")
  B. CE batch cap (cross_encoder_rerank slices candidates before expansion)
  C. Separate RERANK_BACKEND_TIMEOUT_SEC from BACKEND_HTTP_TIMEOUT_SEC
  D. NLI_RERANKING_ENABLED=False default
  E. HEAVY_RERANK_ENABLED=False kill switch
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# A. Hook lightweight profile
# ---------------------------------------------------------------------------


class TestHookLightweightProfile:
    """GET /hooks/prompt-recall must run recall with profile='fast'.

    v5.113.0: prompt-recall FORWARDS to the backend (hook-recall-forward plan),
    so 'fast' is now asserted on the forward path (_forward_to_backend), not the
    in-core retriever. The intent is unchanged: the hook must run the cheap
    BM25+HNSW-only profile, never the full CE/NLI/MP pipeline.
    """

    def test_prompt_recall_uses_fast_profile(self, monkeypatch):
        """hook_prompt_recall forwards recall with profile='fast', skipping CE/NLI."""
        import asyncio
        from unittest.mock import patch

        import yadgar._shared.runtime.state as _st

        # Spy on the forward-to-backend call the hook now drives.
        forward_kwargs: dict = {}

        def _spy_forward(**kwargs):
            forward_kwargs.update(kwargs)
            return []

        monkeypatch.setattr(_st, "_retriever", object())  # non-None so handler proceeds
        # Suppress throttle check
        monkeypatch.setattr(_st, "_last_session_context", {})
        monkeypatch.setattr(_st, "_last_prompt_recall", {})

        # Import and call the handler directly in asyncio
        import yadgar.core.server.http  # noqa: F401 — ensures routes registered
        from yadgar.core.server.http import hook_prompt_recall

        class _FakeRequest:
            query_params = {"query": "test query", "directory": "/tmp"}

        async def _run():
            with patch(
                "yadgar.core.server.tools.recall._forward_to_backend", side_effect=_spy_forward
            ):
                return await hook_prompt_recall(_FakeRequest())

        asyncio.run(_run())

        assert forward_kwargs.get("profile") == "fast", (
            f"Expected forward called with profile='fast', got {forward_kwargs.get('profile')!r}"
        )


# ---------------------------------------------------------------------------
# B. CE batch cap
# ---------------------------------------------------------------------------


class TestCEBatchCap:
    """cross_encoder_rerank slices memories to TOP_K before expansion."""

    def test_batch_capped_to_top_k(self, monkeypatch):
        """With 50 input memories and TOP_K=10, score_cross_encoder sees ≤20 texts."""
        import yadgar._shared.config as cfg

        monkeypatch.setenv("YADGAR_CROSS_ENCODER_TOP_K", "10")
        cfg.get_settings.cache_clear()

        from yadgar._shared.config import Settings
        from yadgar._shared.retrieval._reranking_cross_encoder import _CrossEncoderMixin

        settings = Settings()
        assert settings.CROSS_ENCODER_TOP_K == 10

        # Build a minimal Reranker-like object
        scored_texts: list[str] = []

        class _FakeML:
            def score_cross_encoder(self, query, texts):
                scored_texts.extend(texts)
                return [0.5] * len(texts)

        class _Reranker(_CrossEncoderMixin):
            def __init__(self):
                self._settings = settings
                self._ml = _FakeML()

        reranker = _Reranker()
        memories = [{"content": f"memory {i}", "_retrieval_score": 0.5} for i in range(50)]
        result = reranker.cross_encoder_rerank(memories, "test query")

        # With TOP_K=10, at most 10 base + up to 10 variants = max 20 texts scored
        assert len(scored_texts) <= 20, (
            f"Expected ≤20 texts sent to CE (TOP_K=10 + variants), got {len(scored_texts)}"
        )
        assert len(result) <= 10, f"Expected ≤10 results returned, got {len(result)}"

        cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# C. Separate RERANK_BACKEND_TIMEOUT_SEC
# ---------------------------------------------------------------------------


class TestRerankTimeout:
    """RemoteMLClient /rerank calls use RERANK_BACKEND_TIMEOUT_SEC, not BACKEND_HTTP_TIMEOUT_SEC."""

    def test_rerank_timeout_separate_from_general(self, monkeypatch):
        """RemoteMLClient has a dedicated _rerank_timeout using RERANK_BACKEND_TIMEOUT_SEC."""
        import yadgar._shared.config as cfg

        monkeypatch.setenv("YADGAR_BACKEND_HTTP_TIMEOUT_SEC", "5")
        monkeypatch.setenv("YADGAR_RERANK_BACKEND_TIMEOUT_SEC", "90")
        cfg.get_settings.cache_clear()

        try:
            import yadgar.backend.ml_client as ml

            client = ml.RemoteMLClient(base_url="http://127.0.0.1:19999")

            # General client timeout should be 5s
            assert client._client.timeout.read == pytest.approx(5.0), (
                f"General client read timeout should be 5.0, got {client._client.timeout.read}"
            )
            # Rerank-specific timeout should be 90s
            assert hasattr(client, "_rerank_timeout"), (
                "RemoteMLClient must have _rerank_timeout attribute"
            )
            assert client._rerank_timeout.read == pytest.approx(90.0), (
                f"Rerank timeout should be 90.0, got {client._rerank_timeout.read}"
            )
        finally:
            cfg.get_settings.cache_clear()

    def test_rerank_call_uses_rerank_timeout(self, monkeypatch):
        """score_cross_encoder passes _rerank_timeout (not default) for non-probe calls."""
        import yadgar._shared.config as cfg

        monkeypatch.setenv("YADGAR_BACKEND_HTTP_TIMEOUT_SEC", "5")
        monkeypatch.setenv("YADGAR_RERANK_BACKEND_TIMEOUT_SEC", "90")
        cfg.get_settings.cache_clear()

        try:
            import httpx

            import yadgar.backend.ml_client as ml

            client = ml.RemoteMLClient(base_url="http://127.0.0.1:19999")

            captured_timeout = {}

            def fake_post(url, *, json=None, timeout=None, **kw):
                captured_timeout["timeout"] = timeout
                raise httpx.ConnectError("no server")

            monkeypatch.setattr(client._client, "post", fake_post)

            # Disable CB so probe logic doesn't interfere
            client._breaker_enabled = False

            client.score_cross_encoder("q", ["text"])

            # Should have used _rerank_timeout (not None and not the probe timeout)
            t = captured_timeout.get("timeout")
            assert t is not None, "Expected explicit timeout passed to /rerank POST"
            assert t.read == pytest.approx(90.0), (
                f"Expected rerank timeout 90.0, got {t.read if hasattr(t, 'read') else t!r}"
            )
        finally:
            cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# D. NLI_RERANKING_ENABLED=False default
# ---------------------------------------------------------------------------


class TestNLIDefaultOff:
    """NLI_RERANKING_ENABLED defaults to False; overrideable via env."""

    def test_nli_off_by_default(self, monkeypatch):
        """Without env override, NLI_RERANKING_ENABLED is False."""
        monkeypatch.delenv("YADGAR_NLI_RERANKING_ENABLED", raising=False)

        from yadgar._shared.config import Settings

        s = Settings()
        assert s.NLI_RERANKING_ENABLED is False, (
            f"NLI_RERANKING_ENABLED should default to False, got {s.NLI_RERANKING_ENABLED}"
        )

    def test_nli_on_when_env_set(self, monkeypatch):
        """YADGAR_NLI_RERANKING_ENABLED=true enables NLI."""
        monkeypatch.setenv("YADGAR_NLI_RERANKING_ENABLED", "true")

        from yadgar._shared.config import Settings

        s = Settings()
        assert s.NLI_RERANKING_ENABLED is True, (
            f"Expected NLI_RERANKING_ENABLED=True with env set, got {s.NLI_RERANKING_ENABLED}"
        )


# ---------------------------------------------------------------------------
# E. HEAVY_RERANK_ENABLED=False kill switch
# ---------------------------------------------------------------------------


class TestHeavyRerankKillSwitch:
    """HEAVY_RERANK_ENABLED=False bypasses CE/NLI/MP entirely."""

    def test_kill_switch_skips_all_heavy_rerank(self, monkeypatch):
        """With HEAVY_RERANK_ENABLED=False, _apply_rerank_pipeline returns early — no CE/NLI/MP."""
        import yadgar._shared.config as cfg

        monkeypatch.setenv("YADGAR_HEAVY_RERANK_ENABLED", "false")
        cfg.get_settings.cache_clear()

        try:
            from yadgar._shared.config import Settings
            from yadgar._shared.retrieval.reranking import _RerankingMixin

            settings = Settings()
            assert settings.HEAVY_RERANK_ENABLED is False

            ce_called = []
            nli_called = []
            mp_called = []

            class _FakeReranker:
                def heuristic_rerank(self, mems, query, top_k=None):
                    return mems

                def cross_encoder_rerank(self, mems, query):
                    ce_called.append(True)
                    return mems

                def nli_rerank(self, query, mems):
                    nli_called.append(True)
                    return mems

                def multi_passage_rerank(self, query, mems, top_k):
                    mp_called.append(True)
                    return mems

                def mmr_rerank(self, mems, emb, top_k=5, lambda_param=0.7):
                    return mems

                def detect_adversarial(self, mems):
                    return {"confidence": 1.0, "is_uncertain": False, "score_gap": 0.0}

            class _FakeRetriever(_RerankingMixin):
                def __init__(self):
                    self._settings = settings
                    self._reranker = _FakeReranker()
                    self._rules_engine = None
                    self._engram = None
                    self._metacognition = None

                def _comparison_dual_search(self, *a, **kw):
                    return []

                def _search_profiles_and_beliefs(self, *a, **kw):
                    return []

            retriever = _FakeRetriever()
            memories = [{"id": i, "content": f"mem {i}", "_retrieval_score": 0.5} for i in range(5)]
            from yadgar._shared.retrieval.fusion import PROFILES
            from yadgar._shared.retrieval.reranking import RerankContext

            profile = PROFILES["balanced"]
            ctx = RerankContext(
                query="test query",
                query_analysis={},
                query_embedding=None,
                profile=profile,
                profile_name="balanced",
                open_domain_mode=False,
                use_cross_encoder=True,  # should be bypassed
                max_results=5,
            )
            result = retriever._apply_rerank_pipeline(
                memories,
                set(range(5)),
                ctx,
            )

            assert not ce_called, "CE should not be called when HEAVY_RERANK_ENABLED=False"
            assert not nli_called, "NLI should not be called when HEAVY_RERANK_ENABLED=False"
            assert not mp_called, (
                "Multi-passage should not be called when HEAVY_RERANK_ENABLED=False"
            )
            assert len(result) <= 5
        finally:
            cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# F. _apply_rerank_pipeline characterization — full pipeline (non-bypass path)
# ---------------------------------------------------------------------------


class TestApplyRerankPipelineCharacterization:
    """Characterization tests for _apply_rerank_pipeline after RerankContext refactor.

    Verifies that stage ordering and output are preserved when HEAVY_RERANK_ENABLED=True
    and all optional sub-stages are enabled/disabled individually.
    """

    def _make_retriever(self, settings):
        """Build a _RerankingMixin instance wired to a fake reranker."""
        from yadgar._shared.retrieval.reranking import _RerankingMixin

        call_log = []

        class _FakeReranker:
            def heuristic_rerank(self, mems, query, top_k=None):
                call_log.append(("heuristic", top_k))
                return mems

            def cross_encoder_rerank(self, mems, query):
                call_log.append(("ce",))
                # Tag each mem so we can verify CE ran
                for m in mems:
                    m["_cross_encoder_score"] = 0.8
                return mems

            def nli_rerank(self, query, mems):
                call_log.append(("nli",))
                for m in mems:
                    m["_nli_entailment_score"] = 0.6
                return mems

            def multi_passage_rerank(self, query, mems, top_k):
                call_log.append(("mp", top_k))
                return mems

            def mmr_rerank(self, mems, emb, top_k=5, lambda_param=0.7):
                call_log.append(("mmr",))
                return mems

            def detect_adversarial(self, mems):
                call_log.append(("adv",))
                return {"confidence": 0.9, "is_uncertain": False, "score_gap": 0.1}

        class _FakeRetriever(_RerankingMixin):
            def __init__(self):
                self._settings = settings
                self._reranker = _FakeReranker()
                self._rules_engine = None
                self._engram = None
                self._metacognition = None

            def _comparison_dual_search(self, *a, **kw):
                return []

            def _search_profiles_and_beliefs(self, *a, **kw):
                return []

        return _FakeRetriever(), call_log

    def test_all_optional_stages_disabled(self, monkeypatch):
        """When all optional stages are off, pipeline runs without errors and trims to max_results."""
        from yadgar._shared.config import Settings
        from yadgar._shared.retrieval.fusion import PROFILES
        from yadgar._shared.retrieval.reranking import RerankContext

        s = Settings(
            RERANKER_ENABLED=False,
            NLI_RERANKING_ENABLED=False,
            MULTI_PASSAGE_RERANKING_ENABLED=False,
            ADVERSARIAL_DETECTION_ENABLED=False,
            ADVERSARIAL_DIVERSITY_ENFORCEMENT=False,
            HEAVY_RERANK_ENABLED=True,
        )
        retriever, call_log = self._make_retriever(s)
        memories = [{"id": i, "content": f"m{i}", "_retrieval_score": float(i)} for i in range(10)]
        ctx = RerankContext(
            query="test",
            query_analysis={},
            query_embedding=None,
            profile=PROFILES["balanced"],
            profile_name="balanced",
            open_domain_mode=False,
            use_cross_encoder=False,
            max_results=5,
        )
        result = retriever._apply_rerank_pipeline(memories[:], set(range(10)), ctx)
        assert len(result) == 5
        # No reranker stage should have been called
        assert call_log == []

    def test_cross_encoder_called_when_enabled(self, monkeypatch):
        """CE runs when use_cross_encoder=True."""
        from yadgar._shared.config import Settings
        from yadgar._shared.retrieval.fusion import PROFILES
        from yadgar._shared.retrieval.reranking import RerankContext

        s = Settings(
            RERANKER_ENABLED=False,
            NLI_RERANKING_ENABLED=False,
            MULTI_PASSAGE_RERANKING_ENABLED=False,
            ADVERSARIAL_DETECTION_ENABLED=False,
            ADVERSARIAL_DIVERSITY_ENFORCEMENT=False,
            HEAVY_RERANK_ENABLED=True,
        )
        retriever, call_log = self._make_retriever(s)
        memories = [{"id": i, "content": f"m{i}", "_retrieval_score": float(i)} for i in range(3)]
        ctx = RerankContext(
            query="test",
            query_analysis={},
            query_embedding=None,
            profile=PROFILES["balanced"],
            profile_name="balanced",
            open_domain_mode=False,
            use_cross_encoder=True,
            max_results=3,
        )
        result = retriever._apply_rerank_pipeline(memories[:], set(range(3)), ctx)
        assert ("ce",) in call_log
        assert len(result) <= 3

    def test_heuristic_skipped_for_fast_profile(self, monkeypatch):
        """Heuristic reranker is skipped for 'fast' profile even when RERANKER_ENABLED=True."""
        from yadgar._shared.config import Settings
        from yadgar._shared.retrieval.fusion import PROFILES
        from yadgar._shared.retrieval.reranking import RerankContext

        s = Settings(
            RERANKER_ENABLED=True,
            NLI_RERANKING_ENABLED=False,
            MULTI_PASSAGE_RERANKING_ENABLED=False,
            ADVERSARIAL_DETECTION_ENABLED=False,
            ADVERSARIAL_DIVERSITY_ENFORCEMENT=False,
            HEAVY_RERANK_ENABLED=True,
        )
        retriever, call_log = self._make_retriever(s)
        memories = [{"id": i, "content": f"m{i}", "_retrieval_score": float(i)} for i in range(3)]
        ctx = RerankContext(
            query="test",
            query_analysis={},
            query_embedding=None,
            profile=PROFILES.get("fast", PROFILES["balanced"]),
            profile_name="fast",
            open_domain_mode=False,
            use_cross_encoder=False,
            max_results=3,
        )
        retriever._apply_rerank_pipeline(memories[:], set(range(3)), ctx)
        heuristic_calls = [c for c in call_log if c[0] == "heuristic"]
        assert heuristic_calls == [], "heuristic should be skipped for fast profile"

    def test_output_trimmed_to_max_results(self, monkeypatch):
        """Result list is always trimmed to max_results."""
        from yadgar._shared.config import Settings
        from yadgar._shared.retrieval.fusion import PROFILES
        from yadgar._shared.retrieval.reranking import RerankContext

        s = Settings(
            RERANKER_ENABLED=False,
            NLI_RERANKING_ENABLED=False,
            MULTI_PASSAGE_RERANKING_ENABLED=False,
            ADVERSARIAL_DETECTION_ENABLED=False,
            ADVERSARIAL_DIVERSITY_ENFORCEMENT=False,
            HEAVY_RERANK_ENABLED=True,
        )
        retriever, _ = self._make_retriever(s)
        memories = [{"id": i, "content": f"m{i}", "_retrieval_score": float(i)} for i in range(20)]
        ctx = RerankContext(
            query="test",
            query_analysis={},
            query_embedding=None,
            profile=PROFILES["balanced"],
            profile_name="balanced",
            open_domain_mode=False,
            use_cross_encoder=False,
            max_results=7,
        )
        result = retriever._apply_rerank_pipeline(memories[:], set(range(20)), ctx)
        assert len(result) == 7
