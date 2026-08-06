"""Engine-#2 car F: the cross-engine backup is a STEP OF THE NIGHTLY CYCLE.

ADR-0210 §2: maintenance windows nest by design, so an independently-scheduled
backup could open a window overlapping the nightly cycle and snapshot
mid-consolidation-write. Welding it to nightly makes overlap structurally
impossible — one holder, one window.

What these pin:
  1. the step runs INSIDE nightly's window (after the step-1 enter, before the
     step-7 exit) and nests rather than replacing it;
  2. a hard failure costs the SNAPSHOT, not the cycle — steps after it still
     run and the gate is still released;
  3. the step does not displace ``_step_post_backup``: an unquiesced
     ``nightly-post`` export that succeeds today must keep succeeding when the
     gate is unobtainable, and car G needs the quiesced pair to be identifiable;
  4. prune covers the two NEW artifact pools, which nothing globbed before.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_MODULE = "yadgar.core.scripts.nightly_cycle"


def _import_module():
    import yadgar.core.scripts.nightly_cycle as _mod

    importlib.reload(_mod)
    return _mod


def _make_args(tmp_path: Path):
    return SimpleNamespace(
        db_path=str(tmp_path / "surreal_db"),
        backend_url="http://127.0.0.1:8080",
        service_mode="systemd",
        retention=3,
    )


def _run_main(mod, tmp_path, **overrides):
    """Run main() with every step stubbed except the ones under test."""
    calls: list[str] = []
    stubs = {
        "_maintenance_http": MagicMock(side_effect=lambda action, *a, **k: calls.append(action)),
        "_step_pre_backup": MagicMock(side_effect=lambda *a, **k: calls.append("pre") or 0),
        "_step_consolidation": MagicMock(side_effect=lambda *a, **k: calls.append("consol") or 0),
        "_step_vacuum": MagicMock(side_effect=lambda *a, **k: calls.append("vacuum") or 0),
        "_step_post_backup": MagicMock(side_effect=lambda *a, **k: calls.append("post") or 0),
        "_step_prune": MagicMock(side_effect=lambda *a, **k: calls.append("prune") or 0),
        "run_cross_engine_backup": MagicMock(
            side_effect=lambda **k: calls.append("cross") or {"ok": True, "sql_dump": "d.sql"}
        ),
    }
    stubs.update(overrides)
    with patch.multiple(_MODULE, **stubs):
        code = mod.main(_make_args(tmp_path))
    return code, calls


def test_cross_engine_step_runs_inside_the_nightly_window(tmp_path):
    """enter ... cross ... exit — the step nests inside steps 1-7, never outside."""
    mod = _import_module()
    code, calls = _run_main(mod, tmp_path)

    assert code == 0
    assert calls.index("enter") < calls.index("cross") < calls.index("exit")


def test_cross_engine_step_runs_after_vacuum_and_before_prune(tmp_path):
    """Ordering: prune must see the artifacts this step just wrote."""
    mod = _import_module()
    _, calls = _run_main(mod, tmp_path)

    assert calls.index("vacuum") < calls.index("cross") < calls.index("prune")


def test_cross_engine_step_does_not_displace_the_unquiesced_post_backup(tmp_path):
    """Both run. The quiesced pair is additive; the existing safety net stays."""
    mod = _import_module()
    _, calls = _run_main(mod, tmp_path)

    assert "post" in calls
    assert "cross" in calls


def test_hard_failure_costs_the_snapshot_not_the_cycle(tmp_path):
    """Gate unobtainable -> distinct exit code, later steps still run, gate released."""
    mod = _import_module()
    code, calls = _run_main(
        mod,
        tmp_path,
        run_cross_engine_backup=MagicMock(side_effect=RuntimeError("write-gate unobtainable")),
    )

    assert code == 55
    assert "prune" in calls
    assert "exit" in calls


def test_hard_failure_does_not_mask_an_earlier_failure(tmp_path):
    """FIRST failing step's code wins — the cycle's documented contract."""
    mod = _import_module()
    code, _ = _run_main(
        mod,
        tmp_path,
        _step_consolidation=MagicMock(return_value=30),
        run_cross_engine_backup=MagicMock(side_effect=RuntimeError("nope")),
    )

    assert code == 30


def test_prune_covers_the_two_new_artifact_pools(tmp_path):
    """The quiesced surql pool and the MariaDB dumps — neither was globbed before."""
    mod = _import_module()
    snapshot_dir = tmp_path / "backups" / "surql"
    mariadb_dir = tmp_path / "backups" / "mariadb"
    seen: list[tuple[Path, str]] = []

    with patch.object(
        mod, "prune_snapshots", side_effect=lambda d, p, retention: seen.append((d, p)) or []
    ):
        assert mod._step_prune(snapshot_dir, mariadb_dir, retention=3) == 0

    patterns = {p for _, p in seen}
    assert "surreal_db.nightly-*" in patterns
    assert "surreal_db.quiesce-*" in patterns
    assert any(d == mariadb_dir for d, _ in seen)


def test_prune_survives_a_missing_mariadb_pool(tmp_path):
    """No engine #2 yet (or a fresh host) must not turn prune into a cycle failure."""
    mod = _import_module()
    assert mod._step_prune(tmp_path / "surql", tmp_path / "nope" / "mariadb", retention=3) == 0
