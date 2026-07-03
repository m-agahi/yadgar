"""RED-first tests for migration_025 (S7, v5.85 rework).

Verifies -vN slug collapse → one page per pattern.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


class TestMigration025AgentPromptSlugCollapse:
    def test_collapses_v1_v2_to_bare_slug(self, storage):
        from yadgar.storage.migrations import _migration_025_agent_prompt_slug_collapse

        # Seed two versioned pages
        storage.insert_wiki_page(
            {
                "slug": "agent-prompt-fix-bug-v1",
                "title": "Agent Prompt: fix-bug v1",
                "content": "Fix bugs version 1.",
                "tags": ["agent-prompt", "task:fix-bug"],
                "links": [],
                "category": "reference",
                "confidence": "high",
                "source_memory_ids": [],
                "directory_context": "global",
            }
        )
        storage.insert_wiki_page(
            {
                "slug": "agent-prompt-fix-bug-v2",
                "title": "Agent Prompt: fix-bug v2",
                "content": "Fix bugs version 2 — improved.",
                "tags": ["agent-prompt", "task:fix-bug"],
                "links": [],
                "category": "reference",
                "confidence": "high",
                "source_memory_ids": [],
                "directory_context": "global",
            }
        )

        _migration_025_agent_prompt_slug_collapse(storage)

        # Bare slug must exist with v2 content
        page = storage.get_wiki_page_by_slug("agent-prompt-fix-bug")
        assert page is not None, "bare slug page not created"
        assert "version 2" in page["content"]

        # Old versioned slugs must be gone
        assert storage.get_wiki_page_by_slug("agent-prompt-fix-bug-v1") is None
        assert storage.get_wiki_page_by_slug("agent-prompt-fix-bug-v2") is None

    def test_noop_when_no_versioned_slugs(self, storage):
        from yadgar.storage.migrations import _migration_025_agent_prompt_slug_collapse

        # Add a regular (non-agent-prompt) page and a bare agent-prompt page
        storage.insert_wiki_page(
            {
                "slug": "some-other-page",
                "title": "Other Page",
                "content": "Content.",
                "tags": ["reference"],
                "links": [],
                "category": "reference",
                "confidence": "high",
                "source_memory_ids": [],
                "directory_context": "global",
            }
        )
        # Should not raise or fail
        _migration_025_agent_prompt_slug_collapse(storage)
        # The other page is untouched
        assert storage.get_wiki_page_by_slug("some-other-page") is not None

    def test_idempotent_rerun(self, storage):
        from yadgar.storage.migrations import _migration_025_agent_prompt_slug_collapse

        storage.insert_wiki_page(
            {
                "slug": "agent-prompt-deploy-v1",
                "title": "Agent Prompt: deploy v1",
                "content": "Deploy carefully.",
                "tags": ["agent-prompt"],
                "links": [],
                "category": "reference",
                "confidence": "high",
                "source_memory_ids": [],
                "directory_context": "global",
            }
        )
        _migration_025_agent_prompt_slug_collapse(storage)
        # Second run — bare slug exists, no -vN slugs → pure no-op
        _migration_025_agent_prompt_slug_collapse(storage)
        page = storage.get_wiki_page_by_slug("agent-prompt-deploy")
        assert page is not None
