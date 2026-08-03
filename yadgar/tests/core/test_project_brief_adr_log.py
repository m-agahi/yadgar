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


# ── 4-5. latest_ids populated + capped at 3 ─────────────────────────────────
# REMOVED in v5.172.0 spine train. The two tests imported
# `_build_index_content` from `yadgar.core.server.tools.adr`; that helper was
# removed with the legacy parser in Car G (commit 5f4edf69, "ADR seed from
# PAGES + delete legacy parser"). With the wiki page replaced by the MariaDB
# ledger row source (Car F, 1186748a), the in-memory index builder has no
# purpose. The sibling `test_restore_adr_log_body_absent` and
# `test_restore_adr_log_no_crash_when_wiki_read_raises` cover the shape and
# graceful-degradation invariants without depending on the removed helper.


# ── 6. adr_log dict has no 'body' key (cheap check) ─────────────────────────


def test_restore_adr_log_body_absent():
    """adr_log dict must NOT have a 'body' key — slug + latest_ids only."""
    result = server.project_brief("/tmp/adr_no_body_test", mode="restore")
    assert "body" not in result["adr_log"], (
        f"adr_log must not contain 'body', got keys: {list(result['adr_log'].keys())}"
    )


# ── 7. canonical index read uses NO branch_hint (531352 fix) ────────────────
# REMOVED in v5.172.0 spine train. Car F (1186748a) re-pointed adr_list/adr_get/
# adr_add to the MariaDB ledger, and `_build_adr_log` (project.py:1866) follows
# the same path: it calls `storage.list_adr_rows(...)`, not `wiki_read(slug)`.
# The wiki page `<project>-adr-index` is no longer the read source for ADR
# metadata — it is a side-effect artifact of the seed (Car G). A new test
# asserting the ledger read path belongs in the car that designs the new
# contract, not as a rewrite of the deleted wiki-read test.


# ── 8. graceful degradation when wiki_read raises ────────────────────────────


def test_restore_adr_log_no_crash_when_wiki_read_raises(tmp_path):
    """If wiki_read raises an exception, restore mode still returns a result."""

    def raising_wiki_read(slug, directory=None, branch_hint=None):
        raise RuntimeError("wiki connection error")

    with patch("yadgar.core.server.tools.wiki.wiki_read", side_effect=raising_wiki_read):
        result = server.project_brief(str(tmp_path), mode="restore")

    # Must not crash; adr_log field present with empty latest_ids or absent but no exception
    assert isinstance(result, dict), "project_brief must return a dict even when wiki_read raises"
    if "adr_log" in result:
        assert result["adr_log"]["latest_ids"] == []
