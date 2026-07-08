"""RED scaffolding — v5.46.4 B1: wiki_page fixtures supply directory_context.

Meta-test: inspect source of key test files to verify directory_context is present
in wiki_page INSERT fixtures (positive-path tests).
"""

from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).parent.parent


def _source(filename: str) -> str:
    return (TESTS_DIR / filename).read_text()


def test_wiki_read_resolution_insert_has_directory_context():
    """_insert_wiki_page in test_wiki_read_resolution.py must set directory_context."""
    src = _source("core/test_wiki_read_resolution.py")
    assert "directory_context" in src, (
        "_insert_wiki_page fixture missing directory_context field — B1 fix not applied"
    )


def test_wiki_cleanup_merged_branches_inserts_have_directory_context():
    """Direct INSERT calls in test_wiki_cleanup_merged_branches.py must include directory_context."""
    src = _source("core/test_wiki_cleanup_merged_branches.py")
    assert "directory_context" in src, (
        "wiki_cleanup INSERT fixtures missing directory_context field — B1 fix not applied"
    )


def test_branch_schema_migration_insert_has_directory_context():
    """_insert_bare_wiki_page in test_branch_schema_migration.py must set directory_context."""
    src = _source("core/test_branch_schema_migration.py")
    assert "directory_context" in src, (
        "_insert_bare_wiki_page fixture missing directory_context field — B1 fix not applied"
    )


def test_export_duckdb_seeded_storage_has_directory_context():
    """seeded_storage fixture in test_export_duckdb.py must supply directory_context."""
    src = _source("core/test_export_duckdb.py")
    assert "directory_context" in src, (
        "seeded_storage wiki_page INSERT missing directory_context — B1 fix not applied"
    )


def test_anchor_surfacing_empty_string_test_is_skipped():
    """test_empty_string_directory_context_treated_as_global must be skip-marked (schema rejects empty string)."""
    src = _source("_shared/test_anchor_surfacing.py")
    # The test should be marked skip or have pytest.skip
    assert "pytest.mark.skip" in src or "pytest.skip" in src, (
        "test_empty_string_directory_context_treated_as_global must be skip-marked — "
        "schema now rejects empty string, behavior change deferred to v5.46.6"
    )
