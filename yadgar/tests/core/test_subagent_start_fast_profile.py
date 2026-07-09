"""hook_subagent_start must recall with profile="fast" (v5.25.2 contract).

Originally TDD for the in-core retriever.recall(profile="fast") fix. ADR-0078
forwarded this hook to the backend (no core DB path remains), so the SAME
profile="fast" contract is now asserted at the _forward_hook_recall seam —
an authorized substitution (mechanism changed, guarded property preserved):
the hook fires on every agent dispatch and must never trigger the full
rerank pipeline.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


class TestSubagentStartUseFastProfile:
    """hook_subagent_start must forward with profile='fast'."""

    def _fire(self, params: dict, body: dict) -> dict:
        """Run the handler with _forward_hook_recall patched; return its kwargs."""
        import yadgar.core.server.http as _http

        captured: dict = {}

        def _fake_forward(query, **kwargs):
            captured["query"] = query
            captured.update(kwargs)
            return []

        mock_request = MagicMock()
        mock_request.query_params = MagicMock()
        mock_request.query_params.get = MagicMock(side_effect=lambda k, d="": params.get(k, d))
        mock_request.json = AsyncMock(return_value=body)

        async def _run():
            with patch("yadgar.core.server.http._forward_hook_recall", side_effect=_fake_forward):
                return await _http.hook_subagent_start(mock_request)

        asyncio.run(_run())
        return captured

    def test_recall_called_with_fast_profile(self):
        """The forwarded recall must carry profile='fast' on subagent_start."""
        captured = self._fire(
            {"agent_type": "general-purpose", "cwd": "/tmp"},
            {"description": "investigate failing tests"},
        )
        assert captured, "forward was not called"
        assert captured.get("profile") == "fast", (
            f"Expected profile='fast', got profile={captured.get('profile')!r}. "
            "Full rerank pipeline causes 2.5-10s CPU bursts per dispatch."
        )

    def test_fast_profile_present_alongside_other_kwargs(self):
        """profile='fast' must coexist with max_results and min_heat kwargs."""
        captured = self._fire(
            {"agent_type": "Explore", "cwd": "/home/user/proj"},
            {"description": "search for usages"},
        )
        assert captured.get("profile") == "fast", "profile='fast' missing"
        assert "max_results" in captured, "max_results kwarg missing"
        assert "min_heat" in captured, "min_heat kwarg missing"

    def test_matches_prompt_recall_pattern(self):
        """Both subagent_start and prompt_recall must use profile='fast'.

        Regression guard: prompt_recall already uses profile='fast'. Verify
        the pattern is consistent across both handlers.
        """
        import pathlib

        http_src = pathlib.Path(__file__).parent.parent.parent / "core" / "server" / "http.py"
        source = http_src.read_text()

        assert source.count('profile="fast"') >= 2, (
            'Expected at least 2 occurrences of profile="fast" in http.py '
            "(prompt_recall + subagent_start). Found fewer. "
            "Both hooks fire 50+ times/hour; both need fast profile."
        )
