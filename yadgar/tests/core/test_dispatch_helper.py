"""Tests for agent_dispatch_prelude — v5.3.6 M2.

Covers:
1. Returns prelude with yadgar contract section.
2. Includes latest agent_prompt if pattern exists.
3. Pattern with no prompts → graceful empty section.
4. Returns string under 2000 chars (orchestrator budget).
"""

from __future__ import annotations

import pytest

from yadgar.tests.core.conftest import TEST_PROJECT_ID

# R3 Car 3c: agent_prompt_save (called in test setup) forwards to backend /admin.
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    # R3 Car 3c: wire module_storage into _st._storage so admin_backend_bypass's
    # run_admin_op call (which calls _get_storage() → _st._storage) resolves
    # the engine. Restored at module teardown.
    import yadgar._shared.runtime.state as _st

    _prev = _st._storage
    _st._storage = module_storage
    yield module_storage
    _st._storage = _prev


class TestDispatchHelperContract:
    """agent_dispatch_prelude always includes the Yadgar protocol contract."""

    def test_prelude_contains_contract_section(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        prelude = agent_dispatch_prelude(
            "dispatch-fix-bug", "vacuum regression", storage=storage, project=TEST_PROJECT_ID
        )
        assert "## Yadgar subagent contract" in prelude

    def test_prelude_contains_recall_directive(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        prelude = agent_dispatch_prelude(
            "any-pattern", "some topic", storage=storage, project=TEST_PROJECT_ID
        )
        # Contract should mention recall
        assert "recall" in prelude.lower()

    def test_prelude_contains_findings_directive(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        prelude = agent_dispatch_prelude(
            "any-pattern", "some topic", storage=storage, project=TEST_PROJECT_ID
        )
        # Contract should mention Yadgar findings
        assert "Yadgar findings" in prelude

    def test_prelude_mentions_no_memorize(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        prelude = agent_dispatch_prelude(
            "any-pattern", "some topic", storage=storage, project=TEST_PROJECT_ID
        )
        # Contract should tell agent not to memorize
        assert "memorize" in prelude.lower() or "NOT" in prelude


class TestDispatchHelperAgentPrompt:
    """Includes latest agent_prompt when pattern exists."""

    def test_includes_agent_prompt_content(self, storage):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        agent_prompt_save(
            "dispatch-fix-bug",
            "Focus on root cause. Check test suite.",
            storage=storage,
            directory="global",
            project=TEST_PROJECT_ID,
        )

        prelude = agent_dispatch_prelude(
            "dispatch-fix-bug", "fix the thing", storage=storage, project=TEST_PROJECT_ID
        )
        assert "root cause" in prelude

    def test_includes_version_label(self, storage):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        agent_prompt_save(
            "dispatch-review",
            "Review carefully.",
            storage=storage,
            directory="global",
            project=TEST_PROJECT_ID,
        )
        agent_prompt_save(
            "dispatch-review",
            "Review even more carefully.",
            storage=storage,
            directory="global",
            project=TEST_PROJECT_ID,
        )

        prelude = agent_dispatch_prelude(
            "dispatch-review", "review task", storage=storage, project=TEST_PROJECT_ID
        )
        # Should reference v2 (latest)
        assert "v2" in prelude

    def test_pattern_not_found_graceful(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        # No prompt saved for this pattern
        prelude = agent_dispatch_prelude(
            "nonexistent-xyz-pattern", "some task", storage=storage, project=TEST_PROJECT_ID
        )
        # Should still return a valid prelude (just without agent-prompt section)
        assert "## Yadgar subagent contract" in prelude
        assert isinstance(prelude, str)
        assert len(prelude) > 0


class TestDispatchHelperNoPatternsGraceful:
    """Empty pattern → no prompt lookup attempted, prelude still valid."""

    def test_empty_pattern_skips_prompt_lookup(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        prelude = agent_dispatch_prelude(
            "", "refactor database layer", storage=storage, project=TEST_PROJECT_ID
        )
        # Should not mention agent-prompt section
        assert "## Yadgar subagent contract" in prelude
        assert "Recall hint" in prelude

    def test_recall_hint_includes_task_topic(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        prelude = agent_dispatch_prelude(
            "", "memory pressure investigation", storage=storage, project=TEST_PROJECT_ID
        )
        assert "memory pressure investigation" in prelude


class TestDispatchHelperSizeBudget:
    """Output is always ≤ 2000 chars."""

    def test_short_prelude_under_budget(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        prelude = agent_dispatch_prelude(
            "short-pattern", "short topic", storage=storage, project=TEST_PROJECT_ID
        )
        assert len(prelude) <= 2000

    def test_long_agent_prompt_capped(self, storage):
        from yadgar.core.server.tools.agent_prompts import agent_prompt_save
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        # Save a very long agent prompt
        long_content = "x" * 5000
        agent_prompt_save(
            "long-pattern",
            long_content,
            storage=storage,
            directory="global",
            project=TEST_PROJECT_ID,
        )

        prelude = agent_dispatch_prelude(
            "long-pattern", "long topic", storage=storage, project=TEST_PROJECT_ID
        )
        assert len(prelude) <= 2000

    def test_returns_string(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        result = agent_dispatch_prelude("any", "any", storage=storage, project=TEST_PROJECT_ID)
        assert isinstance(result, str)
