"""RED scaffolding — v5.46.4 B8: seeded_storage uses 384-dim vectors.

Meta-test: inspect test_export_duckdb.py source to verify embedding_dim=384 and
no residual 4-dim references.
"""

from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).parent.parent


def _source(filename: str) -> str:
    return (TESTS_DIR / filename).read_text()


def test_seeded_storage_uses_384_dim():
    """seeded_storage fixture must use embedding_dim=384, not 4."""
    src = _source("core/test_export_duckdb.py")
    assert "embedding_dim=384" in src, (
        "seeded_storage still uses embedding_dim=4 — B8 fix not applied"
    )


def test_no_4_dim_embedding_in_export_tests():
    """test_export_duckdb.py must not reference embedding_dim=4 anywhere."""
    src = _source("core/test_export_duckdb.py")
    assert "embedding_dim=4" not in src, (
        "test_export_duckdb.py still references embedding_dim=4 — B8 fix not fully applied"
    )


def test_no_4_element_embedding_list():
    """The 4-element embedding list [0.1, 0.2, 0.3, 0.4] must be gone."""
    src = _source("core/test_export_duckdb.py")
    assert "[0.1, 0.2, 0.3, 0.4]" not in src, (
        "4-element embedding list still present in test_export_duckdb.py — B8 fix not applied"
    )
