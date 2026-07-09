"""hook_instructions_loaded must recall with profile="fast" (v5.25.3 contract).

Originally TDD for the in-core retriever.recall(profile="fast") fix. ADR-0078
forwarded this hook to the backend (no core DB path remains), so the SAME
profile="fast" contract is now asserted at the _forward_hook_recall seam —
an authorized substitution (mechanism changed, guarded property preserved):
the hook fires on every session_start + compact event and must never trigger
the full rerank pipeline.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


class TestInstructionsLoadedUseFastProfile:
    """hook_instructions_loaded must forward with profile='fast'."""

    def _fire(self, params: dict) -> dict:
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

        async def _run():
            with patch("yadgar.core.server.http._forward_hook_recall", side_effect=_fake_forward):
                return await _http.hook_instructions_loaded(mock_request)

        asyncio.run(_run())
        return captured

    def test_recall_called_with_fast_profile(self):
        """The forwarded recall must carry profile='fast' on instructions_loaded."""
        captured = self._fire(
            {
                "file_path": "/home/user/.claude/CLAUDE.md",
                "load_reason": "session_start",
                "cwd": "/home/user/project",
            }
        )
        assert captured, "forward was not called"
        assert captured.get("profile") == "fast", (
            f"Expected profile='fast', got profile={captured.get('profile')!r}. "
            "Full rerank pipeline causes 2.5-10s CPU bursts per session start."
        )

    def test_fast_profile_present_alongside_other_kwargs(self):
        """profile='fast' must coexist with max_results and min_heat kwargs."""
        captured = self._fire(
            {
                "file_path": "/tmp/workspace/CLAUDE.md",
                "load_reason": "compact",
                "cwd": "/tmp/workspace",
            }
        )
        assert captured.get("profile") == "fast", "profile='fast' missing"
        assert "max_results" in captured, "max_results kwarg missing"
        assert "min_heat" in captured, "min_heat kwarg missing"

    def test_matches_siblings_pattern(self):
        """instructions_loaded, subagent_start, and prompt_recall must all use profile='fast'.

        Regression guard: all three hook handlers fire on every
        session_start/compact/dispatch; all must request the fast profile.
        """
        import pathlib

        http_src = pathlib.Path(__file__).parent.parent.parent / "core" / "server" / "http.py"
        source = http_src.read_text()

        count = source.count('profile="fast"')
        assert count >= 3, (
            f'Expected at least 3 occurrences of profile="fast" in http.py '
            f"(prompt_recall + subagent_start + instructions_loaded). Found {count}. "
            "All hooks fire on every session_start/compact/dispatch; all need fast profile."
        )
