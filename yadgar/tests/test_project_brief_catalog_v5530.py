"""Tests for v5.53.0 project_brief wiki catalog (Phase A + D).

TDD: these tests were written before implementation. Red → green.

Coverage:
- catalog/restore/full modes include wiki_catalog key (grouped titles + counts).
- No bare-slug-only lines in the rendered catalog (titles, not slugs alone).
- Per-group length cap: "…M more" affordance appears when a group exceeds cap.
- Multiple categories are grouped separately with per-group counts.
- signals mode is unchanged: no wiki_catalog, no _render, token budget still ≤100.
- MCP instructions string contains read-first contract keywords.
- docs/RECOMMENDED_CLAUDE_RULES.md exists with the read-first rule.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from yadgar import server
from yadgar.server.tools.project import (
    _WIKI_CATALOG_MAX_PER_GROUP,
    _build_wiki_catalog,
    _render_wiki_catalog,
)

# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    with (
        patch("yadgar.server.tools.project._detect_branch", return_value="feat/test"),
        patch("yadgar.server._detect_branch", return_value="feat/test"),
    ):
        yield
    server.shutdown()


@pytest.fixture
def flush_queue():
    def _flush():
        from yadgar import server as _s

        if _s._queue_drainer is not None:
            _s._queue_drainer.drain_now()

    return _flush


def _add_wiki_page(title: str, category: str, directory: str, flush) -> None:
    """Helper: add a wiki page via wiki_add (wait=True for read-your-writes)."""
    server.wiki_add(
        title=title,
        content=f"Content for {title}.",
        category=category,
        wait=True,
        directory=directory,
        force=True,
        branch_hint="feat/test",
    )
    flush()


# ── _build_wiki_catalog helper ────────────────────────────────────────────────


def test_build_wiki_catalog_empty_when_no_pages():
    storage = server._get_storage()
    catalog = _build_wiki_catalog(storage, "/tmp/no_such_project_xyz")
    assert catalog["total"] == 0
    assert catalog["groups"] == {}


def test_build_wiki_catalog_includes_title_not_bare_slug(flush_queue):
    directory = "/tmp/catalog_title_test"
    _add_wiki_page("My Module Overview", "architecture", directory, flush_queue)

    storage = server._get_storage()
    catalog = _build_wiki_catalog(storage, directory)

    assert catalog["total"] >= 1
    arch = catalog["groups"].get("architecture")
    assert arch is not None
    titles = [p["title"] for p in arch["pages"]]
    assert "My Module Overview" in titles


def test_build_wiki_catalog_groups_by_category(flush_queue):
    directory = "/tmp/catalog_group_test"
    _add_wiki_page("Architecture Doc", "architecture", directory, flush_queue)
    _add_wiki_page("Decision Record", "decision", directory, flush_queue)
    _add_wiki_page("Convention Guide", "convention", directory, flush_queue)

    storage = server._get_storage()
    catalog = _build_wiki_catalog(storage, directory)

    assert "architecture" in catalog["groups"]
    assert "decision" in catalog["groups"]
    assert "convention" in catalog["groups"]


def test_build_wiki_catalog_per_group_counts(flush_queue):
    directory = "/tmp/catalog_count_test"
    for i in range(3):
        _add_wiki_page(f"Arch Page {i}", "architecture", directory, flush_queue)
    for i in range(2):
        _add_wiki_page(f"Decision {i}", "decision", directory, flush_queue)

    storage = server._get_storage()
    catalog = _build_wiki_catalog(storage, directory)

    arch = catalog["groups"]["architecture"]
    dec = catalog["groups"]["decision"]
    assert len(arch["pages"]) + arch["more"] == 3
    assert len(dec["pages"]) + dec["more"] == 2


def test_build_wiki_catalog_length_cap_and_more(flush_queue):
    """When a group exceeds _WIKI_CATALOG_MAX_PER_GROUP, 'more' > 0."""
    directory = "/tmp/catalog_cap_test"
    over_limit = _WIKI_CATALOG_MAX_PER_GROUP + 3
    for i in range(over_limit):
        _add_wiki_page(f"Extra Arch Page {i}", "architecture", directory, flush_queue)

    storage = server._get_storage()
    catalog = _build_wiki_catalog(storage, directory)

    arch = catalog["groups"]["architecture"]
    assert len(arch["pages"]) == _WIKI_CATALOG_MAX_PER_GROUP
    assert arch["more"] == 3


# ── _render_wiki_catalog helper ───────────────────────────────────────────────


def test_render_wiki_catalog_empty_returns_nudge():
    catalog = {"total": 0, "groups": {}}
    lines = _render_wiki_catalog(catalog, "/tmp/test")
    assert any("no wiki pages" in ln.lower() or "wiki_list" in ln for ln in lines)


def test_render_wiki_catalog_total_in_output():
    catalog = {
        "total": 7,
        "groups": {"architecture": {"pages": [{"slug": "s1", "title": "Arch Doc"}], "more": 6}},
    }
    lines = _render_wiki_catalog(catalog, "/tmp/test")
    combined = "\n".join(lines)
    assert "7" in combined


def test_render_wiki_catalog_shows_titles_not_bare_slugs():
    catalog = {
        "total": 2,
        "groups": {
            "architecture": {
                "pages": [
                    {"slug": "arch-overview", "title": "Architecture Overview"},
                    {"slug": "module-core", "title": "Core Module"},
                ],
                "more": 0,
            }
        },
    }
    lines = _render_wiki_catalog(catalog, "/tmp/test")
    combined = "\n".join(lines)
    # Titles must appear
    assert "Architecture Overview" in combined
    assert "Core Module" in combined
    # Bare-slug-only lines must not appear (slug without title context)
    # A line that is ONLY the slug (no title decoration) would be a failure.
    for ln in lines:
        stripped = ln.strip().lstrip("- ").strip()
        if stripped == "arch-overview" or stripped == "module-core":
            pytest.fail(f"Bare slug-only line found in catalog render: {ln!r}")


def test_render_wiki_catalog_more_affordance():
    catalog = {
        "total": 10,
        "groups": {
            "architecture": {
                "pages": [{"slug": f"s{i}", "title": f"Page {i}"} for i in range(5)],
                "more": 5,
            }
        },
    }
    lines = _render_wiki_catalog(catalog, "/tmp/test")
    combined = "\n".join(lines)
    assert "more" in combined.lower()
    assert "wiki_list" in combined


def test_render_wiki_catalog_multiple_groups():
    catalog = {
        "total": 4,
        "groups": {
            "architecture": {
                "pages": [{"slug": "a1", "title": "Arch A"}],
                "more": 0,
            },
            "decision": {
                "pages": [{"slug": "d1", "title": "Decision D"}],
                "more": 0,
            },
        },
    }
    lines = _render_wiki_catalog(catalog, "/tmp/test")
    combined = "\n".join(lines)
    assert "architecture" in combined
    assert "decision" in combined
    assert "Arch A" in combined
    assert "Decision D" in combined


# ── project_brief catalog mode ────────────────────────────────────────────────


def test_catalog_mode_has_wiki_catalog_key():
    result = server.project_brief("/tmp/myproject")
    assert "wiki_catalog" in result


def test_catalog_mode_wiki_catalog_is_dict():
    result = server.project_brief("/tmp/myproject")
    catalog = result["wiki_catalog"]
    assert isinstance(catalog, dict)
    assert "total" in catalog
    assert "groups" in catalog


def test_catalog_mode_wiki_catalog_empty_when_no_pages():
    result = server.project_brief("/tmp/no_pages_project_xyz")
    catalog = result["wiki_catalog"]
    assert catalog["total"] == 0
    assert catalog["groups"] == {}


def test_catalog_mode_render_contains_wiki_index_header():
    result = server.project_brief("/tmp/myproject")
    render = result.get("_render", "")
    assert "## Wiki Index" in render


def test_catalog_mode_render_groups_titles_with_pages(flush_queue):
    directory = "/tmp/catalog_render_test"
    _add_wiki_page("Core Architecture", "architecture", directory, flush_queue)
    _add_wiki_page("Tech Decision 1", "decision", directory, flush_queue)

    result = server.project_brief(directory)
    render = result["_render"]

    # Must contain titles
    assert "Core Architecture" in render
    assert "Tech Decision 1" in render
    # Must group by category
    assert "architecture" in render
    assert "decision" in render


def test_catalog_mode_render_no_bare_slug_only_line(flush_queue):
    """Render must not contain a line that is ONLY a bare slug."""
    directory = "/tmp/catalog_no_bare_slug"
    server.wiki_add(
        title="Bare Slug Test Page",
        content="content",
        category="architecture",
        wait=True,
        directory=directory,
        force=True,
        branch_hint="feat/test",
    )
    flush_queue()

    result = server.project_brief(directory)
    render = result["_render"]
    # Find the slug that was generated (it's based on the title)
    catalog = result["wiki_catalog"]
    arch_pages = catalog["groups"].get("architecture", {}).get("pages", [])
    for p in arch_pages:
        slug = p["slug"]
        for ln in render.splitlines():
            stripped = ln.strip().lstrip("- ").strip()
            if stripped == slug:
                pytest.fail(f"Bare slug-only line found in render: {ln!r} (slug={slug!r})")


def test_catalog_mode_render_length_capped(flush_queue):
    """When a group exceeds the cap, the render contains the '…more' affordance."""
    directory = "/tmp/catalog_render_cap"
    over_limit = _WIKI_CATALOG_MAX_PER_GROUP + 2
    for i in range(over_limit):
        server.wiki_add(
            title=f"Cap Test Page {i}",
            content="content",
            category="architecture",
            wait=True,
            directory=directory,
            force=True,
            branch_hint="feat/test",
        )
        flush_queue()

    result = server.project_brief(directory)
    render = result["_render"]
    assert "more" in render.lower()
    assert "wiki_list" in render


# ── project_brief restore mode ────────────────────────────────────────────────


def test_restore_mode_has_wiki_catalog_key():
    result = server.project_brief("/tmp/myproject", mode="restore")
    assert "wiki_catalog" in result


def test_restore_mode_wiki_catalog_is_dict():
    result = server.project_brief("/tmp/myproject", mode="restore")
    catalog = result["wiki_catalog"]
    assert isinstance(catalog, dict)
    assert "total" in catalog
    assert "groups" in catalog


def test_restore_mode_still_has_key_wiki_pages():
    """key_wiki_pages must stay for back-compat (test_restore_mode_returns_required_keys)."""
    result = server.project_brief("/tmp/myproject", mode="restore")
    assert "key_wiki_pages" in result


def test_restore_mode_catalog_groups_with_pages(flush_queue):
    directory = "/tmp/restore_catalog_test"
    _add_wiki_page("Restore Architecture", "architecture", directory, flush_queue)

    result = server.project_brief(directory, mode="restore")
    catalog = result["wiki_catalog"]
    assert catalog["total"] >= 1
    assert "architecture" in catalog["groups"]
    titles = [p["title"] for p in catalog["groups"]["architecture"]["pages"]]
    assert "Restore Architecture" in titles


# ── project_brief full mode ───────────────────────────────────────────────────


def test_full_mode_has_wiki_catalog_key():
    result = server.project_brief("/tmp/myproject", mode="full")
    assert "wiki_catalog" in result


# ── signals mode unchanged ────────────────────────────────────────────────────


def test_signals_mode_has_no_wiki_catalog():
    result = server.project_brief("/tmp/myproject", mode="signals")
    assert "wiki_catalog" not in result


def test_signals_mode_has_no_render():
    result = server.project_brief("/tmp/myproject", mode="signals")
    assert "_render" not in result


def test_signals_mode_has_no_key_wiki_pages():
    result = server.project_brief("/tmp/myproject", mode="signals")
    assert "key_wiki_pages" not in result


def test_signals_mode_token_budget_still_met():
    """signals mode payload must remain ≤100 tokens after v5.53.0 changes."""
    result = server.project_brief("/tmp/myproject", mode="signals")
    tokens = len(json.dumps(result)) // 4
    assert tokens <= 100, f"signals mode too large: {tokens} tokens (budget: 100)"


def test_signals_mode_required_keys_still_present():
    result = server.project_brief("/tmp/myproject", mode="signals")
    required = {
        "init_memory_present",
        "active_work_present",
        "stale_wiki_count",
        "stale_checkpoint_hours",
        "active_work_age_hours",
        "init_memory_age_hours",
        "recommended_actions",
    }
    assert required.issubset(result.keys())


# ── MCP instructions string ───────────────────────────────────────────────────


def test_mcp_instructions_contains_read_first_contract():
    from yadgar.server._app import mcp_server

    instructions = mcp_server._mcp_server.instructions or ""
    # Must contain the key contract phrases
    assert "wiki" in instructions.lower()
    assert "wiki_list" in instructions
    assert "read" in instructions.lower()
    assert "grep" in instructions.lower()


def test_mcp_instructions_mentions_wiki_index():
    from yadgar.server._app import mcp_server

    instructions = mcp_server._mcp_server.instructions or ""
    # The read-first contract should mention wiki index / catalog concept
    assert "wiki_list" in instructions or "wiki index" in instructions.lower()


def test_mcp_instructions_mentions_wiki_query_caveat():
    from yadgar.server._app import mcp_server

    instructions = mcp_server._mcp_server.instructions or ""
    # Should warn that wiki_query is for fuzzy search only (~0.34)
    assert "wiki_query" in instructions
    assert "0.34" in instructions or "fuzzy" in instructions.lower()


# ── docs/RECOMMENDED_CLAUDE_RULES.md ─────────────────────────────────────────


def test_recommended_claude_rules_file_exists():
    """docs/RECOMMENDED_CLAUDE_RULES.md must exist in the repo."""
    # Locate repo root by traversing from this file upward
    here = Path(__file__).parent
    # Go up from yadgar/tests/ → yadgar/ → repo root
    repo_root = here.parent.parent
    rules_file = repo_root / "docs" / "RECOMMENDED_CLAUDE_RULES.md"
    assert rules_file.exists(), f"Expected {rules_file} to exist"


def test_recommended_claude_rules_contains_wiki_map_rule():
    here = Path(__file__).parent
    repo_root = here.parent.parent
    rules_file = repo_root / "docs" / "RECOMMENDED_CLAUDE_RULES.md"
    content = rules_file.read_text()
    assert "wiki" in content.lower()
    assert "map" in content.lower()
    assert "grep" in content.lower()
    assert "wiki_list" in content
    assert "wiki_read" in content
    assert "wiki_query" in content


def test_recommended_claude_rules_mentions_catalog():
    here = Path(__file__).parent
    repo_root = here.parent.parent
    rules_file = repo_root / "docs" / "RECOMMENDED_CLAUDE_RULES.md"
    content = rules_file.read_text()
    assert "catalog" in content.lower()
