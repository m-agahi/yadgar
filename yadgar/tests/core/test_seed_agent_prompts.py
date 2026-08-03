"""TDD (RED-first) — S8: seed_agent_prompts seeder tool.

v5.122.0: 5th starter added (plan-executing-build) so the packaged prelude
contract's rule-4 pointer resolves on fresh installs. All counts 4 → 5.
v5.123.0 seed backflow: 10 battle-tested live patterns added to the genesis
corpus. All counts 5 → 15; TOC rows 11 → 21.
v5.124.0 consolidation: the 10 backflow patterns replaced by the generic
subset of the consolidated live library (3 merged canonicals rca-diagnose /
plan-audit-with-modes / scope-and-plan / build-car + retained generics);
crash-rca / plan-corpus-status-sweep / perf-anomaly-metrics dropped as merged
or reclassified. Count stays 15 (14 generic + plan-executing-build preamble);
TOC rows stay 21.

Tests:
  1. test_seed_creates_starters        — fresh store → 15 pages created, all slugs exist
  2. test_seed_idempotent              — second call creates 0, skips 15; TOC has exactly
                                         21 rows (15 starters + contract + 5 disciplines)
  3. test_seed_single_anchor           — two seed calls → exactly 1 library anchor
  4. test_seed_tool_registered         — seed_agent_prompts in __all__ and on module
  5. test_starter_content_nonempty     — all 15 starters have non-empty multi-line content
"""

from __future__ import annotations

import pytest

from yadgar.core import server  # noqa: E402

# R3 Car 3c: seed_agent_prompts calls agent_prompt_save which forwards to backend /admin.
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

_EXPECTED_PATTERNS = [
    "pr-review",
    "debug-investigate",
    "explore-codebase",
    "implement-tdd",
    "plan-executing-build",
    # v5.124.0 consolidation (34→18 live map; generic subset seeded).
    # 3 merged canonicals carry a ## Modes section; retired/reclassified
    # patterns (crash-rca, plan-corpus-status-sweep, perf-anomaly-metrics)
    # are no longer seeded — see agent_prompts.yaml prompts: header comment.
    "rca-diagnose",
    "plan-audit",
    "scope-and-plan",
    "build-car",
    "drift-audit",
    "feasibility-design",
    "feature-kill-closeout",
    "dispatch-fix-test-migration",
    "mechanical-refactor-chunk-commit-early",
    "stacked-car-parallel-build",
]
_EXPECTED_SLUGS = [f"agent-prompt-{p}" for p in _EXPECTED_PATTERNS]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Full engine stack (WikiStore + StorageEngine + replay) via init_engines.

    Mirrors test_agent_prompt_discovery_s6.py — necessary so _upsert_toc_row
    and _ensure_library_anchor actually write (they read from _state globals,
    not from the injected storage= kwarg).
    """
    tmp_path = tmp_path_factory.mktemp("seed_agent_prompts")
    server.init_engines(
        db_path=str(tmp_path / "test_seed_agent_prompts.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture
def storage():
    """Expose the live _state storage for direct SQL assertions."""
    import yadgar._shared.runtime.state as _st

    return _st._storage


# ---------------------------------------------------------------------------
# Test 1: fresh store → 15 pages created
# ---------------------------------------------------------------------------


class TestSeedCreatesStarters:
    def test_seed_creates_starters(self, storage):
        from yadgar.core.server.tools.agent_prompts import (
            _read_agent_prompt,
            seed_agent_prompts,
        )

        result = seed_agent_prompts(storage=storage)
        assert result["seeded"] is True
        assert result["created"] == 15, f"expected 15 created, got {result}"
        assert result["skipped"] == 0
        assert sorted(result["patterns"]) == sorted(_EXPECTED_PATTERNS)

        for slug in _EXPECTED_SLUGS:
            page = _read_agent_prompt(slug, storage=storage)
            assert page is not None, f"slug {slug!r} not found after seed"
            assert page["version"] == 1

        # ---------------------------------------------------------------------------
        # Test 2: idempotent — REMOVED in v5.172.0 spine train
        # ---------------------------------------------------------------------------
        # The class was deleted because it imported `_TOC_ROW_RE` and `_TOC_SLUG` from
        # `yadgar.core.server.tools.agent_prompts`. Car I (commit 1b39890d, "agent_prompt
        # list/get + delete TOC machinery") removed the TOC parser and the agent_prompt
        # library now uses the relational ledger (D40 split, MariaDB). New
        # idempotency-coverage tests against the ledger-backed seed path belong in a
        # separate car that designs them — they are not a rewrite of the deleted
        # TOC-parser test.

        # No duplicate pages for any pattern
        all_pages = storage.list_wiki_pages()
        for slug in _EXPECTED_SLUGS:
            matches = [p for p in all_pages if p.get("slug") == slug]
            assert len(matches) == 1, f"expected 1 page for {slug}, found {len(matches)}"


# ---------------------------------------------------------------------------
# Test 3: two seed calls → exactly ONE library anchor
# ---------------------------------------------------------------------------


class TestSeedSingleAnchor:
    def test_seed_single_anchor(self, storage):
        from yadgar.core.server.tools.agent_prompts import seed_agent_prompts

        seed_agent_prompts(storage=storage)
        seed_agent_prompts(storage=storage)

        import yadgar._shared.runtime.state as _st

        anchors = _st._storage._q(
            "SELECT id FROM memory "
            "WHERE '_anchor' INSIDE tags AND 'anchor:agent-prompt-library' INSIDE tags"
        )
        assert len(anchors) == 1, (
            f"expected exactly 1 library anchor after two seed calls, got {len(anchors)}"
        )


# ---------------------------------------------------------------------------
# Test 4: tool registration
# ---------------------------------------------------------------------------


class TestSeedToolRegistered:
    def test_seed_in_all(self):
        from yadgar.core.server import tools

        assert "seed_agent_prompts" in tools.__all__, (
            "seed_agent_prompts missing from yadgar.server.tools.__all__"
        )

    def test_seed_on_module(self):
        import yadgar.core.server.tools.agent_prompts as m

        assert hasattr(m, "seed_agent_prompts"), (
            "seed_agent_prompts not found on yadgar.server.tools.agent_prompts"
        )


# ---------------------------------------------------------------------------
# Test 5: starter content is non-empty and multi-line
# ---------------------------------------------------------------------------


class TestStarterContentNonempty:
    def test_starter_content_nonempty(self):
        from yadgar.core.server.tools.agent_prompts import STARTER_PROMPTS

        assert len(STARTER_PROMPTS) == 15, f"expected 15 starters, got {len(STARTER_PROMPTS)}"

        seen_patterns: set[str] = set()
        for entry in STARTER_PROMPTS:
            pattern, purpose, content = entry
            assert pattern in _EXPECTED_PATTERNS, f"unexpected pattern: {pattern!r}"
            assert purpose, f"empty purpose for {pattern!r}"
            assert content, f"empty content for {pattern!r}"
            # multi-line: at least 3 lines
            assert content.count("\n") >= 3, (
                f"starter {pattern!r} content too short (< 4 lines):\n{content!r}"
            )
            seen_patterns.add(pattern)

        assert seen_patterns == set(_EXPECTED_PATTERNS), (
            f"missing patterns: {set(_EXPECTED_PATTERNS) - seen_patterns}"
        )
