"""ADR-0077 hotfix — profile=fast must actually be fast.

Measured (Tempo, n=300+, Jul 4-9): backend /recall profile=fast ran the FULL
fanout — 45 SurrealDB queries, WikiProvider ~450ms, engram-link rerank
250-560ms — pushing hook p50 to ~2000ms AT the 2.0s budget (32-52% of prompt
turns got NO memory injection).

Fix under test (red-first):
  1. PROFILES["fast"] declares wiki=False + engram_links=False; all other
     profiles keep both enabled.
  2. _fanout_recall(profile="fast", type_filter="all") skips WikiProvider
     entirely. Explicit type_filter="wiki" still honors caller intent
     (mirrors the episodic-query wiki-skip, which is also type="all"-only).
  3. _apply_rerank_pipeline skips _rerank_engram_links when the profile
     disables it (fast); balanced/full unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from yadgar._shared.retrieval.profiles import PROFILES
from yadgar._shared.storage.directory import RecallScope

_DIR = "/home/test/yadgar-project"


# ---------------------------------------------------------------------------
# 1. Profile declarations
# ---------------------------------------------------------------------------


class TestFastProfileDeclaration:
    def test_fast_profile_disables_wiki(self):
        assert PROFILES["fast"].get("wiki", True) is False

    def test_fast_profile_disables_engram_links(self):
        assert PROFILES["fast"].get("engram_links", True) is False

    def test_other_profiles_keep_wiki_and_engram(self):
        for name in ("balanced", "full", "debug"):
            assert PROFILES[name].get("wiki", True) is True, name
            assert PROFILES[name].get("engram_links", True) is True, name

    def test_fusion_profiles_fast_also_disables_wiki_and_engram(self):
        """There are TWO PROFILES dicts (fusion.py is the one the monolithic
        Retriever.recall actually consumes — core.py imports it). Both must
        declare the fast-profile skips, or the rerank gate silently no-ops."""
        from yadgar.backend.retrieval.fusion import PROFILES as FUSION_PROFILES

        assert FUSION_PROFILES["fast"].get("wiki", True) is False
        assert FUSION_PROFILES["fast"].get("engram_links", True) is False
        for name in ("balanced", "full"):
            assert FUSION_PROFILES[name].get("wiki", True) is True, name
            assert FUSION_PROFILES[name].get("engram_links", True) is True, name


# ---------------------------------------------------------------------------
# 2. _fanout_recall skips WikiProvider on fast profile
# ---------------------------------------------------------------------------


def _fanout(profile, type_filter="all", monkey_wiki=None, monkey_memory=None):
    """Run _fanout_recall with both providers mocked; return the two mocks."""
    import yadgar._shared.runtime.state as _st
    import yadgar.backend.retrieval.recall_pipeline as _pl

    wiki_provider_cls = MagicMock(name="WikiProvider")
    wiki_provider_cls.return_value.candidates.return_value = monkey_wiki or []
    memory_provider_cls = MagicMock(name="MemoryProvider")
    memory_provider_cls.return_value.candidates.return_value = monkey_memory or []

    with (
        patch.object(_pl, "WikiProvider", wiki_provider_cls),
        patch.object(_pl, "MemoryProvider", memory_provider_cls),
        patch.object(_st, "_retriever", MagicMock()),
        patch.object(_st, "_wiki", MagicMock()),
    ):
        _pl._fanout_recall(
            query="architecture decisions",
            max_results=5,
            min_heat=0.0,
            recall_scope=RecallScope(project_id=_DIR),
            type_filter=type_filter,
            tags=None,
            profile=profile,
        )
    return memory_provider_cls, wiki_provider_cls


class TestFanoutSkipsWikiOnFast:
    def test_fast_profile_skips_wiki_provider(self):
        _mem, wiki = _fanout(profile="fast")
        wiki.assert_not_called()

    def test_fast_profile_still_runs_memory_provider(self):
        mem, _wiki = _fanout(profile="fast")
        mem.assert_called_once()
        mem.return_value.candidates.assert_called_once()

    def test_none_profile_still_runs_wiki_provider(self):
        _mem, wiki = _fanout(profile=None)
        wiki.assert_called_once()

    def test_balanced_profile_still_runs_wiki_provider(self):
        _mem, wiki = _fanout(profile="balanced")
        wiki.assert_called_once()

    def test_explicit_type_wiki_honors_caller_intent_even_on_fast(self):
        """type_filter='wiki' is explicit caller intent — the fast-profile wiki
        skip applies only to the default type='all' path (hook path)."""
        _mem, wiki = _fanout(profile="fast", type_filter="wiki")
        wiki.assert_called_once()

    def test_unknown_profile_does_not_crash_and_keeps_wiki(self):
        """Defensive: an unrecognized profile string must not raise and must
        not silently drop wiki (fail-open to full behavior)."""
        _mem, wiki = _fanout(profile="no-such-profile")
        wiki.assert_called_once()


# ---------------------------------------------------------------------------
# 3. _apply_rerank_pipeline skips engram-link enrichment on fast profile
# ---------------------------------------------------------------------------


def _run_rerank_pipeline(profile_name: str):
    """Drive _apply_rerank_pipeline on a stub with all stages identity-patched
    except _rerank_engram_links (a MagicMock we can assert on).

    ctx.profile is built from fusion.PROFILES — the dict core.py's monolithic
    recall() actually threads into RerankContext in production."""
    from yadgar.backend.retrieval.fusion import PROFILES as FUSION_PROFILES
    from yadgar.backend.retrieval.reranking import RerankContext, _RerankingMixin

    class _Harness(_RerankingMixin):
        def __init__(self):
            self._settings = SimpleNamespace(HEAVY_RERANK_ENABLED=True)

    h = _Harness()
    identity = lambda mems, *a, **k: mems  # noqa: E731
    for stage in (
        "_rerank_heuristic",
        "_rerank_comparison_merge",
        "_rerank_cross_encoder",
        "_rerank_nli",
        "_rerank_multi_passage",
        "_rerank_profile_belief_merge",
        "_rerank_mmr",
        "_rerank_adversarial_detect",
        "_rerank_rules",
        "_rerank_metacognition",
    ):
        setattr(h, stage, identity)
    engram_mock = MagicMock(side_effect=identity)
    h._rerank_engram_links = engram_mock

    ctx = RerankContext(
        query="q",
        query_analysis={},
        query_embedding=None,
        profile=FUSION_PROFILES[profile_name],
        profile_name=profile_name,
        open_domain_mode=False,
        use_cross_encoder=False,
        max_results=5,
    )
    mems = [{"id": 1, "content": "m", "heat": 0.5}]
    out = h._apply_rerank_pipeline(mems, {1}, ctx)
    return engram_mock, out


class TestRerankSkipsEngramOnFast:
    def test_fast_profile_skips_engram_links(self):
        engram, out = _run_rerank_pipeline("fast")
        engram.assert_not_called()
        assert out, "pipeline must still return results"

    def test_balanced_profile_keeps_engram_links(self):
        engram, _out = _run_rerank_pipeline("balanced")
        engram.assert_called_once()

    def test_full_profile_keeps_engram_links(self):
        engram, _out = _run_rerank_pipeline("full")
        engram.assert_called_once()
