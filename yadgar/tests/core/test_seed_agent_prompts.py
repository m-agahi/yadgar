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
from yadgar.tests.core.conftest import TEST_PROJECT_ID

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


@pytest.fixture(autouse=True)
def _ledger_row_mocks():
    """In-memory dict mocks for the 7 new ledger ops (no engine #2 in unit tests).

    Car I (0047 spine train) routes agent_prompt_save / discipline_save through
    ``save_agent_pattern_row`` / ``save_agent_discipline_row`` (and the read
    surface through ``list_agent_pattern_rows_uses_desc`` /
    ``get_agent_pattern_row``). The unit test environment has no MariaDB
    composed, so the real ops return ``{ok: False, error: 'engine #2 not
    composed...'}``. The conftest's ``_unit_backend_harness`` autouse fixture
    routes ``_forward_admin`` → ``run_admin_op_blocking`` (in-process dispatch
    against ``_ADMIN_OPS``) — so we install in-memory mocks by replacing the
    ``_ADMIN_OPS`` entries, leaving the dispatch surface intact.
    """
    rows: dict[str, dict] = {}

    def _save_agent_pattern_row(payload):
        name = payload["name"]
        rows[name] = {
            "name": name,
            "body_slug": payload.get("body_slug") or f"agent-prompt-{name}",
            "purpose": payload.get("purpose", ""),
            "status": payload.get("status", "active"),
            "content_hash": payload.get("content_hash", ""),
            "baseline_hash": payload.get("baseline_hash"),
            "uses": rows.get(name, {}).get("uses", 0),
        }
        return rows[name]

    def _save_agent_discipline_row(payload):
        return {"ok": True, "name": payload["name"]}

    def _get_agent_pattern_row(payload):
        return {"row": rows.get(payload["name"])}

    def _list_agent_pattern_rows_uses_desc(payload):
        sorted_rows = sorted(
            rows.values(),
            key=lambda r: (-r.get("uses", 0), r.get("name", "")),
        )
        return {"rows": sorted_rows[: int(payload.get("limit", 20))]}

    def _increment_agent_pattern_uses(payload):
        name = payload["pattern"]
        if name in rows:
            rows[name]["uses"] = rows[name].get("uses", 0) + 1
        return {"ok": True, "pattern": name}

    def _list_pattern_composes(payload):
        return {"rows": []}

    def _get_agent_prompt_toc_updated_at(payload):  # noqa: ARG001
        return {"timestamp": None}

    import yadgar.backend.admin_exec as _exec_module
    import yadgar.backend.admin_exec.ledger as _ledger_module

    _originals = {
        "save_agent_pattern_row": _ledger_module.save_agent_pattern_row,
        "save_agent_discipline_row": _ledger_module.save_agent_discipline_row,
        "get_agent_pattern_row": _ledger_module.get_agent_pattern_row,
        "list_agent_pattern_rows_uses_desc": _ledger_module.list_agent_pattern_rows_uses_desc,
        "increment_agent_pattern_uses": _ledger_module.increment_agent_pattern_uses,
        "list_pattern_composes": _ledger_module.list_pattern_composes,
        "get_agent_prompt_toc_updated_at": _ledger_module.get_agent_prompt_toc_updated_at,
    }
    _exec_module._ADMIN_OPS["save_agent_pattern_row"] = _save_agent_pattern_row
    _exec_module._ADMIN_OPS["save_agent_discipline_row"] = _save_agent_discipline_row
    _exec_module._ADMIN_OPS["get_agent_pattern_row"] = _get_agent_pattern_row
    _exec_module._ADMIN_OPS["list_agent_pattern_rows_uses_desc"] = (
        _list_agent_pattern_rows_uses_desc
    )
    _exec_module._ADMIN_OPS["increment_agent_pattern_uses"] = _increment_agent_pattern_uses
    _exec_module._ADMIN_OPS["list_pattern_composes"] = _list_pattern_composes
    _exec_module._ADMIN_OPS["get_agent_prompt_toc_updated_at"] = _get_agent_prompt_toc_updated_at
    yield rows
    for name, op in _originals.items():
        _exec_module._ADMIN_OPS[name] = op


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

        result = seed_agent_prompts(storage=storage, project=TEST_PROJECT_ID)
        assert result["seeded"] is True
        assert result["created"] == 15, f"expected 15 created, got {result}"
        assert result["skipped"] == 0
        assert sorted(result["patterns"]) == sorted(_EXPECTED_PATTERNS)

        for slug in _EXPECTED_SLUGS:
            page = _read_agent_prompt(slug, storage=storage)
            assert page is not None, f"slug {slug!r} not found after seed"
            assert page["version"] == 1


# ---------------------------------------------------------------------------
# Test 2: idempotent — second call creates 0, skips 15; TOC has exactly 21 rows
# ---------------------------------------------------------------------------


class TestSeedIdempotent:
    def test_seed_idempotent(self, storage):
        from yadgar.core.server.tools.agent_prompts import seed_agent_prompts

        # First call
        r1 = seed_agent_prompts(storage=storage, project=TEST_PROJECT_ID)
        assert r1["created"] == 15, f"first call should create 15, got {r1}"

        # Second call — must skip all 15
        r2 = seed_agent_prompts(storage=storage, project=TEST_PROJECT_ID)
        assert r2["created"] == 0, f"second call should create 0, got {r2}"
        assert r2["skipped"] == 15

        # 0047 Car I: the wiki-TOC page is RETIRED (D35a). The seed no longer
        # upserts TOC rows — the discovery surface is the ``agent_pattern``
        # ledger table. Idempotence is asserted via ledger-row count instead:
        # 15 starters each write one row on first call, zero on second.
        from yadgar.core.server.tools.agent_prompts import agent_prompt_list

        listing = agent_prompt_list(directory="global", limit=50)
        names = {p["name"] for p in listing["patterns"]}
        for slug in _EXPECTED_SLUGS:
            pattern = slug.replace("agent-prompt-", "")
            assert pattern in names, f"agent_pattern row missing for {pattern} after two seed calls"

        # No duplicate pages for any pattern
        all_pages = storage.list_wiki_pages()
        for slug in _EXPECTED_SLUGS:
            matches = [p for p in all_pages if p.get("slug") == slug]
            assert len(matches) == 1, f"expected 1 page for {slug}, found {len(matches)}"


# ---------------------------------------------------------------------------
# Test 3: two seed calls → exactly ONE library anchor
# ---------------------------------------------------------------------------


class TestSeedSingleAnchor:
    def test_seed_single_anchor_no_anchor_emitted(self, storage):
        """0047 Car I: the ``anchor:agent-prompt-library`` memory row is RETIRED.

        The library-discovery anchor was a wiki-era hack that pointed callers at
        the wiki-TOC page. With the TOC retired (D35a) and the discovery
        surface now the ``agent_pattern`` ledger table, the anchor is no
        longer load-bearing — and a global anchor that links to a retired
        page is a permanent lie. The seed must NOT emit any
        ``anchor:agent-prompt-library`` rows.
        """
        from yadgar.core.server.tools.agent_prompts import seed_agent_prompts

        seed_agent_prompts(storage=storage, project=TEST_PROJECT_ID)
        seed_agent_prompts(storage=storage, project=TEST_PROJECT_ID)

        import yadgar._shared.runtime.state as _st

        anchors = _st._storage._q(
            "SELECT id FROM memory "
            "WHERE '_anchor' INSIDE tags AND 'anchor:agent-prompt-library' INSIDE tags"
        )
        assert anchors == [], (
            f"expected ZERO library anchors (Car I retired them), got {len(anchors)}"
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
