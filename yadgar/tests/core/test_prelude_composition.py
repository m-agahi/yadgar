"""TDD (RED-first) — Stage 3 item 3: composition resolution in agent_dispatch_prelude.

The prelude resolves the pattern page's ## Composes [[slug]] references and
assembles: contract → disciplines (Composes order) → pattern → recall hint.

Rules under test:
  - order is deterministic (contract, disciplines, pattern, hint)
  - dedup: a discipline in CONTRACT_COVERS is never re-included; a slug listed
    twice is included once
  - the ## Composes section is stripped from the injected pattern snippet
  - budget: total ≤ _TOTAL_BUDGET; overflow drops disciplines last-listed-first
    and logs a warning; the contract is never dropped
  - seed-on-miss: an absent discipline page is re-seeded from genesis (or the
    genesis text is used as fallback)
  - MagicMock storage (X1 back-compat) never crashes the prelude
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# R3 Car 3c: agent_prompt_save forwards to backend /admin.
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


@pytest.fixture(scope="module")
def storage(module_storage):
    import yadgar._shared.runtime.state as _st

    _prev = _st._storage
    _st._storage = module_storage
    yield module_storage
    _st._storage = _prev


@pytest.fixture(autouse=True)
def _fresh_cache():
    from yadgar.core.server.tools.dispatch_helper import _prompt_cache

    _prompt_cache.clear()
    yield
    _prompt_cache.clear()


def _seed_all(storage):
    from yadgar.core.server.tools.agent_prompts import (
        _seed_contract_page,
        _seed_discipline_pages,
    )

    _seed_contract_page(storage=storage)
    _seed_discipline_pages(storage=storage)


def _save_pattern(storage, pattern: str, body: str) -> None:
    from yadgar.core.server.tools.agent_prompts import agent_prompt_save

    agent_prompt_save(pattern, body, directory="global", storage=storage)


class TestParseComposes:
    def test_parses_slugs_in_order(self):
        from yadgar.core.server.tools.dispatch_helper import _parse_composes

        content = (
            "## Purpose\n\np\n\n## Prompt\n\nbody\n\n## Composes\n"
            "- [[agent-discipline-plan-lifecycle]]\n"
            "- [[agent-discipline-process-hygiene]]\n"
        )
        assert _parse_composes(content) == [
            "agent-discipline-plan-lifecycle",
            "agent-discipline-process-hygiene",
        ]

    def test_dedups_repeated_slug(self):
        from yadgar.core.server.tools.dispatch_helper import _parse_composes

        content = (
            "## Composes\n- [[agent-discipline-branch-state]]\n"
            "- [[agent-discipline-branch-state]]\n"
        )
        assert _parse_composes(content) == ["agent-discipline-branch-state"]

    def test_no_composes_section(self):
        from yadgar.core.server.tools.dispatch_helper import _parse_composes

        assert _parse_composes("## Purpose\n\nx\n\n## Prompt\n\ny\n") == []

    def test_ignores_links_outside_composes(self):
        from yadgar.core.server.tools.dispatch_helper import _parse_composes

        content = "## Prompt\n\nsee [[agent-discipline-branch-state]]\n"
        assert _parse_composes(content) == []


class TestCompositionAssembly:
    def test_discipline_text_included_in_order(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_all(storage)
        _save_pattern(
            storage,
            "compose-order-test",
            "PATTERN_BODY_MARKER\n\n## Composes\n- [[agent-discipline-plan-lifecycle]]\n",
        )
        prelude = agent_dispatch_prelude("compose-order-test", "topic x", storage=storage)

        assert "ADR-0081" in prelude, "composed discipline body missing from prelude"
        assert "PATTERN_BODY_MARKER" in prelude
        # Deterministic order: contract → discipline → pattern → hint
        i_contract = prelude.index("## Yadgar subagent contract")
        i_disc = prelude.index("agent-discipline-plan-lifecycle")
        i_pattern = prelude.index("PATTERN_BODY_MARKER")
        i_hint = prelude.index("## Recall hint")
        assert i_contract < i_disc < i_pattern < i_hint

    def test_composes_section_stripped_from_pattern_snippet(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_all(storage)
        _save_pattern(
            storage,
            "compose-strip-test",
            "BODY_X\n\n## Composes\n- [[agent-discipline-plan-lifecycle]]\n",
        )
        prelude = agent_dispatch_prelude("compose-strip-test", "topic", storage=storage)
        # The raw composes bullet list must not appear inside the pattern section
        # (the discipline is injected as its own section instead).
        assert "## Composes" not in prelude

    def test_contract_covered_discipline_not_reincluded(self, storage):
        from yadgar.core.server.tools.agent_prompts import CONTRACT_COVERS
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_all(storage)
        covered = CONTRACT_COVERS[0]  # agent-discipline-recall-first
        _save_pattern(
            storage,
            "compose-dedup-test",
            f"BODY_Y\n\n## Composes\n- [[{covered}]]\n",
        )
        prelude = agent_dispatch_prelude("compose-dedup-test", "topic", storage=storage)
        assert f"## Discipline [{covered}]" not in prelude, (
            "contract-covered discipline must not be re-included"
        )

    def test_budget_overflow_drops_disciplines_last_first(self, storage, caplog):
        """With a fat pattern + several disciplines, total stays ≤ budget, the
        contract + pattern survive, and a warning is logged for dropped ones."""
        import logging

        from yadgar.core.server.tools.dispatch_helper import (
            _TOTAL_BUDGET,
            agent_dispatch_prelude,
        )

        _seed_all(storage)
        _save_pattern(
            storage,
            "compose-budget-test",
            "Z" * 1200
            + "\n\n## Composes\n"
            + "- [[agent-discipline-process-hygiene]]\n"
            + "- [[agent-discipline-branch-state]]\n"
            + "- [[agent-discipline-plan-lifecycle]]\n"
            + "- [[agent-discipline-commit-hygiene]]\n",
        )
        with caplog.at_level(logging.WARNING):
            prelude = agent_dispatch_prelude("compose-budget-test", "topic", storage=storage)
        assert len(prelude) <= _TOTAL_BUDGET
        assert "## Yadgar subagent contract" in prelude
        assert "ZZZZ" in prelude, "pattern body must survive discipline drops"
        dropped_warnings = [r for r in caplog.records if "dropping discipline" in r.message]
        assert dropped_warnings, "expected a warning for dropped disciplines"

    def test_seed_on_miss_for_absent_discipline(self, storage):
        """Discipline page deleted → composition reseeds from genesis and the
        discipline text still lands in the prelude."""
        from yadgar.core.server.tools.agent_prompts import _read_agent_prompt
        from yadgar.core.server.tools.dispatch_helper import (
            _prompt_cache,
            agent_dispatch_prelude,
        )

        _seed_all(storage)
        slug = "agent-discipline-plan-lifecycle"
        page = storage.get_wiki_page_by_slug(slug)
        assert page is not None
        page_id = storage._extract_id(page.get("id"))
        storage._q(f"DELETE wiki_page:{page_id}")
        _prompt_cache.clear()
        assert _read_agent_prompt(slug, storage=storage) is None, "delete failed"

        _save_pattern(
            storage,
            "compose-reseed-test",
            f"BODY_R\n\n## Composes\n- [[{slug}]]\n",
        )
        prelude = agent_dispatch_prelude("compose-reseed-test", "topic", storage=storage)
        assert "ADR-0081" in prelude, "seed-on-miss failed: discipline text absent"
        # And the page is back (reseeded), not just genesis-fallback text.
        assert _read_agent_prompt(slug, storage=storage) is not None

    def test_no_composes_prelude_unchanged_shape(self, storage):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        _seed_all(storage)
        _save_pattern(storage, "compose-none-test", "PLAIN_BODY no refs here")
        prelude = agent_dispatch_prelude("compose-none-test", "topic", storage=storage)
        assert "PLAIN_BODY" in prelude
        assert "## Discipline [" not in prelude


class TestMagicMockStorageX1:
    def test_magicmock_storage_never_crashes(self):
        from yadgar.core.server.tools.dispatch_helper import agent_dispatch_prelude

        result = agent_dispatch_prelude("any-pattern", "any topic", storage=MagicMock())
        assert isinstance(result, str)
        assert "## Yadgar subagent contract" in result
