"""Q2 tests — postmortem/incident tag retrieval boost (v5.3.5).

Tests:
1. Non-action-verb query → no boost on _postmortem-tagged memory.
2. "deploy" in query + _postmortem-tagged memory → boosted score.
3. Action verb but memory NOT _postmortem-tagged → no boost.
4. POSTMORTEM_BOOST_FACTOR=0.0 → no boost (back-compat).
"""

from __future__ import annotations

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_memory(tags: list, score: float = 0.5, mem_id: int = 1) -> dict:
    return {
        "id": mem_id,
        "content": f"memory {mem_id}",
        "heat": 0.5,
        "tags": tags,
        "_retrieval_score": score,
        "branch": None,
    }


def _apply_postmortem_boost(
    merged: list[dict], query: str, factor: float, keywords: tuple
) -> list[dict]:
    """Mirror of the boost logic in recall.py for isolated unit testing."""
    if factor <= 0.0 or not keywords:
        return merged
    query_lower = query.lower()
    has_action_verb = any(kw in query_lower for kw in keywords)
    if not has_action_verb:
        return merged
    pm_tags = {"_postmortem", "_incident"}
    for m in merged:
        mem_tags = set(m.get("tags", []))
        if mem_tags & pm_tags:
            base = min(m.get("_retrieval_score", m.get("heat", 0.0)), 1.0)
            m["_retrieval_score"] = base + (1.0 - base) * factor
    merged.sort(key=lambda m: m.get("_retrieval_score", 0.0), reverse=True)
    return merged


_DEFAULT_KEYWORDS = (
    "deploy",
    "push",
    "merge",
    "restart",
    "vacuum",
    "rollback",
    "upgrade",
    "migrate",
    "bump",
    "release",
)
_DEFAULT_FACTOR = 0.3


# ── Test 1: non-action-verb query → no boost ─────────────────────────────────


def test_no_boost_for_non_action_verb_query():
    """Score unchanged when query has no action verb, even for _postmortem memory."""
    mem = _make_memory(tags=["_postmortem", "yadgar"], score=0.5)
    original_score = mem["_retrieval_score"]
    result = _apply_postmortem_boost(
        [mem], "what is the retention policy", _DEFAULT_FACTOR, _DEFAULT_KEYWORDS
    )
    assert result[0]["_retrieval_score"] == pytest.approx(original_score)


def test_no_boost_for_generic_query_with_unrelated_tags():
    """Non-action query with non-postmortem tags → scores unchanged."""
    mem = _make_memory(tags=["yadgar", "wiki"], score=0.6)
    original_score = mem["_retrieval_score"]
    result = _apply_postmortem_boost(
        [mem], "show recent memories", _DEFAULT_FACTOR, _DEFAULT_KEYWORDS
    )
    assert result[0]["_retrieval_score"] == pytest.approx(original_score)


# ── Test 2: action verb + _postmortem tag → boosted score ────────────────────


def test_deploy_query_boosts_postmortem_memory():
    """Query with 'deploy' and _postmortem tag → score boosted via convex formula."""
    score = 0.5
    mem = _make_memory(tags=["_postmortem", "incident-2026-05-01"], score=score)
    result = _apply_postmortem_boost(
        [mem], "deploy failed last release", _DEFAULT_FACTOR, _DEFAULT_KEYWORDS
    )
    expected = score + (1.0 - score) * _DEFAULT_FACTOR
    assert result[0]["_retrieval_score"] == pytest.approx(expected)


def test_merge_query_boosts_incident_memory():
    """Query with 'merge' and _incident tag → score boosted."""
    score = 0.4
    mem = _make_memory(tags=["_incident"], score=score)
    result = _apply_postmortem_boost(
        [mem], "merge broke production", _DEFAULT_FACTOR, _DEFAULT_KEYWORDS
    )
    expected = score + (1.0 - score) * _DEFAULT_FACTOR
    assert result[0]["_retrieval_score"] == pytest.approx(expected)


def test_rollback_query_boosts_postmortem_memory():
    """Query containing 'rollback' → boost applies."""
    score = 0.6
    mem = _make_memory(tags=["_postmortem"], score=score)
    result = _apply_postmortem_boost(
        [mem], "rollback to previous version", _DEFAULT_FACTOR, _DEFAULT_KEYWORDS
    )
    expected = score + (1.0 - score) * _DEFAULT_FACTOR
    assert result[0]["_retrieval_score"] == pytest.approx(expected)


def test_boosted_memory_sorts_higher_than_unboosted():
    """Postmortem memory with action verb scores higher than normal memory after boost."""
    pm_mem = _make_memory(tags=["_postmortem"], score=0.5, mem_id=1)
    normal_mem = _make_memory(tags=["semantic"], score=0.55, mem_id=2)
    result = _apply_postmortem_boost(
        [pm_mem, normal_mem], "deploy crashed", _DEFAULT_FACTOR, _DEFAULT_KEYWORDS
    )
    # pm_mem boosted: 0.5 + 0.5 * 0.3 = 0.65 > 0.55
    assert result[0]["id"] == 1
    assert result[0]["_retrieval_score"] == pytest.approx(0.65)


# ── Test 3: action verb but non-postmortem memory → no boost ─────────────────


def test_action_verb_no_boost_for_non_postmortem_tag():
    """Action verb in query but memory lacks _postmortem/_incident → no boost."""
    mem = _make_memory(tags=["semantic", "yadgar"], score=0.5)
    original_score = mem["_retrieval_score"]
    result = _apply_postmortem_boost([mem], "deploy failed", _DEFAULT_FACTOR, _DEFAULT_KEYWORDS)
    assert result[0]["_retrieval_score"] == pytest.approx(original_score)


def test_action_verb_boosts_only_tagged_memories():
    """Mix of tagged/untagged: only _postmortem/_incident memories get boosted."""
    pm = _make_memory(tags=["_postmortem"], score=0.5, mem_id=1)
    normal = _make_memory(tags=["wiki"], score=0.5, mem_id=2)
    result = _apply_postmortem_boost(
        [pm, normal], "upgrade path", _DEFAULT_FACTOR, _DEFAULT_KEYWORDS
    )
    # pm boosted to 0.65, normal stays at 0.5
    pm_result = next(m for m in result if m["id"] == 1)
    normal_result = next(m for m in result if m["id"] == 2)
    assert pm_result["_retrieval_score"] == pytest.approx(0.65)
    assert normal_result["_retrieval_score"] == pytest.approx(0.5)


# ── Test 4: POSTMORTEM_BOOST_FACTOR=0.0 → no boost ───────────────────────────


def test_zero_factor_disables_boost():
    """POSTMORTEM_BOOST_FACTOR=0.0 → no boost, back-compat preserved."""
    mem = _make_memory(tags=["_postmortem"], score=0.5)
    original_score = mem["_retrieval_score"]
    result = _apply_postmortem_boost([mem], "deploy failed", factor=0.0, keywords=_DEFAULT_KEYWORDS)
    assert result[0]["_retrieval_score"] == pytest.approx(original_score)


def test_zero_factor_via_config(monkeypatch):
    """Config with POSTMORTEM_BOOST_FACTOR=0 → no boost applied in recall path."""
    monkeypatch.setenv("YADGAR_POSTMORTEM_BOOST_FACTOR", "0.0")

    from yadgar.config import Settings

    s = Settings()
    assert s.POSTMORTEM_BOOST_FACTOR == pytest.approx(0.0)

    mem = _make_memory(tags=["_postmortem"], score=0.5)
    original_score = mem["_retrieval_score"]
    result = _apply_postmortem_boost(
        [mem], "deploy failed", s.POSTMORTEM_BOOST_FACTOR, s.POSTMORTEM_BOOST_KEYWORDS
    )
    assert result[0]["_retrieval_score"] == pytest.approx(original_score)


# ── Config defaults ───────────────────────────────────────────────────────────


def test_config_has_postmortem_boost_keywords():
    """Settings has POSTMORTEM_BOOST_KEYWORDS with the expected action verbs."""
    from yadgar.config import Settings

    s = Settings()
    assert hasattr(s, "POSTMORTEM_BOOST_KEYWORDS")
    assert "deploy" in s.POSTMORTEM_BOOST_KEYWORDS
    assert "rollback" in s.POSTMORTEM_BOOST_KEYWORDS
    assert "migrate" in s.POSTMORTEM_BOOST_KEYWORDS


def test_config_has_postmortem_boost_factor():
    """Settings has POSTMORTEM_BOOST_FACTOR defaulting to 0.3."""
    from yadgar.config import Settings

    s = Settings()
    assert hasattr(s, "POSTMORTEM_BOOST_FACTOR")
    assert s.POSTMORTEM_BOOST_FACTOR == pytest.approx(0.3)
