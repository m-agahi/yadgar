"""RED scaffolding — v5.46.4 B1: wiki_page fixtures supply directory_context.

Meta-test: inspect source of key test files to verify directory_context is present
in wiki_page INSERT fixtures (positive-path tests).

ADR-0215 removed three of the four test files this scanned, and those checks went
with them. The surviving check is the one whose target still exists.
"""

from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).parent.parent


def _source(filename: str) -> str:
    return (TESTS_DIR / filename).read_text()


def test_export_duckdb_seeded_storage_has_directory_context():
    """seeded_storage fixture in test_export_duckdb.py must supply directory_context."""
    src = _source("core/test_export_duckdb.py")
    assert "directory_context" in src, (
        "seeded_storage wiki_page INSERT missing directory_context — B1 fix not applied"
    )
