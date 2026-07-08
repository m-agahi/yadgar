"""TDD tests for v5.25.2 Fix 1 — subagent_start handler must use profile="fast".

Root cause: http.py hook_subagent_start called retriever.recall without
profile="fast", triggering the full rerank pipeline (2.5-10s per dispatch).
Sibling prompt_recall at http.py ~525 already used profile="fast" with
comment warning about 8-46s CPU bursts. Same fix never propagated.

Written BEFORE implementation — starts red.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


class TestSubagentStartUseFastProfile:
    """hook_subagent_start must pass profile='fast' to retriever.recall."""

    def test_recall_called_with_fast_profile(self):
        """retriever.recall kwarg profile must equal 'fast' on subagent_start."""
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = []

        mock_request = MagicMock()
        mock_request.query_params = MagicMock()
        mock_request.query_params.get = MagicMock(
            side_effect=lambda k, d="": {"agent_type": "general-purpose", "cwd": "/tmp"}.get(k, d)
        )
        mock_request.json = AsyncMock(return_value={"description": "investigate failing tests"})

        with patch.object(_st, "_retriever", mock_retriever):
            with patch(
                "asyncio.to_thread",
                new=lambda fn, *a, **kw: asyncio.coroutine(lambda: fn(*a, **kw))(),
            ):

                async def _run():
                    # Patch asyncio.to_thread to call the function synchronously
                    async def _fake_to_thread(fn, *args, **kwargs):
                        return fn(*args, **kwargs)

                    with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                        return await _http.hook_subagent_start(mock_request)

                asyncio.run(_run())

        assert mock_retriever.recall.called, "retriever.recall was not called"
        call_kwargs = mock_retriever.recall.call_args.kwargs
        assert "profile" in call_kwargs, (
            f"profile kwarg missing from retriever.recall call. "
            f"Got kwargs: {call_kwargs}. "
            "hook_subagent_start must pass profile='fast' to avoid full rerank pipeline."
        )
        assert call_kwargs["profile"] == "fast", (
            f"Expected profile='fast', got profile={call_kwargs['profile']!r}. "
            "Full rerank pipeline causes 2.5-10s CPU bursts per dispatch."
        )

    def test_fast_profile_present_alongside_other_kwargs(self):
        """profile='fast' must coexist with max_results and min_heat kwargs."""
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = []

        mock_request = MagicMock()
        mock_request.query_params = MagicMock()
        mock_request.query_params.get = MagicMock(
            side_effect=lambda k, d="": {"agent_type": "Explore", "cwd": "/home/user/proj"}.get(
                k, d
            )
        )
        mock_request.json = AsyncMock(return_value={"description": "search for usages"})

        async def _run():
            async def _fake_to_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch.object(_st, "_retriever", mock_retriever):
                    return await _http.hook_subagent_start(mock_request)

        asyncio.run(_run())

        assert mock_retriever.recall.called
        call_kwargs = mock_retriever.recall.call_args.kwargs
        # All three must be present
        assert call_kwargs.get("profile") == "fast", "profile='fast' missing"
        assert "max_results" in call_kwargs, "max_results kwarg missing"
        assert "min_heat" in call_kwargs, "min_heat kwarg missing"

    def test_matches_prompt_recall_pattern(self):
        """Both subagent_start and prompt_recall must use profile='fast'.

        Regression guard: prompt_recall already uses profile='fast'. Verify
        the pattern is consistent across both handlers.
        """
        import pathlib

        http_src = pathlib.Path(__file__).parent.parent.parent / "core" / "server" / "http.py"
        source = http_src.read_text()

        # Count occurrences of profile="fast" in retriever.recall calls
        # Both handlers must use it
        assert source.count('profile="fast"') >= 2, (
            'Expected at least 2 occurrences of profile="fast" in http.py '
            "(prompt_recall + subagent_start). Found fewer. "
            "Both hooks fire 50+ times/hour; both need fast profile."
        )
