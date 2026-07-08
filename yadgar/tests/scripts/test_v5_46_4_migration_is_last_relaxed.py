"""RED scaffolding — v5.46.4 B11: migration_014 is_last assertion is relaxed.

Meta-test: inspect test_migration_014_wiki_embedding_backfill.py source to verify
the stale is_last assertion is gone.
"""

from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).parent.parent


def _source(filename: str) -> str:
    return (TESTS_DIR / filename).read_text()


def test_migration_014_is_last_assertion_removed():
    """The stale test_migration_014_is_last assertion must be replaced with membership check."""
    src = _source("scripts/test_migration_014_wiki_embedding_backfill.py")
    # The old brittle line was: assert _MIGRATIONS[-1]["version"] == "014_wiki_page_embedding_backfill"
    assert '_MIGRATIONS[-1]["version"] == "014_wiki_page_embedding_backfill"' not in src, (
        "Stale is_last assertion still present in test_migration_014_* — B11 fix not applied"
    )


def test_migration_014_membership_check_present():
    """A membership check (not positional) for 014 must be present."""
    src = _source("scripts/test_migration_014_wiki_embedding_backfill.py")
    assert "014_wiki_page_embedding_backfill" in src, (
        "membership check for 014_wiki_page_embedding_backfill missing after is_last removal"
    )
