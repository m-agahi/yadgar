"""Phase 2a — fan-out orchestrator tests (forward-only cutover).

Phase 2a rewrites:
  - TestUnifiedRecallFlag: flag is removed. Tests rewritten to verify that
    recall() unconditionally calls _forward_to_backend (no flag gate).
  - TestFlagFalseRegression: legacy path is gone — tests deleted.
  - TestFanoutRecall: direct _fanout_recall unit tests — unchanged (backend still uses it).
  - TestFlagTrueEndToEnd: rewritten to verify _forward_to_backend is called with
    correct args; result contains items from backend mock.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import yadgar._shared.runtime.state as _st
import yadgar.core.server.tools.recall as _recall_symbol  # noqa: F401 — imported for side-effects
from yadgar._shared.storage.directory import RecallScope

_recall_module = sys.modules["yadgar.core.server.tools.recall"]

import yadgar.backend.retrieval.recall_pipeline as _rp  # noqa: E402
from yadgar.backend.retrieval.recall_pipeline import _fanout_recall  # noqa: E402

recall_fn = _recall_module.recall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory_dict(mid: int = 1, score: float = 0.8, content: str | None = None) -> dict:
    return {
        "id": mid,
        "content": content or f"memory content {mid}",
        "heat": score * 0.5,
        "_retrieval_score": score,
        "tags": [],
        "branch": None,
        "_source": "memory",
        # Car C7: MemoryProvider applies an is_project_eligible residual guard
        # keyed on project_id (not directory_context) — matches the
        # project_id="/tmp/test" scope every _call_fanout() call in this file
        # uses, so these rows stay eligible.
        "project_id": "/tmp/test",
    }


def _make_wiki_dict(slug: str = "test-wiki", score: float = 0.7) -> dict:
    return {
        "id": hash(slug) % 10000,  # WikiProvider.candidates requires non-None id
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "content": f"wiki content for {slug}",
        "_retrieval_score": score,
        "_source": "wiki",
        "branch": None,
        "directory_context": None,  # None = always eligible (global sentinel)
    }


def _make_mock_retriever(results=None):
    retriever = MagicMock()
    retriever.recall.return_value = results if results is not None else [_make_memory_dict(1)]
    retriever.recall_via_pipeline.return_value = (
        results if results is not None else [_make_memory_dict(2)]
    )
    return retriever


def _make_mock_wiki(results=None):
    wiki = MagicMock()
    wiki.query.return_value = results if results is not None else [_make_wiki_dict()]
    return wiki


# ---------------------------------------------------------------------------
# 1 + 2. Phase 2a: forward-only dispatch — no flag, always _forward_to_backend
# ---------------------------------------------------------------------------


class TestForwardOnlyDispatch:
    """Phase 2a: recall() always calls _forward_to_backend — no flag gate."""

    def _call_recall(self, directory="/tmp/test", project="owner/repo", **kwargs):
        """Helper: call recall with _forward_to_backend mocked.

        ``project`` is explicit because C5 (ADR-0227) put the identity resolver
        at the top of ``recall`` — a call naming only a directory now raises
        ``UnresolvedProjectError`` before the forward these tests spy on is
        ever reached. What is asserted below is what recall FORWARDS, so the
        call has to get that far.
        """
        fake_results = [_make_memory_dict(1)]
        captured = {}

        def _spy_fwd(**kw):
            captured.update(kw)
            return fake_results

        with (
            patch.object(_recall_module, "_forward_to_backend", side_effect=_spy_fwd),
            patch.object(_recall_module, "_apply_recall_session_side_effects"),
            patch.object(_recall_module, "_st") as mock_st,
        ):
            mock_st._consolidation = None
            mock_st._pool = None
            result = recall_fn(
                query="test query",
                max_results=5,
                directory=directory,
                project=project,
                **kwargs,
            )

        return result, captured

    def test_always_calls_forward_to_backend(self):
        """recall() always calls _forward_to_backend — no flag, unconditional."""
        result, captured = self._call_recall()
        # captured is non-empty iff _forward_to_backend was called
        assert captured, "_forward_to_backend was not called"

    def test_type_filter_forwarded(self):
        """type= param is forwarded to backend."""
        _, captured = self._call_recall(type="memory")
        assert captured.get("type_filter") == "memory"

    def test_tags_forwarded(self):
        """tags= param is forwarded to backend."""
        _, captured = self._call_recall(tags=["adr"])
        assert captured.get("tags") == ["adr"]

    def test_mode_forwarded(self):
        """mode= param is forwarded to backend."""
        _, captured = self._call_recall(mode="landscape")
        assert captured.get("mode") == "landscape"

    def test_profile_forwarded(self):
        """profile= param is forwarded to backend."""
        _, captured = self._call_recall(profile="fast")
        assert captured.get("profile") == "fast"

    def test_returns_backend_results(self):
        """recall() returns exactly what _forward_to_backend returns."""
        result, _ = self._call_recall()
        assert len(result) >= 1
        assert result[0]["id"] == 1


# ---------------------------------------------------------------------------
# 3. _fanout_recall unit tests (unchanged — backend still uses it directly)
# ---------------------------------------------------------------------------


class TestFanoutRecall:
    """Direct unit tests for the _fanout_recall() helper.

    _fanout_recall is called by the backend route handler, not by recall() directly
    (Phase 2a). These tests exercise it in-core via test-wired _st.*."""

    def _call_fanout(self, query="test", max_results=5, retriever=None, wiki=None, profile=None):
        kw = {}
        if profile is not None:
            kw["profile"] = profile
        with (
            patch.object(_st, "_retriever", retriever),
            patch.object(_st, "_wiki", wiki),
        ):
            return _fanout_recall(
                query=query,
                max_results=max_results,
                min_heat=0.0,
                recall_scope=RecallScope(project_id="/tmp/test"),
                **kw,
            )

    def _call_fanout_tagged(self, retriever, tags):
        """``_call_fanout`` with a tag filter, memory-only (the live call shape)."""
        with (
            patch.object(_st, "_retriever", retriever),
            patch.object(_st, "_wiki", None),
        ):
            return _fanout_recall(
                query="anchor hygiene",
                max_results=25,
                min_heat=0.0,
                recall_scope=RecallScope(project_id="/tmp/test"),
                type_filter="memory",
                tags=tags,
            )

    def test_tags_reach_the_memory_arm(self):
        """``tags=`` must reach MemoryProvider, not WikiProvider alone (task 82).

        THIS IS THE SEAM THAT BROKE, and it is not the provider's own logic:
        ``_build_provider_tasks`` handed the tag list to the ``WikiProvider``
        constructor and built ``MemoryProvider`` without it, while
        ``_fanout_recall`` stamped ``Scope(opt_in_tags=tags)`` that nothing on
        the memory side read. Under ``type_filter="memory"`` the wiki provider
        is not constructed at all, so the filter was wholly inert — measured
        live 2026-08-19: ``recall(tags=["_anchor"], type="memory")`` returned
        rows tagged ``["semantic", "auto-abstracted"]`` whose auto-abstracted
        CONTENT merely contains the substring ``[tags: _anchor]``.

        A unit test on ``MemoryProvider.candidates`` alone cannot pin this:
        drop ``opt_in_tags=tags`` at the ``Scope`` construction and the
        provider tests stay green while the feature dies exactly as it did.
        """
        tagged = _make_memory_dict(1, 0.9, content="genuinely anchored")
        tagged["tags"] = ["_anchor"]
        content_only = _make_memory_dict(2, 0.8, content="recall audit [tags: _anchor]")
        content_only["tags"] = ["semantic", "auto-abstracted"]

        results = self._call_fanout_tagged(
            _make_mock_retriever([tagged, content_only]), tags=["_anchor"]
        )

        ids = [r.get("id") for r in results]
        assert 1 in ids, "the row genuinely tagged _anchor was dropped — filter is over-broad"
        assert 2 not in ids, (
            "a row whose CONTENT mentions '[tags: _anchor]' but whose tags field is "
            "['semantic', 'auto-abstracted'] survived the fan-out — tags= is not "
            "reaching the memory provider"
        )

    def test_no_tags_leaves_the_memory_arm_unfiltered(self):
        """Control: ``tags=None`` is general recall and must filter nothing out."""
        tagged = _make_memory_dict(1, 0.9, content="genuinely anchored")
        tagged["tags"] = ["_anchor"]
        content_only = _make_memory_dict(2, 0.8, content="recall audit [tags: _anchor]")
        content_only["tags"] = ["semantic", "auto-abstracted"]

        results = self._call_fanout_tagged(_make_mock_retriever([tagged, content_only]), tags=None)

        assert sorted(r.get("id") for r in results) == [1, 2]

    def test_kill_gate_strips_the_tag_from_BOTH_arms(self, monkeypatch):
        """Flag-off must hand the SAME stripped tag list to both providers.

        The S6 kill-gate used to run inside ``_build_provider_tasks``, where it
        rebound that function's LOCAL ``tags`` — by then ``_fanout_recall`` had
        already stamped ``Scope(opt_in_tags=tags)`` with the UNSTRIPPED list. So
        with the flag OFF, WikiProvider was constructed with ``tags=None`` while
        the memory arm still carried ``["agent-prompt"]``. That divergence was
        inert only for as long as ``MemoryProvider`` ignored ``opt_in_tags``;
        ledger task 82 made it read the field, which made the divergence live.

        Both halves are asserted because either alone is satisfiable by the bug:

        * WIKI ARM (the kill-gate itself, which must NOT weaken) — the provider
          is constructed with ``tags=None``, so the include path cannot fire and
          the default agent-prompt exclude turns back on.
        * MEMORY ARM (the divergence) — a row NOT tagged ``agent-prompt`` still
          comes back. Move the strip below the ``Scope`` construction again and
          ``scope.opt_in_tags == ["agent-prompt"]``, so the memory guard filters
          this row out and the assertion fails.
        """
        from yadgar._shared.config import get_settings

        monkeypatch.setattr(get_settings(), "AGENT_PROMPT_LIBRARY_ENABLED", False)

        captured: dict = {}
        real_wiki_provider = _rp.WikiProvider

        def _spy_wiki_provider(store, *, tags=None, exclude_tags=None):
            captured["wiki_tags"] = tags
            captured["wiki_exclude"] = exclude_tags
            return real_wiki_provider(store, tags=tags, exclude_tags=exclude_tags)

        ordinary = _make_memory_dict(1, 0.9, content="an ordinary memory, not a prompt")
        ordinary["tags"] = ["yadgar", "recall"]

        with (
            patch.object(_st, "_retriever", _make_mock_retriever([ordinary])),
            patch.object(_st, "_wiki", _make_mock_wiki([])),
            patch.object(_rp, "WikiProvider", _spy_wiki_provider),
        ):
            results = _fanout_recall(
                query="audit this pull request for vulnerabilities",
                max_results=5,
                min_heat=0.0,
                recall_scope=RecallScope(project_id="/tmp/test"),
                type_filter="all",
                tags=["agent-prompt"],
            )

        assert captured["wiki_tags"] is None, (
            "kill-gate weakened: the agent-prompt include tag reached WikiProvider "
            f"with the flag OFF (got {captured['wiki_tags']!r})"
        )
        assert captured["wiki_exclude"] == ["agent-prompt"], (
            "the default agent-prompt exclude must turn back on once the include "
            f"tag is stripped (got {captured['wiki_exclude']!r})"
        )
        assert 1 in [r.get("id") for r in results], (
            "the memory arm filtered on 'agent-prompt' after the kill-gate stripped "
            "it — the strip ran below the Scope construction, so scope.opt_in_tags "
            "kept the unstripped list and the two arms disagreed"
        )

    def test_pools_memory_and_wiki(self):
        """Fan-out with both retriever + wiki returns items from both sources."""
        mem = _make_memory_dict(1, 0.9)
        wiki = _make_wiki_dict("overview", 0.8)
        mock_retriever = _make_mock_retriever([mem])
        mock_wiki = _make_mock_wiki([wiki])

        results = self._call_fanout(retriever=mock_retriever, wiki=mock_wiki)

        assert any(r.get("id") == 1 for r in results), "Expected memory result"
        assert any(r.get("_source") == "wiki" for r in results), "Expected wiki result"

    def test_memory_only_when_wiki_none(self):
        """With _wiki=None, only memory candidates are returned."""
        mem = _make_memory_dict(1, 0.9)
        mock_retriever = _make_mock_retriever([mem])

        results = self._call_fanout(retriever=mock_retriever, wiki=None)

        assert len(results) >= 1
        assert all(r.get("_source") != "wiki" for r in results)

    def test_wiki_only_when_retriever_none(self):
        """With _retriever=None, only wiki candidates are returned."""
        wiki_page = _make_wiki_dict("overview", 0.8)
        mock_wiki = _make_mock_wiki([wiki_page])

        results = self._call_fanout(retriever=None, wiki=mock_wiki)

        assert len(results) >= 1
        assert all(r.get("_source") == "wiki" for r in results)

    def test_respects_max_results(self):
        """Fan-out result count does not exceed max_results."""
        mems = [_make_memory_dict(i, 0.9 - i * 0.01) for i in range(1, 20)]
        wikis = [_make_wiki_dict(f"page-{i}", 0.8) for i in range(10)]
        mock_retriever = _make_mock_retriever(mems)
        mock_wiki = _make_mock_wiki(wikis)

        results = self._call_fanout(retriever=mock_retriever, wiki=mock_wiki, max_results=3)

        assert len(results) <= 3

    def test_deduplicates_by_content(self):
        """Fan-out deduplicates items with identical content."""
        shared_content = "This content appears in both memory and wiki"
        mem = _make_memory_dict(1, 0.9, content=shared_content)
        wiki_page = {
            "id": 50,
            "slug": "dup-page",
            "content": shared_content,
            "_retrieval_score": 0.7,
            "_source": "wiki",
        }

        mock_retriever = _make_mock_retriever([mem])
        mock_wiki = _make_mock_wiki([wiki_page])

        results = self._call_fanout(retriever=mock_retriever, wiki=mock_wiki, max_results=10)

        contents = [r.get("content") for r in results]
        assert contents.count(shared_content) == 1

    def test_empty_result_when_both_none(self):
        """With both _retriever=None and _wiki=None, returns empty list."""
        results = self._call_fanout(retriever=None, wiki=None)
        assert results == []

    def test_returns_list_of_dicts(self):
        """Fan-out always returns list[dict]."""
        mem = _make_memory_dict(1, 0.9)
        mock_retriever = _make_mock_retriever([mem])
        results = self._call_fanout(retriever=mock_retriever, wiki=None)
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, dict)

    def test_preserves_retriever_native_order(self):
        """Single-provider bypass preserves the retriever's native order verbatim."""
        mems = [
            _make_memory_dict(1, 0.5),
            _make_memory_dict(2, 0.9),
        ]
        mock_retriever = _make_mock_retriever(mems)

        results = self._call_fanout(retriever=mock_retriever, wiki=None, max_results=10)

        ids = [r["id"] for r in results]
        assert ids == [1, 2], "Bypass must preserve retriever native order, not re-sort"


# ---------------------------------------------------------------------------
# 4. Phase 2a end-to-end: recall MCP tool returns results via forward path
# ---------------------------------------------------------------------------


class TestForwardOnlyEndToEnd:
    """Integration: recall MCP tool returns results forwarded from mock backend."""

    def test_returns_memory_and_wiki_from_backend(self):
        """recall() returns whatever _forward_to_backend returns (mock both types)."""
        mem = _make_memory_dict(1, 0.9)
        wiki_page = _make_wiki_dict("test-wiki", 0.75)
        fake_results = [mem, wiki_page]

        with (
            patch.object(_recall_module, "_forward_to_backend", return_value=fake_results),
            patch.object(_recall_module, "_apply_recall_session_side_effects"),
            patch.object(_recall_module, "_st") as mock_st,
        ):
            mock_st._consolidation = None
            mock_st._pool = None
            result = recall_fn(
                query="test query",
                max_results=10,
                directory="/tmp/test",
                project="owner/repo",
            )

        has_wiki = any(r.get("_source") == "wiki" for r in result)
        has_memory = any(r.get("id") == 1 for r in result)

        assert has_wiki, f"Expected wiki result; got: {result}"
        assert has_memory, f"Expected memory result; got: {result}"
