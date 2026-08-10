"""TDD tests for car #13: adr_log field in project_brief restore mode.

Tests written BEFORE implementation. Run red first, then implement, then green.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from yadgar.core import server


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
    """Car 2: adr_log['slug'] must be the canonical '<project>-adr-index'."""
    result = server.project_brief("/tmp/myspecialproject", mode="restore")
    assert result["adr_log"]["slug"] == "myspecialproject-adr-index"


# ── 3. latest_ids empty when absent ──────────────────────────────────────────


def test_restore_adr_log_latest_ids_empty_when_absent():
    """When wiki_read returns error (log absent), latest_ids must be []."""
    result = server.project_brief("/tmp/no_adr_log_proj", mode="restore")
    assert result["adr_log"]["latest_ids"] == []


# ── 4. latest_ids populated when log has ADRs ────────────────────────────────


def test_restore_adr_log_latest_ids_when_present(tmp_path):
    """Car 2 + Car G re-point: list_adr_rows returns 3 rows; latest_ids descending.

    Car G (0047 §7) replaced the wiki ``<project>-adr-index`` page with the SQL
    ledger. The contract is unchanged (newest 3 ids, numerics-as-ADR-NNNN) but
    the seam is now ``_forward_admin('list_adr_rows', ...)`` instead of
    ``wiki_read`` + ``parse_index_rows``. Seeding rows out of order so the
    tests would fail if the sort step were dropped.
    """
    rows = [
        {"id": 2, "status": "accepted", "title": "Decision 2"},
        {"id": 1, "status": "accepted", "title": "Decision 1"},
        {"id": 3, "status": "accepted", "title": "Decision 3"},
    ]

    def fake_list_adr_rows(action, params, **kwargs):
        return {"ok": True, "rows": rows}

    with patch(
        "yadgar.core.server.tools.project._forward_admin",
        side_effect=fake_list_adr_rows,
    ):
        result = server.project_brief(str(tmp_path), mode="restore")

    assert result["adr_log"]["latest_ids"] == ["ADR-0003", "ADR-0002", "ADR-0001"]


# ── 5. latest_ids capped at 3 ────────────────────────────────────────────────


def test_restore_adr_log_latest_ids_capped_at_three(tmp_path):
    """Car 2 + Car G re-point: 5 ledger rows; latest_ids capped at 3, newest first.

    Car G (0047 §7) re-pointed the restore seam from the wiki-index page to
    the SQL ``list_adr_rows`` forward. The cap-at-3 contract is preserved;
    only the data source moved. Seed 5 rows so the slice step is observable.
    """
    rows = [{"id": i, "status": "open", "title": f"Decision {i}"} for i in (1, 2, 3, 4, 5)]

    def fake_list_adr_rows(action, params, **kwargs):
        return {"ok": True, "rows": rows}

    with patch(
        "yadgar.core.server.tools.project._forward_admin",
        side_effect=fake_list_adr_rows,
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


# ── 8. graceful degradation when wiki_read raises ────────────────────────────


def test_restore_adr_log_no_crash_when_wiki_read_raises(tmp_path):
    """If wiki_read raises an exception, restore mode still returns a result."""

    def raising_wiki_read(slug, directory=None):
        raise RuntimeError("wiki connection error")

    with patch("yadgar.core.server.tools.wiki.wiki_read", side_effect=raising_wiki_read):
        result = server.project_brief(str(tmp_path), mode="restore")

    # Must not crash; adr_log field present with empty latest_ids or absent but no exception
    assert isinstance(result, dict), "project_brief must return a dict even when wiki_read raises"
    if "adr_log" in result:
        assert result["adr_log"]["latest_ids"] == []
