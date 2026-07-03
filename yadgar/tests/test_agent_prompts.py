"""Tests for agent-prompt one-page-per-pattern (S2 rework, v5.85).

One wiki page per pattern; wiki versioning carries history.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


class TestAgentPromptSave:
    """agent_prompt_save upserts one page per pattern."""

    def test_first_save_creates_page(self, storage):
        from yadgar.server.tools.agent_prompts import agent_prompt_save

        result = agent_prompt_save(
            "dispatch-fix-bug", "Dispatch a bug-fix agent.", directory="global", storage=storage
        )
        assert result["saved"] is True
        assert result["version"] == 1
        assert result["slug"] == "agent-prompt-dispatch-fix-bug"

    def test_second_save_updates_page_not_creates_new(self, storage):
        from yadgar.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save("dispatch-fix-bug", "First version.", directory="global", storage=storage)
        result = agent_prompt_save(
            "dispatch-fix-bug", "Second version.", directory="global", storage=storage
        )
        assert result["saved"] is True
        assert result["version"] == 2
        assert result["slug"] == "agent-prompt-dispatch-fix-bug"
        # Only one page should exist (not two)
        pages = storage.list_wiki_pages()
        ap_pages = [
            p for p in pages if p.get("slug", "").startswith("agent-prompt-dispatch-fix-bug")
        ]
        assert len(ap_pages) == 1, (
            f"Expected 1 page, found {len(ap_pages)}: {[p['slug'] for p in ap_pages]}"
        )

    def test_save_different_patterns_are_independent(self, storage):
        from yadgar.server.tools.agent_prompts import agent_prompt_save

        r1 = agent_prompt_save(
            "dispatch-fix-bug", "Bug fix prompt.", directory="global", storage=storage
        )
        r2 = agent_prompt_save(
            "dispatch-research", "Research prompt.", directory="global", storage=storage
        )
        assert r1["version"] == 1
        assert r2["version"] == 1
        assert r1["slug"] == "agent-prompt-dispatch-fix-bug"
        assert r2["slug"] == "agent-prompt-dispatch-research"
        assert r1["slug"] != r2["slug"]

    def test_saved_page_has_correct_page_type(self, storage):
        from yadgar.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save("dispatch-fix-bug", "Fix bugs.", directory="global", storage=storage)
        page = storage.get_wiki_page_by_slug("agent-prompt-dispatch-fix-bug")
        assert page is not None
        assert page.get("page_type") == "agent_prompt"


class TestReadAgentPrompt:
    """_read_agent_prompt (internal slug-read) returns the page by deterministic slug.

    v5.85 S4/S5: the agent_prompt_get MCP tool was removed; the exact-key lookup
    logic lives in the internal helper _read_agent_prompt(slug, storage).
    """

    def test_read_returns_latest_content(self, storage):
        from yadgar.server.tools.agent_prompts import _read_agent_prompt, agent_prompt_save

        agent_prompt_save("dispatch-fix-bug", "First version.", directory="global", storage=storage)
        agent_prompt_save(
            "dispatch-fix-bug", "Second version — updated.", directory="global", storage=storage
        )
        result = _read_agent_prompt("agent-prompt-dispatch-fix-bug", storage=storage)
        assert result is not None
        assert result["version"] == 2
        assert "Second version" in result["content"]
        assert result["slug"] == "agent-prompt-dispatch-fix-bug"

    def test_read_unknown_slug_returns_none(self, storage):
        from yadgar.server.tools.agent_prompts import _read_agent_prompt

        result = _read_agent_prompt("agent-prompt-nonexistent-pattern-xyz", storage=storage)
        assert result is None or result == {}

    def test_read_returns_version_1_when_only_one_save(self, storage):
        from yadgar.server.tools.agent_prompts import _read_agent_prompt, agent_prompt_save

        agent_prompt_save(
            "dispatch-review-code", "Review code carefully.", directory="global", storage=storage
        )
        result = _read_agent_prompt("agent-prompt-dispatch-review-code", storage=storage)
        assert result is not None
        assert result["version"] == 1
        assert "Review code" in result["content"]

    def test_read_with_many_saves_returns_latest_version(self, storage):
        from yadgar.server.tools.agent_prompts import _read_agent_prompt, agent_prompt_save

        for i in range(1, 6):
            agent_prompt_save(
                "multi-version-test", f"Prompt version {i}.", directory="global", storage=storage
            )
        result = _read_agent_prompt("agent-prompt-multi-version-test", storage=storage)
        assert result is not None
        assert result["version"] == 5
        assert "Prompt version 5" in result["content"]


class TestAgentPromptDoubleWrap:
    """#68: agent_prompt_save must not double-wrap when content already has Purpose/Prompt headers."""

    def test_pre_wrapped_content_produces_single_wrap(self, storage):
        """RED: saving already-wrapped content currently double-wraps (## Purpose appears twice)."""
        from yadgar.server.tools.agent_prompts import agent_prompt_save

        pre_wrapped = "## Purpose\n\nSome purpose\n\n## Prompt\n\nDO THE THING"
        agent_prompt_save(
            "double-wrap-test",
            pre_wrapped,
            directory="global",
            purpose="Test purpose",
            storage=storage,
        )
        page = storage.get_wiki_page_by_slug("agent-prompt-double-wrap-test")
        assert page is not None
        content = page["content"]
        # Exactly one ## Purpose and one ## Prompt header
        assert content.count("## Purpose") == 1, (
            f"Expected exactly 1 '## Purpose', got {content.count('## Purpose')}:\n{content}"
        )
        assert content.count("## Prompt") == 1, (
            f"Expected exactly 1 '## Prompt', got {content.count('## Prompt')}:\n{content}"
        )
        # Body text is intact
        assert "DO THE THING" in content, f"Body text missing from:\n{content}"

    def test_bare_content_wraps_normally(self, storage):
        """Passthrough: bare content gets wrapped once (no change in behaviour)."""
        from yadgar.server.tools.agent_prompts import agent_prompt_save

        agent_prompt_save(
            "bare-content-test",
            "DO THE THING",
            directory="global",
            purpose="A test purpose",
            storage=storage,
        )
        page = storage.get_wiki_page_by_slug("agent-prompt-bare-content-test")
        assert page is not None
        content = page["content"]
        assert content.count("## Purpose") == 1
        assert content.count("## Prompt") == 1
        assert "DO THE THING" in content

    def test_unwrap_helper_strips_wrapper(self):
        """Unit-test _unwrap_purpose_prompt directly."""
        from yadgar.server.tools.agent_prompts import _unwrap_purpose_prompt

        wrapped = "## Purpose\n\nSome purpose\n\n## Prompt\n\nDO THE THING"
        assert _unwrap_purpose_prompt(wrapped) == "DO THE THING"

    def test_unwrap_helper_passthrough_bare(self):
        """_unwrap_purpose_prompt returns bare content unchanged."""
        from yadgar.server.tools.agent_prompts import _unwrap_purpose_prompt

        bare = "DO THE THING"
        assert _unwrap_purpose_prompt(bare) == bare


class TestAgentPromptToolSurface:
    """v5.85 S4 (I32): bespoke get/search tools removed; save stays a tool."""

    def test_get_and_search_tools_removed_from_all(self):
        from yadgar.server import tools

        assert "agent_prompt_get" not in tools.__all__
        assert "agent_prompt_search" not in tools.__all__

    def test_save_tool_still_exported(self):
        from yadgar.server import tools

        assert "agent_prompt_save" in tools.__all__

    def test_removed_tools_not_importable(self):
        import yadgar.server.tools.agent_prompts as ap_mod

        assert not hasattr(ap_mod, "agent_prompt_get")
        assert not hasattr(ap_mod, "agent_prompt_search")
        assert hasattr(ap_mod, "agent_prompt_save")
