"""TDD tests for car #13: adr_log field in project_brief restore mode.

Tests written BEFORE implementation. Run red first, then implement, then green.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from yadgar import server


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("project_brief_adr_log")
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── 1. restore mode result has adr_log key ────────────────────────────────────


def test_restore_mode_has_adr_log_field():
    """restore mode result must contain 'adr_log' key."""
    result = server.project_brief("/tmp/adr_log_test_proj", mode="restore")
    assert "adr_log" in result, f"'adr_log' missing from restore result keys: {list(result.keys())}"


# ── 2. adr_log slug matches project ──────────────────────────────────────────


def test_restore_adr_log_slug_matches_project():
    """adr_log['slug'] must be '<project>-adr-log' for the test project."""
    result = server.project_brief("/tmp/myspecialproject", mode="restore")
    assert result["adr_log"]["slug"] == "myspecialproject-adr-log"


# ── 3. latest_ids empty when absent ──────────────────────────────────────────


def test_restore_adr_log_latest_ids_empty_when_absent():
    """When wiki_read returns error (log absent), latest_ids must be []."""
    result = server.project_brief("/tmp/no_adr_log_proj", mode="restore")
    assert result["adr_log"]["latest_ids"] == []


# ── 4. latest_ids populated when log has ADRs ────────────────────────────────


def test_restore_adr_log_latest_ids_when_present(tmp_path):
    """Seed log with 3 ADRs; latest_ids must be ['ADR-0003', 'ADR-0002', 'ADR-0001'] (descending)."""
    log_content = (
        "# ADR Log\n\n"
        "## ADR-0001: First decision\n\nsome content\n\n"
        "## ADR-0002: Second decision\n\nsome content\n\n"
        "## ADR-0003: Third decision\n\nsome content\n"
    )
    with patch(
        "yadgar.server.tools.wiki.wiki_read",
        return_value={"content": log_content, "slug": "test-adr-log"},
    ):
        result = server.project_brief(str(tmp_path), mode="restore")

    assert result["adr_log"]["latest_ids"] == ["ADR-0003", "ADR-0002", "ADR-0001"]


# ── 5. latest_ids capped at 3 ────────────────────────────────────────────────


def test_restore_adr_log_latest_ids_capped_at_three(tmp_path):
    """Seed log with 5 ADRs; latest_ids must contain exactly 3, starting with ADR-0005."""
    log_content = (
        "# ADR Log\n\n"
        "## ADR-0001: First\n\nsome content\n\n"
        "## ADR-0002: Second\n\nsome content\n\n"
        "## ADR-0003: Third\n\nsome content\n\n"
        "## ADR-0004: Fourth\n\nsome content\n\n"
        "## ADR-0005: Fifth\n\nsome content\n"
    )
    with patch(
        "yadgar.server.tools.wiki.wiki_read",
        return_value={"content": log_content, "slug": "test-adr-log"},
    ):
        result = server.project_brief(str(tmp_path), mode="restore")

    assert len(result["adr_log"]["latest_ids"]) == 3
    assert result["adr_log"]["latest_ids"][0] == "ADR-0005"


# ── 6. adr_log dict has no 'body' key (cheap check) ─────────────────────────


def test_restore_adr_log_body_absent():
    """adr_log dict must NOT have a 'body' key — slug + latest_ids only."""
    result = server.project_brief("/tmp/adr_no_body_test", mode="restore")
    assert "body" not in result["adr_log"], (
        f"adr_log must not contain 'body', got keys: {list(result['adr_log'].keys())}"
    )


# ── 7. wiki_read called with branch_hint from _get_default_branch ────────────


def test_restore_adr_log_uses_default_branch_hint(tmp_path):
    """wiki_read must be called with branch_hint matching _get_default_branch(resolved)."""
    captured_calls: list[dict] = []

    def fake_wiki_read(slug, directory=None, branch_hint=None):
        captured_calls.append({"slug": slug, "directory": directory, "branch_hint": branch_hint})
        return {"error": "not found"}

    with patch("yadgar.server.tools.wiki.wiki_read", side_effect=fake_wiki_read):
        server.project_brief(str(tmp_path), mode="restore")

    # At least one call should be for the ADR log slug
    from yadgar.server.tools.project import _get_default_branch

    expected_branch = _get_default_branch(str(tmp_path))
    adr_calls = [c for c in captured_calls if "adr-log" in (c.get("slug") or "")]
    assert len(adr_calls) >= 1, f"No wiki_read call for ADR log slug. All calls: {captured_calls}"
    assert adr_calls[0]["branch_hint"] == expected_branch, (
        f"Expected branch_hint={expected_branch!r}, got {adr_calls[0]['branch_hint']!r}"
    )


# ── 8. graceful degradation when wiki_read raises ────────────────────────────


def test_restore_adr_log_no_crash_when_wiki_read_raises(tmp_path):
    """If wiki_read raises an exception, restore mode still returns a result."""

    def raising_wiki_read(slug, directory=None, branch_hint=None):
        raise RuntimeError("wiki connection error")

    with patch("yadgar.server.tools.wiki.wiki_read", side_effect=raising_wiki_read):
        result = server.project_brief(str(tmp_path), mode="restore")

    # Must not crash; adr_log field present with empty latest_ids or absent but no exception
    assert isinstance(result, dict), "project_brief must return a dict even when wiki_read raises"
    if "adr_log" in result:
        assert result["adr_log"]["latest_ids"] == []
