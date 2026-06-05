"""RED scaffolding — v5.46.4 B10: test_harness_hardening.py uses dynamic repo root.

Meta-test: inspect source to verify no hardcoded /home/max/git/yadgar paths remain.
"""

from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).parent
HARDCODED_PATH = "/home/max/git/yadgar"


def _source(filename: str) -> str:
    return (TESTS_DIR / filename).read_text()


def test_harness_hardening_no_hardcoded_repo_path():
    """test_harness_hardening.py must not contain the hardcoded /home/max/git/yadgar path."""
    src = _source("test_harness_hardening.py")
    assert HARDCODED_PATH not in src, (
        f"test_harness_hardening.py still contains hardcoded '{HARDCODED_PATH}' — "
        "B10 fix not applied; replace with Path(__file__).resolve().parents[2]"
    )


def test_harness_hardening_uses_dynamic_root():
    """test_harness_hardening.py must use __file__-relative path resolution."""
    src = _source("test_harness_hardening.py")
    assert "__file__" in src or "Path(__file__" in src, (
        "test_harness_hardening.py must use Path(__file__).resolve().parents[N] for repo root"
    )
