"""TDD tests for v5.25.3 Fix 2 — viz_search must forward with profile="fast".

Root cause: http.py viz_search triggered the full CE/NLI/MP rerank pipeline.
User-initiated search; lower burst frequency than hooks but same CPU cost per call.

Siblings prompt_recall, subagent_start, and instructions_loaded already forward
with profile="fast". Same fix propagated to viz_search.

T2 Car E2 seam migration: viz_search no longer calls the in-core
``retriever.recall`` — memory recall forwards to the backend /recall via
``_HookRecallForwarder("").recall`` → ``_forward_hook_recall`` (ADR-0078). The
profile="fast" contract is now asserted at the ``_forward_hook_recall`` seam
(mechanism moved, guarded property — fast profile rides the forward request —
preserved). Mirrors ``test_instructions_loaded_fast_profile.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


class TestVizSearchUseFastProfile:
    """viz_search must forward the recall with profile='fast'."""

    def _fire(self, query: str) -> dict:
        """Run api_viz_search with _forward_hook_recall patched; return its kwargs."""
        import yadgar._shared.runtime.state as _st
        import yadgar.core.server.http as _http

        captured: dict = {}

        def _fake_forward(q, **kwargs):
            captured["query"] = q
            captured.update(kwargs)
            return []

        mock_wiki = MagicMock()
        mock_wiki.query.return_value = []

        mock_request = MagicMock()
        mock_request.query_params = MagicMock()
        mock_request.query_params.get = MagicMock(
            side_effect=lambda k, d=None: {"q": query}.get(k, d)
        )

        async def _run():
            with patch.object(_st, "_wiki", mock_wiki):
                with patch.object(_st, "_storage", None):
                    with patch(
                        "yadgar.core.server.http._forward_hook_recall",
                        side_effect=_fake_forward,
                    ):
                        return await _http.api_viz_search(mock_request)

        asyncio.run(_run())
        return captured

    def test_recall_called_with_fast_profile(self):
        """The forwarded recall must carry profile='fast' in viz_search."""
        captured = self._fire("session start memories")

        assert captured, "_forward_hook_recall was not called"
        assert "profile" in captured, (
            f"profile kwarg missing from the forwarded recall. "
            f"Got kwargs: {captured}. "
            "viz_search must forward profile='fast' to avoid full rerank pipeline. "
            "Full pipeline causes 2.5-10s CPU bursts per search."
        )
        assert captured["profile"] == "fast", (
            f"Expected profile='fast', got profile={captured['profile']!r}. "
            "Full rerank pipeline is unnecessary for viz graph node lookup."
        )

    def test_fast_profile_present_alongside_other_kwargs(self):
        """profile='fast' must coexist with max_results and min_heat kwargs."""
        captured = self._fire("consolidation patterns")

        assert captured, "_forward_hook_recall was not called"
        assert captured.get("profile") == "fast", "profile='fast' missing"
        assert "max_results" in captured, "max_results kwarg missing"
        assert "min_heat" in captured, "min_heat kwarg missing"

    def test_matches_siblings_pattern(self):
        """viz_search, instructions_loaded, subagent_start, prompt_recall all need profile='fast'.

        Regression guard: verifies http.py has profile="fast" in all known recall call sites.
        """
        import pathlib

        http_src = pathlib.Path(__file__).parent.parent.parent / "core" / "server" / "http.py"
        source = http_src.read_text()

        # Count occurrences — prompt_recall + subagent_start + instructions_loaded + viz_search
        count = source.count('profile="fast"')
        assert count >= 4, (
            f'Expected at least 4 occurrences of profile="fast" in http.py '
            f"(prompt_recall + subagent_start + instructions_loaded + viz_search). "
            f"Found {count}. All retriever.recall call sites need fast profile."
        )
