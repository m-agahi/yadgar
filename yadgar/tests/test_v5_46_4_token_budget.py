"""RED scaffolding — v5.46.4 B9: roadmap_update_lag_hours omitted when -1.0.

Meta-test: inspect project.py source to verify the conditional omission is present.
"""

from __future__ import annotations

from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / "core" / "server"


def _source(relative: str) -> str:
    return (SERVER_DIR / relative).read_text()


def test_project_py_omits_roadmap_lag_when_minus_one():
    """_project_brief_signals must conditionally omit roadmap_update_lag_hours when -1.0."""
    src = _source("tools/project.py")
    # The old unconditional line: "roadmap_update_lag_hours": roadmap_update_lag_hours
    # The new conditional form: either inline if or _omit_sentinel helper call
    assert (
        "roadmap_update_lag_hours != -1" in src
        or "if roadmap_update_lag_hours" in src
        or "_omit_sentinel" in src
    ), "project.py still unconditionally includes roadmap_update_lag_hours — B9 fix not applied"


def test_roadmap_signal_test_handles_absent_key():
    """test_roadmap_update_signal.py line checking -1 must handle absent key (key omitted when -1.0)."""
    src = (Path(__file__).parent / "test_roadmap_update_signal.py").read_text()
    # Old: result.get("roadmap_update_lag_hours") == -1
    # New: must handle None (absent key) as acceptable
    assert (
        'result.get("roadmap_update_lag_hours", -1) == -1' in src
        or 'result.get("roadmap_update_lag_hours") in (-1' in src
        or 'result.get("roadmap_update_lag_hours") in (-1.0' in src
    ), (
        "test_roadmap_update_signal.py still checks result.get() == -1 without default — "
        "will fail when key is absent after B9 trim"
    )
