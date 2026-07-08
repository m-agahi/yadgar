"""Canonical path constants for the yadgar test suite.

After the R3 tests-mirror move, test files sit one level deeper than before
(yadgar/tests/{subdir}/test_*.py).  Computing repo root via __file__ chains
from each test file would need a different depth per subdir; instead, compute
once here and import everywhere.

Layout:
    /tmp/reorg3/                   ← REPO_ROOT
    /tmp/reorg3/yadgar/            ← PKG_ROOT
    /tmp/reorg3/yadgar/tests/      ← TESTS_ROOT  (this file lives here)
"""

from __future__ import annotations

from pathlib import Path

# yadgar/tests/_paths.py → parent=yadgar/tests/ → parent.parent=yadgar/ → parent.parent.parent=repo
TESTS_ROOT: Path = Path(__file__).resolve().parent  # yadgar/tests/
PKG_ROOT: Path = TESTS_ROOT.parent  # yadgar/
REPO_ROOT: Path = PKG_ROOT.parent  # repo root
