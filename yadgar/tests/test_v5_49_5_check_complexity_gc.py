"""Tests for scripts/check_complexity.py --gc flag (v5.49.5).

Tests 17–18 per plan § 5.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add scripts/ to sys.path so we can import check_complexity directly.
# Path layout: yadgar/tests/test_*.py → repo_root/scripts/
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_complexity import gc_baseline, load_baseline  # noqa: E402

# ---------------------------------------------------------------------------
# Test 17 — gc removes orphan entries
# ---------------------------------------------------------------------------


def test_check_complexity_gc_removes_orphan_entries(tmp_path):
    """--gc removes baseline entries whose <symbol>@<line> doesn't exist in code.

    Populates baseline with a fake orphan key; confirms it is removed after GC.
    """
    # Write a minimal real Python file
    src = tmp_path / "real_module.py"
    src.write_text(
        "def real_func(x: int) -> int:\n    return x + 1\n",
        encoding="utf-8",
    )

    # Write baseline with one real key and one orphan key
    baseline_path = tmp_path / ".complexity-baseline.json"
    # Real key format: <rel_path>::<name>@<lineno>
    # We can't know the exact repo-relative path for tmp_path files,
    # so use the absolute path (gc_baseline uses _rel_path which falls back to abs)
    real_key = f"{src}::real_func@1"
    orphan_key = f"{src}::deleted_func@999"
    baseline = {
        real_key: {"cyclo": 1, "loc": 2, "nesting": 0, "params": 1},
        orphan_key: {"cyclo": 5, "loc": 20, "nesting": 2, "params": 3},
        f"{src}::__file__": {"loc": 2},
    }
    baseline_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")

    removed = gc_baseline([str(src)], str(baseline_path))

    result = load_baseline(str(baseline_path))

    assert removed == 1, f"Expected 1 entry removed, got {removed}"
    assert orphan_key not in result, "Orphan key should have been removed"
    assert real_key in result, "Real key should be preserved"
    assert f"{src}::__file__" in result, "File-level key should be preserved"


# ---------------------------------------------------------------------------
# Test 18 — gc preserves current entries
# ---------------------------------------------------------------------------


def test_check_complexity_gc_preserves_current_entries(tmp_path):
    """--gc preserves all baseline entries that correspond to real symbols.

    Verifies that GC is conservative — no false positives.
    """
    # Write a real Python file with two functions
    src = tmp_path / "two_funcs.py"
    src.write_text(
        "def alpha(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n"
        "def beta(x: str) -> str:\n"
        "    return x.upper()\n",
        encoding="utf-8",
    )

    # Run GC with a baseline containing only real keys (pre-populated by scan)
    # Build the baseline first via update_baseline
    from check_complexity import update_baseline

    baseline_path = tmp_path / ".complexity-baseline.json"
    update_baseline([str(src)], str(baseline_path))

    before = load_baseline(str(baseline_path))
    before_count = len(before)

    removed = gc_baseline([str(src)], str(baseline_path))

    after = load_baseline(str(baseline_path))

    assert removed == 0, f"Expected 0 entries removed, got {removed}. Removed: {removed}"
    assert len(after) == before_count, (
        f"Entry count changed: {before_count} → {len(after)}. "
        f"Before: {sorted(before.keys())}, After: {sorted(after.keys())}"
    )
