"""TDD tests for v5.25.3 Fix 2 — viz_search handler must use profile="fast".

Root cause: http.py viz_search called retriever.recall without profile="fast",
triggering the full CE/NLI/MP rerank pipeline. User-initiated search; lower burst
frequency than hooks but same CPU cost per call.

Siblings prompt_recall (~line 524), subagent_start (~line 1048), and
instructions_loaded (~line 953) already use profile="fast". Same fix never
propagated to viz_search.

Written BEFORE implementation — starts red.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


class TestVizSearchUseFastProfile:
    """viz_search must pass profile='fast' to retriever.recall."""

    def test_recall_called_with_fast_profile(self):
        """retriever.recall kwarg profile must equal 'fast' in viz_search."""
        import yadgar.server._state as _st
        import yadgar.server.http as _http

        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = []

        mock_wiki = MagicMock()
        mock_wiki.query.return_value = []

        mock_request = MagicMock()
        mock_request.query_params = MagicMock()
        mock_request.query_params.get = MagicMock(
            side_effect=lambda k, d=None: {
                "q": "session start memories",
            }.get(k, d)
        )

        with patch.object(_st, "_retriever", mock_retriever):
            with patch.object(_st, "_wiki", mock_wiki):

                async def _run():
                    async def _fake_to_thread(fn, *args, **kwargs):
                        return fn(*args, **kwargs)

                    with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                        return await _http.api_viz_search(mock_request)

                asyncio.run(_run())

        assert mock_retriever.recall.called, "retriever.recall was not called"
        call_kwargs = mock_retriever.recall.call_args.kwargs
        assert "profile" in call_kwargs, (
            f"profile kwarg missing from retriever.recall call. "
            f"Got kwargs: {call_kwargs}. "
            "viz_search must pass profile='fast' to avoid full rerank pipeline. "
            "Full pipeline causes 2.5-10s CPU bursts per search."
        )
        assert call_kwargs["profile"] == "fast", (
            f"Expected profile='fast', got profile={call_kwargs['profile']!r}. "
            "Full rerank pipeline is unnecessary for viz graph node lookup."
        )

    def test_fast_profile_present_alongside_other_kwargs(self):
        """profile='fast' must coexist with max_results and min_heat kwargs."""
        import yadgar.server._state as _st
        import yadgar.server.http as _http

        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = []

        mock_wiki = MagicMock()
        mock_wiki.query.return_value = []

        mock_request = MagicMock()
        mock_request.query_params = MagicMock()
        mock_request.query_params.get = MagicMock(
            side_effect=lambda k, d=None: {
                "q": "consolidation patterns",
            }.get(k, d)
        )

        async def _run():
            async def _fake_to_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch.object(_st, "_retriever", mock_retriever):
                    with patch.object(_st, "_wiki", mock_wiki):
                        return await _http.api_viz_search(mock_request)

        asyncio.run(_run())

        assert mock_retriever.recall.called
        call_kwargs = mock_retriever.recall.call_args.kwargs
        assert call_kwargs.get("profile") == "fast", "profile='fast' missing"
        assert "max_results" in call_kwargs, "max_results kwarg missing"
        assert "min_heat" in call_kwargs, "min_heat kwarg missing"

    def test_matches_siblings_pattern(self):
        """viz_search, instructions_loaded, subagent_start, prompt_recall all need profile='fast'.

        Regression guard: verifies http.py has profile="fast" in all known recall call sites.
        """
        import pathlib

        http_src = pathlib.Path(__file__).parent.parent / "server" / "http.py"
        source = http_src.read_text()

        # Count occurrences — prompt_recall + subagent_start + instructions_loaded + viz_search
        count = source.count('profile="fast"')
        assert count >= 4, (
            f'Expected at least 4 occurrences of profile="fast" in http.py '
            f"(prompt_recall + subagent_start + instructions_loaded + viz_search). "
            f"Found {count}. All retriever.recall call sites need fast profile."
        )
