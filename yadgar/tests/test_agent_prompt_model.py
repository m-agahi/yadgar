"""RED-first tests for AgentPrompt model (S1, v5.85 rework)."""

from __future__ import annotations

import pytest


class TestAgentPromptModel:
    def test_construction_with_required_fields(self):
        from yadgar.models import AgentPrompt

        ap = AgentPrompt(
            pattern="dispatch-fix-bug", purpose="Fix bugs quickly.", content="Review the error."
        )
        assert ap.pattern == "dispatch-fix-bug"
        assert ap.purpose == "Fix bugs quickly."
        assert ap.content == "Review the error."

    def test_missing_required_field_raises(self):
        from pydantic import ValidationError

        from yadgar.models import AgentPrompt

        with pytest.raises(ValidationError):
            AgentPrompt(pattern="x", content="y")  # missing purpose

    def test_page_type_registered_in_wiki_meta(self):
        from yadgar.wiki_meta import PAGE_TYPES

        assert "agent_prompt" in PAGE_TYPES
        assert "Purpose" in PAGE_TYPES["agent_prompt"]
        assert "Prompt" in PAGE_TYPES["agent_prompt"]
