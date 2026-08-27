"""Task 395 — a baseline entry naming a symbol that no longer exists must FAIL.

``.complexity-baseline.json`` carried
``yadgar/core/server/tools/admin_other.py::_parse_since_duration@146`` long
after that function moved to ``yadgar/core/server/tools/_recent_memories.py``.
Nothing flagged it: I30 has a NO-STALE-ENTRIES property, but it governs
``.complexity-allowlist.json`` only. That is task 282's "recorded number is
decorative" class, one level up — an entry that describes nothing cannot be
wrong, so it never becomes wrong.

The check deliberately separates two kinds of staleness, because conflating
them is why ``--gc`` was left opt-in (see the STANDING DECISION in
``check_complexity.gc_baseline``):

  * LINE DRIFT — ``path::name`` still exists, only ``@lineno`` moved.
    Benign and enormous (1576 of 1711 stale keys measured 2026-08-27);
    NOT an error.
  * DEAD — ``path::name`` exists nowhere in the scanned tree, or the file
    itself is gone. 135 entries measured; these are the decorative ones.

Written BEFORE implementation — all tests start red.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_complexity import dead_baseline_keys  # noqa: E402


def _write(tmp_path, baseline: dict) -> str:
    p = tmp_path / ".complexity-baseline.json"
    p.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    return str(p)


def test_dead_entry_is_reported(tmp_path):
    """A symbol that exists nowhere in the scanned files is DEAD."""
    src = tmp_path / "mod.py"
    src.write_text("def alive(x):\n    return x\n", encoding="utf-8")
    baseline_path = _write(
        tmp_path,
        {
            f"{src}::alive@1": {"loc": 2},
            f"{src}::vanished@42": {"loc": 9},
            f"{src}::__file__": {"loc": 2},
        },
    )

    dead = dead_baseline_keys([str(src)], baseline_path)

    assert dead == [f"{src}::vanished@42"]


def test_line_drift_is_not_reported(tmp_path):
    """A symbol that merely MOVED is not dead — that is the 92% benign bulk."""
    src = tmp_path / "mod.py"
    src.write_text("# a new leading comment\n\ndef alive(x):\n    return x\n", encoding="utf-8")
    baseline_path = _write(
        tmp_path, {f"{src}::alive@1": {"loc": 2}, f"{src}::__file__": {"loc": 4}}
    )

    assert dead_baseline_keys([str(src)], baseline_path) == []


def test_entry_for_a_deleted_file_is_reported(tmp_path):
    """A whole file that no longer exists takes all its entries with it."""
    src = tmp_path / "mod.py"
    src.write_text("def alive(x):\n    return x\n", encoding="utf-8")
    gone = tmp_path / "removed.py"
    baseline_path = _write(
        tmp_path,
        {
            f"{src}::alive@1": {"loc": 2},
            f"{gone}::orphan@3": {"loc": 5},
            f"{gone}::__file__": {"loc": 5},
        },
    )

    dead = dead_baseline_keys([str(src)], baseline_path)

    assert sorted(dead) == sorted([f"{gone}::orphan@3", f"{gone}::__file__"])


def test_unparseable_file_does_not_condemn_its_entries(tmp_path):
    """A file the analyser cannot read is UNKNOWN, not dead.

    ``_collect_live_keys`` skips unreadable files silently; treating their
    entries as dead would delete real records on a transient parse failure.
    """
    src = tmp_path / "broken.py"
    src.write_text("def oops(:\n", encoding="utf-8")
    baseline_path = _write(tmp_path, {f"{src}::oops@1": {"loc": 1}})

    assert dead_baseline_keys([str(src)], baseline_path) == []


def test_entries_outside_the_scanned_set_are_ignored(tmp_path):
    """Only files actually scanned are judged; anything else is out of scope."""
    src = tmp_path / "mod.py"
    src.write_text("def alive(x):\n    return x\n", encoding="utf-8")
    other = tmp_path / "not_scanned.py"
    other.write_text("def elsewhere():\n    pass\n", encoding="utf-8")
    baseline_path = _write(
        tmp_path,
        {f"{src}::alive@1": {"loc": 2}, f"{other}::elsewhere@1": {"loc": 2}},
    )

    assert dead_baseline_keys([str(src)], baseline_path) == []


class TestI30Property:
    """The gate itself — (e) NO DEAD BASELINE ENTRIES in check_complexity_allowlist."""

    def test_property_e_is_wired_into_run_check(self):
        from check_complexity_allowlist import run_check

        result = run_check(_REPO_ROOT)
        assert len(result) == 5, "run_check must return the five I30 properties"

    def test_repo_baseline_has_no_dead_entries(self):
        """The live baseline is clean — the whole point of the car."""
        from check_complexity_allowlist import check_dead_baseline

        assert check_dead_baseline(_REPO_ROOT) == []

    def test_the_named_stale_entry_is_gone(self):
        baseline = json.loads((_REPO_ROOT / ".complexity-baseline.json").read_text())
        assert (
            "yadgar/core/server/tools/admin_other.py::_parse_since_duration@146" not in baseline
        ), "task 395's named entry must be removed"

    def test_a_planted_dead_entry_fails_the_gate(self, tmp_path, monkeypatch):
        """Regression: the gate must actually catch a newly-introduced corpse."""
        import check_complexity_allowlist as cca

        baseline = json.loads((_REPO_ROOT / ".complexity-baseline.json").read_text())
        baseline["yadgar/core/server/tools/audit.py::_never_existed@1"] = {"loc": 3}

        shadow = tmp_path / "repo"
        shadow.mkdir()
        (shadow / ".complexity-baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
        # Point the scan at the real tree, the baseline at the poisoned copy.
        monkeypatch.setattr(cca, "_baseline_scan_root", lambda _root: _REPO_ROOT / "yadgar")

        errors = cca.check_dead_baseline(shadow)

        assert any("_never_existed" in e for e in errors)
