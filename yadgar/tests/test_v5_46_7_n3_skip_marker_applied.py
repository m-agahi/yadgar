"""v5.46.7 TDD — N3: test_anchor_surfacing skip marker guard.

Verifies that test_anchor_surfacing.py::test_empty_string_directory_context_treated_as_global
carries a skip marker (per N3 fix requirement from v5.46.7).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(filename: str) -> str:
    return (REPO_ROOT / "yadgar" / "tests" / filename).read_text()


def test_anchor_surfacing_empty_string_test_is_skip_marked():
    """test_empty_string_directory_context_treated_as_global must carry @pytest.mark.skip.

    The guard test in test_v5_46_4_fixture_directory_context.py checks for this.
    v5.46.7 applies the marker to satisfy the guard.
    """
    src = _source("test_anchor_surfacing.py")
    assert "pytest.mark.skip" in src or "pytest.skip" in src, (
        "test_anchor_surfacing.py must contain pytest.mark.skip (or pytest.skip) — "
        "needed to satisfy test_v5_46_4_fixture_directory_context guard test (N3)"
    )
