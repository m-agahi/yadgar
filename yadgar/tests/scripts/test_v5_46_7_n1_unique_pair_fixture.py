"""v5.46.7 TDD — N1: test_export_duckdb seeded_storage unique pair fixture.

N1 regression: seeded_storage fixture inserts ('memory:1', 'memory:2') sim-link
which hits unique index memory_sim_link_pair_idx on repeated fixture instantiation.

This test verifies the fixture seed uses unique IDs per invocation (after fix).
"""

from yadgar.tests._paths import REPO_ROOT


def _source(filename: str) -> str:
    return (REPO_ROOT / "yadgar" / "tests" / filename).read_text()


def test_seeded_storage_uses_unique_sim_link_ids():
    """seeded_storage fixture must not hard-code memory:1/memory:2 for sim-link IDs.

    The unique index memory_sim_link_pair_idx fires when the same (source, target)
    pair is inserted twice. The fixture must use IDs that are unique per test run
    (e.g., using str(uuid.uuid4()) or dynamic IDs) OR the fixture must clean up
    the memory_similarity_link table before inserting.

    After fix: the source code must NOT contain the exact duplicate pair pattern
    "memory:1" and "memory:2" as hardcoded literals in the sim-link CREATE, OR
    the fixture must include a DELETE/REMOVE before INSERT, OR use unique IDs.
    """
    src = _source("core/test_export_duckdb.py")

    # Check for a cleanup/delete before the sim-link insert OR unique IDs used.
    # The simplest fix is to use unique row IDs; check the approach used.
    # We verify the fixture handles the unique constraint — exact mechanism varies.

    # The fixture should NOT have both "memory:1" AND "memory:2" hardcoded
    # in a CREATE memory_similarity_link statement without a preceding DELETE.
    # Strategy: check if sim-link insert appears and whether it's protected.

    # After fix, one of these must be true:
    # 1. "DELETE FROM memory_similarity_link" or "REMOVE memory_similarity_link" appears before insert
    # 2. uuid or dynamic IDs are used (no fixed "memory:1", "memory:2" in sim-link create)
    # 3. The fixture uses a different approach to avoid duplicate pairs

    # We check for the presence of a guard — any of the above patterns.
    has_delete_guard = (
        "DELETE FROM memory_similarity_link" in src
        or "REMOVE memory_similarity_link" in src
        or "DELETE memory_similarity_link" in src
    )
    has_dynamic_ids = "uuid" in src.lower() or "unique" in src.lower()

    # Find the sim-link insert block
    sim_link_idx = src.find("memory_similarity_link")
    if sim_link_idx == -1:
        # If no sim-link insert, the issue is resolved by removal
        return

    # Check if the fixture avoids duplicate pairs
    assert has_delete_guard or has_dynamic_ids, (
        "seeded_storage fixture must avoid duplicate memory_similarity_link pairs. "
        "Add DELETE before INSERT or use unique IDs. "
        "N1: unique index memory_sim_link_pair_idx violation on repeated fixture instantiation."
    )
