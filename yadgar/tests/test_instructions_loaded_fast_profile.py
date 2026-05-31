"""TDD tests for v5.25.3 Fix 1 — hook_instructions_loaded must use profile="fast".

Root cause: http.py hook_instructions_loaded called retriever.recall without
profile="fast", triggering the full CE/NLI/MP rerank pipeline. This hook fires
on EVERY session_start + compact event — highest-frequency burst path missed by
v5.25.2 (which fixed subagent_start but not this sibling).

Siblings prompt_recall (~line 524) and subagent_start (~line 1048) already use
profile="fast". Same fix never propagated to instructions_loaded.

Written BEFORE implementation — starts red.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


class TestInstructionsLoadedUseFastProfile:
    """hook_instructions_loaded must pass profile='fast' to retriever.recall."""

    def test_recall_called_with_fast_profile(self):
        """retriever.recall kwarg profile must equal 'fast' on instructions_loaded."""
        import yadgar.server._state as _st
        import yadgar.server.http as _http

        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = []

        mock_request = MagicMock()
        mock_request.query_params = MagicMock()
        mock_request.query_params.get = MagicMock(
            side_effect=lambda k, d="": {
                "file_path": "/home/user/.claude/CLAUDE.md",
                "load_reason": "session_start",
                "cwd": "/home/user/project",
            }.get(k, d)
        )

        with patch.object(_st, "_retriever", mock_retriever):

            async def _run():
                async def _fake_to_thread(fn, *args, **kwargs):
                    return fn(*args, **kwargs)

                with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                    return await _http.hook_instructions_loaded(mock_request)

            asyncio.run(_run())

        assert mock_retriever.recall.called, "retriever.recall was not called"
        call_kwargs = mock_retriever.recall.call_args.kwargs
        assert "profile" in call_kwargs, (
            f"profile kwarg missing from retriever.recall call. "
            f"Got kwargs: {call_kwargs}. "
            "hook_instructions_loaded must pass profile='fast' to avoid full rerank "
            "pipeline. This hook fires on every session_start + compact event."
        )
        assert call_kwargs["profile"] == "fast", (
            f"Expected profile='fast', got profile={call_kwargs['profile']!r}. "
            "Full rerank pipeline causes 2.5-10s CPU bursts per session start."
        )

    def test_fast_profile_present_alongside_other_kwargs(self):
        """profile='fast' must coexist with max_results and min_heat kwargs."""
        import yadgar.server._state as _st
        import yadgar.server.http as _http

        mock_retriever = MagicMock()
        mock_retriever.recall.return_value = []

        mock_request = MagicMock()
        mock_request.query_params = MagicMock()
        mock_request.query_params.get = MagicMock(
            side_effect=lambda k, d="": {
                "file_path": "/tmp/workspace/CLAUDE.md",
                "load_reason": "compact",
                "cwd": "/tmp/workspace",
            }.get(k, d)
        )

        async def _run():
            async def _fake_to_thread(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            with patch("asyncio.to_thread", side_effect=_fake_to_thread):
                with patch.object(_st, "_retriever", mock_retriever):
                    return await _http.hook_instructions_loaded(mock_request)

        asyncio.run(_run())

        assert mock_retriever.recall.called
        call_kwargs = mock_retriever.recall.call_args.kwargs
        assert call_kwargs.get("profile") == "fast", "profile='fast' missing"
        assert "max_results" in call_kwargs, "max_results kwarg missing"
        assert "min_heat" in call_kwargs, "min_heat kwarg missing"

    def test_matches_siblings_pattern(self):
        """instructions_loaded, subagent_start, and prompt_recall must all use profile='fast'.

        Regression guard: prompt_recall and subagent_start already use profile='fast'.
        This test verifies the pattern is consistent across all three handlers.
        """
        import pathlib

        http_src = pathlib.Path(__file__).parent.parent / "server" / "http.py"
        source = http_src.read_text()

        # Count occurrences of profile="fast" in retriever.recall calls
        # All three handlers must use it (prompt_recall + subagent_start + instructions_loaded)
        count = source.count('profile="fast"')
        assert count >= 3, (
            f'Expected at least 3 occurrences of profile="fast" in http.py '
            f"(prompt_recall + subagent_start + instructions_loaded). Found {count}. "
            "All hooks fire on every session_start/compact/dispatch; all need fast profile."
        )
