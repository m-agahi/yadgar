"""TDD (RED-first) — S8: seed_agent_prompts seeder tool.

Tests:
  1. test_seed_creates_four_starters   — fresh store → 4 pages created, all slugs exist
  2. test_seed_idempotent              — second call creates 0, skips 4; TOC has exactly 4 rows
  3. test_seed_single_anchor           — two seed calls → exactly 1 library anchor
  4. test_seed_tool_registered         — seed_agent_prompts in __all__ and on module
  5. test_starter_content_nonempty     — all 4 starters have non-empty multi-line content
"""

from __future__ import annotations

import pytest

from yadgar.core import server  # noqa: E402

_EXPECTED_SLUGS = [
    "agent-prompt-code-review",
    "agent-prompt-debug-investigate",
    "agent-prompt-explore-codebase",
    "agent-prompt-implement-tdd",
]
_EXPECTED_PATTERNS = [
    "code-review",
    "debug-investigate",
    "explore-codebase",
    "implement-tdd",
]


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
# Test 1: fresh store → 4 pages created
# ---------------------------------------------------------------------------


class TestSeedCreatesFourStarters:
    def test_seed_creates_four_starters(self, storage):
        from yadgar.core.server.tools.agent_prompts import (
            _read_agent_prompt,
            seed_agent_prompts,
        )

        result = seed_agent_prompts(storage=storage)
        assert result["seeded"] is True
        assert result["created"] == 4, f"expected 4 created, got {result}"
        assert result["skipped"] == 0
        assert sorted(result["patterns"]) == sorted(_EXPECTED_PATTERNS)

        for slug in _EXPECTED_SLUGS:
            page = _read_agent_prompt(slug, storage=storage)
            assert page is not None, f"slug {slug!r} not found after seed"
            assert page["version"] == 1


# ---------------------------------------------------------------------------
# Test 2: idempotent — second call creates 0, skips 4; TOC has exactly 4 rows
# ---------------------------------------------------------------------------


class TestSeedIdempotent:
    def test_seed_idempotent(self, storage):
        from yadgar.core.server.tools.agent_prompts import (
            _TOC_ROW_RE,
            _TOC_SLUG,
            seed_agent_prompts,
        )

        # First call
        r1 = seed_agent_prompts(storage=storage)
        assert r1["created"] == 4, f"first call should create 4, got {r1}"

        # Second call — must skip all 4
        r2 = seed_agent_prompts(storage=storage)
        assert r2["created"] == 0, f"second call should create 0, got {r2}"
        assert r2["skipped"] == 4

        # TOC must have exactly 4 pattern rows (not 8)
        import yadgar._shared.runtime.state as _st

        toc_page = _st._storage.get_wiki_page_by_slug(_TOC_SLUG)
        assert toc_page is not None, "TOC page absent after seed"
        content = toc_page.get("content", "")
        row_matches = list(_TOC_ROW_RE.finditer(content))
        assert len(row_matches) == 4, (
            f"TOC should have exactly 4 rows, found {len(row_matches)}:\n{content}"
        )

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

        assert len(STARTER_PROMPTS) == 4, f"expected 4 starters, got {len(STARTER_PROMPTS)}"

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
