"""Tests for agent-prompt versioning — v5.3.0 A4.

Covers:
1. agent_prompt_save("dispatch-fix-bug", "...") creates v1.
2. Second save creates v2 (incremented).
3. agent_prompt_get("dispatch-fix-bug") returns latest (v2).
4. Get with unknown pattern returns None/empty.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def storage(tmp_path):
    from yadgar.storage import StorageEngine

    engine = StorageEngine(str(tmp_path / "test_agent_prompts.db"))
    yield engine
    engine.close()


class TestAgentPromptSave:
    """agent_prompt_save creates versioned wiki pages."""

    def test_first_save_creates_v1(self, storage):
        from yadgar.server.tools.agent_prompts import agent_prompt_save

        result = agent_prompt_save("dispatch-fix-bug", "Dispatch a bug-fix agent.", storage=storage)
        assert result["saved"] is True
        assert result["version"] == 1
        assert result["slug"] == "agent-prompt-dispatch-fix-bug-v1"

    def test_second_save_creates_v2(self, storage):
        from yadgar.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save("dispatch-fix-bug", "First version.", storage=storage)
        result = agent_prompt_save("dispatch-fix-bug", "Second version.", storage=storage)
        assert result["saved"] is True
        assert result["version"] == 2
        assert result["slug"] == "agent-prompt-dispatch-fix-bug-v2"

    def test_save_different_patterns_are_independent(self, storage):
        from yadgar.server.tools.agent_prompts import agent_prompt_save

        r1 = agent_prompt_save("dispatch-fix-bug", "Bug fix prompt.", storage=storage)
        r2 = agent_prompt_save("dispatch-research", "Research prompt.", storage=storage)
        assert r1["version"] == 1
        assert r2["version"] == 1
        assert r1["slug"] != r2["slug"]


class TestAgentPromptGet:
    """agent_prompt_get returns the latest version."""

    def test_get_returns_latest_version(self, storage):
        from yadgar.server.tools.agent_prompts import agent_prompt_get, agent_prompt_save

        agent_prompt_save("dispatch-fix-bug", "First version.", storage=storage)
        agent_prompt_save("dispatch-fix-bug", "Second version — updated.", storage=storage)

        result = agent_prompt_get("dispatch-fix-bug", storage=storage)
        assert result is not None
        assert result["version"] == 2
        assert "Second version" in result["content"]
        assert result["slug"] == "agent-prompt-dispatch-fix-bug-v2"

    def test_get_unknown_pattern_returns_none(self, storage):
        from yadgar.server.tools.agent_prompts import agent_prompt_get

        result = agent_prompt_get("nonexistent-pattern-xyz", storage=storage)
        assert result is None or result == {}

    def test_get_returns_v1_when_only_v1_exists(self, storage):
        from yadgar.server.tools.agent_prompts import agent_prompt_get, agent_prompt_save

        agent_prompt_save("dispatch-review-code", "Review code carefully.", storage=storage)

        result = agent_prompt_get("dispatch-review-code", storage=storage)
        assert result is not None
        assert result["version"] == 1
        assert "Review code" in result["content"]

    def test_get_with_many_versions_returns_highest(self, storage):
        from yadgar.server.tools.agent_prompts import agent_prompt_get, agent_prompt_save

        for i in range(1, 6):
            agent_prompt_save("multi-version-test", f"Prompt version {i}.", storage=storage)

        result = agent_prompt_get("multi-version-test", storage=storage)
        assert result is not None
        assert result["version"] == 5
        assert "Prompt version 5" in result["content"]
